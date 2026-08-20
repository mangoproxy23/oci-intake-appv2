"""Cheap targeted A/B for mapping waves (Chris's protocol, 2026-08-19).

Prices testdata/azure_unmapped_fixture.csv (the still-carried/unmapped services
extracted once from the July 2026 Quad/Graphics export, daily rows collapsed to
monthly - quantities and costs sum-identical to the raw bill) and diffs each row's
(ociProduct, monthly, carried) against scripts/ab_fixture_baseline.json. Run AFTER a
batch of mapping changes instead of re-pricing the 125MB bill; expected diffs are the
rows the wave intentionally converts. NOTE: the fixture alone parses ~200 more small
rows than the full bill (the full upload hits the parse row cap), so fixture totals
run slightly higher than full-bill totals - always compare fixture-vs-fixture. Save
the full-bill A/B for one pre-merge pass.

Run:  python3 scripts/ab_fixture_diff.py            # diff vs baseline
      python3 scripts/ab_fixture_diff.py --update   # accept current output as baseline
"""
import gzip
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "testdata" / "azure_unmapped_fixture.csv"
BASELINE = ROOT / "scripts" / "ab_fixture_baseline.json"
PORT = 8797


def price_fixture():
    base = f"http://127.0.0.1:{PORT}"
    proc = subprocess.Popen([sys.executable, "app.py"], cwd=ROOT,
                            env={**os.environ, "PORT": str(PORT)},
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        for _ in range(120):
            try:
                urllib.request.urlopen(base + "/api/health", timeout=2)
                break
            except Exception:
                time.sleep(1)
        else:
            raise SystemExit("app.py never came up")
        req = urllib.request.Request(
            base + "/api/upload", data=gzip.compress(FIXTURE.read_bytes()),
            method="POST",
            headers={"Content-Type": "application/gzip",
                     "X-Upload-Filename": FIXTURE.name,
                     "X-Intake-Mode": "cloud-bill", "X-Provider-Hint": "azure"})
        up = json.load(urllib.request.urlopen(req, timeout=300))
        pay = {"fields": up["fields"], "rows": up["rows"],
               "intakeMode": "cloud-bill", "providerHint": "azure"}
        req2 = urllib.request.Request(
            base + "/api/price", data=json.dumps(pay).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(req2, timeout=300))
    finally:
        proc.terminate()


def slim(pricing):
    out = []
    for r in pricing["rows"]:
        lis = r.get("lineItems") or []
        out.append({
            "key": [r.get("sourceService"), r.get("sourceProduct"),
                    round(float(r.get("sourceUsageQty") or 0), 4),
                    round(float(r.get("sourceMonthlyCost") or 0), 2)],
            "ociProduct": r.get("ociProduct"),
            "monthly": round(sum(float(li.get("monthly") or 0) for li in lis), 2),
            "carried": any(li.get("carriedOver") for li in lis),
        })
    out.sort(key=lambda x: json.dumps(x["key"]))
    totals = pricing.get("totals", {})
    return {"rows": out, "monthly": totals.get("monthly"),
            "carriedSourceMonthly": totals.get("carriedSourceMonthly")}


def main():
    cur = slim(price_fixture())
    if "--update" in sys.argv or not BASELINE.exists():
        BASELINE.write_text(json.dumps(cur, indent=0))
        print(f"baseline written: {len(cur['rows'])} rows, "
              f"monthly {cur['monthly']}, carried {cur['carriedSourceMonthly']}")
        return
    old = json.loads(BASELINE.read_text())
    om = {json.dumps(r["key"]): r for r in old["rows"]}
    nm = {json.dumps(r["key"]): r for r in cur["rows"]}
    changed = 0
    for k in sorted(set(om) | set(nm)):
        a, b = om.get(k), nm.get(k)
        if a != b:
            changed += 1
            print("CHANGED", k)
            print("  was:", a and (a["ociProduct"], a["monthly"], a["carried"]))
            print("  now:", b and (b["ociProduct"], b["monthly"], b["carried"]))
    print(f"\n{changed} changed rows; monthly {old['monthly']} -> {cur['monthly']}; "
          f"carried {old['carriedSourceMonthly']} -> {cur['carriedSourceMonthly']}")
    print("Review the changes; if all intentional: python3 scripts/ab_fixture_diff.py --update")


if __name__ == "__main__":
    main()
