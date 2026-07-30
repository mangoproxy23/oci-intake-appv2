"""Database lines in an imported OCI BOM must never become re-mappable compute VMs.

A database engine bills per OCPU/ECPU exactly like compute does. When classify_resource()
typed those lines "ocpu", _merge_server_compute() folded them into one synthetic VM and
the Shape step's "Continue to Services" re-priced them at plain compute rates - which on a
real customer BOM turned $394.62 of Base Database into $80.59, a 23% understatement of the
whole estate, with no warning anywhere. These checks pin the three things that has to hold.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bom_convert


DATABASE_TERMS = ("database", "autonomous", "exadata", "mysql", "heatwave", "postgres",
                  "nosql", "goldengate", "timesten", "berkeley db")

# (description, unit, expected kind, expected category)
CLASSIFY_CASES = [
    # --- database engines that bill on a processor metric: must NOT be "ocpu" ---
    ("Oracle Base Database Compute Infrastructure", "ECPU Per Hour", "database", "Database"),
    ("Oracle Base Database Service - Enterprise - x86 - ECPU", "ECPU Per Hour", "database", "Database"),
    ("Oracle Base Database Service - BYOL - ECPU", "ECPU Per Hour", "database", "Database"),
    ("Exadata Database ECPU - Dedicated Infrastructure", "ECPU Per Hour", "database", "Database"),
    ("Oracle Exadata Exascale Database - ECPU", "ECPU Per Hour", "database", "Database"),
    ("Oracle Autonomous AI Transaction Processing - ECPU", "ECPU Per Hour", "database", "Database"),
    ("Oracle Cloud Infrastructure Database with PostgreSQL - X86", "OCPU Per Hour", "database", "Database"),
    ("MySQL Database - AWS - ECPU", "ECPU Per Hour", "database", "Database"),
    ("MySQL HeatWave", "ECPU Per Hour", "database", "Database"),
    ("Oracle NoSQL Database Cloud Service", "OCPU Per Hour", "database", "Database"),
    ("Oracle Cloud Infrastructure - GoldenGate", "OCPU Per Hour", "database", "Database"),
    # a database billing on a MEMORY metric is the same trap
    ("Oracle Database Cloud Service - Enterprise Edition - Virtual Machine", "Gigabyte Per Hour",
     "database", "Database"),

    # --- genuine compute must be untouched, or nothing re-shapes any more ---
    ("OCI - Compute - Standard - E6 Ax - OCPU", "OCPU Per Hour", "ocpu", "Compute"),
    ("OCI - Compute - Standard - E6 Ax - Memory", "Gigabyte Per Hour", "memory", "Compute"),
    ("Compute - Virtual Machine Standard - X9 - OCPU", "OCPU Per Hour", "ocpu", "Compute"),
    ("Compute VM Dense I/O - X7", "OCPU Per Hour", "ocpu", "Compute"),
    ("Windows OS", "OCPU Per Hour", "license", "Licensing"),
    ("Compute - GPU - BM.GPU.L40S.4", "GPU Per Hour", "gpu", "Compute"),

    # --- a database's STORAGE line is still storage: typing it "database" would drop it out
    # of totals.blockStorageGb and shrink both the Storage sheet and the diagram ---
    ("Oracle Base Database Service - Database Storage", "Gigabyte Storage Capacity Per Month",
     "blockStorage", "Storage"),
    ("Autonomous Database - Exadata Storage", "Gigabyte Storage Capacity Per Month",
     "blockStorage", "Storage"),
]


def check_classification():
    failures = []
    for product, unit, want_kind, want_cat in CLASSIFY_CASES:
        got = bom_convert.classify_resource(product, unit)
        if got != (want_kind, want_cat):
            failures.append(f"classify_resource({product!r}, {unit!r}) -> {got}, want {(want_kind, want_cat)}")
    return failures


def check_catalog_sweep():
    """No database SKU anywhere in the price list may be typed as re-mappable compute."""
    import json
    path = Path(__file__).resolve().parents[1] / "data" / "oci_price_list.json"
    if not path.exists():
        return []
    items = json.loads(path.read_text()).get("items") or []
    offenders = []
    for item in items:
        desc, metric = item.get("desc") or "", item.get("metric") or ""
        blob = f"{desc} {metric}".lower()
        # Deliberately a local list, not bom_convert's - this test has to be able to run
        # against a build that predates the fix and still report a real failure.
        if not any(term in blob for term in DATABASE_TERMS):
            continue
        kind, _ = bom_convert.classify_resource(desc, metric)
        if kind in ("ocpu", "memory"):
            offenders.append(f"{item.get('sku')}: {desc[:60]}")
    if offenders:
        return [f"{len(offenders)} database SKUs still type as re-mappable compute, e.g. "
                + "; ".join(offenders[:3])]
    return []


def build_fixture(path):
    """An estimator-style sheet: one real flex VM, plus a Base Database section whose two
    SKUs bill the SAME 4 ECPUs (infrastructure + edition licence)."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "PAAS"
    ws.append(["Oracle Investment Proposal"])
    ws.append(["Currency: USD"])
    ws.append(["Part", "Description", "Part Qty", "Instance Qty", "Usage Qty",
               "Unit Price", "Monthly Cost"])
    ws.append([None, "Virtual Machine"])
    ws.append(["B112530", "OCI - Compute - Standard - E6 Ax - OCPU (OCPU Per Hour)", 1, 1, 744, 0.0138, 10.2672])
    ws.append(["B112531", "OCI - Compute - Standard - E6 Ax - Memory (Gigabyte Per Hour)", 8, 1, 744, 0.0108, 64.2816])
    ws.append([None, "Base Database Service - Virtual Machine"])
    ws.append(["B112724", "Oracle Base Database Compute Infrastructure (ECPU Per Hour)", 4, 1, 744, 0.0251, 74.6976])
    ws.append(["B112726", "Oracle Base Database Service - Enterprise - x86 - ECPU (ECPU Per Hour)", 4, 1, 744, 0.1075, 319.92])
    ws.append([None, "Monthly Total", None, None, None, None, 469.1664])
    wb.save(path)


def check_conversion():
    import tempfile
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "db_bom.xlsx"
        build_fixture(path)
        result = bom_convert.convert_oci_bom(path)
        rows = result["rows"]

        db_rows = [r for r in rows if r["ociServiceCategory"] == "Database"]
        vm_rows = [r for r in rows if r.get("isConvertedCompute")]

        if len(db_rows) != 2:
            failures.append(f"expected the 2 Base Database SKUs as Database rows, got {len(db_rows)}")
        # THE regression: a database line tagged re-mappable is what loses the licence.
        for row in rows:
            if row["ociServiceCategory"] == "Database" and row.get("isConvertedCompute"):
                failures.append(f"database row {row['ociProduct']!r} is flagged isConvertedCompute")
        if len(vm_rows) != 1:
            failures.append(f"expected exactly 1 re-mappable VM (the real E6 Ax), got {len(vm_rows)}")
        elif abs(vm_rows[0]["originalMemoryGb"] - 8) > 0.001:
            failures.append(f"the re-mappable VM lost its memory: {vm_rows[0]['originalMemoryGb']} GB")

        # A re-mappable VM must carry BOTH halves of the flex pair, or picking a shape prices
        # its memory at $0.
        for row in vm_rows:
            if not (row["originalOcpus"] > 0 and row["originalMemoryGb"] > 0):
                failures.append(f"{row['ociProduct']!r} is re-mappable with "
                                f"{row['originalOcpus']} OCPU / {row['originalMemoryGb']} GB")

        total = round(sum(r["monthly"] for r in rows), 4)
        if abs(total - 469.1664) > 0.01:
            failures.append(f"rows sum to {total}, the sheet says 469.1664")
        # ECPUs are a database meter, not compute sizing.
        if abs(result["totals"]["ocpus"] - 1.0) > 0.001:
            failures.append(f"totals.ocpus == {result['totals']['ocpus']}, want 1.0 (the real VM only)")
    return failures


def main():
    failures = check_classification() + check_catalog_sweep() + check_conversion()
    if failures:
        print("FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print(f"Database BOM mapping checks passed "
          f"({len(CLASSIFY_CASES)} classifications, full price-list sweep, conversion round-trip).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
