#!/usr/bin/env python3
"""Guard tests for the two sizing invariants a row can never violate.

1. FLEX CAP - a flex shape has a hard per-VM maximum (VM.Standard.E6.Ax.Flex tops out at
   94 OCPU / 712 GB), and a row is never billed past it. The spec'd size is kept in
   specs.specOcpus / specs.specMemoryGb so the table shows "(was N)", and the size flag
   still judges the SPEC'D size, so the row stays flagged until a fitting shape is chosen.

2. BARE-METAL BOX MODEL - a single-VM row on a bare-metal shape bills exactly ONE box of
   that shape, at the box's fixed size. A VM bigger than the box is flagged `impossible`,
   never quietly sized up to more boxes: a physical machine is not a cluster. This is the
   regression that already happened once (a 288-OCPU workload reading 576 OCPU); this test
   pins it.
"""

import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app

AX_MAX_OCPU, AX_MAX_MEM = 94, 712       # VM.Standard.E6.Ax.Flex
BM_192_OCPU, BM_192_MEM = 192, 1536     # BM.Standard.E6.Ax.192
BM_256_OCPU, BM_256_MEM = 256, 3072     # BM.Standard.E6.256


def build_rows():
    # "CPU Cores" is a physical-core column, read as OCPUs 1:1 (see detect_cpu_unit).
    return pd.DataFrame(
        [
            {"Machine Name": "cap-both", "Application": "App", "Environment": "Prod",
             "CPU Cores": 288, "MemoryGB(RAM)": 4031, "Total Storage (GB)": 500},
            {"Machine Name": "cap-memory-only", "Application": "App", "Environment": "Prod",
             "CPU Cores": 16, "MemoryGB(RAM)": 1024, "Total Storage (GB)": 500},
            {"Machine Name": "fits-fine", "Application": "App", "Environment": "Prod",
             "CPU Cores": 8, "MemoryGB(RAM)": 64, "Total Storage (GB)": 500},
            # Fits no AMD flex shape (150 OCPU > 126) but fits BM.Standard.E6.Ax.192.
            {"Machine Name": "needs-bm-small", "Application": "App", "Environment": "Prod",
             "CPU Cores": 150, "MemoryGB(RAM)": 800, "Total Storage (GB)": 500},
            # Fits no AMD flex shape and not the Ax.192 box either (200 > 192) - only E6.256.
            {"Machine Name": "needs-bm-big", "Application": "App", "Environment": "Prod",
             "CPU Cores": 200, "MemoryGB(RAM)": 500, "Total Storage (GB)": 500},
        ]
    )


class SizeCapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as temp_dir:
            workbook = Path(temp_dir) / "size-caps.xlsx"
            build_rows().to_excel(workbook, index=False)
            parsed = app.parse_workbook_rule_based(workbook)
        cls.fields, cls.rows = parsed["fields"], parsed["rows"]
        cls.by_name = {}
        pricing = app.calculate_pricing(cls.fields, cls.rows, shape_key="e6-standard-ax")
        for row in pricing["rows"]:
            cls.by_name[row["name"]] = row
        cls.by_name_auto = {}
        auto_pricing = app.calculate_pricing(cls.fields, cls.rows, shape_key="e6-standard-ax", auto=True)
        for row in auto_pricing["rows"]:
            cls.by_name_auto[row["name"]] = row

    def row(self, name):
        self.assertIn(name, self.by_name)
        return self.by_name[name]

    # ---- 1. Flex cap -------------------------------------------------------------

    def test_oversized_vm_is_billed_at_the_shape_maximum(self):
        row = self.row("cap-both")
        self.assertEqual(row["specs"]["ocpus"], AX_MAX_OCPU)
        self.assertEqual(row["specs"]["memoryGb"], AX_MAX_MEM)
        # The spec'd size survives for the "(was N)" note.
        self.assertEqual(row["specs"]["specOcpus"], 288)
        self.assertEqual(row["specs"]["specMemoryGb"], 4031)
        self.assertEqual(row["sizeCapped"], {"maxOcpu": AX_MAX_OCPU, "maxMemGb": AX_MAX_MEM})

    def test_capped_row_bills_the_capped_size_not_the_spec(self):
        row = self.row("cap-both")
        ocpu_items = [li for li in row["lineItems"] if li["unit"] == "OCPU-hour"]
        self.assertEqual(len(ocpu_items), 1)
        self.assertEqual(ocpu_items[0]["quantity"], round(AX_MAX_OCPU * 730, 4))
        self.assertIn("Capped at", ocpu_items[0]["mapping"])
        self.assertAlmostEqual(row["monthly"], round(sum(li["monthly"] for li in row["lineItems"]), 2), places=2)

    def test_capped_row_keeps_its_size_flag(self):
        # The cap changes what is billed, not the flag: the row must stay flagged (judged on
        # the SPEC'D size) until a shape that actually fits is chosen.
        self.assertNotEqual(self.row("cap-both")["sizeCheck"]["status"], "ok")

    def test_memory_only_overflow_caps_memory_and_leaves_ocpus_alone(self):
        row = self.row("cap-memory-only")
        self.assertEqual(row["specs"]["ocpus"], 16)
        self.assertEqual(row["specs"]["memoryGb"], AX_MAX_MEM)
        self.assertEqual(row["specs"]["specMemoryGb"], 1024)
        self.assertIsNotNone(row["sizeCapped"])

    def test_fitting_vm_is_untouched(self):
        row = self.row("fits-fine")
        self.assertEqual(row["specs"]["ocpus"], row["specs"]["specOcpus"])
        self.assertEqual(row["specs"]["memoryGb"], row["specs"]["specMemoryGb"])
        self.assertIsNone(row["sizeCapped"])
        self.assertEqual(row["sizeCheck"]["status"], "ok")

    def test_overflow_that_fits_no_flex_is_capped_but_not_bare_metal_without_auto(self):
        # Selected-shape mode never lands a row on bare metal by itself: the row is capped
        # and flagged (status baremetal, no flexAlt -> the OVERSIZED badge), nothing more.
        for name in ("needs-bm-small", "needs-bm-big"):
            row = self.row(name)
            self.assertEqual(row["bareMetalServers"], 0)
            self.assertEqual(row["specs"]["ocpus"], AX_MAX_OCPU)
            self.assertIsNotNone(row["sizeCapped"])
            self.assertEqual(row["sizeCheck"]["status"], "baremetal")
            self.assertFalse(row["sizeCheck"].get("flexAlt"))

    # ---- 2. Bare-metal box model ---------------------------------------------------

    def price_with_override(self, machine, bm_key):
        target = next(r for r in self.rows if r.get("machine_name") == machine)
        pricing = app.calculate_pricing(
            self.fields, self.rows, shape_key="e6-standard-ax",
            shape_overrides={str(target["__id"]): bm_key},
        )
        return next(r for r in pricing["rows"] if r["name"] == machine)

    def test_bare_metal_row_bills_exactly_one_box_at_its_fixed_size(self):
        row = self.price_with_override("fits-fine", "bm-e6-256")
        self.assertEqual(row["bareMetalServers"], 1)
        self.assertEqual(row["specs"]["ocpus"], BM_256_OCPU)
        self.assertEqual(row["specs"]["memoryGb"], BM_256_MEM)
        self.assertEqual(row["specs"]["specOcpus"], 8)
        self.assertIsNone(row["sizeCapped"])  # the flex cap never touches a bare-metal row
        self.assertEqual(row["sizeCheck"]["status"], "ok")

    def test_vm_bigger_than_the_box_is_impossible_never_two_boxes(self):
        # THE regression guard: a 288 OCPU / 4,031 GB VM on BM.Standard.E6.Ax.192 must bill
        # ONE box (192 / 1,536) and flag impossible - never ceil() up to 2+ boxes (576 OCPU).
        row = self.price_with_override("cap-both", "bm-e6-ax-192")
        self.assertEqual(row["bareMetalServers"], 1)
        self.assertEqual(row["specs"]["ocpus"], BM_192_OCPU)
        self.assertEqual(row["specs"]["memoryGb"], BM_192_MEM)
        self.assertEqual(row["sizeCheck"]["status"], "impossible")
        ocpu_items = [li for li in row["lineItems"] if li["unit"] == "OCPU-hour"]
        self.assertEqual(ocpu_items[0]["quantity"], round(BM_192_OCPU * 730, 4))

    # ---- 3. Auto mode maps flex-impossible VMs onto bare metal ----------------------

    def auto_row(self, name):
        self.assertIn(name, self.by_name_auto)
        return self.by_name_auto[name]

    def test_auto_maps_a_flex_impossible_vm_to_the_smallest_fitting_box(self):
        row = self.auto_row("needs-bm-small")  # 150 OCPU / 800 GB
        self.assertEqual(row["shapeUsed"]["key"], "bm-e6-ax-192")
        self.assertEqual(row["bareMetalServers"], 1)
        self.assertEqual(row["specs"]["ocpus"], BM_192_OCPU)
        self.assertEqual(row["specs"]["memoryGb"], BM_192_MEM)
        self.assertEqual(row["specs"]["specOcpus"], 150)
        self.assertEqual(row["sizeCheck"]["status"], "ok")
        self.assertIsNone(row["sizeCapped"])

    def test_auto_skips_boxes_the_vm_does_not_fit(self):
        row = self.auto_row("needs-bm-big")  # 200 OCPU: past Ax.192, fits E6.256
        self.assertEqual(row["shapeUsed"]["key"], "bm-e6-256")
        self.assertEqual(row["bareMetalServers"], 1)
        self.assertEqual(row["specs"]["ocpus"], BM_256_OCPU)
        self.assertEqual(row["sizeCheck"]["status"], "ok")

    def test_auto_leaves_a_fitting_vm_on_flex(self):
        row = self.auto_row("fits-fine")
        self.assertEqual(row["bareMetalServers"], 0)
        self.assertFalse((row["shapeUsed"] or {}).get("key", "").startswith("bm-"))

    def test_auto_never_maps_to_a_box_that_cannot_hold_the_vm(self):
        # 288 OCPU / 4,031 GB fits NO AMD box: auto must leave it on flex - capped and
        # impossible - rather than "solve" it with a box it overflows.
        row = self.auto_row("cap-both")
        self.assertEqual(row["bareMetalServers"], 0)
        self.assertIsNotNone(row["sizeCapped"])
        self.assertEqual(row["sizeCheck"]["status"], "impossible")


if __name__ == "__main__":
    unittest.main()
