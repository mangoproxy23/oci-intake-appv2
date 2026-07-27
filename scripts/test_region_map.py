#!/usr/bin/env python3
"""Regression checks for the architecture region map and renderer catalog."""

import re
import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bom_diagram


APP_JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

EXPECTED_MULTI_AD = {
    "eu-frankfurt-1",
    "uk-london-1",
    "us-ashburn-1",
    "us-chicago-1",
    "us-phoenix-1",
}


def main():
    region_block = APP_JS.split("const OCI_REGIONS = [", 1)[1].split("];", 1)[0]
    frontend_regions = set(re.findall(r'\bid:\s*"([^"]+)"', region_block))

    assert len(frontend_regions) == 45, (
        f"Expected 45 commercial OCI regions, found {len(frontend_regions)}."
    )
    assert frontend_regions == set(bom_diagram.OCI_REGION_LABELS), (
        "The frontend map and architecture renderer region catalogs differ."
    )
    assert set(bom_diagram.OCI_REGION_ADS) == EXPECTED_MULTI_AD
    assert all(bom_diagram._region_ad_count(region) == 3 for region in EXPECTED_MULTI_AD)
    assert bom_diagram._region_ad_count("ap-sydney-1") == 1

    for required_id in (
        "ociRegionMap",
        "ociRegionMarkers",
        "primaryRegionSummary",
        "drRegionSummary",
        "regionMapLive",
    ):
        assert f'id="{required_id}"' in INDEX_HTML

    assert "selectRegionFromMap" in APP_JS
    assert "setDrRegionEnabled(true)" in APP_JS
    assert "same OCI realm" in APP_JS
    assert "diagramOptions: state.diagramOptions || {}" in APP_JS
    assert "US Midwest (Chicago) (us-chicago-1)" == bom_diagram._region_label(
        "us-chicago-1"
    )
    spec = bom_diagram.build_spec(
        {"totals": {}},
        [],
        diagram_options={
            "primaryRegion": "us-ashburn-1",
            "enableDr": True,
            "drRegion": "us-phoenix-1",
        },
    )
    serialized_spec = json.dumps(spec)
    assert "US East (Ashburn)" in serialized_spec
    assert "US West (Phoenix)" in serialized_spec

    print("Architecture region map regression passed.")


if __name__ == "__main__":
    main()
