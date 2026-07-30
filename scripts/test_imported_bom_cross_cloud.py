"""Regression checks for cross-cloud comparison in Import Other BOM mode."""

import os
import sys

os.environ["OCI_APP_NO_BOOTSTRAP"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app


def imported_compute_row(vendor="arm"):
    return {
        "rowId": "converted-1",
        "costAction": "price",
        "hoursPerMonth": 730,
        "originalOcpus": 2,
        "originalMemoryGb": 16,
        # Converted BOM rows carry shape_payload(), whose public key is
        # processorVendor rather than the normalized pricing-row key vendor.
        "shapeUsed": {"processorVendor": vendor},
        "specs": {
            "ocpus": 2,
            "memoryGb": 16,
            "blockStorageGb": 100,
            "fileStorageGb": 0,
        },
        "windowsLicenseMonthly": 0,
        "sqlLicenseMonthly": 0,
        "lineItems": [],
    }


def test_existing_engine_accepts_converted_shape_schema():
    seen_vendors = []
    original = app.equivalent_instance

    def equivalent(cloud, vendor, vcpus, memory, top_of_line=False):
        seen_vendors.append((cloud, vendor, top_of_line))
        return {"instance": "test", "hourly": 1.0, "vcpu": vcpus, "mem": memory}

    app.equivalent_instance = equivalent
    try:
        comparison = app.cross_cloud_estimate([imported_compute_row("arm")])
    finally:
        app.equivalent_instance = original

    assert comparison["bestMatch"]["aws"]["monthlyTotal"] > 0
    assert comparison["bestMatch"]["azure"]["monthlyTotal"] > 0
    assert comparison["topTier"]["aws"]["monthlyTotal"] > 0
    assert seen_vendors
    assert all(vendor == "arm" for _, vendor, _ in seen_vendors)


def test_cross_cloud_endpoint_returns_the_existing_comparison_shape():
    class FakeHandler:
        def __init__(self):
            self.status = None
            self.payload = None

        def read_json_body(self):
            return {"rows": [imported_compute_row("amd")], "convertedBom": True}

        def send_json(self, status, payload):
            self.status = status
            self.payload = payload

        def send_error_json(self, status, message):
            raise AssertionError(f"unexpected endpoint error {status}: {message}")

    handler = FakeHandler()
    app.IntakeHandler.handle_cross_cloud(handler)
    assert handler.status == 200
    comparison = handler.payload["crossCloud"]
    assert comparison["convertedBom"] is True
    assert comparison["cloudBillMode"] is False
    assert comparison["bestMatch"]["aws"]["priced"] is True
    assert comparison["bestMatch"]["azure"]["priced"] is True
    assert comparison["bestMatch"]["gcp"]["priced"] is False


def test_frontend_refreshes_and_persists_converted_comparison():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "static", "app.js"), encoding="utf-8") as handle:
        source = handle.read()
    assert '"/api/cross-cloud"' in source
    assert "await refreshConvertedCrossCloud();" in source
    assert "? state.pricing" in source
    assert "convertedPricing: state.pricing?.converted ? state.pricing : null" in source


if __name__ == "__main__":
    tests = [
        test_existing_engine_accepts_converted_shape_schema,
        test_cross_cloud_endpoint_returns_the_existing_comparison_shape,
        test_frontend_refreshes_and_persists_converted_comparison,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
