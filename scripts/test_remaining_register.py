"""Remaining-services register (doc #13 + Chris's walkthrough rulings, 2026-08-19).

App Gateway -> LB+WAF priced (orange, carry option); Event Hubs / Foundry -> carry
default with approximate Streaming / per-meter GenAI options (orange when chosen);
Load Balancer -> approximate conversion DEFAULT flagged YELLOW (carry option);
Monitor -> split onto real OCI meters, web tests carry; Defender -> CSPM->Cloud
Guard $0 (no such meters in this bill), node protection carries; AKS Uptime SLA ->
$0 with the not-a-free-platform note; Storage -> HNS zero-source rows stop billing
phantom OCI cost, geo-replication/queue-IO tail carries flagged.

Run: python3 scripts/test_remaining_register.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app


def _row(svc, meter, qty, cost, unit="1 Hour", sub=""):
    return {"source_provider": "Microsoft", "source_service": svc,
            "__meterName": meter, "__meterSub": sub, "source_product": meter,
            "usage_quantity": qty, "usage_unit": unit,
            "source_monthly_cost": cost}


def price(row, path=None, ctx=None):
    return app.price_azure_register_row(row, path, ctx=ctx or {})


class RemainingRegisterTest(unittest.TestCase):
    # ---- App Gateway -------------------------------------------------------
    def test_appgw_fixed_converts_to_lb_base_plus_waf(self):
        ctx = {"waf_instance_pool": [1.0]}
        items, label, _c, kind, path, level = price(
            _row("Application Gateway", "Standard Fixed Cost", 2835.221111, 1020.68,
                 "1/Hour"), ctx=ctx)
        self.assertEqual((kind, path, level), ("reg_appgw_fixed", "appgw_convert", "orange"))
        self.assertEqual(items[0]["sku"], "B93030")
        self.assertAlmostEqual(items[0]["monthly"], 32.04, places=2)
        self.assertEqual(items[1]["sku"], "B94579")
        self.assertAlmostEqual(items[1]["monthly"], 14.05, places=2)  # first inst free
        self.assertIn("REQUESTS not priceable", items[0]["mapping"])

    def test_appgw_capacity_units_use_azure_approximation(self):
        items, *_ = price(_row("Application Gateway", "Standard Capacity Units",
                               28352.211111, 408.27, "1/Hour"))
        self.assertEqual(items[0]["sku"], "B93031")
        self.assertAlmostEqual(items[0]["monthly"], 6.29, places=2)   # x2.22 Mbps/CU
        self.assertIn("APPROXIMATION", items[0]["mapping"])
        c, _l, _c2, _k, p, lv = price(_row("Application Gateway",
                                           "Standard Fixed Cost", 100.0, 36.0),
                                      "appgw_carry")
        self.assertTrue(c[0]["carriedOver"]); self.assertEqual((p, lv), ("appgw_carry", ""))

    # ---- Event Hubs --------------------------------------------------------
    def test_event_hubs_default_carries(self):
        items, _l, _c, kind, path, level = price(
            _row("Event Hubs", "Standard Throughput Unit", 20694.0, 620.82))
        self.assertEqual((kind, path), ("reg_eh_tu", "eh_carry"))
        self.assertTrue(items[0]["carriedOver"])

    def test_event_hubs_streaming_option_prices_assumed_kb(self):
        tu, *_ = price(_row("Event Hubs", "Standard Throughput Unit", 20694.0,
                            620.82), "eh_streaming")
        self.assertEqual(tu[0]["monthly"], 0.0)          # absorbed into per-GB
        items, _l, _c, _k, path, level = price(
            _row("Event Hubs", "Standard Ingress Events", 192.293401, 5.38, "1M"),
            "eh_streaming")
        self.assertEqual((path, level), ("eh_streaming", "orange"))
        self.assertEqual(items[0]["sku"], "B90938")
        self.assertAlmostEqual(items[0]["monthly"], 9.17, places=2)
        self.assertAlmostEqual(items[1]["monthly"], 0.88, places=2)
        self.assertIn("ASSUMED 1 KB", items[0]["mapping"])

    # ---- Foundry Models ----------------------------------------------------
    def test_foundry_default_carries_genai_option_prices_per_meter(self):
        c, *_ = price(_row("Foundry Models", "5.4 inp Gl 1M Tokens", 72.100917,
                           180.25, "1M"))
        self.assertTrue(c[0]["carriedOver"])
        cases = (("5.4 inp Gl 1M Tokens", 72.100917, "1M", "B111910", 90.13),
                 ("5.4 cd inp Gl 1M Tokens", 104.36096, "1M", "B111911", 20.87),
                 ("gpt 4.1 Outp regnl Tokens", 2731.107, "1K", "B112076", 6.83),
                 ("gpt-4o-mini-0718-Inp-glbl Tokens", 77469.256, "1K", "B112004", 11.62),
                 ("GPT 5 Mini outpt Glbl 1M Tokens", 2.337559, "1M", "B112005", 1.4),
                 ("text-embedding-3-small-glbl Tokens", 611614.896, "1K", "B108079", 61.16))
        for meter, qty, unit, sku, dollars in cases:
            items, _l, _c2, _k, path, level = price(
                _row("Foundry Models", meter, qty, 1.0, unit), "fdry_genai")
            self.assertEqual(items[0]["sku"], sku, meter)
            self.assertAlmostEqual(items[0]["monthly"], dollars, places=2, msg=meter)
            self.assertEqual(level, "orange")
            self.assertIn("MODEL SUBSTITUTION", items[0]["mapping"])

    # ---- Load Balancer (yellow) --------------------------------------------
    def test_lb_converts_by_default_flagged_yellow(self):
        rules, _l, _c, kind, path, level = price(
            _row("Load Balancer", "Standard Included LB Rules and Outbound Rules",
                 10416.0, 260.40))
        self.assertEqual((kind, path, level), ("reg_lb_rules", "lb_convert", ""))
        self.assertAlmostEqual(rules[0]["monthly"], 117.70, places=2)
        self.assertIn("UPPER BOUND", rules[0]["mapping"])
        data, *_ = price(_row("Load Balancer", "Standard Data Processed",
                              14934.165724, 74.67, "1 GB"))
        self.assertAlmostEqual(data[0]["monthly"], 3.40, places=2)
        carry, _l2, _c2, _k2, p2, _v = price(
            _row("Load Balancer", "Standard Data Processed", 100.0, 0.5, "1 GB"),
            "lb_carry")
        self.assertTrue(carry[0]["carriedOver"])

    # ---- Azure Monitor split -----------------------------------------------
    def test_monitor_split_prices_real_oci_meters(self):
        ctx = {"logging_pool": [10.0], "monitor_dp_pool": [500.0], "email_pool": [1.0]}
        logs, _l, _c, k, p, lv = price(_row("Azure Monitor",
                                            "Basic Logs Data Ingestion",
                                            351.862285, 177.66, "1 GB"), ctx=ctx)
        self.assertEqual((k, lv), ("reg_mon_logs", "orange"))
        self.assertAlmostEqual(logs[0]["monthly"], 17.09, places=2)   # 10 GB free
        met, *_ = price(_row("Azure Monitor", "Metrics ingestion Metric samples",
                             631.338328, 101.01, "10M"), ctx=ctx)
        self.assertAlmostEqual(met[0]["monthly"], 14.53, places=2)    # 500M free
        al, *_ = price(_row("Azure Monitor", "Alerts Metric Monitored", 360.07,
                            35.01, "1/Month"), ctx=ctx)
        self.assertEqual(al[0]["monthly"], 0.0)
        ar, *_ = price(_row("Azure Monitor", "Data Archive", 574.951699, 11.66,
                            "1 GB/Month"), ctx=ctx)
        self.assertAlmostEqual(ar[0]["monthly"], 1.49, places=2)
        em, *_ = price(_row("Azure Monitor", "Emails", 14558.0, 0.27, "1"), ctx=ctx)
        self.assertAlmostEqual(em[0]["monthly"], 0.27, places=2)      # 1K free
        wt, _l2, _c2, k2, _p2, _v2 = price(
            _row("Azure Monitor", "Standard Web Test Execution", 142848.0, 79.99, "1"))
        self.assertEqual(k2, "reg_monitor_carry")
        self.assertTrue(wt[0]["carriedOver"])

    # ---- Defender / AKS / Storage ------------------------------------------
    def test_defender_nodes_carry_cspm_would_be_cloud_guard(self):
        n, _l, _c, kind, *_ = price(_row("Microsoft Defender for Cloud",
                                         "Standard Node", 9.0, 135.0, "1/Month"))
        self.assertEqual(kind, "reg_carry"); self.assertTrue(n[0]["carriedOver"])
        cg, _l2, _c2, k2, _p, lv = price(_row("Microsoft Defender for Cloud",
                                              "Defender CSPM", 10.0, 16.33))
        self.assertEqual((k2, lv), ("reg_defender_cloudguard", "orange"))
        self.assertEqual(cg[0]["monthly"], 0.0)

    def test_aks_uptime_sla_is_zero_with_enhanced_note(self):
        items, _l, _c, kind, _p, lv = price(
            _row("Azure Kubernetes Service", "Standard Uptime SLA", 1488.04, 148.80))
        self.assertEqual((kind, lv), ("reg_aks_sla", "orange"))
        self.assertEqual(items[0]["monthly"], 0.0)
        self.assertIn("NOT a free Kubernetes platform", items[0]["mapping"])
        self.assertIn("$148.80", items[0]["mapping"])

    def test_storage_hns_and_georeplication_tail(self):
        hns, _l, _c, kind, *_ = price(_row(
            "Storage", "General Block Blob v2 Hierarchical Namespace - Hot LRS - Data Stored",
            20952.8, 0.0, "1 GB/Month"))
        self.assertEqual(kind, "reg_storage_hns")
        self.assertEqual(hns[0]["monthly"], 0.0)         # was phantom $209.52
        geo, _l2, _c2, k2, *_ = price(_row(
            "Storage", "Bandwidth - Geo-Replication v2 Data Transfer", 100.0, 31.63, "1 GB"))
        self.assertEqual(k2, "reg_carry"); self.assertTrue(geo[0]["carriedOver"])
        # Priced storage rows are untouched (cost > 0 HNS, tiered blobs, etc.)
        self.assertIsNone(price(_row("Storage", "Tiered Block Blob - Archive LRS - Data Stored",
                                     100.0, 5.0, "1 GB/Month")))

    def test_other_services_untouched(self):
        self.assertIsNone(price(_row("Azure App Service", "Basic Plan - Linux - B1",
                                     744.0, 25.30)))
        self.assertIsNone(price(_row("Bandwidth", "Standard Data Transfer Out",
                                     1000.0, 87.0, "1 GB")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
