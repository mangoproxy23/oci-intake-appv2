"""Regression checks for OCI Object Storage tier pricing and SKU breakdowns."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app
import oci_catalog


def entry(catalog_id):
    return next(item for item in oci_catalog.CURATED if item["id"] == catalog_id)


def sku_map(lines):
    return {line["sku"]: line for line in lines}


def main():
    storage = oci_catalog.search("", "Storage")
    tiers = {item["id"]: item for item in storage}

    # The curated five must all be present. The group also carries estimator-generated cards
    # now, so assert on what this test is about rather than on a total that will keep moving.
    assert {"block", "object", "object_ia", "archive", "file"} <= set(tiers)
    assert len([t for t in storage if t.get("source") != "estimator"]) == 5
    assert tiers["object"]["sku"] == "B91628"
    assert tiers["object_ia"]["sku"] == "B93000"
    assert tiers["archive"]["sku"] == "B91633"

    standard_values = {"gb": 1000, "requests": 0}
    ia_values = {"gb": 1000, "retrievalGb": 100, "requests": 15}
    archive_values = {"gb": 1000, "requests": 15}

    assert oci_catalog.line_cost(entry("object"), standard_values) == 25.24
    assert oci_catalog.line_cost(entry("object_ia"), ia_values) == 10.83
    assert oci_catalog.line_cost(entry("archive"), archive_values) == 2.61

    ia_lines = sku_map(oci_catalog.line_breakdown(entry("object_ia"), ia_values))
    assert set(ia_lines) == {"B93000", "B93001", "B91627"}
    assert ia_lines["B93000"]["monthly"] == 9.9
    assert ia_lines["B93001"]["monthly"] == 0.9
    assert ia_lines["B91627"]["monthly"] == 0.03

    archive_lines = sku_map(oci_catalog.line_breakdown(entry("archive"), archive_values))
    assert set(archive_lines) == {"B91633", "B91627"}
    assert archive_lines["B91633"]["monthly"] == 2.57
    assert archive_lines["B91627"]["monthly"] == 0.03

    priced, total = oci_catalog.price_extras([
        {"catalogId": "object_ia", "values": ia_values},
        {"catalogId": "archive", "values": archive_values},
    ])
    assert total == 13.44
    assert [item["sku"] for item in priced] == ["B93000", "B91633"]
    assert {line["sku"] for item in priced for line in item["skus"]} == {
        "B93000", "B93001", "B91627", "B91633",
    }

    fields = [
        {"key": "source_service", "label": "Service"},
        {"key": "source_product", "label": "Product"},
        {"key": "usage_unit", "label": "Unit"},
    ]
    ia_capacity, _ = app.classify_full_service_item({
        "source_service": "Amazon S3",
        "source_product": "Standard-IA TimedStorage",
        "usage_unit": "GB",
    }, fields)
    ia_retrieval, _ = app.classify_full_service_item({
        "source_service": "Amazon S3",
        "source_product": "Standard-IA Data Retrieval",
        "usage_unit": "GB",
    }, fields)
    archive, _ = app.classify_full_service_item({
        "source_service": "Amazon S3",
        "source_product": "Glacier Deep Archive TimedStorage",
        "usage_unit": "GB",
    }, fields)
    assert ia_capacity["sku"] == "B93000"
    assert ia_retrieval["sku"] == "B93001"
    assert archive["sku"] == "B91633"

    assert app.map_service_comparison("aws", "Amazon S3", "Standard-IA")["product"] == (
        "OCI Infrequent Access Storage"
    )
    assert app.map_service_comparison("azure", "Blob Cool LRS")["product"] == (
        "OCI Infrequent Access Storage"
    )

    # Every S3 line carries the same service name, so the tier lives only in the product text or
    # the usage type. Matching on the service alone priced all of them as Standard ($0.0255/GB)
    # instead of IA ($0.0100) or Archive ($0.0026) - 8 lines on a real AWS bill. The detail is
    # allowed to refine the service ONLY along this tier ladder, so a named service still wins
    # everywhere else (AmazonCloudFront must not become plain egress).
    tier_cases = [
        (("Amazon S3", "Standard-IA"), "OCI Infrequent Access Storage"),
        (("Amazon S3", "One Zone-IA"), "OCI Infrequent Access Storage"),
        (("Amazon S3", "Glacier"), "OCI Archive Storage"),
        (("Amazon S3", "Glacier Deep Archive"), "OCI Archive Storage"),
        (("Amazon S3", "Standard"), "OCI Object Storage"),
        (("Amazon S3",), "OCI Object Storage"),
        (("Amazon Simple Storage Service", "TimedStorage-SIA-ByteHrs"),
         "OCI Infrequent Access Storage"),
        (("Amazon Simple Storage Service", "TimedStorage-ZIA-ByteHrs"),
         "OCI Infrequent Access Storage"),
        (("Amazon Simple Storage Service", "TimedStorage-GDA-ByteHrs"), "OCI Archive Storage"),
        (("Amazon Simple Storage Service", "TimedStorage-ByteHrs"), "OCI Object Storage"),
        # Guards on the narrowness of the rule - these must NOT be refined by detail text.
        (("AmazonCloudFront", "US-DataTransfer-Out-Bytes"),
         "Third-Party CDN (Akamai / CloudFlare)"),
        (("AWSDataTransfer", "regional data transfer - in/out/between EC2 AZs"),
         "OCI Outbound Data Transfer"),
        (("Amazon Elastic Compute Cloud", "BoxUsage:m5.large"), "OCI Virtual Machine Instances"),
    ]
    for texts, expected in tier_cases:
        got = (app.map_service_comparison("aws", *texts) or {}).get("product")
        assert got == expected, f"{texts} mapped to {got!r}, expected {expected!r}"

    print("Object Storage tier pricing regression checks passed.")


if __name__ == "__main__":
    main()
