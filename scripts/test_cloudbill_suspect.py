"""Cloud bill dropped into On-prem mode -> "Are you sure this isn't a cloud bill?"
confirm (Chris's ruling 2026-08-19). Backend half: the on-prem rule-based parser
sets metadata.cloudBillSuspected to the detected provider when >=4 distinctive
billing-header tokens appear in the first raw rows (the detected header row is
often a DATA row on bills, so the first rows are scanned). Frontend renders the
dialog: "Yes - use Cloud Bill mode" re-parses in cloud-bill mode; "No" (red)
keeps the on-prem parse.

Run: python3 scripts/test_cloudbill_suspect.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app

AZ = ["BillingAccountName", "BillingPeriodStartDate", "SubscriptionName",
      "MeterCategory", "MeterSubCategory", "MeterRegion", "MeterName",
      "Quantity", "EffectivePrice", "Cost", "BillingCurrency", "ChargeType"]
AWS_CUR = ["bill/BillingPeriodStartDate", "lineItem/UsageType",
           "lineItem/LineItemType", "lineItem/UnblendedCost",
           "product/ProductCode"]
AWS_DBR = ["InvoiceID", "PayerAccountId", "RecordType",
           "BillingPeriodStartDate", "ProductCode", "UsageType",
           "UsageQuantity", "BlendedRate", "CostBeforeTax", "TotalCost"]


class CloudBillSuspectTest(unittest.TestCase):
    def test_bill_headers_detect_with_provider(self):
        self.assertEqual(app.looks_like_cloud_bill_headers(AZ), "Azure")
        self.assertEqual(app.looks_like_cloud_bill_headers(AWS_CUR), "AWS")
        self.assertEqual(app.looks_like_cloud_bill_headers(AWS_DBR), "AWS")

    def test_inventories_never_trigger(self):
        for labels in (["Server Name", "Environment", "CPU", "Memory GB",
                        "Storage GB", "OS"],
                       ["VM", "Powerstate", "CPUs", "Memory",
                        "Provisioned MiB", "Datacenter", "Host"],
                       [], None):
            self.assertEqual(app.looks_like_cloud_bill_headers(labels), "")

    def test_onprem_parse_flags_the_azure_fixture(self):
        md = app.parse_workbook_rule_based(
            str(Path(__file__).resolve().parents[1]
                / "testdata" / "azure_unmapped_fixture.csv"))["metadata"]
        self.assertEqual(md.get("cloudBillSuspected"), "Azure")
        self.assertEqual(md.get("intakeMode"), app.INTAKE_MODE_ON_PREM)


class EnrollmentFormatMappingTest(unittest.TestCase):
    """Detail_Enrollment_*.xlsx format (Chris 2026-08-19): ConsumedService must
    never beat MeterCategory for the service column, and empty Meter Sub-Category
    rows fall back to MeterName, then PartNumber, for SKU/Meter."""

    HEADERS = ["SubscriptionGuid", "SubscriptionName", "Date", "Product",
               "PartNumber", "MeterId", "MeterCategory", "MeterSubCategory",
               "MeterRegion", "MeterName", "ConsumedQuantity", "ResourceRate",
               "ExtendedCost", "ResourceLocation", "ConsumedService",
               "CostCenter", "UnitOfMeasure", "ResourceGroup"]

    def test_metercategory_beats_consumedservice(self):
        m = app.infer_cloud_bill_mappings(self.HEADERS, "Azure")
        self.assertEqual(m["source_service"]["sourceHeader"], "MeterCategory")
        self.assertEqual(m["source_monthly_cost"]["sourceHeader"], "ExtendedCost")
        self.assertEqual(m["usage_quantity"]["sourceHeader"], "ConsumedQuantity")

    def test_consumedservice_still_maps_when_no_metercategory(self):
        headers = [h for h in self.HEADERS if h != "MeterCategory"]
        m = app.infer_cloud_bill_mappings(headers, "Azure")
        self.assertEqual(m["source_service"]["sourceHeader"], "ConsumedService")

    def test_quad_csv_mappings_unchanged(self):
        csv_headers = ["BillingAccountName", "SubscriptionName", "Date", "Product",
                       "PartNumber", "MeterCategory", "MeterSubCategory",
                       "MeterRegion", "MeterName", "Quantity", "EffectivePrice",
                       "Cost", "BillingCurrency", "ResourceLocation", "UnitOfMeasure"]
        m = app.infer_cloud_bill_mappings(csv_headers, "Azure")
        self.assertEqual(m["source_service"]["sourceHeader"], "MeterCategory")
        self.assertEqual(m["source_product"]["sourceHeader"], "MeterSubCategory")
        self.assertEqual(m["source_monthly_cost"]["sourceHeader"], "Cost")


if __name__ == "__main__":
    unittest.main(verbosity=2)
