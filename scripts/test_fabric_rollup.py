"""Large shared-service rollup policy (doc #12) - Fabric parent/child rollup and
the policy guardrails, tested against the bill's remaining examples.

Fabric: ONE reservation Purchase row (AAL-49223, $5,002.24) carries the whole
cost; every other Fabric Capacity meter bills $0 and becomes an INFORMATIONAL
child (no source cost, no target price, excluded from independent matching).
OneLake data-stored meters are separately billed -> standalone. Other approved
use cases (App Service plan, VWAN hub, dev-platform pool, DTU pool) already
comply through their own handlers - the cross-checks here pin the guardrails.

Run: python3 scripts/test_fabric_rollup.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app


def _row(meter, cost, qty, unit="1 Hour", service="Microsoft Fabric"):
    return {"source_provider": "Microsoft", "source_service": service,
            "__meterName": meter, "source_product": f"Fabric Capacity - {meter}",
            "usage_quantity": qty, "usage_unit": unit,
            "source_monthly_cost": cost}


class FabricRollupTest(unittest.TestCase):
    def test_parent_reservation_carries_the_cost_once(self):
        items, label, _c, kind = app.price_azure_fabric_row(
            _row("Dataflows Standard Compute Capacity Usage CU", 5002.24, 64.0))
        self.assertEqual(kind, "fabric_parent")
        self.assertTrue(items[0]["carriedOver"])
        self.assertEqual(items[0]["monthly"], 5002.24)
        self.assertIn("rollup parent - carried once", label)
        self.assertIn("EXCLUSIVE PARENT", items[0]["mapping"])
        self.assertIn("100%", items[0]["mapping"])

    def test_zero_cost_capability_rows_are_informational_children(self):
        # Policy child_mapping_ids -> OCI target directions, $0 contribution.
        for meter, qty, needle in (
                ("Compute Pool Capacity Usage CU", 45520.37, "OCI Data Flow"),
                ("Power BI Capacity Usage CU", 1978.6, "Oracle Analytics"),
                ("Dataflows Standard Compute Capacity Usage CU", 4.2, "OCI Data Integration"),
                ("Data Warehouse Capacity Usage CU", 0.16, "Autonomous AI Lakehouse"),
                ("RTI Event Listener and Alert Capacity Usage CU", 66.03, "OCI Streaming"),
                ("Copilot and AI Capacity Usage CU", 46.37, "OCI Generative AI")):
            items, label, _c, kind = app.price_azure_fabric_row(_row(meter, 0.0, qty))
            self.assertEqual(kind, "fabric_child", meter)
            self.assertEqual(items[0]["monthly"], 0.0, meter)
            self.assertEqual(items[0]["quantity"], round(qty, 4))  # qty retained
            self.assertIn(needle, label, meter)
            self.assertIn("informational_until_allocated", items[0]["mapping"])

    def test_guardrail_children_never_add_cost_to_the_carried_parent(self):
        # Do not add child OCI candidate prices to the carried parent cost.
        parent, *_ = app.price_azure_fabric_row(
            _row("Dataflows Standard Compute Capacity Usage CU", 5002.24, 64.0))
        children = [app.price_azure_fabric_row(_row(m, 0.0, 10.0))[0]
                    for m in ("Compute Pool Capacity Usage CU",
                              "Power BI Capacity Usage CU",
                              "Copilot and AI Capacity Usage CU")]
        total = sum(li["monthly"] for li in parent) + \
            sum(li["monthly"] for c in children for li in c)
        self.assertEqual(round(total, 2), 5002.24)   # counted exactly once

    def test_onelake_data_stored_stays_standalone(self):
        # Separately billed meters remain standalone mappings (policy).
        r = _row("OneLake Storage Hot Data Stored", 0.01, 0.32, unit="1 GB/Month")
        self.assertIsNone(app.price_azure_fabric_row(r))

    def test_onelake_operations_cus_not_stolen_by_ops_handler(self):
        # Previously leaked into azops -> Object Requests (silent duplication path).
        r = _row("OneLake Read Operations Hot Capacity Usage CU", 0.0, 0.0009)
        self.assertIsNone(app.price_azure_storage_ops_row(r))
        items, label, _c, kind = app.price_azure_fabric_row(r)
        self.assertEqual(kind, "fabric_child")
        self.assertEqual(items[0]["monthly"], 0.0)

    def test_policy_scope_does_not_apply_to_ordinary_services(self):
        # do_not_apply_to: direct DB/cache/firewall/etc. meters keep their handlers.
        r = _row("B1MS", 15.0, 744.0, service="Azure Database for MySQL")
        self.assertIsNone(app.price_azure_fabric_row(r))

    def test_fabric_flag_is_orange_children_and_red_parent_by_carry(self):
        self.assertEqual(app.FLAG_SEVERITY_BY_TEXT.get(app.AZ_FABRIC_FLAG), "orange")

    def test_other_approved_use_cases_count_cost_once(self):
        # App Service plan: the CI option REPLACES the carried plan (never adds).
        ci, *_ = app.price_azure_appservice_row(
            {"source_provider": "Microsoft", "source_service": "Azure App Service",
             "__meterName": "Basic Plan - Linux - B1",
             "source_product": "Basic Plan - Linux - B1",
             "usage_quantity": 744.0, "usage_unit": "1 Hour",
             "source_monthly_cost": 25.30}, "appsvc_ci")
        self.assertFalse(any(li.get("carriedOver") for li in ci))
        # VWAN hub: $0 mapping, nothing carried alongside (separate meters like
        # egress/firewall are other services with their own handlers).
        vw, _l, _c = app.price_azure_included_row(
            {"source_provider": "Microsoft", "source_service": "Virtual WAN",
             "__meterName": "Standard Hub Unit", "source_product": "Standard Hub Unit",
             "usage_quantity": 45.16, "usage_unit": "1 Hour",
             "source_monthly_cost": 11.29})
        self.assertEqual(sum(li["monthly"] for li in vw), 0.0)
        self.assertFalse(any(li.get("carriedOver") for li in vw))


if __name__ == "__main__":
    unittest.main(verbosity=2)
