"""Azure Firewall -> OCI Network Firewall mapping (methodology doc #5, 2026-08-19).

Azure bills per DEPLOYMENT UNIT (a Secure Virtual Hub typically runs two units as ONE
logical hub's HA pair); an OCI regional Network Firewall has HA built in. Default reads
the units as one hub -> each unit-row carries HALF of one OCI firewall's hours; the
two-independent-firewalls reading and carry are the dropdown alternatives. Data
Processed rows are AUTOMATIC: $0 inside the 10 TB/month included allowance (B95404),
auto-carried only when every deployment row is carried. Everything is flagged
"architecture validation required" (throughput p95, inbound DNAT blocker, VWAN transit,
DNS/TLS/logging validations in the note).

Doc-pinned dollars for the Quad bill (2 x 744 unit-hours @ $930, 1,753.67 GB data):
one regional $2,046.00 / two firewalls $4,092.00 / carry $1,888.06.

Run: python3 scripts/test_azure_firewall_mapping.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app


def _dep_row(cost=930.0, hours=744.0):
    return {"source_provider": "Microsoft", "source_service": "Azure Firewall",
            "__meterName": "Standard Secure Virtual Hub Deployment",
            "usage_quantity": hours, "usage_unit": "1 Hour",
            "source_monthly_cost": cost}


def _data_row(gb=1753.6656, cost=28.0586):
    return {"source_provider": "Microsoft", "source_service": "Azure Firewall",
            "__meterName": "Standard Secure Virtual Hub Data Processed",
            "usage_quantity": gb, "usage_unit": "1 GB",
            "source_monthly_cost": cost}


class AzureFirewallMappingTest(unittest.TestCase):
    def test_one_regional_default_halves_each_unit_row(self):
        items, label, _cat, kind, path = app.price_azure_firewall_row(_dep_row())
        self.assertEqual((kind, path), ("azfw_deployment", "fw_one_regional"))
        self.assertEqual(items[0]["quantity"], 372.0)          # 744 unit-hours / 2
        self.assertEqual(items[0]["monthly"], 1023.0)          # x $2.75
        self.assertIn("one regional", label.lower())
        # Two unit rows sum to exactly one 744-hour firewall = the doc's $2,046.
        self.assertEqual(2 * items[0]["monthly"], 2046.0)

    def test_two_firewalls_option(self):
        items, label, _c, _k, path = app.price_azure_firewall_row(_dep_row(), "fw_two")
        self.assertEqual(path, "fw_two")
        self.assertEqual(items[0]["monthly"], 2046.0)          # 744 x $2.75 per row
        self.assertIn("two independent", label.lower())

    def test_carry_is_last_option_and_prices_at_source(self):
        self.assertEqual(list(app.AZ_FW_PATHS)[-1], "fw_carry")
        items, _l, _c, _k, path = app.price_azure_firewall_row(_dep_row(), "fw_carry")
        self.assertEqual(path, "fw_carry")
        self.assertTrue(items[0]["carriedOver"])
        self.assertEqual(items[0]["monthly"], 930.0)

    def test_data_processing_inside_included_allowance_is_zero(self):
        pool = [app.OCI_NFW_DATA_FREE_GB]
        items, _l, _c, kind, path = app.price_azure_firewall_row(_data_row(), data_pool=pool)
        self.assertEqual((kind, path), ("azfw_data", "auto"))  # automatic - no dropdown
        self.assertEqual(items[0]["monthly"], 0.0)
        self.assertAlmostEqual(pool[0], app.OCI_NFW_DATA_FREE_GB - 1753.6656, places=3)

    def test_data_processing_beyond_ten_tb_bills_a_cent_per_gb(self):
        pool = [app.OCI_NFW_DATA_FREE_GB]
        items, _l, _c, _k, _p = app.price_azure_firewall_row(
            _data_row(gb=12240.0, cost=195.84), data_pool=pool)
        self.assertEqual(items[0]["monthly"], 20.0)            # 2,000 GB over x $0.01

    def test_data_auto_carries_when_all_deployments_carried(self):
        items, label, _c, _k, path = app.price_azure_firewall_row(
            _data_row(), deployments_carried=True)
        self.assertEqual(path, "auto")
        self.assertTrue(items[0]["carriedOver"])
        self.assertEqual(items[0]["monthly"], 28.06)
        self.assertIn("with firewall rows", label.lower())

    def test_validation_note_covers_the_docs_caveats(self):
        items, _l, _c, _k, _p = app.price_azure_firewall_row(_dep_row())
        note = items[0]["mapping"].lower()
        for needle in ("ha pair", "peak", "dnat", "vwan", "dns", "tls", "siem"):
            self.assertIn(needle, note, f"validation note must mention {needle}")
        self.assertIn("not a secure virtual hub", note)

    def test_never_fires_on_other_services(self):
        row = {"source_provider": "Microsoft", "source_service": "Azure Front Door Service",
               "__meterName": "Standard Data Processed", "usage_quantity": 10,
               "usage_unit": "1 GB", "source_monthly_cost": 1.0}
        self.assertIsNone(app.price_azure_firewall_row(row))


if __name__ == "__main__":
    unittest.main(verbosity=2)
