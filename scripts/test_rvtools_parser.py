#!/usr/bin/env python3

import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app


class RVToolsParserTests(unittest.TestCase):
    def write_native_export(self, path):
        info = pd.DataFrame([
            {
                "VM": "orders-prod",
                "Powerstate": "poweredOn",
                "Template": False,
                "CPUs": 4,
                "Memory": 16384,
                "VM UUID": "uuid-prod",
                "Path": "[Prod] orders-prod/orders-prod.vmx",
                "OS according to the VMware Tools": "Microsoft Windows Server 2022 (64-bit)",
            },
            {
                "VM": "orders-dr",
                "Powerstate": "poweredOff",
                "Template": False,
                "CPUs": 2,
                "Memory": 8192,
                "VM UUID": "uuid-dr",
                "Path": "[DR] orders-dr/orders-dr.vmx",
                "OS according to the VMware Tools": "Red Hat Enterprise Linux 9",
            },
            {
                "VM": "template-win",
                "Powerstate": "poweredOff",
                "Template": True,
                "CPUs": 8,
                "Memory": 32768,
                "VM UUID": "uuid-template",
                "Path": "[Templates] template-win/template-win.vmx",
                "OS according to the VMware Tools": "Microsoft Windows Server 2022 (64-bit)",
            },
        ])
        disks = pd.DataFrame([
            {"VM": "orders-prod", "VM UUID": "uuid-prod", "Capacity MiB": 102400},
            {"VM": "orders-prod", "VM UUID": "uuid-prod", "Capacity MiB": 51200},
            {"VM": "orders-dr", "VM UUID": "uuid-dr", "Capacity MiB": 40960},
            {"VM": "template-win", "VM UUID": "uuid-template", "Capacity MiB": 20480},
        ])
        with pd.ExcelWriter(path) as writer:
            info.to_excel(writer, sheet_name="vInfo", index=False)
            disks.to_excel(writer, sheet_name="vDisk", index=False)

    def test_native_export_joins_disks_and_normalizes_binary_units(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workbook = Path(temp_dir) / "RVTools_export_all.xlsx"
            self.write_native_export(workbook)
            parsed = app.parse_workbook(workbook)

        self.assertEqual(parsed["metadata"]["parser"], "rvtools")
        self.assertFalse(parsed["metadata"]["aiAssisted"])
        self.assertEqual(parsed["metadata"]["rvtoolsTemplateRowsExcluded"], 1)
        self.assertEqual(parsed["metadata"]["rvtoolsPoweredOffRowsRetained"], 1)
        self.assertEqual(len(parsed["rows"]), 2)

        by_machine = {row["machine_name"]: row for row in parsed["rows"]}
        prod = by_machine["orders-prod"]
        self.assertEqual(prod["environment"], "Prod")
        self.assertEqual(prod["application_details_number_of_cpu_cores_per_server"], 2)
        self.assertEqual(prod["application_details_memory_per_server_gb"], 16)
        self.assertEqual(prod["application_details_local_storage_gb"], 150)
        self.assertEqual(by_machine["orders-dr"]["environment"], "DR")

        data_check = app.inventory_data_check(parsed["fields"], parsed["rows"])
        present = {item["key"] for item in data_check["signals"] if item["present"]}
        self.assertTrue({"cpu", "memory", "storage", "server", "environment"} <= present)

    def test_compact_extract_ignores_precomputed_vcpu_column(self):
        frame = pd.DataFrame([
            {
                "VM": "billing-prod",
                "Powerstate": "poweredOn",
                "CPUs": 8,
                "VCPU": 4,
                "Memory": 65536,
                "OS": "Microsoft Windows Server 2022 (64-bit)",
                "Storage": 500,
            }
        ])
        with tempfile.TemporaryDirectory() as temp_dir:
            workbook = Path(temp_dir) / "RW RVTools.xlsx"
            frame.to_excel(workbook, index=False)
            parsed = app.parse_workbook_rule_based(workbook)

        row = parsed["rows"][0]
        self.assertEqual(row["machine_name"], "billing-prod")
        self.assertEqual(row["environment"], "Prod")
        self.assertEqual(row["application_details_number_of_cpu_cores_per_server"], 4)
        self.assertEqual(row["application_details_memory_per_server_gb"], 64)
        self.assertEqual(row["application_details_local_storage_gb"], 500)


if __name__ == "__main__":
    unittest.main()
