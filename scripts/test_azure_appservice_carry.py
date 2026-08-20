"""Azure App Service: carry-at-source DEFAULT (red) + doc #11 optional Container
Instances approximate sizing for containerizable Linux plans (orange dropdown).

Doc #11 (2026-08-19): OCPUs/worker = MAX(1, plan vCPUs/2); memory = plan GiB
retained; billing worker-hours already include scale-out. Rates cetools-verified:
Container Instances bill allocated OCPU/memory at compute rates with no
per-container fee - E4 Flex B93113 $0.025/OCPU-hr + B93114 $0.0015/GB-hr.
Windows/OS-dependent plans, Static Web, SSL, free tiers, and unknown SKUs stay
carry-only (doc: do not force into Container Instances).

Run: python3 scripts/test_azure_appservice_carry.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app


def _row(product, cost, qty=744.0, unit="1 Hour"):
    return {"source_provider": "Microsoft", "source_service": "Azure App Service",
            "__meterName": product, "source_product": product,
            "usage_quantity": qty, "usage_unit": unit,
            "source_monthly_cost": cost}


class AzureAppServiceTest(unittest.TestCase):
    def test_default_is_carry_even_for_linux_plans(self):
        items, label, _c, kind, path = app.price_azure_appservice_row(
            _row("Basic Plan - Linux - B1", 25.30))
        self.assertEqual((kind, path), ("appsvc_linux_plan", "appsvc_carry"))
        self.assertTrue(items[0]["carriedOver"])
        self.assertEqual(items[0]["monthly"], 25.30)
        self.assertEqual(list(app.AZ_APPSVC_PATHS)[-1], "appsvc_carry")  # carry LAST

    def test_windows_ssl_staticweb_free_stay_carry_only(self):
        for product, cost in (("Standard Plan - S3", 595.20),          # Windows
                              ("Premium v2 Plan - P1 v2 - Dev/Test", 54.31),
                              ("Premium v4 Plan - P0v4 - Dev/Test", 5.71),  # unknown SKU
                              ("SSL Connections - IP SSL", 39.00),
                              ("Static Web Apps - Standard", 12.70),
                              ("Free Plan - F1", 0.0)):
            items, _l, _c, kind, path = app.price_azure_appservice_row(
                _row(product, cost), "appsvc_ci")   # CI requested but not offered
            self.assertEqual((kind, path), ("appsvc_other", "auto"), product)
            self.assertTrue(items[0]["carriedOver"], product)
            self.assertEqual(items[0]["monthly"], cost, product)

    def test_ci_sizing_doc11_examples(self):
        # B1 Linux: 1 vCPU/1.75 GiB -> MAX(1, 0.5) = 1 OCPU / 1.75 GB per worker.
        items, label, _c, _k, path = app.price_azure_appservice_row(
            _row("Basic Plan - Linux - B1", 25.30, qty=744.0), "appsvc_ci")
        self.assertEqual(path, "appsvc_ci")
        self.assertEqual(items[0]["sku"], "B93113")
        self.assertEqual(items[0]["quantity"], 744.0)            # 1 OCPU x hrs
        self.assertAlmostEqual(items[0]["monthly"], 18.60, places=2)
        self.assertEqual(items[1]["sku"], "B93114")
        self.assertEqual(items[1]["quantity"], 1302.0)           # 1.75 GB x hrs
        self.assertAlmostEqual(items[1]["monthly"], 1.95, places=2)
        self.assertIn("Container Instances", label)
        # P1v3 Linux: 2 vCPU/8 GiB -> 1 OCPU / 8 GB (doc table row).
        p1, *_ = app.price_azure_appservice_row(
            _row("Premium v3 Plan - Linux - P1 v3 - US Central", 252.96,
                 qty=100.0), "appsvc_ci")
        self.assertEqual(p1[0]["quantity"], 100.0)
        self.assertEqual(p1[1]["quantity"], 800.0)
        # S3 (doc example): 4 vCPU/7 GiB -> 2 OCPU / 7 GB.
        s3, *_ = app.price_azure_appservice_row(
            _row("Standard Plan - Linux - S3", 100.0, qty=100.0), "appsvc_ci")
        self.assertEqual(s3[0]["quantity"], 200.0)
        self.assertEqual(s3[1]["quantity"], 700.0)

    def test_ci_note_carries_doc11_controls_and_exclusions(self):
        items, *_ = app.price_azure_appservice_row(
            _row("Basic Plan - Linux - B1", 25.30), "appsvc_ci")
        note = items[0]["mapping"]
        for needle in ("APPROXIMATE COMPUTE CAPACITY CONVERSION", "PLAN worker pool",
                       "EXCLUDES", "Load Balancer", "blue/green",
                       "resource principals", "OIDC", "OKE",
                       "one source worker is not necessarily one application"):
            self.assertIn(needle, note, f"note must mention {needle}")

    def test_carry_note_names_ruling_and_review_items(self):
        items, *_ = app.price_azure_appservice_row(_row("Standard Plan - S3", 595.20))
        note = items[0]["mapping"]
        for needle in ("architecture decision", "OKE", "OCI Functions",
                       "deployment slots", "VNet integration",
                       "ARCHITECTURE REVIEW REQUIRED", "Re-price"):
            self.assertIn(needle, note, f"note must mention {needle}")

    def test_other_services_untouched(self):
        r = _row("Standard Plan - S3", 100.0)
        r["source_service"] = "Functions"
        self.assertIsNone(app.price_azure_appservice_row(r))


if __name__ == "__main__":
    unittest.main(verbosity=2)
