"""Azure Virtual WAN / VPN Gateway -> OCI Site-to-Site VPN + DRG at $0 (doc #10,
2026-08-19, formalizing the wave-13 approval).

All four bill meters (VpnGw1AZ gateway-hours, S2S scale units, S2S connection units,
Standard Hub Units - $1,028.81/mo total, quantities re-verified against the July
export) map to $0: Oracle-verified "no per-hour connection fee or per-byte data
processing fee" for Site-to-Site VPN and no DRG meter. AUTOMATIC (no dropdown - the
free mapping is OCI's charge model, not a choice), flagged, with doc #10's
architecture-validation stipulations in the hover note.

Run: python3 scripts/test_azure_vwan_vpn_mapping.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app


def _row(service, meter, hours, cost):
    return {"source_provider": "Microsoft", "source_service": service,
            "__meterName": meter, "source_product": meter,
            "usage_quantity": hours, "usage_unit": "1 Hour",
            "source_monthly_cost": cost}


class AzureVwanVpnMappingTest(unittest.TestCase):
    def test_all_four_doc_meters_map_to_zero(self):
        for svc, meter, hrs, cost in (
                ("VPN Gateway", "VpnGw1AZ", 1488.0, 312.48),
                ("Virtual WAN", "VPN S2S Scale Unit", 1529.66, 552.21),
                ("Virtual WAN", "VPN S2S Connection Unit", 3056.6, 152.83),
                ("Virtual WAN", "Standard Hub Unit", 45.16, 11.29)):
            items, label, cat = app.price_azure_included_row(_row(svc, meter, hrs, cost))
            self.assertEqual(items[0]["monthly"], 0.0, meter)
            self.assertEqual(items[0]["quantity"], round(hrs, 4))  # qty retained
            self.assertEqual(cat, "Networking")
            self.assertIn(f"${cost:,.2f}/mo goes to $0", items[0]["mapping"])

    def test_hub_units_name_the_drg_transit_outcome(self):
        _i, label, _c = app.price_azure_included_row(
            _row("Virtual WAN", "Standard Hub Unit", 45.16, 11.29))
        self.assertIn("DRG route tables", label)
        _i2, label2, _c2 = app.price_azure_included_row(
            _row("Virtual WAN", "VPN S2S Scale Unit", 1529.66, 552.21))
        self.assertIn("VPN Connect", label2)

    def test_architecture_validation_note_covers_doc_stipulations(self):
        items, *_ = app.price_azure_included_row(
            _row("VPN Gateway", "VpnGw1AZ", 1488.0, 312.48))
        note = items[0]["mapping"]
        self.assertIn("ARCHITECTURE VALIDATION", note)
        for needle in ("DRG attached", "IPSec", "CPE", "BGP",
                       "encryption overhead", "connection equivalents",
                       "not free-everything"):
            self.assertIn(needle, note, f"note must mention {needle}")

    def test_zero_dollar_boundaries_are_named(self):
        items, *_ = app.price_azure_included_row(
            _row("Virtual WAN", "VPN S2S Connection Unit", 3056.6, 152.83))
        note = items[0]["mapping"]
        for excluded in ("FastConnect", "Network Firewall", "point-to-site",
                         "inter-region", "egress"):
            self.assertIn(excluded, note, f"$0 boundary must name {excluded}")

    def test_inter_region_converts_onto_zone_pool_not_carried(self):
        # Ruling 2026-08-19 (overrides doc #6's carry guardrail): inter-region GB
        # meters against the origin zone's 10 TB free pool, overage at zone rate.
        r = _row("Bandwidth", "Inter-Region - Intra Continent Data Transfer Out",
                 500.0, 8.88)
        r["usage_unit"] = "1 GB"; r["source_region"] = "Iowa"
        pools = {"na_eu_uk": [10240.0]}
        items, label, cat = app.price_azure_included_row(r, transfer_pools=pools)
        self.assertFalse(any(li.get("carriedOver") for li in items))
        self.assertEqual(items[0]["monthly"], 0.0)          # inside the free pool
        self.assertEqual(pools["na_eu_uk"][0], 9740.0)      # pool drawn down
        self.assertIn("Outbound Data Transfer", label)
        self.assertIn("TOPOLOGY ASSUMPTION", items[0]["mapping"])
        # Pool exhausted -> overage bills at the zone rate.
        r2 = _row("Bandwidth", "Inter-Region - Intra Continent Data Transfer Out",
                  1000.0, 17.76)
        r2["usage_unit"] = "1 GB"; r2["source_region"] = "Iowa"
        items2, *_ = app.price_azure_included_row(r2, transfer_pools={"na_eu_uk": [0.0]})
        self.assertAlmostEqual(items2[0]["monthly"], 8.50, places=2)  # 1000 x $0.0085

    def test_vnet_global_peering_converts_not_free(self):
        r = _row("Virtual Network", "Global Peering - Data Transfer Out", 100.0, 1.04)
        r["usage_unit"] = "1 GB"; r["source_region"] = "Iowa"
        items, label, _c = app.price_azure_included_row(
            r, transfer_pools={"na_eu_uk": [0.0]})
        self.assertAlmostEqual(items[0]["monthly"], 0.85, places=2)
        self.assertIn("inter-region", label)

    def test_ordinary_bandwidth_egress_not_grabbed_here(self):
        # Plain internet egress stays with the generic zone pricer downstream.
        r = _row("Bandwidth", "Standard Data Transfer Out", 1000.0, 87.0)
        self.assertIsNone(app.price_azure_included_row(r, transfer_pools={}))

    def test_other_services_untouched(self):
        self.assertIsNone(app.price_azure_included_row(
            _row("Azure Firewall", "Deployment Hours", 744.0, 100.0)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
