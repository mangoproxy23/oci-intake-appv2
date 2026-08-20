"""Azure MySQL Flexible Server burstable -> OCI MySQL HeatWave DB System (doc #9,
2026-08-19).

Rule: raw ECPUs = Azure vCores / 2 PER SOURCE SERVER (never pooled), rounded up to
the next supported shape with a hard 2-ECPU floor -> B1MS (1 vCore) => MySQL.2
(2 ECPU). Compute at B108030 $0.0366/ECPU-hr (cetools-verified USD PAYG) plus the
50-GB minimum provisioned storage per DB system at B92426 $0.04/GB-month; stored-data
rows bill only GB beyond the bill-wide minimum pool; paid-I/O rows are $0 by meter
mismatch (flagged for performance validation). Standalone default / HA (x3) / carry
LAST. Doc-pinned dollars for the Quad bill: 3,720 B1MS hours = 5 server-months ->
$272.30 compute + $10.00 minimum storage; 100 GB stored -> $0 (inside the 250-GB
minimum); paid I/O -> $0. HA alternative: $816.91 compute.

Run: python3 scripts/test_azure_mysql_mapping.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app


def _compute_row(hours, cost=15.0, meter="B1MS", rid="c1",
                 product="Flexible Server Burstable BS Series Compute"):
    return {"__id": rid, "source_provider": "Microsoft",
            "source_service": "Azure Database for MySQL",
            "__meterName": meter, "__meterSub": product, "source_product": product,
            "usage_quantity": hours, "usage_unit": "1 Hour",
            "source_monthly_cost": cost}


def _storage_row(gb, cost=5.0, meter="Storage Data Stored", rid="s1",
                 product="Flexible Server Storage"):
    return {"__id": rid, "source_provider": "Microsoft",
            "source_service": "Azure Database for MySQL",
            "__meterName": meter, "__meterSub": product, "source_product": product,
            "usage_quantity": gb, "usage_unit": "1 GB/Month",
            "source_monthly_cost": cost}


def _io_row(millions, cost=77.20, rid="i1"):
    return {"__id": rid, "source_provider": "Microsoft",
            "source_service": "Azure Database for MySQL",
            "__meterName": "Paid IO LRS IO Rate Operations",
            "__meterSub": "Flexible Server Storage",
            "source_product": "Flexible Server Storage",
            "usage_quantity": millions, "usage_unit": "1M",
            "source_monthly_cost": cost}


class AzureMysqlMappingTest(unittest.TestCase):
    def test_default_standalone_b1ms_floors_to_mysql2(self):
        # 3,720 B1MS hours (the whole Quad bill in one row) = 5 server-months.
        items, label, _c, kind, path = app.price_azure_mysql_row(
            _compute_row(3720.0), min_storage_pool=[250.0])
        self.assertEqual((kind, path), ("mysql_compute", "mysql_std"))
        self.assertEqual(items[0]["sku"], "B108030")
        self.assertEqual(items[0]["quantity"], 7440.0)          # x2 ECPU floor
        self.assertAlmostEqual(items[0]["monthly"], 272.30, places=2)
        # Minimum storage rides on the compute row: 5 server-months x 50 GB x $0.04.
        self.assertEqual(items[1]["sku"], "B92426")
        self.assertEqual(items[1]["quantity"], 250.0)
        self.assertAlmostEqual(items[1]["monthly"], 10.00, places=2)
        self.assertIn("standalone", label.lower())

    def test_ha_is_three_instances_compute_only(self):
        items, _l, _c, _k, path = app.price_azure_mysql_row(
            _compute_row(3720.0), "mysql_ha", min_storage_pool=[250.0])
        self.assertEqual(path, "mysql_ha")
        self.assertEqual(items[0]["quantity"], 22320.0)         # x3 instances
        self.assertAlmostEqual(items[0]["monthly"], 816.91, places=2)
        self.assertIn("NOT source parity", items[0]["mapping"])
        # Minimum storage stays at the standalone amount; replication not modeled.
        self.assertAlmostEqual(items[1]["monthly"], 10.00, places=2)
        self.assertIn("NOT modeled", items[1]["mapping"])

    def test_carry_is_last_and_prices_at_source(self):
        self.assertEqual(list(app.AZ_MYSQL_PATHS)[-1], "mysql_carry")
        items, _l, _c, _k, path = app.price_azure_mysql_row(
            _compute_row(3720.0, cost=75.40), "mysql_carry")
        self.assertTrue(items[0]["carriedOver"])
        self.assertEqual(items[0]["monthly"], 75.40)

    def test_sizing_convention_disclaimed_and_never_pooled(self):
        items, *_ = app.price_azure_mysql_row(_compute_row(744.0),
                                              min_storage_pool=[50.0])
        note = items[0]["mapping"]
        self.assertIn("BOM SIZING CONVENTION", note)
        self.assertIn("NEVER pooled", note)
        self.assertIn("2-ECPU floor", note)
        self.assertIn("no HeatWave analytics cluster", note)

    def test_shape_rounding_uses_supported_shapes(self):
        # B4MS (4 vCores) -> raw 2 -> MySQL.2; B8MS (8 vCores) -> raw 4 -> MySQL.4.
        four, *_ = app.price_azure_mysql_row(_compute_row(100.0, meter="B4MS"),
                                             min_storage_pool=[999.0])
        self.assertEqual(four[0]["quantity"], 200.0)            # 2 ECPU
        eight, *_ = app.price_azure_mysql_row(_compute_row(100.0, meter="B8MS"),
                                              min_storage_pool=[999.0])
        self.assertEqual(eight[0]["quantity"], 400.0)           # 4 ECPU

    def test_stored_data_inside_minimum_is_zero_dollars(self):
        pool = [250.0]
        items, _l, _c, kind, path = app.price_azure_mysql_row(
            _storage_row(100.0), min_storage_pool=pool)
        self.assertEqual((kind, path), ("mysql_storage", "auto"))  # no dropdown
        self.assertEqual(items[0]["sku"], "B92426")
        self.assertEqual(items[0]["quantity"], 100.0)           # quantity retained
        self.assertEqual(items[0]["monthly"], 0.0)
        self.assertEqual(pool[0], 150.0)                        # pool drawn down
        self.assertIn("PROVISIONED", items[0]["mapping"])

    def test_stored_data_beyond_minimum_bills_overage(self):
        items, *_ = app.price_azure_mysql_row(_storage_row(400.0),
                                              min_storage_pool=[250.0])
        self.assertAlmostEqual(items[0]["monthly"], 6.00, places=2)  # 150 GB x $0.04

    def test_paid_io_is_zero_with_performance_validation(self):
        items, _l, _c, kind, path = app.price_azure_mysql_row(_io_row(305.38))
        self.assertEqual((kind, path), ("mysql_io", "auto"))    # no dropdown
        self.assertEqual(items[0]["monthly"], 0.0)
        self.assertEqual(items[0]["quantity"], 305.38)          # quantity retained
        self.assertIn("METER MISMATCH", items[0]["mapping"])
        self.assertIn("640 max", items[0]["mapping"])
        # And the ops handler must NOT grab MySQL paid-I/O rows first.
        self.assertIsNone(app.price_azure_storage_ops_row(_io_row(305.38)))

    def test_all_computes_carried_carries_storage_and_io(self):
        s, _l, _c, _k, _p = app.price_azure_mysql_row(
            _storage_row(100.0, cost=13.02), computes_carried=True)
        self.assertTrue(s[0]["carriedOver"]); self.assertEqual(s[0]["monthly"], 13.02)
        i, *_ = app.price_azure_mysql_row(_io_row(305.38), computes_carried=True)
        self.assertTrue(i[0]["carriedOver"]); self.assertEqual(i[0]["monthly"], 77.20)

    def test_min_storage_pool_skips_carried_compute_rows(self):
        rows = [_compute_row(744.0, rid="a"), _compute_row(1488.0, rid="b"),
                _storage_row(60.0, rid="s")]
        self.assertEqual(app.collect_azure_mysql_min_storage_gb(rows), 150.0)
        self.assertEqual(app.collect_azure_mysql_min_storage_gb(
            rows, {"a": "mysql_carry"}), 100.0)

    def test_unknown_meters_and_other_services_safe_fail(self):
        # Non-burstable MySQL compute meter (no recognized size token) -> untouched.
        r = _compute_row(100.0, meter="vCore",
                         product="Flexible Server General Purpose Ddsv4 Series Compute")
        self.assertIsNone(app.price_azure_mysql_row(r))
        # Backup storage -> untouched (keeps existing Object Storage mapping).
        b = _storage_row(50.0, meter="Backup Storage LRS Data Stored",
                         product="Flexible Server Backup Storage")
        self.assertIsNone(app.price_azure_mysql_row(b))
        # PostgreSQL never matches (word "mysql" absent from the service).
        p = _compute_row(100.0); p["source_service"] = "Azure Database for PostgreSQL"
        self.assertIsNone(app.price_azure_mysql_row(p))


if __name__ == "__main__":
    unittest.main(verbosity=2)
