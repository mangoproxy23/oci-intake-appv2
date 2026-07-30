"""Regression checks for the official cross-cloud compute-shape catalog."""

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
os.environ["OCI_APP_NO_BOOTSTRAP"] = "1"
sys.path.insert(0, str(ROOT))


def main():
    payload = json.loads((ROOT / "data" / "cloud_shape_map.json").read_text())
    rows = payload["shapes"]
    assert payload["meta"]["count"] == len(rows)
    assert len(rows) >= 3807

    keys = [row["key"] for row in rows]
    assert len(keys) == len(set(keys)), "cloud shape keys must be globally unique"

    official = [row for row in rows if row.get("addedFrom") == "official-provider-docs"]
    assert len(official) >= 907
    assert {row["provider"] for row in official} == {"aws", "azure", "gcp"}
    assert all(re.match(r"^\d{4}-\d{2}-\d{2}$", row.get("addedOn", "")) for row in official)
    assert all(re.match(r"^\d{4}-\d{2}-\d{2}$", row.get("sourceRetrievedOn", ""))
               for row in official)
    allowed_hosts = {
        "aws": "docs.aws.amazon.com",
        "azure": "learn.microsoft.com",
        "gcp": "docs.cloud.google.com",
    }
    assert all(urlparse(row["sourceUrl"]).netloc == allowed_hosts[row["provider"]]
               for row in official)

    by_key = {row["key"]: row for row in rows}

    # Arm generation and OCPU semantics: A1 is one core/OCPU; A4 is two cores/OCPU.
    assert by_key["a1metal"]["ociShape"] == "Ampere A1"
    assert by_key["a1metal"]["ocpus"] == 16
    assert by_key["c8gb24xlarge"]["ociShape"] == "A4 Standard"
    assert by_key["c8gb24xlarge"]["ocpus"] == 48
    assert by_key["c8gb24xlarge"]["ociShapeName"] == "BM.Standard.A4.48"

    # Processor generation must survive capacity fallback.
    assert by_key["d128dsv7"]["ociShape"] == "X12 Standard Ax"
    assert by_key["d128dsv7"]["ociShapeName"] == "BM.Standard4.Ax.120"
    assert by_key["d128adsv7"]["ociShape"] == "E6 Standard"
    assert by_key["d128adsv7"]["ociShapeName"] == "VM.Standard.E6.Flex"

    import app

    assert app.equivalent_gen_shape_key("aws", "arm", 8) == "a4-standard"
    assert app.equivalent_gen_shape_key("azure", "amd", 7) == "e6-standard"
    assert app.equivalent_gen_shape_key("gcp", "intel", 4) == "x12-standard-ax"
    assert app.SHAPE_KEY_TO_OCI["e5-standard"][1:3] == (126, 1049)
    assert app.SHAPE_KEY_TO_OCI["a1-standard"][1:3] == (76, 472)
    assert app.SHAPE_KEY_TO_OCI["a2-standard"][1:3] == (78, 946)
    assert app.SHAPE_KEY_TO_OCI["a4-standard"][1:3] == (45, 700)
    assert app.lookup_cloud_shape("running instance c8gb.24xlarge")["key"] == "c8gb24xlarge"
    assert app.lookup_cloud_shape("Standard_D128ds_v7")["key"] == "d128dsv7"
    print(f"cloud shape catalog OK: {len(rows)} total, {len(official)} official additions")


if __name__ == "__main__":
    main()
