"""AWS developer-services meters must price per the aws_dev_mapping methodology doc.

What this pins, per the doc's statuses:

  priceable / priceable_with_sizing
      CodeArtifact storage  -> OCI Artifact Registry at Object Storage rates (1:1 GB-mo)
      CodeArtifact egress   -> OCI Outbound Data Transfer through the shared 10 TB/region
                               free pool
      CodeBuild minutes     -> OCI DevOps Managed Build runner: OCPU-hours =
                               minutes x vCPU / 120, GB-hours = minutes x GB / 60
  absorbed_no_separate_meter ($0, quantity RETAINED)
      CodeArtifact requests, CodePipeline pipelines/executions, CodeCommit active users
  limit_check / decision_required / direct_vendor_carry (carry at SOURCE, never repriced)
      CodeCommit storage overage, Amazon Q Developer (3rd-party: follows the estate)

And the bug class that motivated the sweep: a PER-SEAT / PER-USER meter must NEVER be
priced as storage GB or compute (the "Copilot seats priced as Block Volume GB" class).
Every handled row carries the forced review flag, and the per-row override channel
(awsDevOverrides -> aws_dev_overrides) must re-price a row onto "carry over source cost".

Run: python3 scripts/test_aws_devservices_mapping.py
"""
import csv
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app


HEADERS = [
    "InvoiceID", "PayerAccountId", "LinkedAccountId", "RecordType",
    "BillingPeriodStartDate", "BillingPeriodEndDate", "InvoiceDate",
    "PayerAccountName", "LinkedAccountName", "ProductCode", "ProductName",
    "SellerOfRecord", "UsageType", "Operation", "ItemDescription",
    "UsageStartDate", "UsageEndDate", "UsageQuantity", "CurrencyCode",
    "CostBeforeTax", "Credits", "TaxAmount", "TotalCost",
]

# (ProductCode, ProductName, UsageType, ItemDescription, quantity, cost)
BILL_LINES = [
    ("AWSCodeArtifact", "AWS CodeArtifact", "USE1-TimedStorage-ByteHrs",
     "$0.05 per GB-month of storage", 120.0, 6.00),
    ("AWSCodeArtifact", "AWS CodeArtifact", "USE1-DataTransfer-Out-Bytes",
     "$0.09 per GB data transfer out", 500.0, 45.00),
    ("AWSCodeArtifact", "AWS CodeArtifact", "USE1-Requests",
     "$0.05 per 10,000 requests", 100000.0, 5.00),
    ("CodeBuild", "AWS CodeBuild", "USE1-Build-Min:Linux:g1.medium",
     "$0.005 per build minute on general1.medium", 12000.0, 60.00),
    ("AWSCodePipeline", "AWS CodePipeline", "USE1-activePipeline",
     "$1.00 per active pipeline", 12.0, 12.00),
    ("AWSCodeCommit", "AWS CodeCommit", "USE1-User-Month",
     "$1.00 per active user-month", 25.0, 25.00),
    ("AWSCodeCommit", "AWS CodeCommit", "USE1-TimedStorage-ByteHrs",
     "$0.06 per GB-month storage overage", 40.0, 2.40),
    ("AmazonQ", "Amazon Q Developer", "USE1-QDevPro-User-Month",
     "$19.00 per Amazon Q Developer Pro user-month", 10.0, 190.00),
]


def _bill_csv_path(tmpdir):
    path = os.path.join(tmpdir, "aws_dev_bill.csv")
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_ALL)
        w.writerow(HEADERS)
        for code, name, usage_type, desc, qty, cost in BILL_LINES:
            w.writerow([
                "1111111111", "123456789012", "", "LinkedLineItem",
                "2026/05/01 00:00:00", "2026/05/31 23:59:59", "2026/06/01 00:00:00",
                "acme-payer", "acme-dev", code, name, "Amazon Web Services, Inc.",
                usage_type, "Usage", desc,
                "2026/05/01 00:00:00", "2026/05/31 23:59:59",
                f"{qty:.6f}", "USD", f"{cost:.6f}", "0.0", "0.0", f"{cost:.6f}",
            ])
    return path


def _price(parsed, aws_dev_overrides=None):
    return app.calculate_pricing(
        parsed["fields"], [dict(r) for r in parsed["rows"]],
        intake_mode=app.INTAKE_MODE_CLOUD_BILL, full_service_beta=True,
        source_provider="AWS", aws_dev_overrides=aws_dev_overrides)


class AwsDevServicesMappingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        cls.parsed = app.parse_cloud_bill(_bill_csv_path(cls.tmpdir))
        cls.pricing = _price(cls.parsed)
        cls.by_usage = {}
        for r in cls.pricing["rows"]:
            cls.by_usage[r.get("sourceUsageType")] = r

    def row(self, usage_type):
        r = self.by_usage.get(usage_type)
        self.assertIsNotNone(r, f"no priced row for usageType {usage_type}")
        return r

    def test_every_handled_row_is_flagged(self):
        for _, _, usage_type, _, _, _ in BILL_LINES:
            r = self.row(usage_type)
            self.assertEqual(r.get("mappingFlag"),
                             "AWS developer services mapping - review required",
                             f"{usage_type} lost the forced review flag")

    def test_codeartifact_storage_prices_as_artifact_registry(self):
        r = self.row("USE1-TimedStorage-ByteHrs")  # CodeArtifact one (CodeCommit's differs)
        # by_usage keyed on usageType collides for the two TimedStorage rows; find by service
        r = next(x for x in self.pricing["rows"]
                 if x.get("sourceService") == "AWSCodeArtifact"
                 and x.get("sourceUsageType") == "USE1-TimedStorage-ByteHrs")
        self.assertEqual(r["ociProduct"], "OCI Artifact Registry (Object Storage rates)")
        self.assertAlmostEqual(r["monthly"], round(120.0 * 0.0255, 2), places=2)  # 3.06
        li = r["lineItems"][0]
        self.assertEqual(li["rate"], 0.0255)
        self.assertIn("aws-codeartifact-storage-to-oci-artifact-registry-storage",
                      li["mapping"])

    def test_codeartifact_egress_uses_transfer_pool(self):
        r = self.row("USE1-DataTransfer-Out-Bytes")
        self.assertEqual(r["ociProduct"], "OCI Outbound Data Transfer")
        li = r["lineItems"][0]
        self.assertEqual(li["quantity"], 500.0)
        # 500 GB sits inside the tenancy-wide 10 TB/region free allowance -> $0.
        self.assertEqual(r["monthly"], 0.0)
        self.assertIn("aws-codeartifact-internet-egress-to-oci-outbound-data-transfer",
                      li["mapping"])
        # $0 because of the free pool, NOT because the row fell into auto-carry.
        self.assertFalse(any(x.get("carriedOver") for x in r["lineItems"]))

    def test_codeartifact_requests_absorbed_at_zero_quantity_retained(self):
        r = self.row("USE1-Requests")
        self.assertEqual(r["ociProduct"], "OCI Artifact Registry (requests included)")
        self.assertEqual(r["monthly"], 0.0)
        li = r["lineItems"][0]
        self.assertEqual(li["quantity"], 100000.0)   # retained for sizing
        self.assertIn("aws-codeartifact-requests-to-oci-artifact-registry", li["mapping"])
        self.assertFalse(any(x.get("carriedOver") for x in r["lineItems"]))

    def test_codebuild_minutes_price_ocpu_and_memory_per_doc_formulas(self):
        r = self.row("USE1-Build-Min:Linux:g1.medium")
        self.assertEqual(r["ociProduct"], "OCI DevOps Managed Build (runner compute)")
        # general1.medium = 4 vCPU / 7 GB.
        # OCPU-hours = 12,000 min x 4 vCPU / 120 = 400 -> x $0.025 (B93113 live) = $10.00
        # GB-hours   = 12,000 min x 7 GB / 60 = 1,400 -> x $0.0015 (B93114 live) = $2.10
        ocpu = next(li for li in r["lineItems"] if li["unit"] == "OCPU-hour")
        mem = next(li for li in r["lineItems"] if li["unit"] == "GB-hour")
        self.assertAlmostEqual(ocpu["quantity"], 400.0, places=2)
        self.assertAlmostEqual(ocpu["monthly"], 400.0 * app.AWS_DEVOPS_BUILD_OCPU_RATE, places=2)
        self.assertEqual(ocpu["sku"], app.AWS_DEVOPS_BUILD_OCPU_SKU)
        self.assertAlmostEqual(mem["quantity"], 1400.0, places=2)
        self.assertAlmostEqual(mem["monthly"], 1400.0 * app.AWS_DEVOPS_BUILD_MEM_RATE, places=2)
        self.assertAlmostEqual(r["monthly"], 400.0 * app.AWS_DEVOPS_BUILD_OCPU_RATE
                               + 1400.0 * app.AWS_DEVOPS_BUILD_MEM_RATE, places=2)
        self.assertIn("aws-codebuild-compute-minutes-to-oci-devops-build-runner-ocpu-hours",
                      ocpu["mapping"])
        self.assertIn("aws-codebuild-memory-minutes-to-oci-devops-build-runner-gb-hours",
                      mem["mapping"])

    def test_codepipeline_absorbed_at_zero(self):
        r = self.row("USE1-activePipeline")
        self.assertEqual(r["ociProduct"], "OCI DevOps (pipelines free)")
        self.assertEqual(r["monthly"], 0.0)
        self.assertEqual(r["lineItems"][0]["quantity"], 12.0)
        self.assertIn("aws-codepipeline-active-pipelines-to-oci-devops-pipeline-executions",
                      r["lineItems"][0]["mapping"])
        self.assertFalse(any(x.get("carriedOver") for x in r["lineItems"]))

    def test_codecommit_users_absorbed_at_zero(self):
        r = self.row("USE1-User-Month")
        self.assertEqual(r["ociProduct"], "OCI DevOps Code Repositories (users included)")
        self.assertEqual(r["monthly"], 0.0)
        self.assertEqual(r["lineItems"][0]["quantity"], 25.0)
        self.assertIn("aws-codecommit-active-users-to-oci-devops-code-repositories",
                      r["lineItems"][0]["mapping"])

    def test_codecommit_storage_carries_with_limit_check(self):
        r = next(x for x in self.pricing["rows"]
                 if x.get("sourceService") == "AWSCodeCommit"
                 and x.get("sourceUsageType") == "USE1-TimedStorage-ByteHrs")
        self.assertEqual(r["ociProduct"], "Carried - OCI DevOps repository limit check")
        self.assertAlmostEqual(r["monthly"], 2.40, places=2)  # at SOURCE cost
        li = r["lineItems"][0]
        self.assertTrue(li.get("carriedOver"))
        self.assertIn("aws-codecommit-storage-and-git-requests-to-oci-devops-code-repositories",
                      li["mapping"])
        self.assertIn("1,024 MB", li["mapping"])

    def test_amazon_q_carries_as_third_party_decision(self):
        r = self.row("USE1-QDevPro-User-Month")
        self.assertEqual(r["ociProduct"], "Carried - Amazon Q Developer (decision required)")
        self.assertAlmostEqual(r["monthly"], 190.00, places=2)  # at SOURCE cost
        li = r["lineItems"][0]
        self.assertTrue(li.get("carriedOver"))
        self.assertTrue(li.get("thirdParty"),
                        "vendor-style subscription must be 3rd-party (no OCI discount)")
        self.assertIn("aws-amazon-q-developer-pro-to-oci", li["mapping"])

    def test_per_seat_meters_never_price_as_storage_gb(self):
        # The Copilot-seats-priced-as-Block-Volume-GB bug class: user/seat meters must
        # never produce a priced (>$0) GB-denominated line item.
        for usage_type in ("USE1-User-Month", "USE1-QDevPro-User-Month"):
            r = self.row(usage_type)
            for li in r["lineItems"]:
                unit = str(li.get("unit") or "").lower()
                if "gb" in unit and not li.get("carriedOver"):
                    self.assertEqual(float(li.get("monthly") or 0), 0.0,
                                     f"{usage_type} priced a seat meter as {unit}")
            self.assertNotIn("block volume", str(r.get("ociProduct") or "").lower())

    def test_carried_totals_track_carry_rows(self):
        carried = self.pricing["totals"]["carriedSourceMonthly"]
        self.assertAlmostEqual(carried, 2.40 + 190.00, places=2)

    def test_override_channel_reprices_build_row_to_carry(self):
        build_src = next(r for r in self.parsed["rows"]
                         if "Build-Min" in str(r.get("__usageType")))
        pricing = _price(self.parsed,
                         aws_dev_overrides={str(build_src["__id"]): "awsdev_carry"})
        r = next(x for x in pricing["rows"]
                 if x.get("sourceUsageType") == "USE1-Build-Min:Linux:g1.medium")
        self.assertEqual(r["ociProduct"], "Carried over source cost (by selection)")
        self.assertAlmostEqual(r["monthly"], 60.00, places=2)
        self.assertTrue(r["lineItems"][0].get("carriedOver"))
        self.assertEqual(r.get("awsDevPath"), "awsdev_carry")
        self.assertEqual(r.get("awsDevKind"), "build")

    def test_provider_gate_never_fires_on_azure_rows(self):
        row = {"source_provider": "Azure", "source_service": "AWSCodeBuild",
               "__usageType": "USE1-Build-Min:Linux:g1.medium",
               "usage_quantity": 100, "source_monthly_cost": 1.0}
        self.assertIsNone(app.price_aws_devservices_row(row))

    def test_dropdown_kinds_expose_path_metadata(self):
        r = self.row("USE1-Build-Min:Linux:g1.medium")
        self.assertEqual(r.get("awsDevKind"), "build")
        self.assertEqual(r.get("awsDevPath"), "awsdev_build")
        # Absorbed rows are automatic - no dropdown path options.
        self.assertEqual(self.row("USE1-activePipeline").get("awsDevPath"), "auto")


class StorageGatewayThroughputTest(unittest.TestCase):
    """Storage Gateway Uploaded/Downloaded-Bytes are THROUGHPUT, not stored capacity.
    The real Quad AWS bill priced $36.76 of upload throughput as $1,198.62 of File
    Storage GB-month (32.6x). Upload = free OCI ingest ($0); download = outbound
    transfer via the shared free pool; unknown gateway meters carry."""

    def _price(self, usage_type, qty, path=None, pools=None):
        row = {"source_provider": "AWS", "source_service": "AWSStorageGateway",
               "source_product": "$0.01 per GB - data written by your gateway",
               "__usageType": usage_type, "usage_quantity": qty,
               "source_monthly_cost": qty * 0.01}
        return row, app.price_aws_devservices_row(row, path, transfer_pools=pools)

    def test_uploaded_bytes_is_free_ingest_not_capacity(self):
        row, res = self._price("USE1-Uploaded-Bytes", 2576.3174)
        self.assertIsNotNone(res)
        items, label, _cat, kind, apath = res
        self.assertEqual(kind, "sgw_upload")
        self.assertEqual(apath, "awsdev_sgw_ingress")
        self.assertEqual(sum(li["monthly"] for li in items), 0.0)
        self.assertIn("ingest", label.lower())
        # NEVER a capacity product.
        self.assertNotIn("file storage", label.lower())
        self.assertNotIn("gb-mo", " ".join(li["unit"].lower() for li in items))

    def test_downloaded_bytes_prices_as_outbound_transfer(self):
        pools = {}
        row, res = self._price("EUC1-Downloaded-Bytes", 500.0, pools=pools)
        self.assertIsNotNone(res)
        items, label, _cat, kind, _p = res
        self.assertEqual(kind, "sgw_download")
        self.assertEqual(label, "OCI Outbound Data Transfer")
        # 500 GB sits inside the 10 TB/region free pool -> $0 but through the POOL.
        self.assertEqual(sum(li["monthly"] for li in items), 0.0)
        self.assertTrue(pools, "must consume the shared transfer pool")

    def test_unknown_gateway_meter_safe_fails_to_carry(self):
        row, res = self._price("USE1-CachedVolumeUsage", 100.0)
        items, _label, _cat, kind, _p = res
        self.assertEqual(kind, "sgw_other")
        self.assertTrue(items[0].get("carriedOver"))
        self.assertEqual(items[0]["monthly"], round(row["source_monthly_cost"], 2))

    def test_carry_option_available_last(self):
        row, res = self._price("USE1-Uploaded-Bytes", 100.0, path="awsdev_carry")
        items, _label, _cat, _kind, apath = res
        self.assertEqual(apath, "awsdev_carry")
        self.assertTrue(items[0].get("carriedOver"))
        self.assertEqual(app.AWS_DEV_PATHS_BY_KIND["sgw_upload"][-1], "awsdev_carry")
        self.assertEqual(app.AWS_DEV_PATHS_BY_KIND["sgw_download"][-1], "awsdev_carry")


if __name__ == "__main__":
    unittest.main(verbosity=2)
