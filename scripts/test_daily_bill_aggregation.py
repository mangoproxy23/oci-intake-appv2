"""Daily-bill aggregation must NEVER scramble columns.

The regression this pins: pandas 3's groupby(as_index=False).agg(<dict>) returns the
integer-labelled key columns with their VALUES misaligned from their labels. On the
403,950-line Quad/Graphics Azure bill that silently shifted every non-summed column -
source_service picked up the Product column, oci_product picked up AccountName (people's
names showed up as OCI products), the archive tier stopped matching - while the summed
quantity/cost stayed exact, so total-based checks all passed. aggregate_daily_bill_lines
now aggregates on unambiguous string labels; this test fails loudly if any column ever
drifts again, on any pandas version.

Run: python3 scripts/test_daily_bill_aggregation.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

import app


HEADERS = [
    "BillingAccountName", "BillingPeriodStartDate", "AccountOwnerId", "AccountName",
    "SubscriptionName", "Date", "Product", "PartNumber", "MeterCategory",
    "MeterSubCategory", "MeterRegion", "MeterName", "Quantity", "EffectivePrice",
    "Cost", "UnitPrice", "BillingCurrency", "ResourceLocation", "UnitOfMeasure",
    "PublisherName",
]

# Two meters x three "days" each. Every key column's value encodes its own column name,
# so any label/value misalignment is immediately visible.
METERS = [
    {
        "AccountOwnerId": "ravassell@example.com", "AccountName": "Richard Vassell",
        "SubscriptionName": "sub-prod-connectivity",
        "Product": "Tiered Block Blob - Archive LRS - Data Stored - US East 2",
        "PartNumber": "AAD-11111", "MeterCategory": "Storage",
        "MeterSubCategory": "Tiered Block Blob", "MeterRegion": "Virginia",
        "MeterName": "Archive LRS Data Stored", "ResourceLocation": "EastUS2",
        "UnitOfMeasure": "1 GB/Month", "qty": 100.0, "cost": 0.26,
    },
    {
        "AccountOwnerId": "mtraff@example.com", "AccountName": "Mario Trafficante",
        "SubscriptionName": "Marketing Technology",
        "Product": "Rtn Preference: MGN - Standard Data Transfer Out",
        "PartNumber": "Q5H-00003", "MeterCategory": "Bandwidth",
        "MeterSubCategory": "Rtn Preference: MGN", "MeterRegion": "All Regions",
        "MeterName": "Standard Data Transfer Out", "ResourceLocation": "EastUS2",
        "UnitOfMeasure": "1 GB", "qty": 7.5, "cost": 0.06,
    },
]


def _frame(days=3):
    rows = []
    for day in range(1, days + 1):
        for m in METERS:
            rows.append([
                "Example Corp", "7/1/2026", m["AccountOwnerId"], m["AccountName"],
                m["SubscriptionName"], f"7/{day}/2026", m["Product"], m["PartNumber"],
                m["MeterCategory"], m["MeterSubCategory"], m["MeterRegion"],
                m["MeterName"], m["qty"], 0.1, m["cost"], 0.1, "USD",
                m["ResourceLocation"], m["UnitOfMeasure"], "Microsoft",
            ])
    return pd.DataFrame(rows)


def _mappings():
    # 1-based sourceColumn, mirroring infer_cloud_bill_mappings output.
    def col(name):
        return {"sourceColumn": HEADERS.index(name) + 1, "sourceHeader": name}
    return {
        "source_account": col("SubscriptionName"),
        "source_service": col("MeterCategory"),
        "source_product": col("MeterSubCategory"),
        "source_region": col("ResourceLocation"),
        "usage_quantity": col("Quantity"),
        "usage_unit": col("UnitOfMeasure"),
        "source_monthly_cost": col("Cost"),
        "source_currency": col("BillingCurrency"),
        "source_period": col("Date"),
    }


class DailyBillAggregationTests(unittest.TestCase):
    def test_every_column_keeps_its_own_value(self):
        days = 3
        frame = _frame(days)
        out, orig_rows, counts = app.aggregate_daily_bill_lines(frame, HEADERS, _mappings())
        self.assertEqual(len(out.index), len(METERS), "one aggregated row per meter")
        self.assertEqual(counts, [days] * len(METERS))
        self.assertEqual(orig_rows, [0, 1], "first source line of each meter group")

        qty_i, cost_i = HEADERS.index("Quantity"), HEADERS.index("Cost")
        for pos, meter in enumerate(METERS):
            got = out.iloc[pos].tolist()
            # Summed columns are exact (days x per-day value).
            self.assertAlmostEqual(float(got[qty_i]), meter["qty"] * days, places=6)
            self.assertAlmostEqual(float(got[cost_i]), meter["cost"] * days, places=6)
            # EVERY other mapped column still holds its own value - this is the
            # pandas-3 scramble regression.
            for header, want in [
                ("AccountName", meter["AccountName"]),
                ("SubscriptionName", meter["SubscriptionName"]),
                ("Product", meter["Product"]),
                ("PartNumber", meter["PartNumber"]),
                ("MeterCategory", meter["MeterCategory"]),
                ("MeterSubCategory", meter["MeterSubCategory"]),
                ("MeterName", meter["MeterName"]),
                ("ResourceLocation", meter["ResourceLocation"]),
                ("UnitOfMeasure", meter["UnitOfMeasure"]),
                ("BillingCurrency", "USD"),
            ]:
                self.assertEqual(
                    str(got[HEADERS.index(header)]), str(want),
                    f"column {header!r} lost its value after aggregation - "
                    f"columns are scrambled")
            # Date/price columns keep the FIRST value.
            self.assertEqual(str(got[HEADERS.index("Date")]), "7/1/2026")

    def test_end_to_end_parse_maps_the_right_columns(self):
        # A real parse through parse_cloud_bill with enough lines to trigger
        # aggregation: the parsed row's fields must come from the RIGHT columns.
        import csv as _csv
        import tempfile

        days_needed = app.CLOUD_BILL_AGGREGATE_MIN_ROWS // len(METERS) + 1
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                         newline="") as fh:
            w = _csv.writer(fh)
            w.writerow(HEADERS)
            for row in _frame(days_needed).itertuples(index=False):
                w.writerow(list(row))
            path = fh.name
        try:
            parsed = app.parse_cloud_bill(path)
        finally:
            Path(path).unlink(missing_ok=True)

        rows = parsed["rows"]
        self.assertEqual(len(rows), len(METERS), "aggregated to one row per meter")
        by_service = {r["source_service"]: r for r in rows}
        self.assertIn("Storage", by_service, "source_service must be MeterCategory")
        self.assertIn("Bandwidth", by_service)
        storage = by_service["Storage"]
        self.assertEqual(storage["source_product"], "Tiered Block Blob")
        self.assertEqual(storage["source_account"], "sub-prod-connectivity")
        self.assertEqual(storage["source_currency"], "USD")
        self.assertEqual(storage["__meterName"], "Archive LRS Data Stored")
        # The tier only lives in the Product string - it must be captured and
        # must drive the archive mapping.
        self.assertEqual(storage["__azureProduct"],
                         "Tiered Block Blob - Archive LRS - Data Stored - US East 2")
        self.assertEqual(storage["oci_product"], "OCI Archive Storage",
                         "archive tier in the Product string must map to Archive Storage")
        # A person's name must never be an OCI product.
        for r in rows:
            self.assertNotIn(str(r.get("oci_product")),
                             ("Richard Vassell", "Mario Trafficante"))
        bw = by_service["Bandwidth"]
        self.assertEqual(bw["oci_product"], "OCI Outbound Data Transfer")
        # Sums survived aggregation exactly.
        self.assertAlmostEqual(float(storage["usage_quantity"]), 100.0 * days_needed, places=4)
        self.assertAlmostEqual(float(storage["source_monthly_cost"]), 0.26 * days_needed, places=4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
