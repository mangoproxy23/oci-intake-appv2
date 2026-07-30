#!/usr/bin/env python3

import json
import tempfile
import unittest
from pathlib import Path
import sys

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bom_diagram
import oci_catalog
from architecture_engine import integration


class ArchitectureIconTests(unittest.TestCase):
    def test_every_curated_service_has_sku_and_renderable_architecture_icon(self):
        catalog = integration.boeing_renderer.SnippetCatalog(integration.ROOT)
        pillow_catalog = bom_diagram._PillowOciIconRenderer().lib

        # Hand-written cards declare their icon explicitly; estimator-generated cards resolve
        # theirs through architecture_mapping(). Both must end up with a renderable icon, but
        # only the hand-written ones belong in the map - and the map must not carry ids that
        # no longer exist (it kept genai_dedicated/genai_rag after the GenAI rework).
        hand_written = {e["id"] for e in oci_catalog.CURATED if e.get("source") == "curated"}
        self.assertEqual(set(oci_catalog.ARCHITECTURE_ICON_BY_ID), hand_written)
        for entry in oci_catalog.CURATED:
            self.assertTrue(entry["sku"], entry["id"])
            icon_title = entry["architectureIcon"]
            self.assertTrue(
                icon_title in catalog.library_snippets
                or icon_title in catalog.toolkit_titles,
                f"{entry['id']} uses an unknown OCI icon: {icon_title}",
            )
            self.assertEqual(icon_title, pillow_catalog.resolve(icon_title))

    def test_every_raw_price_list_sku_has_a_renderable_architecture_mapping(self):
        catalog = integration.boeing_renderer.SnippetCatalog(integration.ROOT)
        pillow_catalog = bom_diagram._PillowOciIconRenderer().lib

        self.assertGreater(len(oci_catalog._PRICES), 600)
        for sku, item in oci_catalog._PRICES.items():
            self.assertTrue(sku)
            self.assertEqual(sku, item.get("sku"))
            name = item.get("desc") or sku
            group = (
                "Licensing"
                if any(
                    term in oci_catalog._norm(name)
                    for term in oci_catalog._THIRD_PARTY_TERMS
                )
                else "Other Services"
            )
            icon_title, resolution = oci_catalog.architecture_mapping(name, group)
            self.assertIn(
                resolution,
                {"direct", "fallback", "category-fallback"},
                f"{sku}: {name}",
            )
            self.assertTrue(
                icon_title in catalog.library_snippets
                or icon_title in catalog.toolkit_titles,
                f"{sku} uses an unknown OCI icon: {icon_title}",
            )
            self.assertEqual(icon_title, pillow_catalog.resolve(icon_title))

    def test_all_added_services_keep_skus_and_reach_architecture_spec(self):
        selections = []
        for entry in oci_catalog.CURATED:
            values = {
                field["key"]: field.get("default", 0)
                for field in entry.get("fields", [])
            }
            selections.append({"catalogId": entry["id"], "values": values})

        extra_priced, _total = oci_catalog.price_extras(selections)
        self.assertEqual(len(oci_catalog.CURATED), len(extra_priced))
        for service in extra_priced:
            self.assertTrue(service["sku"], service["name"])
            self.assertTrue(service["skus"], service["name"])
            self.assertTrue(
                all(line.get("sku") for line in service["skus"]),
                service["name"],
            )
            self.assertTrue(service["architectureIcon"], service["name"])

        pricing = {
            "rows": [
                {
                    "rowId": "vm-1",
                    "monthly": 100,
                    "specs": {
                        "ocpus": 2,
                        "memoryGb": 16,
                        "blockStorageGb": 100,
                    },
                    "lineItems": [
                        {
                            "sku": "B111129",
                            "description": "OCPU-hr rate (E6 Standard Ax)",
                            "unit": "OCPU-hour",
                            "monthly": 90,
                        },
                        {
                            "sku": "B111130",
                            "description": "Memory GB-hr rate (E6 Standard Ax)",
                            "unit": "GB-hour",
                            "monthly": 10,
                        },
                    ],
                }
            ]
        }
        rows = [{"__id": "vm-1", "Environment": "Prod"}]
        diagram_pricing = {
            "rows": [
                *pricing["rows"],
                *bom_diagram._addins_as_rows(extra_priced),
            ]
        }
        segments = bom_diagram.collect_segments(
            diagram_pricing,
            rows,
            {"env": "Environment"},
        )
        spec = bom_diagram.build_spec(
            diagram_pricing,
            segments,
            "All Services",
            "E6 Ax",
            segment_source="env",
            diagram_options={"enableDr": False},
        )
        labels = "\n".join(
            str(node.get("label") or "")
            for node in spec["nodes"]
        )

        for service in extra_priced:
            self.assertIn(service["sku"], labels, service["name"])
        self.assertIn("B111129", labels)
        self.assertIn("B111130", labels)

        with tempfile.TemporaryDirectory() as temp_dir:
            drawio, png = bom_diagram.render(
                spec,
                temp_dir,
                name="all_services",
                diagram_options={"enableDr": False},
            )
            self.assertTrue(Path(drawio).exists())
            self.assertIsNotNone(png)
            self.assertTrue(Path(png).exists())
            report = json.loads(
                (Path(temp_dir) / "all_services_icon_mapping.json").read_text()
            )
            mapped_skus = {
                sku
                for item in report
                for sku in (item.get("skus") or [])
            }
            for service in extra_priced:
                self.assertIn(service["sku"], mapped_skus, service["name"])
            self.assertIn("B111129", mapped_skus)
            self.assertIn("B111130", mapped_skus)
            self.assertFalse(
                [
                    item
                    for item in report
                    if item.get("skus") and item.get("kind") == "placeholder"
                ]
            )

    def test_raw_price_list_service_keeps_sku_and_specific_icon(self):
        raw = oci_catalog._raw_matches("exadata", 1)[0]
        values = {
            field["key"]: field.get("default", 0)
            for field in raw.get("fields", [])
        }
        service, _total = oci_catalog.price_extras(
            [{**raw, "catalogId": raw["id"], "values": values}]
        )

        self.assertEqual(1, len(service))
        self.assertEqual(raw["sku"], service[0]["sku"])
        self.assertEqual("Database - Exadata", service[0]["architectureIcon"])
        self.assertEqual("direct", service[0]["architectureResolution"])
        self.assertEqual("Database", service[0]["architectureGroup"])

        pricing = {"rows": bom_diagram._addins_as_rows(service)}
        databases = bom_diagram.collect_databases(pricing)
        self.assertEqual("Database - Exadata", databases[0]["shape"])
        self.assertIn(raw["sku"], databases[0]["skus"])

    def test_all_configured_service_icons_exist_in_bundled_assets(self):
        catalog = integration.boeing_renderer.SnippetCatalog(integration.ROOT)
        titles = {
            stencil
            for stencil, _label in bom_diagram._SERVICE_STENCILS.values()
        }
        titles.update(
            stencil
            for _match, stencil, _label in bom_diagram._DB_TYPE_STENCILS
        )
        titles.update(oci_catalog.ARCHITECTURE_GROUP_ICONS.values())
        titles.update(
            icon_title
            for _keyword, icon_title, _resolution
            in oci_catalog.ARCHITECTURE_NAME_ICONS
        )
        missing = sorted(
            title
            for title in titles
            if title not in catalog.library_snippets
            and title not in catalog.toolkit_titles
        )
        self.assertEqual([], missing)

    def test_renderer_recovers_legacy_autonomous_database_icon_title(self):
        catalog = integration.boeing_renderer.SnippetCatalog(integration.ROOT)
        renderer = integration.boeing_renderer.DrawioRenderer(catalog)
        spec = {
            "clarification_gate": {
                "status": "waived",
                "notes": "Renderer regression test.",
                "waiver_reason": "The test supplies a single known service.",
            },
            "pages": [
                {
                    "name": "Physical",
                    "page_type": "physical",
                    "width": 500,
                    "height": 300,
                    "elements": [
                        {
                            "id": "adb",
                            "type": "library",
                            "icon_title": "Database - ADB",
                            "label": "Autonomous Database",
                            "x": 80,
                            "y": 60,
                            "w": 100,
                            "h": 100,
                        }
                    ],
                }
            ],
        }

        _mxfile, report = renderer.render_spec(spec)

        self.assertEqual("library", report[0]["kind"])
        self.assertEqual("Database - Autonomous DB", report[0]["icon_title"])
        self.assertEqual("alias", report[0]["resolution"])

    def test_pillow_renderer_uses_official_oci_stencils(self):
        shapes = [
            "Compute - Virtual Machine VM",
            "Networking - Dynamic Routing Gateway DRG",
            "Storage - Object Storage",
            "Identity and Security - Firewall",
            "Database - Autonomous Data Warehouse ADW",
            "Observability and Management - Monitoring",
        ]
        nodes = [
            {
                "id": f"icon-{index}",
                "shape": shape,
                "x": 40 + index * 130,
                "y": 40,
                "w": 88,
                "h": 88,
            }
            for index, shape in enumerate(shapes)
        ]
        spec = {
            "page": {"width": 900, "height": 220},
            "containers": [],
            "nodes": nodes,
            "edges": [],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "icons.png"
            bom_diagram._render_png_pillow(spec, output)
            with Image.open(output) as image:
                rgb = image.convert("RGB")
                for node in nodes:
                    crop = rgb.crop(
                        (
                            node["x"],
                            node["y"],
                            node["x"] + node["w"],
                            node["y"] + node["h"],
                        )
                    )
                    colored_pixels = sum(
                        1
                        for y in range(crop.height)
                        for x in range(crop.width)
                        if min(crop.getpixel((x, y))) < 230
                    )
                    self.assertGreater(
                        colored_pixels,
                        500,
                        f"{node['shape']} did not render as a visible OCI stencil.",
                    )


if __name__ == "__main__":
    unittest.main()
