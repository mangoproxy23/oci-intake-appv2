import os, sys
os.environ["OCI_APP_NO_BOOTSTRAP"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _fmt(v):
    try:
        return "${:,.2f}".format(float(v))
    except Exception:
        return str(v)


def net_breakdown(rows, target_cloud):
    comps = {}
    carried = 0.0
    for row in rows:
        if not app._is_networking_row(row):
            continue
        comp = app._net_component(row.get("sourceUsageType"), row.get("sourceProduct"))
        rates = app._NET_RATE.get(target_cloud) or {}
        val = app._reprice_networking_row(row, target_cloud)
        key = comp if (comp and comp in rates) else "carried"
        if key == "carried":
            carried += val
        comps[key] = comps.get(key, 0.0) + val
    return comps


def run(path, provider_hint, label):
    print("=" * 74)
    print("FILE:", label, "| hint:", provider_hint)
    parsed = app.parse_cloud_bill(path, provider_hint)
    fields, rows = parsed["fields"], parsed["rows"]
    meta = parsed.get("metadata", {})
    print("rows:", len(rows), "| detectedProvider:", meta.get("detectedProvider"),
          "| mapped:", meta.get("mappedCount"), "| currency:", meta.get("sourceCurrency"))
    pricing = app.calculate_pricing(
        fields, rows,
        shape_key=app.DEFAULT_SHAPE_KEY,
        full_service_beta=True,
        intake_mode=app.INTAKE_MODE_CLOUD_BILL,
        source_provider=provider_hint,
    )
    priced_rows = pricing["rows"]
    oci_total = pricing.get("totals", {}).get("monthlyTotal") or pricing.get("totals", {}).get("totalMonthly")
    cc = pricing["crossCloud"]
    src = cc.get("sourceCloud")
    print("OCI monthly (mapped target): ", _fmt(oci_total))
    print("crossCloud.sourceCloud:", src)
    for mode_key in ("bestMatch", "topTier"):
        m = cc.get(mode_key, {})
        print("  [%s]" % mode_key)
        for ck in ("aws", "azure", "gcp"):
            cv = m.get(ck)
            if not isinstance(cv, dict):
                continue
            if not cv.get("priced"):
                print("     %-6s (not priced) %s" % (ck, cv.get("note", "")))
                continue
            print("     %-6s monthly=%-14s basis=%s | est=%s carried=%s actual=%s" % (
                ck, _fmt(cv.get("monthlyTotal")), cv.get("basis"),
                cv.get("estimatedRows"), cv.get("carriedRows"), cv.get("actualRows")))
    # Networking component breakdown for the non-source target cloud
    target = "azure" if src == "aws" else "aws"
    nb = net_breakdown(priced_rows, target)
    if nb:
        print("  networking re-priced on %s:" % target)
        for k, v in sorted(nb.items(), key=lambda x: -x[1]):
            print("     %-10s %s" % (k, _fmt(v)))
    print()


if __name__ == "__main__":
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ecsv = os.path.join(root, "ecsv_5_2026(1).csv")
    az = "/sessions/blissful-awesome-keller/mnt/uploads/Azure Bill compare.xlsx"
    run(ecsv, "aws", "ecsv_5_2026(1).csv")
    run(az, "azure", "Azure Bill compare.xlsx")
