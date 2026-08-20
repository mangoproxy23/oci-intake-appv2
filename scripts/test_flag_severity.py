"""Flag badge severity tiers (Chris's ruling 2026-08-19).

RED = carried dollars (decided from the row's line items at output time, so
dropdown and automatic carries both go red) and badly mis-sized VM shapes (>20%
over the selected shape's max CPU or RAM); ORANGE = conversions that embed
architecture/topology assumptions (DTU-like); everything else stays YELLOW.

Run: python3 scripts/test_flag_severity.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app


class FlagSeverityTest(unittest.TestCase):
    def test_architecture_review_conversions_are_orange(self):
        for flag in (app.AZ_SQL_DTU_FLAG, app.AZ_SQL_VCORE_FLAG, app.AZ_FW_FLAG,
                     app.AZ_PG_FLAG, app.AZ_MYSQL_FLAG, app.AZ_REDIS_FLAG,
                     app.AZ_INCLUDED_FLAG):
            self.assertEqual(app.FLAG_SEVERITY_BY_TEXT.get(flag), "orange", flag)

    def test_app_service_flag_is_orange_carry_trumps_to_red(self):
        # Doc #11: a CI-priced App Service row is an architecture-review
        # conversion (orange); its default-carry rows still render red because
        # all-carried line items trump the map at row output.
        self.assertEqual(app.FLAG_SEVERITY_BY_TEXT.get(app.AZ_APPSVC_FLAG), "orange")

    def test_previous_stuff_stays_yellow(self):
        # No severity entry -> the frontend default (yellow review badge).
        for flag in (app.GH_FLAG, app.AWS_DEV_FLAG, app.AWS_SGW_FLAG):
            self.assertNotIn(flag, app.FLAG_SEVERITY_BY_TEXT, flag)

    def test_size_check_severe_beyond_20_percent(self):
        # Pick a real flex shape and overflow it by >20% on OCPU -> severe (red).
        key, (shape, max_o, max_m, vendor) = next(iter(app.SHAPE_KEY_TO_OCI.items()))
        over = app.oci_size_check(key, max_o * 1.3, max_m * 0.5)
        self.assertIn(over["status"], ("baremetal", "impossible"))
        if over["status"] == "baremetal":
            self.assertTrue(over["severe"], "30% CPU overflow must be severe")
        # RAM axis triggers independently.
        ram = app.oci_size_check(key, max_o * 0.5, max_m * 1.3)
        if ram["status"] == "baremetal":
            self.assertTrue(ram["severe"], "30% RAM overflow must be severe")

    def test_size_check_mild_overflow_is_not_severe(self):
        key, (shape, max_o, max_m, vendor) = next(iter(app.SHAPE_KEY_TO_OCI.items()))
        mild = app.oci_size_check(key, max_o * 1.1, max_m * 0.5)
        if mild["status"] == "baremetal":     # within 20% of the cap
            self.assertFalse(mild["severe"], "10% overflow stays amber, not red")
        fits = app.oci_size_check(key, max_o * 0.5, max_m * 0.5)
        self.assertEqual(fits["status"], "ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
