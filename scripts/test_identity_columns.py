#!/usr/bin/env python3

import tempfile
import unittest
from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app


class IdentityColumnTests(unittest.TestCase):
    def parse_rows(self, frame):
        with tempfile.TemporaryDirectory() as temp_dir:
            workbook = Path(temp_dir) / "identity.xlsx"
            frame.to_excel(workbook, index=False)
            plan = {
                "sheetName": "Sheet1",
                "headerRows": [1],
                "dataStartRow": 2,
                "dataEndRow": None,
                "serverGrain": "server",
                "confidence": 1,
                "columnMappings": {},
                "notes": [],
            }
            return app.parse_workbook_from_plan(workbook, plan)

    def test_application_and_machine_names_map_separately(self):
        payload = self.parse_rows(
            pd.DataFrame(
                [
                    {
                        "Application Name": "Order Management",
                        "Machine Name": "ord-prod-01",
                        "Environment": "Prod",
                        "CPU": 8,
                        "RAM (GB)": 32,
                    }
                ]
            )
        )

        self.assertEqual(payload["rows"][0]["application_name"], "Order Management")
        self.assertEqual(payload["rows"][0]["machine_name"], "ord-prod-01")

    def test_machine_only_inventory_keeps_the_row(self):
        payload = self.parse_rows(
            pd.DataFrame(
                [
                    {
                        "Server Name": "db-prod-01",
                        "Environment": "Prod",
                        "CPU": 4,
                        "RAM (GB)": 16,
                    }
                ]
            )
        )

        self.assertEqual(payload["rows"][0]["application_name"], "")
        self.assertEqual(payload["rows"][0]["machine_name"], "db-prod-01")

    def test_machine_id_maps_to_machine_name(self):
        payload = self.parse_rows(
            pd.DataFrame(
                [
                    {
                        "Machine ID": "app-prod-01",
                        "Application": "Order Management",
                        "Environment": "Prod",
                        "CPU": 4,
                        "RAM (GB)": 16,
                    }
                ]
            )
        )

        self.assertEqual(payload["rows"][0]["application_name"], "Order Management")
        self.assertEqual(payload["rows"][0]["machine_name"], "app-prod-01")
        data_check = app.inventory_data_check(payload["fields"], payload["rows"])
        server_signal = next(
            signal
            for signal in data_check["signals"]
            if signal["key"] == "server"
        )
        self.assertTrue(server_signal["present"])
        self.assertEqual(server_signal["column"], "Machine ID")


class CostCenterEnvironmentFallbackTest(unittest.TestCase):
    """Chris's ruling 2026-08-19: the environment field can use the cost-center
    column - as the LAST fallback, so a real environment column always wins."""

    def _fields(self, labels):
        return [{"key": l.lower().replace(" ", "_"), "label": l} for l in labels]

    def test_cost_center_resolves_when_no_environment_column(self):
        fields = self._fields(["Server Name", "Cost Center", "CPU"])
        rows = [{"server_name": "web01", "cost_center": "PROD-Finance", "cpu": 4}]
        env = [s for s in app.inventory_data_check(fields, rows)["signals"]
               if s["key"] == "environment"][0]
        self.assertEqual(env["column"], "Cost Center")

    def test_resource_group_is_the_third_fallback(self):
        # Precedence: environment > cost center > resource group (rulings 2026-08-19).
        rows = [{"server_name": "web01", "resource_group": "rg-prod-web",
                 "cost_center": "PROD-Fin", "cpu": 4}]
        rg_only = self._fields(["Server Name", "Resource Group", "CPU"])
        env = [s for s in app.inventory_data_check(rg_only, rows)["signals"]
               if s["key"] == "environment"][0]
        self.assertEqual(env["column"], "Resource Group")
        both = self._fields(["Server Name", "Resource Group", "Cost Center", "CPU"])
        env2 = [s for s in app.inventory_data_check(both, rows)["signals"]
                if s["key"] == "environment"][0]
        self.assertEqual(env2["column"], "Cost Center")

    def test_real_environment_column_beats_cost_center(self):
        fields = self._fields(["Server Name", "Environment", "Cost Center", "CPU"])
        rows = [{"server_name": "web01", "environment": "prod",
                 "cost_center": "PROD-Finance", "cpu": 4}]
        env = [s for s in app.inventory_data_check(fields, rows)["signals"]
               if s["key"] == "environment"][0]
        self.assertEqual(env["column"], "Environment")


if __name__ == "__main__":
    unittest.main()
