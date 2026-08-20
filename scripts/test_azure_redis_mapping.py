"""Azure Redis -> OCI Cache with Redis 7.0 (methodology doc #7, 2026-08-19).

One logical cache -> one OCI cluster. Default = 3-node production HA (Oracle: one/two-
node clusters are not reliable), even for Azure Basic sources. Single-node is a
non-default exception; carry is last. memory/node = max(tier GB, 2 GB OCI minimum);
GB-hours = hours x nodes x GB/node x $0.0194 (B98217), memory beyond 10 GB/node at
$0.0136 (B99591). Doc-pinned totals for the Quad bill: HA $6,472.00 / single $2,157.32
/ carry $1,776.45. Tier tokens are word-boundary matched (b1 vs b10/b100 - the S1/S12
substring bug class); unknown tiers safe-fail to carry.

Run: python3 scripts/test_azure_redis_mapping.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app


def _row(meter, hours, cost=100.0):
    return {"source_provider": "Microsoft", "source_service": "Redis Cache",
            "__meterName": meter, "source_product": meter,
            "usage_quantity": hours, "usage_unit": "1 Hour",
            "source_monthly_cost": cost}


class AzureRedisMappingTest(unittest.TestCase):
    def test_c0_ha_default_three_nodes_two_gb_minimum(self):
        items, label, _c, path = app.price_azure_redis_row(_row("C0 Cache Instance", 744.0))
        self.assertEqual(path, "redis_ha")
        # 744 h x 3 nodes x max(0.25, 2) GB = 4,464 GB-hr x $0.0194 = $86.60
        self.assertEqual(items[0]["quantity"], 4464.0)
        self.assertAlmostEqual(items[0]["monthly"], 86.60, places=2)
        self.assertIn("3-node", label)

    def test_single_node_option_is_one_third(self):
        items, label, _c, path = app.price_azure_redis_row(
            _row("C0 Cache Instance", 744.0), "redis_single")
        self.assertEqual(items[0]["quantity"], 1488.0)
        self.assertAlmostEqual(items[0]["monthly"], 28.87, places=2)
        self.assertIn("single node", label.lower())
        self.assertIn("exception", items[0]["mapping"].lower())

    def test_carry_is_last_and_prices_at_source(self):
        self.assertEqual(list(app.AZ_REDIS_PATHS)[-1], "redis_carry")
        items, _l, _c, path = app.price_azure_redis_row(
            _row("C1 Cache Instance", 744.0, cost=51.34), "redis_carry")
        self.assertTrue(items[0]["carriedOver"])
        self.assertEqual(items[0]["monthly"], 51.34)

    def test_tier_word_boundaries_b1_vs_b10_vs_b100(self):
        r1 = _row("B1 Cache Instance", 10.0)
        r10 = _row("B10 Cache Instance", 10.0)
        r100 = _row("B100 Cache Instance", 10.0)
        self.assertEqual(app._azure_redis_tier_gb(r1), 1.0)
        self.assertEqual(app._azure_redis_tier_gb(r10), 12.0)
        self.assertEqual(app._azure_redis_tier_gb(r100), 104.0)

    def test_high_memory_tier_splits_at_ten_gb_per_node(self):
        # B100 = 104 GB/node: 10 GB at $0.0194 + 94 GB at $0.0136, x3 nodes.
        items, _l, _c, _p = app.price_azure_redis_row(_row("B100 Cache Instance", 100.0))
        low = next(li for li in items if li["sku"] == app.OCI_CACHE_LOW_SKU)
        high = next(li for li in items if li["sku"] == app.OCI_CACHE_HIGH_SKU)
        self.assertEqual(low["quantity"], 100.0 * 3 * 10)
        self.assertEqual(high["quantity"], 100.0 * 3 * 94)
        self.assertAlmostEqual(low["monthly"], 58.20, places=2)
        self.assertAlmostEqual(high["monthly"], 383.52, places=2)

    def test_unknown_tier_safe_fails_to_carry(self):
        items, _l, _c, path = app.price_azure_redis_row(_row("M20 Cache Instance", 744.0))
        self.assertEqual(path, "redis_carry")
        self.assertTrue(items[0]["carriedOver"])
        self.assertIn("unrecognized", items[0]["mapping"].lower())

    def test_doc_pinned_line_basic_c0(self):
        # Doc: Basic C0 18,796.5 hrs -> 112,779.0 GB-hrs -> $2,187.91 (HA).
        items, _l, _c, _p = app.price_azure_redis_row(
            _row("C0 Cache", 18796.5, cost=413.52))
        self.assertAlmostEqual(items[0]["quantity"], 112779.0, places=1)
        self.assertAlmostEqual(items[0]["monthly"], 2187.91, places=2)

    def test_validation_note_present(self):
        items, _l, _c, _p = app.price_azure_redis_row(_row("C0 Cache", 100.0))
        note = items[0]["mapping"].lower()
        for needle in ("resource id", "telemetry", "tls", "config", "consolidation"):
            self.assertIn(needle, note)

    def test_never_fires_on_non_redis_or_non_hour_rows(self):
        r = _row("C0 Cache", 100.0); r["source_service"] = "SQL Database"
        self.assertIsNone(app.price_azure_redis_row(r))
        r2 = _row("C0 Cache", 100.0); r2["usage_unit"] = "1 GB"
        self.assertIsNone(app.price_azure_redis_row(r2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
