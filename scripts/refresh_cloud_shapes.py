#!/usr/bin/env python3
"""Add missing AWS, Azure, and GCP compute shapes from official provider documentation.

This importer is deliberately additive. Existing rows in data/cloud_shape_map.json are curated
and are never overwritten. New rows include the retrieval date, exact source URL, source
processor vendor/generation, and the OCI generation-mapping rule used.

The mapping has two independent parts:

1. Compatibility: preserve CPU architecture and select the closest OCI processor generation.
   Exact processor signatures win; provider instance-name generation is the fallback.
2. Capacity: calculate required OCI OCPUs with the selected target's documented core/OCPU
   semantics, then choose a fitting flex or bare-metal shape from data/oci_shapes.json.

Usage:
    python3 scripts/refresh_cloud_shapes.py --dry-run
    python3 scripts/refresh_cloud_shapes.py
    python3 scripts/refresh_cloud_shapes.py --provider aws

Exit codes: 0 success, 1 if a provider could not be refreshed (nothing is written).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import math
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from lxml import html


REPO = Path(__file__).resolve().parents[1]
MAP_PATH = REPO / "data" / "cloud_shape_map.json"
OCI_SHAPES_PATH = REPO / "data" / "oci_shapes.json"
POLICY_PATH = REPO / "data" / "processor_generation_mapping.json"
HOURS_PER_MONTH = 730
TIMEOUT = 60
TODAY = dt.date.today().isoformat()

AWS_PAGES = {
    "gp": "General Purpose",
    "co": "Compute Optimized",
    "mo": "Memory Optimized",
    "so": "Storage Optimized",
    "ac": "Accelerated Computing",
    "hpc": "High Performance Computing",
    "pg": "Previous Generation",
}
AWS_BASE = "https://docs.aws.amazon.com/ec2/latest/instancetypes"
AZURE_OVERVIEW = "https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/overview"
AZURE_BASE = "https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/"
GCP_PAGES = {
    "general-purpose-machines": "General Purpose",
    "compute-optimized-machines": "Compute Optimized",
    "memory-optimized-machines": "Memory Optimized",
    "storage-optimized-machines": "Storage Optimized",
    "accelerator-optimized-machines": "Accelerator Optimized",
}
GCP_BASE = "https://docs.cloud.google.com/compute/docs"

SOURCE_RATES = {
    "aws": (0.031, 0.0043),
    "azure": (0.031, 0.0043),
    "gcp": (0.030, 0.0040),
}


def clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def key_for(name) -> str:
    return re.sub(r"[^a-z0-9]", "", clean(name).lower())


def number(value):
    text = clean(value).replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group()) if match else None


def fetch(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "oci-intake-app-cloud-shape-refresh/1.0",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read().decode("utf-8", "replace")


def parse_document(text: str):
    document = html.fromstring(text)
    # Documentation footnote markers often become an extra digit (for example 192³ -> 1923).
    # Remove them before extracting numeric values.
    for node in document.xpath("//sup|//script|//style"):
        node.drop_tree()
    return document


def table_matrix(table):
    rows = []
    for tr in table.xpath(".//tr"):
        cells = tr.xpath("./th|./td")
        if cells:
            rows.append([clean(cell.text_content()) for cell in cells])
    return rows


def normalized_header(value):
    return re.sub(r"[^a-z0-9]", "", clean(value).lower())


def find_columns(matrix):
    """Find a header row containing name, vCPU/core, and memory columns."""
    for row_index, row in enumerate(matrix):
        headers = [normalized_header(value) for value in row]
        name_index = next(
            (
                i
                for i, value in enumerate(headers)
                if value in {
                    "instancetype",
                    "machinetype",
                    "machinetypes",
                    "size",
                    "sizename",
                    "vmsize",
                }
            ),
            None,
        )
        vcpu_index = next(
            (
                i
                for i, value in enumerate(headers)
                if value.startswith("vcpu")
                or value in {"cores", "core", "virtualcores", "numberofcores"}
            ),
            None,
        )
        memory_index = next(
            (
                i
                for i, value in enumerate(headers)
                if value.startswith("memory")
                and "accelerator" not in value
                and "gpu" not in value
            ),
            None,
        )
        processor_index = next(
            (i for i, value in enumerate(headers) if value.startswith("processor")),
            None,
        )
        accelerator_index = next(
            (
                i
                for i, value in enumerate(headers)
                if "accelerator" in value or value in {"gpus", "gpu"}
            ),
            None,
        )
        if name_index is not None and vcpu_index is not None and memory_index is not None:
            return {
                "row": row_index,
                "name": name_index,
                "vcpu": vcpu_index,
                "memory": memory_index,
                "processor": processor_index,
                "accelerator": accelerator_index,
            }
    return None


def cell(row, index):
    return row[index] if index is not None and index < len(row) else ""


def aws_name(value):
    name = clean(value).lower()
    if re.match(
        r"^[a-z][a-z0-9-]*\.(?:nano|micro|small|medium|large|xlarge|\d+xlarge|metal(?:-\d+xl)?)$",
        name,
    ):
        return name
    return None


def azure_name(value):
    name = clean(value).replace("\u200b", "")
    name = re.sub(r"^Standard[_ ]", "", name, flags=re.I)
    name = re.sub(r"_v(\d+)\b", r" v\1", name, flags=re.I)
    if re.match(r"^[A-Za-z][A-Za-z0-9_ -]*\d[A-Za-z0-9_ -]*$", name):
        return name
    return None


def gcp_name(value):
    name = clean(value).lower().replace(" ", "")
    if re.match(r"^[a-z][a-z0-9]*-[a-z0-9]+(?:-[a-z0-9]+)+$", name):
        return name
    return None


def source_generation(provider, name):
    text = clean(name).lower()
    if provider == "azure":
        match = re.search(r"(?:\s|_)v(\d+)\b", text)
        return int(match.group(1)) if match else 1
    if provider == "gcp":
        match = re.match(r"[a-z]+(\d+)", text.split("-")[0])
        return int(match.group(1)) if match else 1
    match = re.match(r"[a-z]+(\d+)", text.split(".")[0])
    return int(match.group(1)) if match else 1


def vendor_from_processor(processor):
    text = clean(processor).lower()
    if re.search(r"graviton|ampere|arm|axion|nvidia grace|cobalt", text):
        return "arm"
    if re.search(r"\bamd\b|epyc", text):
        return "amd"
    if re.search(r"\bintel\b|xeon", text):
        return "intel"
    return None


def azure_vendor(name):
    # In Azure's documented naming convention, the feature suffix after the vCPU count starts
    # with "a" for AMD and "p" for Arm. No processor marker means Intel.
    base = re.sub(r"\s+v\d+\b", "", clean(name), flags=re.I)
    match = re.match(r"^[A-Za-z_]+(\d+)([A-Za-z_].*)?$", base)
    suffix = (match.group(2) if match else "") or ""
    suffix = suffix.lower().lstrip("_")
    if suffix.startswith("a"):
        return "amd"
    if suffix.startswith("p"):
        return "arm"
    return "intel"


def gcp_vendor(name):
    series = clean(name).lower().split("-")[0]
    if series in {"t2a", "c4a", "n4a", "a4x"}:
        return "arm"
    if series in {"n2d", "c2d", "c3d", "c4d", "n4d", "h4d", "g4"}:
        return "amd"
    return "intel"


def source_vendor(provider, name, processor):
    exact = vendor_from_processor(processor)
    if exact:
        return exact
    if provider == "azure":
        return azure_vendor(name)
    if provider == "gcp":
        return gcp_vendor(name)
    # AWS family suffixes are reliable when the processor cell is absent.
    family = clean(name).lower().split(".")[0]
    if re.search(r"(?:g|gd|gn|gb)$", family) or family.startswith(("a1", "hpc7g")):
        return "arm"
    if re.search(r"(?:a|ad)$", family):
        return "amd"
    return "intel"


def is_accelerated(provider, name, category, accelerator):
    text = " ".join([clean(name).lower(), clean(category).lower(), clean(accelerator).lower()])
    if provider == "aws":
        family = clean(name).lower().split(".")[0]
        return bool(
            re.match(r"^(?:p\d|g\d|dl\d|f\d|inf\d|trn\d|vt\d)", family)
            or "accelerated" in category.lower()
        )
    if provider == "azure":
        return bool(re.match(r"^N(?:C|D|V|G|P)", clean(name), re.I))
    return any(token in text for token in ("gpu", "accelerator", "a2-", "a3-", "a4x-"))


def extract_rows(document, provider, category, source_url, page_processor=""):
    parser = {"aws": aws_name, "azure": azure_name, "gcp": gcp_name}[provider]
    found = {}
    for table in document.xpath("//table"):
        matrix = table_matrix(table)
        columns = find_columns(matrix)
        if not columns:
            continue
        for row in matrix[columns["row"] + 1 :]:
            name = parser(cell(row, columns["name"]))
            vcpu = number(cell(row, columns["vcpu"]))
            memory = number(cell(row, columns["memory"]))
            if not name or vcpu is None or memory is None or vcpu <= 0 or memory <= 0:
                continue
            processor = clean(cell(row, columns["processor"])) or clean(page_processor)
            accelerator = clean(cell(row, columns["accelerator"]))
            found[key_for(name)] = {
                "provider": provider,
                "family": f"{provider.upper()} {category} (official)",
                "instance": name,
                "vcpu": vcpu,
                "memoryGb": memory,
                "sourceProcessor": processor,
                "sourceUrl": source_url,
                "sourceCategory": category,
                "sourceAccelerator": accelerator,
            }
    return list(found.values())


def scrape_aws():
    records = []
    for page, category in AWS_PAGES.items():
        url = f"{AWS_BASE}/{page}.html"
        records.extend(extract_rows(parse_document(fetch(url)), "aws", category, url))
    return dedupe(records)


def azure_category(url):
    path = urllib.parse.urlparse(url).path.lower()
    labels = {
        "general-purpose": "General Purpose",
        "compute-optimized": "Compute Optimized",
        "memory-optimized": "Memory Optimized",
        "storage-optimized": "Storage Optimized",
        "gpu-accelerated": "GPU Accelerated",
        "fpga-accelerated": "FPGA Accelerated",
        "high-performance-compute": "High Performance Computing",
    }
    return next((label for token, label in labels.items() if token in path), "Compute")


def discover_azure_pages(document, base_url):
    urls = set()
    for anchor in document.xpath("//a[@href]"):
        href = clean(anchor.get("href")).split("#", 1)[0]
        if not href:
            continue
        url = urllib.parse.urljoin(base_url, href)
        parsed = urllib.parse.urlparse(url)
        if parsed.netloc != "learn.microsoft.com":
            continue
        if "/azure/virtual-machines/sizes/" not in parsed.path:
            continue
        if parsed.path.rstrip("/").endswith("/sizes/overview"):
            continue
        # Stay inside the VM-size catalog. This excludes generic pricing, migration, disk,
        # networking, and API links that happen to be referenced from a series page.
        if not any(
            token in parsed.path
            for token in (
                "/general-purpose/",
                "/compute-optimized/",
                "/memory-optimized/",
                "/storage-optimized/",
                "/gpu-accelerated/",
                "/fpga-accelerated/",
                "/high-performance-compute/",
                "/previous-gen-",
            )
        ):
            continue
        urls.add(urllib.parse.urlunparse(parsed._replace(query="", fragment="")))
    return sorted(urls)


def processor_hint(document):
    text = clean(document.text_content())
    patterns = [
        r"(AMD EPYC [A-Za-z0-9 -]+)",
        r"(Intel Xeon [A-Za-z0-9 -]+)",
        r"(Microsoft Cobalt \d+)",
        r"(Ampere Altra [A-Za-z0-9 -]+)",
    ]
    hits = []
    for pattern in patterns:
        hits.extend(re.findall(pattern, text, flags=re.I))
    unique = []
    for hit in hits:
        value = clean(hit).rstrip(".,")
        if value and value.lower() not in {item.lower() for item in unique}:
            unique.append(value)
    return unique[0] if len(unique) == 1 else ""


def scrape_azure():
    overview_document = parse_document(fetch(AZURE_OVERVIEW))
    pending = set(discover_azure_pages(overview_document, AZURE_OVERVIEW))
    seen = set()
    records = []
    errors = []

    def one(url):
        try:
            document = parse_document(fetch(url))
            rows = extract_rows(
                    document,
                    "azure",
                    azure_category(url),
                    url,
                    page_processor=processor_hint(document),
                )
            return rows, discover_azure_pages(document, url)
        except Exception as exc:
            return exc

    # The overview links family pages and those pages link the actual per-series size tables.
    # Crawl both levels. Bounded concurrency keeps the refresh fast without hammering Learn.
    for _depth in range(3):
        urls = sorted(pending - seen)
        if not urls:
            break
        seen.update(urls)
        next_urls = set()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            jobs = {executor.submit(one, url): url for url in urls}
            for future in concurrent.futures.as_completed(jobs):
                result = future.result()
                if isinstance(result, Exception):
                    errors.append(f"{jobs[future]}: {result}")
                else:
                    rows, discovered = result
                    records.extend(rows)
                    next_urls.update(discovered)
        pending.update(next_urls)
        if len(pending) > 500:
            raise RuntimeError("Azure size crawl escaped the expected catalog boundary.")
    if not records:
        raise RuntimeError("Azure size index yielded no parseable shape tables.")
    # A few retired-series links can disappear between index updates. Do not fail the whole
    # refresh when most official pages succeeded, but preserve the count in run metadata.
    return dedupe(records), errors


def scrape_gcp():
    records = []
    for page, category in GCP_PAGES.items():
        url = f"{GCP_BASE}/{page}"
        document = parse_document(fetch(url))
        records.extend(
            extract_rows(
                document,
                "gcp",
                category,
                url,
                page_processor=processor_hint(document),
            )
        )
    return dedupe(records)


def dedupe(records):
    unique = {}
    for record in records:
        unique[(record["provider"], key_for(record["instance"]))] = record
    return sorted(unique.values(), key=lambda row: (row["provider"], row["instance"].lower()))


def match_policy(policy, provider, vendor, generation, processor):
    processor_text = clean(processor).lower()
    for rule in policy.get("processorSignatureRules", []):
        if rule.get("vendor") == vendor and re.search(rule["pattern"], processor_text, re.I):
            return rule["target"], f"processor signature /{rule['pattern']}/"
    rules = (
        policy.get("providerGenerationRules", {})
        .get(provider, {})
        .get(vendor, [])
    )
    for threshold, target in rules:
        if generation >= int(threshold):
            return target, f"{provider} {vendor} generation {generation} >= {threshold}"
    raise ValueError(f"No OCI generation rule for {provider}/{vendor}/{generation}")


def shape_limits(oci_shapes, target):
    wanted = target["shape"]
    rows = [row for row in oci_shapes.get("allShapes", []) if row.get("shape") == wanted]
    return rows[0] if rows else None


def oci_shape_generation(vendor, shape_name):
    name = clean(shape_name).lower()
    if vendor == "amd":
        match = re.search(r"\.e([456])(?:\.|$)", name)
        return int(match.group(1)) - 3 if match else 0
    if vendor == "intel":
        if "standard4" in name:
            return 2
        if "standard3" in name:
            return 1
        return 0
    if vendor == "arm":
        match = re.search(r"\.a([124])(?:\.|$)", name)
        return {"1": 1, "2": 2, "4": 3}.get(match.group(1), 0) if match else 0
    return 0


def fit_capacity(oci_shapes, vendor, target, ocpus, memory):
    preferred = shape_limits(oci_shapes, target)
    if preferred and ocpus <= preferred["maxOcpu"] and memory <= preferred["maxMem"]:
        return preferred, ""

    target_generation = oci_shape_generation(vendor, target["shape"])
    candidates = [
        row
        for row in oci_shapes.get("allShapes", [])
        if row.get("vendor") == vendor
        and row.get("tier") in {"flex", "baremetal"}
        and ocpus <= row.get("maxOcpu", 0)
        and memory <= row.get("maxMem", 0)
    ]
    candidates.sort(
        key=lambda row: (
            # Preserve processor generation before optimizing flex/bare-metal size:
            # same generation, then newer, then older only as a last resort.
            (
                0
                if oci_shape_generation(vendor, row.get("shape")) == target_generation
                else (
                    10 + oci_shape_generation(vendor, row.get("shape")) - target_generation
                    if oci_shape_generation(vendor, row.get("shape")) > target_generation
                    else 100 + target_generation - oci_shape_generation(vendor, row.get("shape"))
                )
            ),
            0 if row.get("tier") == "flex" else 1,
            row.get("maxMem", 0),
            row.get("maxOcpu", 0),
        )
    )
    if candidates:
        chosen = candidates[0]
        note = (
            f"Preferred generation {target['shape']} is too small; "
            f"capacity fits {chosen['shape']}."
        )
        return chosen, note
    vendor_rows = [
        row for row in oci_shapes.get("allShapes", []) if row.get("vendor") == vendor
    ]
    biggest = max(
        vendor_rows,
        key=lambda row: (row.get("maxMem", 0), row.get("maxOcpu", 0)),
        default={},
    )
    note = (
        f"{ocpus:g} OCPU / {memory:g} GB exceeds largest cataloged OCI {vendor} shape "
        f"{biggest.get('shape')} ({biggest.get('maxOcpu')} OCPU / {biggest.get('maxMem')} GB)."
    )
    return None, note


def enrich(record, policy, oci_shapes):
    provider = record["provider"]
    name = record["instance"]
    processor = record.get("sourceProcessor", "")
    vendor = source_vendor(provider, name, processor)
    generation = source_generation(provider, name)
    target_key, reason = match_policy(policy, provider, vendor, generation, processor)
    target = policy["ociTargets"][target_key]

    # Convert source logical CPUs to physical cores, then to target OCPUs. x86 source vCPUs
    # are treated as two threads/core. Arm source vCPUs are full cores in the documented
    # Graviton/Cobalt/Axion families. OCI A1 uses 1 core/OCPU; OCI A2/A4 use 2 cores/OCPU.
    source_threads_per_core = 1 if vendor == "arm" else 2
    source_cores = record["vcpu"] / source_threads_per_core
    ocpus = source_cores / float(target.get("coresPerOcpu") or 1)
    # Flexible shapes accept integral OCPU counts; never under-provision a fractional result.
    ocpus = float(math.ceil(ocpus))
    memory = float(record["memoryGb"])
    fitted, capacity_note = fit_capacity(oci_shapes, vendor, target, ocpus, memory)

    per_vcpu, per_gb = SOURCE_RATES[provider]
    approx_hourly = round(record["vcpu"] * per_vcpu + memory * per_gb, 6)
    label_by_key = {
        "e4-standard": "E4 Standard",
        "e5-standard": "E5 Standard",
        "e6-standard": "E6 Standard",
        "x9-standard": "X9 Standard",
        "x12-standard-ax": "X12 Standard Ax",
        "a1-standard": "Ampere A1",
        "a2-standard": "A2 Standard",
        "a4-standard": "A4 Standard",
    }
    is_gpu = is_accelerated(
        provider,
        name,
        record.get("sourceCategory", ""),
        record.get("sourceAccelerator", ""),
    )

    enriched = {
        "provider": provider,
        "family": record["family"],
        "instance": name,
        "key": key_for(name),
        "vcpu": float(record["vcpu"]),
        "memoryGb": memory,
        "sourceProcessor": processor or f"{vendor.upper()} processor (series documentation)",
        "sourceProcessorVendor": vendor,
        "sourceGeneration": generation,
        "ociShape": label_by_key[target_key],
        "ocpus": ocpus,
        "ramGb": memory,
        "ociVendor": vendor,
        "ociShapeName": (fitted or target)["shape"],
        "ociTier": (fitted or {}).get("tier"),
        "mappable": bool(fitted) and not is_gpu,
        "isGpu": is_gpu,
        "mappingApproximate": True,
        "mappingRule": reason,
        "mappingNote": (
            "Accelerator shape retained for recognition; GPU/accelerator equivalence requires "
            "model/count validation." if is_gpu else capacity_note
        ),
        "approxSourceHourly": approx_hourly,
        "approxSourceMonthly": round(approx_hourly * HOURS_PER_MONTH, 2),
        "sourcePriceReal": False,
        "addedFrom": "official-provider-docs",
        "addedOn": TODAY,
        "sourceUrl": record["sourceUrl"],
        "sourceRetrievedOn": TODAY,
    }
    if not fitted:
        enriched["mapFlag"] = capacity_note
    elif is_gpu:
        enriched["mapFlag"] = enriched["mappingNote"]
    return enriched


def workbook_metadata(path):
    if not path:
        return None
    workbook = Path(path).expanduser().resolve()
    if not workbook.exists():
        raise FileNotFoundError(f"Reference workbook not found: {workbook}")
    return {
        "name": workbook.name,
        "sha256": hashlib.sha256(workbook.read_bytes()).hexdigest(),
        "verifiedOn": TODAY,
        "role": "Baseline source for existing AWS/Azure/GCP compute mappings and formulas.",
    }


def validate_additions(additions):
    errors = []
    seen = set()
    official_hosts = {
        "aws": "docs.aws.amazon.com",
        "azure": "learn.microsoft.com",
        "gcp": "docs.cloud.google.com",
    }
    for row in additions:
        identity = (row["provider"], row["key"])
        if identity in seen:
            errors.append(f"duplicate shape: {identity}")
        seen.add(identity)
        if urllib.parse.urlparse(row["sourceUrl"]).netloc != official_hosts[row["provider"]]:
            errors.append(f"non-official source URL: {row['sourceUrl']}")
        if not (0 < row["vcpu"] <= 2048):
            errors.append(f"implausible vCPU count: {row['instance']}={row['vcpu']}")
        if not (0 < row["memoryGb"] <= 32768):
            errors.append(f"implausible memory: {row['instance']}={row['memoryGb']}")
        if not row.get("sourceProcessorVendor") in {"intel", "amd", "arm"}:
            errors.append(f"unknown processor vendor: {row['instance']}")
        if row.get("addedOn") != TODAY or row.get("sourceRetrievedOn") != TODAY:
            errors.append(f"missing current addition/retrieval date: {row['instance']}")
        if row.get("ocpus", 0) <= 0:
            errors.append(f"invalid OCI OCPU requirement: {row['instance']}")
    if errors:
        raise ValueError("Shape validation failed:\n" + "\n".join(errors[:50]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report additions without writing")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--provider",
        choices=("aws", "azure", "gcp", "all"),
        default="all",
        help="Provider to refresh (default: all)",
    )
    parser.add_argument(
        "--workbook",
        default="",
        help="Optional reference workbook path to checksum into metadata",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=0,
        help="Print this many missing-shape examples per provider",
    )
    args = parser.parse_args()

    payload = json.loads(MAP_PATH.read_text())
    policy = json.loads(POLICY_PATH.read_text())
    oci_shapes = json.loads(OCI_SHAPES_PATH.read_text())
    providers = ("aws", "azure", "gcp") if args.provider == "all" else (args.provider,)
    scraped = {}
    azure_errors = []
    try:
        if "aws" in providers:
            scraped["aws"] = scrape_aws()
        if "azure" in providers:
            scraped["azure"], azure_errors = scrape_azure()
        if "gcp" in providers:
            scraped["gcp"] = scrape_gcp()
    except Exception as exc:
        print(f"Could not refresh cloud shape catalog: {exc}", file=sys.stderr)
        print("No mapping data was changed.", file=sys.stderr)
        return 1

    existing = {row.get("key") or key_for(row.get("instance")) for row in payload["shapes"]}
    additions = []
    for provider in providers:
        for record in scraped.get(provider, []):
            if key_for(record["instance"]) in existing:
                continue
            enriched = enrich(record, policy, oci_shapes)
            additions.append(enriched)
            existing.add(enriched["key"])

    additions.sort(key=lambda row: (row["provider"], row["instance"].lower()))
    validate_additions(additions)
    counts = {provider: len(scraped.get(provider, [])) for provider in providers}
    added_counts = {
        provider: sum(1 for row in additions if row["provider"] == provider)
        for provider in providers
    }
    if not args.quiet:
        print("Official shapes found:", ", ".join(f"{k}={v}" for k, v in counts.items()))
        print("Missing shapes:", ", ".join(f"{k}={v}" for k, v in added_counts.items()))
        if azure_errors:
            print(f"Azure linked pages skipped after fetch errors: {len(azure_errors)}")
        for provider in providers:
            examples = [row for row in additions if row["provider"] == provider][: args.sample]
            for row in examples:
                print(
                    f"  {provider}: {row['instance']} {row['vcpu']:g} vCPU / "
                    f"{row['memoryGb']:g} GB, {row['sourceProcessorVendor']} gen "
                    f"{row['sourceGeneration']} -> {row['ociShapeName']} "
                    f"({row['ocpus']:g} OCPU)"
                )

    if args.dry_run:
        return 0

    payload["shapes"].extend(additions)
    meta = payload.setdefault("meta", {})
    meta["count"] = len(payload["shapes"])
    meta["remap"] = (
        "Preserve Intel/AMD/Arm architecture and select the closest OCI processor generation "
        "using data/processor_generation_mapping.json; then validate flex/bare-metal capacity."
    )
    meta["officialAdditions"] = sum(
        1 for row in payload["shapes"] if row.get("addedFrom") == "official-provider-docs"
    )
    limits = meta.setdefault("ociShapeLimits", {})
    for shape in oci_shapes.get("allShapes", []):
        if shape.get("tier") != "flex":
            continue
        limits.setdefault(shape["shape"], {}).update(
            {
                "maxOcpu": shape["maxOcpu"],
                "maxMemGb": shape["maxMem"],
                "vendor": shape["vendor"],
            }
        )
    meta["officialCatalogRefresh"] = {
        "refreshedOn": TODAY,
        "providers": list(providers),
        "officialShapesFound": counts,
        "added": added_counts,
        "method": "Additive scrape of official provider compute specification tables",
        "azureSkippedPageCount": len(azure_errors),
        "scope": "Published predefined VM/instance/machine shapes and compute series.",
        "exclusions": [
            "Parameterized custom/flexible source-cloud configurations without finite names.",
            "Region-specific availability and prices; new rows use explicitly non-real approximate source rates.",
            "Non-compute managed services.",
        ],
    }
    meta["processorGenerationPolicy"] = "data/processor_generation_mapping.json"
    reference = workbook_metadata(args.workbook)
    if reference:
        meta["referenceWorkbook"] = reference
    if additions:
        shutil.copy2(MAP_PATH, MAP_PATH.with_suffix(".json.bak"))
        MAP_PATH.write_text(json.dumps(payload, indent=1) + "\n")
    elif reference:
        MAP_PATH.write_text(json.dumps(payload, indent=1) + "\n")

    if not args.quiet:
        if additions:
            print(f"Added {len(additions)} shapes; {len(payload['shapes'])} total.")
        else:
            print("Already current.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
