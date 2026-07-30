#!/usr/bin/env python3
"""Distil the Oracle Cost Estimator snapshot into the compact file the catalog loads.

The snapshot in oracle-cost-estimator-ai-integration/ is ~12 MB of every SKU in every one of
Oracle's 57 currencies - far too heavy to import at app start. This script pulls out only what
the "Add OCI services" catalog needs (USD prices, billing unit, tier ranges, and the estimator's
own card/form metadata) and writes data/estimator_services.json.

Three source files, each doing a different job:
    oracle-cost-estimator-catalog-*.json   SKUs, prices, services, metrics   (source of truth)
    oracle-cost-estimator-selections.json  per-service form + SKU choices    (pricing shape)
    oracle-cost-estimator-visual-spec.json per-SKU quantity control          (UI shape)

Which services are included is a deliberate editorial decision, not everything Oracle lists -
see EXCLUDED_SERVICES for the three buckets that are left out and why.

Usage:  python3 scripts/gen_estimator_services.py [--check]
        --check exits 1 if the generated file would differ (for CI / pre-commit).
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAP_DIR = ROOT / "oracle-cost-estimator-ai-integration"
OUT = ROOT / "data" / "estimator_services.json"
CURRENCY = "USD"

# --- what we leave out ----------------------------------------------------------------------
# Nothing in Oracle's snapshot is flagged retired - every SKU comes back NOT_RESTRICTED - so
# these are an editorial call about what belongs in a BOM, not a claim that Oracle stopped
# selling them. Each id is commented with the reason so the call can be revisited.
EXCLUDED_SERVICES = {
    # (a) Heritage / superseded by a newer service.
    2441: "Tuxedo - heritage middleware",
    1181: "Blockchain Platform",
    1143: "Developer Cloud Service - superseded by DevOps",
    1142: "Java Cloud Service - heritage WebLogic",
    2821: "WebCenter for OCI",
    1286: "SOA Suite",
    1261: "Data Integrator (ODI) - superseded by Data Integration",
    1121: "Big Data Cloud Service - superseded by Big Data Service (1422)",
    1961: "Essbase on OCI - marketplace image",
    245: "Digital Assistant",
    3463: "IoT Platform",
    409: "Oracle Public Cloud Services (CLOUDCM)",
    1001: "Oracle Database - legacy DBCS entry",
    1542: "Analytics - superseded by Analytics Cloud (189)",
    532: "Edge Services",
    1282: "Exadata Cloud Infrastructure X7 - superseded",
    1281: "Exadata Cloud Infrastructure X8 - superseded",
    1104: "Exadata Cloud Infrastructure X8M - superseded",
    1681: "Exadata Cloud Infrastructure X9M - superseded",
    1103: "Exadata Database Service OCPUs - ECPU (2862) is current",
    1102: "Exadata Cloud Infrastructure Base System",
    # (b) Not native OCI cloud, or tied to shipped hardware.
    1841: "MySQL HeatWave on AWS - not OCI",
    3261: "Oracle Database Backup to Amazon S3 - not OCI",
    1461: "Roving Edge Infrastructure - shipped hardware",
    2841: "Managed Services for Mac - 3-year commit",
    3542: "Base Database Cloud@Customer",
    349: "Exadata Database Cloud@Customer",
    2541: "Compute Cloud@Customer",
    # (c) Free-tier-only cards: every SKU prices at $0, so the card adds nothing to a BOM.
    2162: "Bastion - free",
    2143: "Certificates - free",
    2161: "Cloud Guard - free",
    2142: "Scanning - free",
    2163: "Security Zones - free",
    3341: "Resource Analytics - free",
    3001: "HeatWave Free Tier - free",
}

# Estimator services a hand-written curated card already covers. Keyed by estimator service id
# so a generated card never duplicates a tuned one. Derived by matching each curated card's SKU
# back to its product, with overrides where the curated SKU is wrong or spans services.
COVERED_SERVICES = {
    881: "block", 882: "object / object_ia / archive", 862: "file",
    885: "lb", 901: "fastconnect", 886: "dns",
    1101: "basedb", 1661: "adb", 581: "mysql", 2621: "pg", 883: "dbbackup",
    2021: "recovery", 2121: "queue", 1285: "oic",
    887: "waf", 527: "kms", 2141: "fsdr", 2521: "desktops",
    1621: "vision", 2181: "docunderstanding", 1521: "language", 1741: "speech",
    2741: "genai", 3562: "genai", 3081: "genai_agents",
    827: "winlic", 824: "sqllic", 2421: "logging",
}

# The estimator names services "<Category> - <Service>". That prefix is Oracle's own grouping,
# so it is what decides the app's category chip; services with no prefix are mapped by name.
GROUP_BY_PREFIX = {
    "Compute": "Compute",
    "Storage": "Storage",
    "Networking": "Networking",
    "Database": "Database",
    "Analytics": "Analytics",
    "Observability": "Observability",
    "Security": "Security",
    "Application Integration": "Integration",
    "Data Integration": "Integration",
    "Application Development": "Integration",
    "Data Science": "AI & Machine Learning",
    "Data Management": "Database",
    "Generative AI": "AI & Machine Learning",
    "Media Services": "Other Services",
    "Cloud Guard": "Security",
}
GROUP_BY_ID = {
    2801: "Compute", 2895: "Compute", 801: "Compute", 829: "Compute", 1321: "Compute",
    2301: "Compute", 3522: "Compute", 1161: "Integration",
    237: "Database", 2862: "Database", 2861: "Database", 3161: "Database", 3703: "Database",
    2461: "Database", 3462: "Database", 825: "Database", 2601: "Database", 1901: "Database",
    3241: "Storage", 1881: "Storage",
    1941: "Networking", 2641: "Networking", 3041: "Networking", 3643: "Networking",
    521: "Security", 2561: "Security", 1721: "Security", 1182: "Security", 1921: "Security",
    2781: "Security",
    1301: "Observability", 1288: "Observability", 1289: "Observability", 1290: "Observability",
    2681: "Observability", 3061: "Observability", 2661: "Observability",
    1141: "Integration", 3101: "Integration", 2281: "Integration", 2721: "Integration",
    2761: "Integration", 2261: "Integration", 861: "Integration",
    3381: "AI & Machine Learning", 2321: "AI & Machine Learning", 3623: "AI & Machine Learning",
    1422: "Analytics", 189: "Analytics",
    528: "Other Services", 1981: "Other Services", 1821: "Other Services",
    2101: "Other Services", 3181: "Other Services", 3141: "Other Services",
    1342: "Database", 1201: "Integration",
}

# Billing units that bill per hour of utilisation; everything else is a flat monthly quantity.
HOURLY_UNITS = {"HOUR", "HOUR_UTILIZED"}

_TAG = re.compile(r"<[^>]+>")


def _plain(html):
    """Oracle ships service notes as HTML fragments; the card note is plain text."""
    if not html:
        return ""
    txt = _TAG.sub(" ", html).replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", txt).strip()


def _snapshot():
    files = sorted(SNAP_DIR.glob("oracle-cost-estimator-catalog-*.json"))
    if not files:
        sys.exit(f"no catalog snapshot in {SNAP_DIR}")
    return files[-1]


def _display_name(label, group):
    """Drop the estimator's category prefix only when the chip already says it.

    "Observability - Ops Insights" under the Observability chip becomes "Ops Insights". But
    "Data Integration - Data Processed" sits under the Integration chip, so stripping its prefix
    would leave a card called "Data Processed" with nothing saying what it belongs to - keep
    Oracle's full label there.
    """
    for prefix in GROUP_BY_PREFIX:
        if label.startswith(prefix + " - ") and GROUP_BY_PREFIX[prefix] == group == prefix:
            return label[len(prefix) + 3:]
    return label


def _group(sid, label):
    if sid in GROUP_BY_ID:
        return GROUP_BY_ID[sid]
    for prefix, grp in GROUP_BY_PREFIX.items():
        if label.startswith(prefix + " - ") or label == prefix:
            return grp
    return "Other Services"


def _usd_tiers(choice):
    """Graduated price tiers for a SKU, in USD, lowest range first.

    Oracle expresses a free allowance as a first tier priced at 0 (e.g. Monitoring ingestion is
    $0 up to 500M datapoints, then $0.0025). Keeping every tier - rather than collapsing to one
    rate - is what makes those allowances price correctly.
    """
    for block in choice.get("prices_by_currency") or []:
        if block.get("currencyCode") != CURRENCY:
            continue
        tiers = []
        for p in block.get("prices") or []:
            if p.get("model") and p["model"] != "PAY_AS_YOU_GO":
                continue
            hi = p.get("rangeMax")
            # Oracle uses a sentinel like 999999999999999 for "no upper bound".
            if hi is not None and hi >= 1e12:
                hi = None
            tiers.append({"min": float(p.get("rangeMin") or 0),
                          "max": None if hi is None else float(hi),
                          "rate": float(p.get("value") or 0)})
        tiers.sort(key=lambda t: t["min"])
        return tiers
    return []


def build():
    snap_path = _snapshot()
    snap = json.loads(snap_path.read_text())
    sel = json.loads((SNAP_DIR / "oracle-cost-estimator-selections.json").read_text())
    vis = json.loads((SNAP_DIR / "oracle-cost-estimator-visual-spec.json").read_text())

    services = {s["id"]: s for s in snap["datasets"]["services"]["items"]}
    selections = {x["selection"]["service_id"]: x for x in sel["service_selections"]}
    visuals = {x["service"]["id"]: x for x in vis["service_visual_selections"]}

    out, skipped = [], []
    for sid, svc in sorted(services.items(), key=lambda kv: kv[1]["displayName"]):
        label = svc["displayName"]
        if sid in EXCLUDED_SERVICES:
            skipped.append((sid, label, "excluded: " + EXCLUDED_SERVICES[sid]))
            continue
        if sid in COVERED_SERVICES:
            skipped.append((sid, label, "curated card: " + COVERED_SERVICES[sid]))
            continue
        s_sel = selections.get(sid)
        s_vis = visuals.get(sid)
        if not s_sel:
            skipped.append((sid, label, "no selection record"))
            continue

        vis_by_sku = {r["sku"]: r for r in (s_vis or {}).get("sku_selections", [])}
        skus = []
        for n, choice in enumerate(s_sel.get("sku_choices") or []):
            tiers = _usd_tiers(choice)
            if not tiers or all(t["rate"] == 0 for t in tiers):
                continue  # nothing to price in USD
            qc = ((vis_by_sku.get(choice["sku"]) or {}).get("row_layout") or {}) \
                .get("quantity_control") or {}
            metric = ((choice.get("metric") or {}).get("display_name")
                      or (choice.get("metric") or {}).get("name") or "").strip()
            billing = (choice.get("billing_unit") or "MONTH").upper()
            skus.append({
                "key": f"m{n}",
                "sku": choice["sku"],
                "label": choice.get("label") or choice["sku"],
                "metric": metric,
                "billing": billing,
                "hourly": billing in HOURLY_UNITS,
                "decimal": bool(qc.get("accepts_decimal_quantity")),
                "step": qc.get("step") or 1,
                "min": qc.get("minimum"),
                "max": qc.get("maximum"),
                "tiers": tiers,
                # Headline rate = the first non-zero tier, i.e. the price past any free allowance.
                "rate": next((t["rate"] for t in tiers if t["rate"]), 0.0),
            })
        if not skus:
            skipped.append((sid, label, "no priceable USD SKU"))
            continue

        form = s_sel.get("form") or {}
        notes = " ".join(_plain(n.get("note")) for n in (form.get("notes") or []))
        out.append({
            "id": f"est{sid}",
            "serviceId": sid,
            "label": label,
            "name": _display_name(label, _group(sid, label)),
            "group": _group(sid, label),
            "componentType": s_sel["selection"].get("component_type"),
            "docUrl": s_sel["selection"].get("documentation_url") or "",
            "productUrl": s_sel["selection"].get("product_page_url") or "",
            "form": {
                "days": form.get("default_days") or 31,
                "hoursPerDay": form.get("default_hours") or 24,
                "fixedUtilization": bool(form.get("fixed_utilization")),
                "showHours": bool(form.get("show_hours_input")),
                "showInstances": bool(form.get("show_instance_input")),
            },
            "note": notes,
            "skus": skus,
        })

    return {
        "generatedFrom": snap_path.name,
        "extractedAtUtc": snap.get("extracted_at_utc"),
        "datasetVersion": (snap.get("source") or {}).get("dataset_version"),
        "currency": CURRENCY,
        "counts": {"services": len(out), "skus": sum(len(s["skus"]) for s in out)},
        "services": out,
    }, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the generated file differs from what is on disk")
    ap.add_argument("--verbose", action="store_true", help="list every skipped service")
    args = ap.parse_args()

    data, skipped = build()
    text = json.dumps(data, indent=1, sort_keys=False) + "\n"

    if args.check:
        current = OUT.read_text() if OUT.exists() else ""
        if current != text:
            print("estimator_services.json is stale - run scripts/gen_estimator_services.py")
            return 1
        print("estimator_services.json is current")
        return 0

    OUT.write_text(text)
    print(f"wrote {OUT.relative_to(ROOT)}: "
          f"{data['counts']['services']} services, {data['counts']['skus']} SKUs "
          f"(from {data['generatedFrom']})")
    if args.verbose:
        for sid, label, why in skipped:
            print(f"  skipped {sid:>5} {label[:48]:<48} {why}")
    else:
        print(f"  skipped {len(skipped)} services (--verbose to list)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
