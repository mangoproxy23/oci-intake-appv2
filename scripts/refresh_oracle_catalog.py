#!/usr/bin/env python3
"""Pull the newest Oracle Cost Estimator catalog and merge it into the app's price list.

Oracle's estimator is a client-side app: it reads a manifest that names the current dataset
files and their version, then fetches those static JSON files. Following that manifest is the
supported way to get current SKUs and prices - never scrape the rendered UI, and never hard-code
the ?ver= number, because it changes whenever Oracle publishes.

    manifest  https://www.oracle.com/a/ocom/docs/cloudestimator2/js/env.json
    datasets  <base>/<datasets.default.*>   e.g. ./data/products.json?ver=439

What this adds over the bundled refresh-oracle-cost-estimator.sh: no jq/shasum dependency (that
script needs both, which is a poor bet on a sales laptop), and it finishes the job by merging the
snapshot into data/oci_price_list.json so the Other OCI Bill converter actually recognizes the
new SKUs. The shell script only produces the snapshot file.

Usage:
    python3 scripts/refresh_oracle_catalog.py                 # fetch, merge, report
    python3 scripts/refresh_oracle_catalog.py --dry-run       # report what would change
    python3 scripts/refresh_oracle_catalog.py --snapshot-only # write the snapshot, don't merge

Exit codes: 0 changed or already current, 1 fetch/parse failure (nothing written).
"""

import argparse
import datetime as _dt
import json
import shutil
import sys
import urllib.request
from pathlib import Path

BASE_URL = "https://www.oracle.com/a/ocom/docs/cloudestimator2"
MANIFEST_URL = f"{BASE_URL}/js/env.json"
DATASET_FIELDS = {
    "prodURL": "products", "presetsURL": "product_presets",
    "categoriesURL": "product_preset_categories", "servicesURL": "services",
    "currenciesURL": "currencies", "metricsURL": "metrics", "signaturesURL": "signatures",
    "estimatesURL": "estimates", "shapeCriterionsURL": "shape_criterions",
    "shapesURL": "shapes", "exadataURL": "exadata",
}
REPO = Path(__file__).resolve().parents[1]
PRICE_LIST = REPO / "data" / "oci_price_list.json"
SNAPSHOT_DIR = REPO / "oracle-cost-estimator-ai-integration"
TIMEOUT = 60


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "oci-bom-app/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def fetch_catalog():
    """Follow the manifest and return {version, datasets{...}}."""
    manifest = _get(MANIFEST_URL)
    default = ((manifest.get("datasets") or {}).get("default")) or {}
    if not default:
        raise ValueError("Manifest has no datasets.default - Oracle changed the format.")
    version = ""
    datasets = {}
    for field, name in DATASET_FIELDS.items():
        rel = default.get(field)
        if not rel:
            continue                      # Oracle drops fields occasionally; skip, don't fail
        if "ver=" in rel and not version:
            version = rel.split("ver=", 1)[1].split("&")[0]
        datasets[name] = _get(f"{BASE_URL}/{rel.lstrip('./')}")
    if "products" not in datasets:
        raise ValueError("Manifest resolved no products dataset.")
    return {"version": version, "datasets": datasets}


def usd_payg(product):
    """PAYG price in USD. Oracle ships ~57 currencies per SKU; the app prices in USD."""
    for loc in product.get("currencyCodeLocalizations") or []:
        if loc.get("currencyCode") == "USD":
            for price in loc.get("prices") or []:
                if price.get("model") == "PAY_AS_YOU_GO":
                    return price.get("value")
    return None


def merge_into_price_list(catalog, dry_run=False):
    """Fold the snapshot into data/oci_price_list.json. Returns (added, corrected, total)."""
    ds = catalog["datasets"]
    items_of = lambda k: (ds.get(k) or {}).get("items") or []
    metrics = {m.get("id"): (m.get("displayName") or m.get("name") or "")
               for m in items_of("metrics")}
    price_list = json.loads(PRICE_LIST.read_text())
    items = price_list["items"]
    by_sku = {str(i.get("sku", "")).upper(): i for i in items}

    added = corrected = 0
    for product in items_of("products"):
        sku = (product.get("partNumber") or "").upper()
        if not sku:
            continue
        value = usd_payg(product)
        metric = metrics.get(product.get("metricId"), "")
        name = product.get("displayName") or ""
        existing = by_sku.get(sku)
        if existing:
            changed = False
            if not existing.get("metric") and metric:
                existing["metric"] = metric
                changed = True
            # The scraped price list sometimes stores a bundle factor (a literal 1.0) where a
            # real per-unit rate belongs. Oracle's own catalog is authoritative, so a genuine
            # rate replaces a missing or placeholder one - but never an existing real rate,
            # which may have been curated deliberately.
            if value is not None and existing.get("payg") in (None, 1.0) and abs(float(value) - 1.0) > 1e-9:
                existing["payg"] = value
                changed = True
            corrected += 1 if changed else 0
        else:
            items.append({
                "sku": sku, "payg": value, "metric": metric, "desc": name,
                "serviceCategory": product.get("serviceCategoryDisplayName"),
                "source": f"oracle-cost-estimator-catalog ver {catalog['version']}",
            })
            added += 1

    if not dry_run and (added or corrected):
        shutil.copy2(PRICE_LIST, PRICE_LIST.with_suffix(".json.bak"))
        price_list["count"] = len(items)
        price_list["oracleCatalogVersion"] = catalog["version"]
        price_list["oracleCatalogRefreshedUtc"] = _dt.datetime.utcnow().isoformat(timespec="seconds")
        PRICE_LIST.write_text(json.dumps(price_list, indent=1))
    return added, corrected, len(items)


def write_snapshot(catalog):
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.utcnow().strftime("%Y-%m-%d")
    path = SNAPSHOT_DIR / f"oracle-cost-estimator-catalog-{stamp}.json"
    counts = {k: len((v or {}).get("items") or []) for k, v in catalog["datasets"].items()}
    path.write_text(json.dumps({
        "schema_version": 1,
        "extracted_at_utc": _dt.datetime.utcnow().isoformat(timespec="seconds"),
        "source": {"manifest_url": MANIFEST_URL, "dataset_version": catalog["version"],
                   "terms_note": "Public source snapshot. Prices change; not a binding quote."},
        "counts": counts,
        "datasets": catalog["datasets"],
    }, indent=1))
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report changes without writing")
    ap.add_argument("--snapshot-only", action="store_true", help="write the snapshot, skip merge")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    try:
        catalog = fetch_catalog()
    except Exception as exc:
        print(f"Could not refresh the Oracle catalog: {exc}", file=sys.stderr)
        print("The app keeps its current price list - nothing was changed.", file=sys.stderr)
        return 1

    products = len((catalog["datasets"].get("products") or {}).get("items") or [])
    if not args.quiet:
        print(f"Oracle Cost Estimator dataset version {catalog['version']} - {products} SKUs")

    if not args.dry_run:
        snap = write_snapshot(catalog)
        if not args.quiet:
            print(f"Snapshot: {snap.name}")
    if args.snapshot_only:
        return 0

    added, corrected, total = merge_into_price_list(catalog, dry_run=args.dry_run)
    verb = "would add" if args.dry_run else "added"
    if not args.quiet:
        print(f"Price list: {verb} {added} new SKUs, corrected {corrected}, {total} total")
        if added or corrected:
            print("Restart the app to pick up the new catalog.")
        else:
            print("Already current.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
