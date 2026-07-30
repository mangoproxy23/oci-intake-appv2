#!/usr/bin/env python3
import collections
import functools
import warnings

warnings.filterwarnings("ignore", message="'cgi' is deprecated.*", category=DeprecationWarning)

# Install any missing dependency BEFORE the third-party imports below. Must come first:
# pandas/pypdf/Pillow may not exist yet in a fresh or stale virtualenv, and a missing
# Pillow used to kill the Full BOM export with a cryptic ImportError from inside openpyxl.
import bootstrap
bootstrap.ensure()
# Keep the Oracle SKU catalog current without anyone remembering to. Non-blocking and
# failure-tolerant - see bootstrap.refresh_catalog_if_stale.
bootstrap.refresh_catalog_if_stale()

import cgi
import gzip
import io
import json
import math
import mimetypes
import os
import re
import statistics
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pandas as pd
from pypdf import PdfReader

import bom_export
import aws_pricing


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"


def load_local_environment():
    """Load ignored local settings without overriding an explicitly exported value."""
    if os.environ.get("VERCEL"):
        return
    path = ROOT / ".env.local"
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            os.environ.setdefault(key, value)


load_local_environment()

UPLOAD_DIR = Path(
    os.environ.get(
        "UPLOAD_DIR",
        "/tmp/oci-intake-uploads" if os.environ.get("VERCEL") else str(ROOT / "uploads"),
    ),
)
UPLOAD_DIR.mkdir(exist_ok=True)

PORT = int(os.environ.get("PORT", "8787"))
HOURS_PER_MONTH = 730

DEFAULT_SHAPE_KEY = "e6-standard-ax"
INTAKE_MODE_ON_PREM = "on_prem"
INTAKE_MODE_CLOUD_BILL = "cloud_bill"
PROVIDER_AUTO = "auto"
DEFAULT_OPENAI_MODEL = "gpt-5-mini"
OPENAI_DISABLED_MESSAGE = "OpenAI API calls are temporarily disabled."
OPENAI_ACTIVE_FEATURES = ("inventory_scrub", "cloud_bill_mapping", "architecture", "foreign_bom")
MAX_DECOMPRESSED_UPLOAD_BYTES = 128 * 1024 * 1024


def openai_api_enabled():
    flag = clean_text(os.environ.get("OPENAI_API_ENABLED")).lower()
    if flag:
        return flag in {"1", "true", "yes", "on"}
    return True


def openai_api_configured():
    return bool(clean_text(os.environ.get("OPENAI_API_KEY")))


# ---------------------------------------------------------------------------------------------
# AGENT AUTHORITY POLICY
#
# This app's deterministic engine owns the numbers. An AI agent - the one already wired in here,
# or a future one added by another team - is ADVISORY everywhere except the architecture diagram.
#
#   advisory : the agent may propose, annotate, or fill a gap the deterministic engine left
#              empty, but it can never replace a deterministic result. If both produce an
#              answer, the deterministic one wins and the agent's is kept only as a note.
#   override : the agent may replace the generated result outright.
#
# Only "architecture" is override: the diagram is a drawing, so an agent rearranging or
# restyling it cannot change a price, a mapping, or a BOM figure. Everything that feeds a
# customer-facing number stays advisory, because an estimate has to be reproducible and
# defensible - the same upload must price the same way every time, whether or not an agent ran,
# and whether or not it was having a good day.
#
# Enforce with agent_may_override(domain) / resolve_agent_result(...) at every AI boundary
# rather than trusting call sites to remember the rule.
# ---------------------------------------------------------------------------------------------
AGENT_AUTHORITY = {
    "inventory_scrub": "advisory",     # parsing an uploaded inventory
    "cloud_bill_mapping": "advisory",  # AWS/Azure/GCP bill line -> OCI service
    "foreign_bom_mapping": "advisory", # a foreign OCI BOM line the SKU catalog didn't recognize
    "pricing": "advisory",             # rates, sizing, OCPU/RAM/storage math
    "shape_selection": "advisory",     # which OCI shape a workload lands on
    "bom_export": "advisory",          # workbook contents and totals
    "table_edit": "advisory",          # natural-language edits to the review table
    "architecture": "override",        # the diagram - agents may change this freely
}


def agent_may_override(domain):
    """True only for domains where an agent is allowed to replace the deterministic result."""
    return AGENT_AUTHORITY.get(domain) == "override"


# An agent can't overrule the engine, but it must be able to say "this looks badly wrong".
# These are the signals it can raise; anything here escalates the result for human review
# WITHOUT silently changing a number.
AGENT_MAJOR_ERROR_LEVELS = {"major", "critical", "blocker", "severe", "high"}


def agent_flags_major_error(agent):
    """True when an agent explicitly claims the deterministic result is badly wrong.

    Accepts either an explicit boolean (majorError / blocking) or a severity string, so a
    future agent doesn't have to match one exact schema to be heard."""
    if not isinstance(agent, dict):
        return False
    if agent.get("majorError") or agent.get("blocking") or agent.get("isMajorError"):
        return True
    for key in ("severity", "errorSeverity", "level", "confidenceFlag"):
        if normalize(str(agent.get(key) or "")) in AGENT_MAJOR_ERROR_LEVELS:
            return True
    return False


def resolve_agent_result(domain, deterministic, agent, is_empty=None):
    """Decide between the deterministic result and an agent's, per AGENT_AUTHORITY.

    Returns {result, source, note, review}:
      - override domain      -> the agent wins outright.
      - advisory + a real deterministic result -> the deterministic result stands. If the agent
        raised a MAJOR ERROR the result is unchanged but review=True, so the estimate is flagged
        for a human instead of the objection being swallowed.
      - advisory + nothing deterministic -> the agent fills the gap (it beats a blank).
    """
    empty = is_empty or (lambda v: v is None or v == [] or v == {} or v == "")
    if agent is None or empty(agent):
        return {"result": deterministic, "source": "deterministic", "note": "", "review": False}
    if agent_may_override(domain):
        return {"result": agent, "source": "agent", "review": False,
                "note": "Agent output applied (%s allows override)." % domain}
    major = agent_flags_major_error(agent)
    if not empty(deterministic):
        if major:
            return {
                "result": deterministic, "source": "deterministic", "review": True,
                "note": ("Agent flagged a MAJOR ERROR in %s. The deterministic result is kept "
                         "(it stays primary), and this estimate is flagged for review." % domain),
            }
        return {
            "result": deterministic, "source": "deterministic", "review": False,
            "note": ("Agent suggestion recorded but not applied: %s is advisory, so the "
                     "deterministic result stands." % domain),
        }
    return {
        "result": agent, "source": "agent", "review": major,
        "note": "Agent output used to fill a gap the deterministic engine left empty (%s)." % domain,
    }


LLM_WORKFLOW_CONTRACT = [
    "Upload step: inspect the spreadsheet, PDF, or bill export and identify each workload's core count, RAM, storage, application/workload name, and environment when present.",
    "CPU/core values from uploaded inventory are source vCPU/core counts; normalize them to OCI OCPUs for review using 2 vCPUs = 1 OCPU.",
    "Review step: the editable review table is the source of truth. User edits override values inferred during upload.",
    "Pricing step: price only the approved rows and edited values from the review table against the supplied OCI rate card and curated price catalog.",
    "Never invent OCI rates or use source-cloud spend as an OCI rate. Use the provided rate card/catalog for final pricing math and flag uncertain mappings for review.",
]


CANONICAL_INVENTORY_FIELDS = [
    {
        "key": "application_name",
        "label": "Application Name",
        "description": "Application, workload, server, VM, host, or inventory item name.",
        "aliases": [
            "application name",
            "application",
            "app name",
            "product",
            "workload",
            "application id",
            "app id",
            "appid",
            "tags.appid",
        ],
    },
    {
        "key": "machine_name",
        "label": "Machine Name",
        "description": "Server, VM, host, instance, machine, asset, or infrastructure resource name.",
        "aliases": [
            "machine name",
            "machine id",
            "server name",
            "hostname",
            "host name",
            "vm name",
            "virtual machine name",
            "instance name",
            "asset name",
            "resource id",
            "resourceid",
            "instance id",
            "tags.name",
            "tags.appid",
        ],
    },
    {
        "key": "environment",
        "label": "Environment",
        "description": "Environment such as prod, dev, qa, test, uat, disaster recovery, or staging.",
        "aliases": ["environment", "env", "tier", "stage", "lifecycle"],
    },
    {
        "key": "application_details",
        "label": "Application Details",
        "description": "Application description, type, function, business role, or notes.",
        "aliases": [
            "application details",
            "application type",
            "description",
            "business function",
            "app details",
            "role",
            "purpose",
            "resource id",
            "resourceid",
            "private ip",
            "configuration privateipaddress",
        ],
    },
    {
        "key": "application_details_application_version",
        "label": "Application Details: Application Version",
        "description": "Application, product, or platform version.",
        "aliases": ["application version", "app version", "version", "release"],
    },
    {
        "key": "application_details_operating_system",
        "label": "Application Details: Operating System",
        "description": "Operating system name and version.",
        "aliases": ["operating system", "os", "os version", "platform", "tags.os"],
    },
    {
        "key": "application_details_number_of_servers",
        "label": "Application Details: Number of Servers",
        "description": "How many application servers, VMs, hosts, nodes, or instances this row represents.",
        "aliases": ["number of servers", "server count", "servers", "instances", "nodes", "vm count", "quantity", "qty"],
    },
    {
        "key": "application_details_number_of_cpu_cores_per_server",
        "label": "Application Details: OCPUs",
        "description": "OCPU count per server. Uploaded spreadsheet CPU values are assumed to be vCPUs and converted using 2 vCPUs = 1 OCPU.",
        "aliases": [
            "number of cpu cores per server",
            "number of cpus",
            "cpu/vcpu",
            "cpu vcpu",
            "cpu",
            "cpus",
            "cpu count",
            "v cpu",
            "vcpu",
            "vcpus",
            "vcpu count",
            "virtual cpu",
            "virtual cpus",
            "cores",
            "core count",
            "cpu cores",
            "processor cores",
            "processors",
            "num cpu",
            "num cpus",
            "cpu cores per vm",
            "vcpus per vm",
        ],
    },
    {
        "key": "application_details_memory_per_server_gb",
        "label": "Application Details: Memory per server (GB)",
        "description": "RAM or memory per server in GB.",
        "aliases": [
            "memory per server",
            "memory per server gb",
            "memory per vm",
            "memory in gb",
            "memory",
            "memory gb",
            "memory (gb)",
            "ram",
            "ram gb",
            "ram (gb)",
            "gb ram",
            "mem",
            "mem gb",
            "memory size",
            "ram size",
        ],
    },
    {
        "key": "application_details_chipset",
        "label": "Application Details: Chipset",
        "description": "CPU chipset, processor family, architecture, or platform family.",
        "aliases": ["chipset", "processor family", "cpu type", "architecture", "processor", "hardware family"],
    },
    {
        "key": "application_details_local_storage_gb",
        "label": "Application Details: Local Storage (GB)",
        "description": "Local VM disk, OS disk, data disk, or directly attached block storage in GB.",
        "aliases": [
            "local storage",
            "storage",
            "storage gb",
            "total storage",
            "total storage gb",
            "allocated storage",
            "allocated storage gb",
            "disk gb",
            "disk size",
            "disk capacity",
            "data disk",
            "os disk",
            "block storage",
        ],
    },
    {
        "key": "application_details_shared_storage_gb",
        "label": "Application Details: Shared Storage (GB)",
        "description": "Shared, NAS, NFS, SMB, ETL, or file storage in GB.",
        "aliases": ["shared storage", "nas", "nfs", "file storage", "shared disk", "smb"],
    },
    {
        "key": "database_details_number_of_database_servers",
        "label": "Database Details: Number of Database Servers",
        "description": "How many database servers, database nodes, or DB instances this row represents.",
        "aliases": ["number of database servers", "database servers", "db servers", "db nodes", "database instances"],
    },
    {
        "key": "database_details_number_of_cpu_cores_per_server",
        "label": "Database Details: OCPUs",
        "description": "Database OCPU count per server. Uploaded spreadsheet CPU values are assumed to be vCPUs and converted using 2 vCPUs = 1 OCPU.",
        "aliases": [
            "database cpu",
            "db cpu",
            "database cpus",
            "db cpus",
            "number of cpu cores per server",
            "number of cpus",
            "database cores",
            "db cores",
            "database vcpu",
            "database vcpus",
            "db vcpu",
            "db vcpus",
            "database cpu count",
            "db cpu count",
            "cpu cores per server",
        ],
    },
    {
        "key": "database_details_memory_per_server_gb",
        "label": "Database Details: Memory per server (GB)",
        "description": "Database RAM or memory per DB server in GB.",
        "aliases": [
            "database memory",
            "db memory",
            "database memory gb",
            "db memory gb",
            "database ram",
            "db ram",
            "database ram gb",
            "db ram gb",
        ],
    },
    {
        "key": "database_details_total_allocated_storage_gb",
        "label": "Database Details: Total Allocated Storage (GB)",
        "description": "Database storage, allocated DB storage, datafile size, or total database disk in GB.",
        "aliases": [
            "database total allocated storage",
            "db total allocated storage",
            "database storage",
            "db storage",
            "database size",
            "db size",
            "database total storage",
            "db total storage",
        ],
    },
]

FULL_SERVICE_BETA_FIELDS = [
    {
        "key": "source_provider",
        "label": "Source Provider",
        "description": "Source platform such as AWS, Azure, GCP, OCI, VMware, on-prem, or a billing/export vendor.",
        "aliases": ["provider", "source provider", "cloud provider", "vendor", "publisher", "billing provider", "cloud"],
    },
    {
        "key": "source_service",
        "label": "Source Service",
        "description": "Source service family or meter category such as EC2, S3, EBS, Azure VM, Blob Storage, GCP Compute, or NAS.",
        "aliases": [
            "service",
            "service name",
            "service family",
            "meter category",
            "product code",
            "lineitem productcode",
            "product/service",
            "resource type",
        ],
    },
    {
        "key": "source_product",
        "label": "Source Product",
        "description": "Detailed product, SKU, meter, operation, usage type, or item description from a cloud bill or CMDB.",
        "aliases": [
            "product",
            "product name",
            "sku",
            "sku name",
            "meter name",
            "meter sub category",
            "meter subcategory",
            "usage type",
            "operation",
            "item description",
            "line item description",
            "resource description",
        ],
    },
    {
        "key": "source_region",
        "label": "Source Region",
        "description": "Source cloud region, datacenter, location, or availability zone.",
        "aliases": ["region", "location", "availability zone", "az", "datacenter", "data center", "resource location"],
    },
    {
        "key": "usage_quantity",
        "label": "Usage Quantity",
        "description": "Consumed usage amount from a bill or inventory export.",
        "aliases": ["usage quantity", "usage amount", "consumed quantity", "quantity", "usagequantity", "usage amount", "usage"],
    },
    {
        "key": "usage_unit",
        "label": "Usage Unit",
        "description": "Unit for the consumed usage amount, such as GB-month, TB-month, request, hour, vCPU-hour, or instance-month.",
        "aliases": ["usage unit", "unit", "unit of measure", "pricing unit", "meter unit", "uom", "usageunit"],
    },
    {
        "key": "source_monthly_cost",
        "label": "Source Monthly Cost",
        "description": "Monthly source-cloud or on-prem cost when present; used for review, not as an OCI rate.",
        "aliases": ["cost", "monthly cost", "pretax cost", "pre tax cost", "unblended cost", "amortized cost", "charge", "amount"],
    },
    {
        "key": "oci_service_category",
        "label": "OCI Service Category",
        "description": "Editable target OCI service category inferred from the source row.",
        "aliases": ["oci service", "oci service category", "target service", "oracle service", "oracle cloud service"],
    },
    {
        "key": "oci_product",
        "label": "OCI Product",
        "description": "Editable target OCI product or price-list item inferred from the source row.",
        "aliases": ["oci product", "target product", "oracle product", "mapped product", "mapped sku", "target sku"],
    },
    {
        "key": "mapping_confidence",
        "label": "Mapping Confidence",
        "description": "Confidence score or review status for the full-service beta mapping.",
        "aliases": ["mapping confidence", "confidence", "match confidence", "mapping status", "review status"],
    },
]

CLOUD_BILL_FIELDS = [
    {
        "key": "source_provider",
        "label": "Provider",
        "description": "Detected source provider: AWS, Azure, or GCP.",
        "aliases": ["provider", "cloud provider", "vendor", "publisher"],
    },
    {
        "key": "source_account",
        "label": "Account / project",
        "description": "AWS account, Azure subscription, GCP project, or billing account.",
        "aliases": [
            "account",
            "account id",
            "usage account id",
            "subscription id",
            "subscription name",
            "project id",
            "project name",
            "billing account id",
        ],
    },
    {
        "key": "source_service",
        "label": "Source service",
        "description": "Cloud service family such as EC2, S3, Azure VMs, Blob Storage, GCP Compute, or Cloud Storage.",
        "aliases": [
            "service",
            "service name",
            "service description",
            "meter category",
            "product code",
            "product name",
            "consumed service",
        ],
    },
    {
        "key": "source_product",
        "label": "SKU / meter",
        "description": "Detailed SKU, meter, usage type, operation, or line item description.",
        "aliases": [
            "sku",
            "sku description",
            "meter name",
            "meter subcategory",
            "meter sub category",
            "usage type",
            "operation",
            "line item description",
            "resource description",
        ],
    },
    {
        "key": "source_region",
        "label": "Region",
        "description": "Cloud region, resource location, or availability zone.",
        "aliases": ["region", "location", "resource location", "availability zone", "zone"],
    },
    {
        "key": "usage_quantity",
        "label": "Usage quantity",
        "description": "Consumed usage amount from the source bill.",
        "aliases": ["usage amount", "usage quantity", "quantity", "qty", "consumed quantity", "usage.amount"],
    },
    {
        "key": "usage_unit",
        "label": "Usage unit",
        "description": "Unit of measure such as GB-month, vCPU-hour, request, hour, or quantity unit.",
        "aliases": ["usage unit", "unit", "unit of measure", "pricing unit", "usage.unit"],
    },
    {
        "key": "resource_ocpus",
        "label": "OCPUs",
        "description": "Normalized OCPU quantity inferred from explicit OCPU/vCPU bill lines or recognizable VM instance types.",
        "aliases": [
            "ocpu",
            "ocpus",
            "ocpu count",
            "ocpu quantity",
            "vcpu",
            "vcpus",
            "vcpu count",
            "cpu",
            "cpus",
            "cpu count",
            "core",
            "cores",
            "core count",
        ],
    },
    {
        "key": "resource_memory_gb",
        "label": "RAM (GB)",
        "description": "Normalized memory/RAM quantity in GB inferred from explicit memory bill lines or recognizable VM instance types.",
        "aliases": [
            "ram",
            "ram gb",
            "ram (gb)",
            "memory",
            "memory gb",
            "memory (gb)",
            "memory quantity",
            "gb ram",
            "mem gb",
        ],
    },
    {
        "key": "source_monthly_cost",
        "label": "Source cost",
        "description": "Source-cloud cost for review context only; OCI estimate uses OCI rates.",
        "aliases": ["cost", "source cost", "unblended cost", "net unblended cost", "pretax cost", "cost in billing currency"],
    },
    {
        "key": "source_currency",
        "label": "Currency",
        "description": "Source bill currency.",
        "aliases": ["currency", "billing currency", "billing currency code", "pricing currency"],
    },
    {
        "key": "source_period",
        "label": "Billing period",
        "description": "Usage or billing month/date from the source bill.",
        "aliases": ["billing period", "usage start date", "usage date", "date", "month"],
    },
    {
        "key": "source_tags",
        "label": "Tags / labels",
        "description": "Source tags, labels, dimensions, or resource metadata that identify workload ownership.",
        "aliases": ["tags", "labels", "resource tags", "system tags", "additional info"],
    },
    {
        "key": "oci_service_category",
        "label": "OCI service",
        "description": "Editable target OCI service category inferred from the source bill row.",
        "aliases": ["oci service", "target service", "oracle service", "service category"],
    },
    {
        "key": "oci_product",
        "label": "OCI product / SKU",
        "description": "Editable target OCI product or price-list item inferred from the source bill row.",
        "aliases": ["oci product", "target product", "oracle product", "mapped product", "mapped sku", "target sku"],
    },
    {
        "key": "mapping_confidence",
        "label": "Mapping confidence",
        "description": "Confidence score or review status for the OCI mapping.",
        "aliases": ["mapping confidence", "confidence", "match confidence", "review status"],
    },
]


def normalize_intake_mode(value):
    text = normalize(value)
    if text in {"cloud bill", "cloud billing", "cloud", "bill", "aws", "azure", "gcp"}:
        return INTAKE_MODE_CLOUD_BILL
    return INTAKE_MODE_ON_PREM


def normalize_provider_hint(value):
    text = normalize(value)
    if text in {"aws", "amazon", "amazon web services"}:
        return "aws"
    if text in {"azure", "microsoft", "microsoft azure"}:
        return "azure"
    if text in {"gcp", "google", "google cloud", "google cloud platform"}:
        return "gcp"
    return PROVIDER_AUTO


def guess_provider_from_filename(filename):
    """Guess the cloud provider from an uploaded bill's filename. Returns
    'aws' / 'azure' / 'gcp', or PROVIDER_AUTO when ambiguous or unknown."""
    text = normalize(filename or "")
    signals = {
        "aws": ["aws", "amazon", "ec2", "cur", "cost and usage", "costexplorer", "cost explorer"],
        "azure": ["azure", "microsoft", "msft", "ea ", "enterprise agreement", "consumption"],
        "gcp": ["gcp", "google", "bigquery", "gce", "cloud billing"],
    }
    matched = [p for p, terms in signals.items() if any(term in text for term in terms)]
    return matched[0] if len(matched) == 1 else PROVIDER_AUTO


def inventory_fields(full_service_beta=False, intake_mode=INTAKE_MODE_ON_PREM):
    if intake_mode == INTAKE_MODE_CLOUD_BILL:
        return CLOUD_BILL_FIELDS
    if full_service_beta:
        return [*CANONICAL_INVENTORY_FIELDS, *FULL_SERVICE_BETA_FIELDS]
    return CANONICAL_INVENTORY_FIELDS


CANONICAL_FIELD_BY_KEY = {
    field["key"]: field
    for field in [*CANONICAL_INVENTORY_FIELDS, *FULL_SERVICE_BETA_FIELDS, *CLOUD_BILL_FIELDS]
}
NUMERIC_FIELD_KEYS = {
    "application_details_number_of_servers",
    "application_details_number_of_cpu_cores_per_server",
    "application_details_memory_per_server_gb",
    "application_details_local_storage_gb",
    "application_details_shared_storage_gb",
    "database_details_number_of_database_servers",
    "database_details_number_of_cpu_cores_per_server",
    "database_details_memory_per_server_gb",
    "database_details_total_allocated_storage_gb",
    "resource_ocpus",
    "resource_memory_gb",
}
CPU_FIELD_KEYS = {
    "application_details_number_of_cpu_cores_per_server",
    "database_details_number_of_cpu_cores_per_server",
    "resource_ocpus",
}
SIZE_FIELD_KEYS = {
    "application_details_memory_per_server_gb",
    "application_details_local_storage_gb",
    "application_details_shared_storage_gb",
    "database_details_memory_per_server_gb",
    "database_details_total_allocated_storage_gb",
    "resource_memory_gb",
}
FULL_SERVICE_FIELD_KEYS = {field["key"] for field in FULL_SERVICE_BETA_FIELDS}
CLOUD_BILL_FIELD_KEYS = {field["key"] for field in CLOUD_BILL_FIELDS}
SOURCE_SERVICE_FIELD_KEYS = FULL_SERVICE_FIELD_KEYS | CLOUD_BILL_FIELD_KEYS

SHAPE_DEFINITIONS = [
    {
        "key": "e4-standard",
        "label": "E4 Standard",
        "shortLabel": "E4",
        "family": "AMD flexible shape",
        "processorVendor": "amd",
        "computeSku": "B97384",
        "memorySku": "B97385",
        "computeRate": 0.0250,
        "memoryRate": 0.0015,
        "summary": "Lower memory rate with a mid-tier OCPU rate for steady general workloads.",
        "accent": "#2f5d28",
    },
    {
        "key": "e5-standard",
        "label": "E5 Standard",
        "shortLabel": "E5",
        "family": "AMD flexible shape",
        "processorVendor": "amd",
        "computeSku": "B97384",
        "memorySku": "B97385",
        "computeRate": 0.0300,
        "memoryRate": 0.0020,
        "summary": "Current generation AMD shape with identical E6 Standard compute and memory rates.",
        "accent": "#365f1c",
    },
    {
        "key": "e6-standard",
        "label": "E6 Standard",
        "shortLabel": "E6",
        "family": "AMD flexible shape",
        "processorVendor": "amd",
        "computeSku": "B111129",
        "memorySku": "B111130",
        "computeRate": 0.0300,
        "memoryRate": 0.0020,
        "summary": "AMD E6 general-purpose flex shape (non-Ax). OCPU B111129 ($0.03/OCPU-hr), Memory B111130 ($0.002/GB-hr).",
        "accent": "#2d6a2a",
    },
    {
        "key": DEFAULT_SHAPE_KEY,
        "label": "E6 Standard Ax",
        "shortLabel": "E6 Ax",
        "family": "AMD Ax flexible shape",
        "processorVendor": "amd",
        "computeSku": "B112530",
        "memorySku": "B112531",
        "computeRate": 0.0138,
        "memoryRate": 0.0108,
        "summary": "Lower OCPU rate and higher memory rate; useful when compute-heavy rows dominate.",
        "accent": "#164f68",
    },
    {
        "key": "x9-standard",
        "label": "X9 Standard",
        "shortLabel": "X9",
        "family": "Virtual Machine Standard",
        "processorVendor": "intel",
        "computeSku": "X9-OCPU",
        "memorySku": "X9-MEMORY",
        "computeRate": 0.0400,
        "memoryRate": 0.0015,
        "summary": "Standard X9 VM shape using the public OCPU and memory rates from the supplied rate card.",
        "accent": "#7a3126",
    },
    {
        "key": "x12-standard-ax",
        "label": "X12 Standard Ax",
        "shortLabel": "X12 Ax",
        "family": "Intel Ax flexible shape",
        "processorVendor": "intel",
        "computeSku": "X12AX-OCPU",
        "memorySku": "X12AX-MEMORY",
        "computeRate": 0.0119,
        "memoryRate": 0.0114,
        "summary": "Standard X12 Ax shape using the public OCPU and memory rates from the supplied rate card.",
        "accent": "#8a6f24",
    },
    {
        "key": "a4-standard-ax",
        "label": "A4 Standard Ax",
        "shortLabel": "A4 Ax",
        "family": "Ampere Ax flexible shape",
        "processorVendor": "arm",
        "computeSku": "B112532",
        "memorySku": "B112533",
        "computeRate": 0.0190,
        "memoryRate": 0.0084,
        "summary": "Newest AmpereOne M (Arm) Ax shape; 1 OCPU = 2 Arm cores. OCPU B112532, Memory B112533.",
        "accent": "#3d6b4f",
    },
    {
        "key": "a4-standard",
        "label": "A4 Standard",
        "shortLabel": "A4",
        "family": "AmpereOne M flexible shape",
        "processorVendor": "arm",
        "computeSku": "B112145",
        "memorySku": "B112146",
        "computeRate": 0.0138,
        "memoryRate": 0.0027,
        "summary": "OCI - Compute - Standard - A4; AmpereOne M (Arm), 1 OCPU = 2 cores. OCPU B112145, Memory B112146.",
        "accent": "#347a5c",
    },
    {
        "key": "a2-standard",
        "label": "A2 Standard",
        "shortLabel": "A2",
        "family": "AmpereOne flexible shape",
        "processorVendor": "arm",
        "computeSku": "B109529",
        "memorySku": "B109530",
        "computeRate": 0.0140,
        "memoryRate": 0.0020,
        "summary": "Compute - Standard - A2; AmpereOne (Arm), 1 OCPU = 2 cores. OCPU B109529, Memory B109530.",
        "accent": "#356055",
    },
    {
        "key": "a1-standard",
        "label": "Ampere A1",
        "shortLabel": "A1",
        "family": "Ampere Altra flexible shape",
        "processorVendor": "arm",
        "computeSku": "B93297",
        "memorySku": "B93298",
        "computeRate": 0.0100,
        "memoryRate": 0.0015,
        "summary": "Compute - Standard - A1; Ampere Altra (Arm), 1 OCPU = 1 core. OCPU B93297, Memory B93298.",
        "accent": "#2f5d52",
    },
    # ---- Bare metal -------------------------------------------------------------------
    # Whole physical servers: fixed OCPU/RAM, metered on the SAME per-OCPU-hour and
    # per-GB-hour SKUs as the matching flex shape (confirmed against an Oracle estimator
    # export). Selecting one prices each workload as whole servers - bare metal is sold as a
    # complete box, so a workload that needs part of one still pays for all of it.
    {
        "key": "bm-e6-ax-192",
        "label": "BM.Standard.E6.Ax.192",
        "shortLabel": "BM E6 Ax",
        "family": "AMD bare metal",
        "processorVendor": "amd",
        "computeSku": "B112530",
        "memorySku": "B112531",
        "computeRate": 0.0138,
        "memoryRate": 0.0108,
        "bareMetal": True,
        "bmOcpu": 192,
        "bmMemoryGb": 1536,
        "summary": "Bare metal E6 Acceleron: 192 OCPU / 1,536 GB. OCPU B112530, Memory B112531.",
        "accent": "#7a3126",
    },
    {
        "key": "bm-e6-256",
        "label": "BM.Standard.E6.256",
        "shortLabel": "BM E6",
        "family": "AMD bare metal",
        "processorVendor": "amd",
        "computeSku": "B111129",
        "memorySku": "B111130",
        "computeRate": 0.0300,
        "memoryRate": 0.0020,
        "bareMetal": True,
        "bmOcpu": 256,
        "bmMemoryGb": 3072,
        "summary": "Bare metal E6 Standard: 256 OCPU / 3,072 GB. OCPU B111129, Memory B111130.",
        "accent": "#8c3a2a",
    },
    {
        "key": "bm-x12-ax-120",
        "label": "BM.Standard4.Ax.120",
        "shortLabel": "BM X12 Ax",
        "family": "Intel bare metal",
        "processorVendor": "intel",
        "computeSku": "B112141",
        "memorySku": "B112142",
        "computeRate": 0.0119,
        "memoryRate": 0.0114,
        "bareMetal": True,
        "bmOcpu": 120,
        "bmMemoryGb": 1152,
        "summary": "Bare metal X12 Acceleron (Intel): 120 OCPU / 1,152 GB. OCPU B112141, Memory B112142.",
        "accent": "#1f4e79",
    },
    {
        "key": "bm-a4-ax-48",
        "label": "BM.Standard.A4.Ax.48",
        "shortLabel": "BM A4 Ax",
        "family": "Ampere bare metal",
        "processorVendor": "arm",
        "computeSku": "B112532",
        "memorySku": "B112533",
        "computeRate": 0.0190,
        "memoryRate": 0.0084,
        "bareMetal": True,
        "bmOcpu": 48,
        "bmMemoryGb": 768,
        "summary": "Bare metal A4 Acceleron (Arm): 48 OCPU / 768 GB. OCPU B112532, Memory B112533.",
        "accent": "#2f5d52",
    },
    {
        "key": "bm-a4-48",
        "label": "BM.Standard.A4.48",
        "shortLabel": "BM A4",
        "family": "Ampere bare metal",
        "processorVendor": "arm",
        "computeSku": "B112145",
        "memorySku": "B112146",
        "computeRate": 0.0138,
        "memoryRate": 0.0027,
        "bareMetal": True,
        "bmOcpu": 48,
        "bmMemoryGb": 768,
        "summary": "Bare metal A4 Standard (Arm): 48 OCPU / 768 GB. OCPU B112145, Memory B112146.",
        "accent": "#356055",
    },
]

SHAPE_LOOKUP = {shape["key"]: shape for shape in SHAPE_DEFINITIONS}

# Best (newest) OCI shape per CPU vendor, used by the "Auto" mapping mode.
BEST_SHAPE_BY_VENDOR = {"amd": "e6-standard-ax", "intel": "x12-standard-ax", "arm": "a4-standard-ax"}

# Best Match -> equivalent-generation OCI shape for the source instance's chip era.
# The defaults keep startup resilient, but the auditable source of truth is
# data/processor_generation_mapping.json. Keeping the policy in data lets the shape refresh
# script and the runtime use the same validated Intel/AMD/Arm generation thresholds.
_DEFAULT_EQUIV_GEN_MAP = {
    ("aws", "amd"): [(8, "e6-standard"), (7, "e5-standard"), (6, "e4-standard"), (0, "e4-standard")],
    ("azure", "amd"): [(7, "e6-standard"), (6, "e5-standard"), (5, "e4-standard"), (0, "e4-standard")],
    ("gcp", "amd"): [(4, "e6-standard"), (3, "e5-standard"), (0, "e4-standard")],
    ("aws", "intel"): [(7, "x12-standard-ax"), (0, "x9-standard")],
    ("azure", "intel"): [(6, "x12-standard-ax"), (0, "x9-standard")],
    ("gcp", "intel"): [(4, "x12-standard-ax"), (0, "x9-standard")],
    ("aws", "arm"): [(8, "a4-standard"), (7, "a2-standard"), (0, "a1-standard")],
    ("azure", "arm"): [(7, "a4-standard"), (6, "a2-standard"), (0, "a1-standard")],
    ("gcp", "arm"): [(4, "a4-standard"), (0, "a2-standard")],
}


def _load_equiv_gen_map():
    path = Path(__file__).resolve().parent / "data" / "processor_generation_mapping.json"
    try:
        payload = json.loads(path.read_text())
        rules = payload.get("providerGenerationRules") or {}
        loaded = {}
        for provider, vendors in rules.items():
            for vendor, entries in (vendors or {}).items():
                loaded[(provider, vendor)] = [
                    (int(threshold), shape_key) for threshold, shape_key in entries
                ]
        return loaded or _DEFAULT_EQUIV_GEN_MAP
    except Exception:
        return _DEFAULT_EQUIV_GEN_MAP


EQUIV_GEN_MAP = _load_equiv_gen_map()


def equivalent_gen_shape_key(provider, vendor, generation):
    """Map a source instance's generation to the equivalent-generation OCI shape key.
    Falls back to the newest shape for the vendor when there's no specific rule."""
    table = EQUIV_GEN_MAP.get((provider, vendor))
    if table:
        for threshold, shape_key in table:
            if (generation or 0) >= threshold:
                return shape_key
    return BEST_SHAPE_BY_VENDOR.get(vendor)


# OCI shape generation rank within a vendor (higher = newer) for rightsize gen-gap math.
OCI_GEN_RANK = {
    "e4-standard": 1, "e5-standard": 2, "e6-standard": 3, "e6-standard-ax": 3,
    "x9-standard": 1, "x12-standard-ax": 2,
    "a1-standard": 1, "a2-standard": 2, "a4-standard": 3, "a4-standard-ax": 3,
}
BEST_GEN_RANK = {"amd": 3, "intel": 2, "arm": 3}


# Ax shapes deepen the rightsize cut for older source instances: the base trim is
# multiplied by 2 for each generation behind the first (1 gen = base, 2 gens = base x2,
# 3 gens = base x4, ...). The regular E6 stays flat regardless of generation gap.
AX_GEN_MULTIPLIER = 2.0


def rightsize_plan(shape_key, src_rec):
    """Return (ocpu_rate, ram_rate, gens_behind) for the gen-gap rightsize, or None.

    Only the Ax shapes and the regular E6 are rightsized:
      - Ax shapes (E6 Ax / X12 Ax / A4 Ax): base 15% OCPU / 20% RAM, then multiplied by
        2 for each generation behind the first (compounding with the source instance's
        generation gap), capped at 95%.
      - Regular E6 (non-Ax): flat 10% OCPU and 15% RAM whenever it qualifies (no scaling).
    "Generations behind" is how far the source instance's generation sits below the
    newest OCI generation for the chosen shape's vendor. On-prem / unidentifiable
    source data is treated as exactly one generation behind (so Ax scaling is neutral
    there and only deepens for a bill whose instances are genuinely older)."""
    key = str(shape_key or "")
    if key.endswith("-ax"):
        ocpu_rate, ram_rate = 0.15, 0.20
        is_ax = True
    elif key == "e6-standard":
        ocpu_rate, ram_rate = 0.10, 0.15
        is_ax = False
    else:
        return None
    vendor = SHAPE_LOOKUP.get(shape_key, {}).get("processorVendor")
    provider = (src_rec or {}).get("provider")
    if src_rec and provider in ("aws", "azure", "gcp"):
        gen = _instance_generation(provider, src_rec.get("instance"))
        src_key = equivalent_gen_shape_key(provider, vendor, gen)
        src_rank = OCI_GEN_RANK.get(src_key, BEST_GEN_RANK.get(vendor, 1))
        gens_behind = max(0, BEST_GEN_RANK.get(vendor, src_rank) - src_rank)
    else:
        gens_behind = 1  # on-prem / unknown: assume one generation behind
    if gens_behind <= 0:
        return None
    # Ax: deepen the cut by x1.5 per generation beyond the first (E6 stays flat).
    if is_ax and gens_behind > 1:
        factor = AX_GEN_MULTIPLIER ** (gens_behind - 1)
        ocpu_rate = min(0.95, ocpu_rate * factor)
        ram_rate = min(0.95, ram_rate * factor)
    return (ocpu_rate, ram_rate, gens_behind)

STORAGE_RATE_ITEMS = [
    {
        "sku": "B91961",
        "description": "Block Volume Storage (GB-mo)",
        "unit": "GB-month",
        "rate": 0.0255,
        "notes": "VM OS / data disks",
    },
    {
        "sku": "B91962",
        "description": "Block Volume Performance Units (per GB-mo)",
        "unit": "Performance Units/GB-month",
        "rate": 0.0017,
        "notes": "Balanced performance: 10 performance units per block-volume GB",
    },
    {
        "sku": "B89057",
        "description": "File Storage (GB-mo)",
        "unit": "GB-month",
        "rate": 0.3000,
        "notes": "NAS / ETL shared file storage",
    },
]

# Block-volume performance units billed per GB of block storage (BOM script uses Balanced = 10).
BLOCK_PERFORMANCE_UNITS_PER_GB = 10


def storage_rate(sku):
    """The app's authoritative rate for a storage SKU.

    Single source of truth for anything that prices storage - the pricing engine AND the
    Full BOM export. The exported Rate Card used to carry the source template's own
    numbers, so a rate change in the app would silently leave the deliverable on the old
    ones. Everything now reads from here.
    """
    for item in STORAGE_RATE_ITEMS:
        if item["sku"] == sku:
            return float(item["rate"])
    raise KeyError(f"No storage rate for SKU {sku}")

# Windows OS licensing (BOM script): charged per OCPU-hour for rows detected as Windows.
WINDOWS_LICENSE_SKU = "B88318"
WINDOWS_LICENSE_RATE = 0.0920

# "Rightsize and Cut Costs": follows the Acceleron optimizer methodology - map to the OCI
# target memory ratio of 8 GB per OCPU instead of carrying over the source's (often
# over-provisioned) memory. Memory is capped at ocpus x 8 GB; OCPUs are unchanged.
RIGHTSIZE_MEM_PER_OCPU = 8.0
WINDOWS_LICENSE_ITEM = {
    "sku": WINDOWS_LICENSE_SKU,
    "description": "Compute - Windows OS (OCPU Per Hour)",
    "unit": "OCPU-hour",
    "rate": WINDOWS_LICENSE_RATE,
    "notes": "Windows OS licensing uses each row's Hours Running value (730 by default).",
}

FULL_SERVICE_RATE_ITEMS = [
    {
        "key": "object_storage_standard",
        "sku": "B91628",
        "description": "Object Storage - Standard storage (GB-mo)",
        "unit": "GB-month",
        "rate": 0.0255,
        "category": "Storage",
        "notes": "Maps AWS S3 Standard, Azure Blob hot/standard, GCP Cloud Storage standard, and generic object stores.",
        "keywords": ["object", "s3", "blob", "bucket", "gcs", "cloud storage", "standard storage"],
    },
    {
        "key": "object_storage_infrequent",
        "sku": "B93000",
        "description": "Object Storage - Infrequent Access storage (GB-mo)",
        "unit": "GB-month",
        "rate": 0.0100,
        "category": "Storage",
        "notes": "Maps AWS S3 Standard-IA/One Zone-IA, Azure Blob Cool, GCP Nearline, and equivalent infrequent-access capacity.",
        "keywords": ["infrequent access", "standard-ia", "one zone-ia", "cool blob", "cool tier", "nearline"],
    },
    {
        "key": "object_storage_infrequent_retrieval",
        "sku": "B93001",
        "description": "Object Storage - Infrequent Access retrieval",
        "unit": "GB retrieved",
        "rate": 0.0100,
        "category": "Storage",
        "notes": "Data retrieved from OCI Object Storage Infrequent Access.",
        "keywords": ["infrequent access retrieval", "standard-ia retrieval", "cool retrieval", "nearline retrieval", "data retrieved"],
    },
    {
        "key": "archive_storage",
        "sku": "B91633",
        "description": "Archive Storage (GB-mo)",
        "unit": "GB-month",
        "rate": 0.0026,
        "category": "Storage",
        "notes": "Maps AWS Glacier/Deep Archive, Azure Archive Blob, GCP Archive/Coldline, and backup archive tiers.",
        "keywords": ["archive", "glacier", "deep archive", "coldline", "cold storage", "backup archive"],
    },
    {
        "key": "object_storage_requests",
        "sku": "B91627",
        "description": "Object Storage - Requests",
        "unit": "10,000 requests",
        "rate": 0.0034,
        "category": "Storage",
        "notes": "Maps S3/Blob/GCS request rows when a bill provides request counts.",
        "keywords": ["request", "api request", "put", "get", "list", "object request"],
    },
]

FULL_SERVICE_CATALOG_ITEMS = [
    {
        "key": "compute_ocpu_hours",
        "sku": SHAPE_LOOKUP[DEFAULT_SHAPE_KEY]["computeSku"],
        "description": "OCPU-hr rate (Compute)",
        "unit": "OCPU-hour",
        "rate": SHAPE_LOOKUP[DEFAULT_SHAPE_KEY]["computeRate"],
        "category": "Compute",
        "notes": "Maps source rows that provide OCPU-hours, vCPU-hours, or CPU core-hours.",
        "keywords": ["ocpu", "vcpu", "cpu hour", "cpu-hour", "core hour", "core-hour", "compute"],
    },
    {
        "key": "memory_gb_hours",
        "sku": SHAPE_LOOKUP[DEFAULT_SHAPE_KEY]["memorySku"],
        "description": "Memory GB-hr rate",
        "unit": "GB-hour",
        "rate": SHAPE_LOOKUP[DEFAULT_SHAPE_KEY]["memoryRate"],
        "category": "Compute",
        "notes": "Maps source rows that provide memory GB-hours.",
        "keywords": ["memory", "ram", "gb hour", "gb-hour", "gib hour", "gib-hour"],
    },
    {
        "key": "block_volume_storage",
        "sku": "B91961",
        "description": "Block Volume Storage (GB-mo)",
        "unit": "GB-month",
        "rate": 0.0255,
        "category": "Storage",
        "notes": "Maps AWS EBS, Azure Managed Disk, GCP Persistent Disk, VMware disks, SAN, generic block volumes, and the capacity behind an FSx -> ZFS appliance mapping.",
        # "fsx"/"windows file server" capacity is served from BLOCK volume behind a ZFS HA
        # appliance. Pricing it as OCI File Storage charged $0.30/GB-mo against FSx HDD at
        # $0.013/GB-mo - a 23x markup that made the whole estimate look uncompetitive.
        "keywords": ["ebs", "managed disk", "persistent disk", "block", "volume", "san", "disk",
                     "rds storage", "database storage", "fsx", "windows file server"],
    },
    {
        "key": "file_storage",
        "sku": "B89057",
        "description": "File Storage (GB-mo)",
        "unit": "GB-month",
        "rate": 0.3000,
        "category": "Storage",
        "notes": "Maps AWS EFS, Azure Files, GCP Filestore, NFS, SMB, NAS, and shared file systems.",
        "keywords": ["efs", "azure files", "filestore", "file share", "nfs", "smb", "nas", "file storage"],
    },
    {
        # Oracle ZFS Storage HA appliance. Per Oracle's ZFS HA documentation the cost is a
        # marketplace SOFTWARE IMAGE fee per compute instance-hour, PLUS the compute shape it
        # runs on, PLUS standard block volume for the capacity you provision. This SKU is only
        # the image fee; capacity is priced as block volume on the FSx/NAS rows themselves.
        "key": "zfs_ha_image",
        "sku": "B95410",
        "description": "ZFS Storage HA - marketplace image (instance-hour)",
        "unit": "instance-hour",
        "rate": 1.85,
        "category": "Storage",
        "notes": "Oracle ZFS Storage High Availability marketplace image; the OCI target for AWS FSx / large Windows-and-NFS file estates.",
        "keywords": ["zfs", "zfs storage", "fsx"],
    },
    *FULL_SERVICE_RATE_ITEMS,
]
FULL_SERVICE_RATE_BY_KEY = {item["key"]: item for item in FULL_SERVICE_CATALOG_ITEMS}

OCI_OFFICIAL_REFERENCES = [
    {
        "name": "Oracle cross-cloud service mapping",
        "url": "https://www.oracle.com/a/ocom/docs/ocimapping/ocimapping.html",
        "use": "Comparable AWS, Azure, and GCP services should be mapped to the closest OCI service family before pricing.",
    },
    {
        "name": "OCI price list",
        "url": "https://www.oracle.com/cloud/price-list/",
        "use": "OCI pricing is based on Oracle product meters such as OCPU-hour, GB-hour, GB-month, load balancer hour, bandwidth, requests, transactions, or ECPU/OCPU units.",
    },
]

OCI_SOURCE_SERVICE_MAPPINGS = [
    {
        "sourceServices": ["AWS EC2", "Azure Virtual Machines", "Google Compute Engine"],
        "ociServiceCategory": "Compute",
        "ociComparableServices": ["OCI Virtual Machine Instances", "OCI Bare Metal Instances"],
        "metering": "Map vCPU/core-hour usage to OCPU-hour using 2 vCPU = 1 OCPU for x86 when the bill is vCPU-based. Map memory usage to GB-hour when memory is separately metered.",
        "catalogKeys": ["compute_ocpu_hours", "memory_gb_hours"],
    },
    {
        "sourceServices": ["AWS S3", "Azure Blob Storage", "Google Cloud Storage"],
        "ociServiceCategory": "Storage",
        "ociComparableServices": ["OCI Object Storage Standard", "OCI Object Storage Infrequent Access", "OCI Archive Storage"],
        "metering": "Standard/hot object capacity maps to Standard GB-month. Standard-IA, Cool, and Nearline map to Infrequent Access GB-month, with retrieved GB priced separately. Archive, Glacier, Deep Archive, Archive Blob, and Coldline map to Archive GB-month. Request meters remain request counts and are priced per 10,000 requests where available.",
        "catalogKeys": ["object_storage_standard", "object_storage_infrequent", "object_storage_infrequent_retrieval", "archive_storage", "object_storage_requests"],
    },
    {
        "sourceServices": ["AWS EBS", "Azure Managed Disks", "Google Persistent Disk"],
        "ociServiceCategory": "Storage",
        "ociComparableServices": ["OCI Block Volumes"],
        "metering": "Capacity maps to block volume GB-month. Performance, IOPS, throughput, and provisioned VPU-style meters require review unless a matching OCI performance meter is available.",
        "catalogKeys": ["block_volume_storage"],
    },
    {
        "sourceServices": ["AWS EFS", "Azure Files", "Google Filestore", "NAS", "NFS", "SMB"],
        "ociServiceCategory": "Storage",
        "ociComparableServices": ["OCI File Storage"],
        "metering": "Capacity maps to file storage GB-month. Premium performance or replication meters should be reviewed.",
        "catalogKeys": ["file_storage"],
    },
    {
        "sourceServices": ["AWS Elastic Load Balancing", "Azure Load Balancer", "Azure Application Gateway", "Google Cloud Load Balancing"],
        "ociServiceCategory": "Networking",
        "ociComparableServices": ["OCI Load Balancer", "OCI Web Application Firewall"],
        "metering": "Map base/load-balancer-hours and bandwidth/throughput meters separately. If no matching local rate-card item exists, preserve the target service and mark the row for review.",
        "catalogKeys": [],
    },
    {
        "sourceServices": ["AWS Data Transfer", "Azure Bandwidth", "Google Network Egress", "Cloud CDN egress"],
        "ociServiceCategory": "Networking",
        "ociComparableServices": ["OCI Networking outbound data transfer", "OCI FastConnect"],
        "metering": "Map egress to data transfer GB where regional direction and tier are clear. Mark inter-region, internet, CDN, or private-connectivity rows for review when the target meter is ambiguous.",
        "catalogKeys": [],
    },
    {
        "sourceServices": ["AWS RDS", "AWS Aurora", "Azure SQL Database", "Azure Database for PostgreSQL", "Azure Database for MySQL", "Google Cloud SQL", "AlloyDB"],
        "ociServiceCategory": "Oracle Databases",
        "ociComparableServices": ["Oracle Autonomous AI Transaction Processing", "Oracle MySQL Database Service", "OCI Database with PostgreSQL", "Oracle Base Database Service"],
        "metering": "Database compute commonly maps to OCPU/ECPU hours and storage to GB-month, but engine, license, HA, backup, and deployment model change the target OCI product. Mark for review unless the source row gives a clear Oracle-compatible database target.",
        "catalogKeys": [],
    },
    {
        "sourceServices": ["AWS Lambda", "Azure Functions", "Google Cloud Functions", "Cloud Run functions"],
        "ociServiceCategory": "Containers and Functions",
        "ociComparableServices": ["OCI Functions"],
        "metering": "Function pricing separates invocations from execution duration such as GB-seconds. Keep invocation and duration rows separate and mark for review if the local catalog lacks the meter.",
        "catalogKeys": [],
    },
    {
        "sourceServices": ["AWS EKS", "Azure AKS", "Google GKE"],
        "ociServiceCategory": "Containers and Functions",
        "ociComparableServices": ["OCI Kubernetes Engine", "OCI Registry"],
        "metering": "Cluster management, worker compute, registry, storage, and network rows should be separated. Underlying VM/storage rows can map to compute/storage meters; cluster management rows need review unless a local catalog meter exists.",
        "catalogKeys": ["compute_ocpu_hours", "memory_gb_hours", "block_volume_storage"],
    },
]

OCI_METERING_GUIDANCE = [
    "OCI price-list pages show both vCPU comparison prices and OCPU prices, but OCI products bill in OCPU units; for common x86 shapes, 1 OCPU is equivalent to 2 vCPUs.",
    "Do not turn source-cloud monthly cost into an OCI unit rate. Use source cost only for comparison and prioritization.",
    "Preserve separate bill lines when the source has separate meters, such as compute hours, memory hours, storage capacity, performance units, requests, and network transfer.",
    "Convert storage capacity to GB-month when possible: TB-month x 1024, MB-month / 1024, GB-hour / 730, byte-hours / 1024^3 / 730.",
    "Convert vCPU-hour to OCPU-hour by multiplying by 0.5 when the source meter is vCPU/core based. Leave OCPU-hour unchanged.",
    "Request meters should retain raw request counts; pricing logic converts to 10,000-request units when the OCI product uses that meter.",
    "When the local catalog has no exact OCI price-list item, still populate OCI service/product labels and mark the row as Needs review rather than forcing a bad price.",
]


def load_local_env():
    env_path = ROOT / ".env.local"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_local_env()


def clean_text(value):
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = (
        str(value)
        .replace("\ufb00", "ff")
        .replace("\ufb01", "fi")
        .replace("\ufb02", "fl")
        .replace("\ufb03", "ffi")
        .replace("\ufb04", "ffl")
        .replace("\n", " ")
    )
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize(value):
    return re.sub(r"[^a-z0-9]+", " ", clean_text(value).lower()).strip()


def make_key(label, seen):
    base = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "column"
    key = base
    idx = 2
    while key in seen:
        key = f"{base}_{idx}"
        idx += 1
    seen.add(key)
    return key


def clean_cell(value):
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except TypeError:
        pass
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, float):
        return int(value) if value.is_integer() else round(value, 4)
    return clean_text(value)


def spreadsheet_cpu_label(label):
    raw = str(label or "").lower()
    text = normalize(label)
    if not text:
        return False
    # A "rationalized cores / rationalized vCPU" column (from a migration assessment) is a
    # deliberate CPU-sizing column — recognize it FIRST (note "ratio" is a substring of
    # "rationalized", so this must come before the ratio exclusion below).
    if "rationalized" in text and any(t in text for t in ["core", "cpu", "vcpu"]):
        return True
    # Ratios / rates / perf / architecture columns are NOT a CPU count (e.g. "vCPU:Core Ratio",
    # "Current vCPU:Core", "Uplift", "Target Perf") — exclude them so they aren't sized on.
    if ":" in raw or any(term in text for term in ["chipset", "processor family", "cpu type", "architecture", "model", "vendor", "speed", "ghz", "clock", "utiliz", "percent", "ratio", "per core", "per socket", "uplift", "perf"]):
        return False
    if text in {"cpu", "cpus", "cores", "core", "vcpu", "vcpus"}:
        return True
    return any(
        term in text
        for term in [
            "vcpu",
            "vcpus",
            "v cpu",
            "virtual cpu",
            "cpu vcpu",
            "cpu core",
            "cpu cores",
            "number of cpu",
            "num cpu",
            "cpu count",
            "cores per server",
            "core count",
        ]
    )


def spreadsheet_memory_label(label):
    text = normalize(label)
    if not text:
        return False
    if any(term in text for term in ["storage", "disk", "iops", "swap"]):
        return False
    return any(term in text for term in ["ram", "memory", "mem gb", "gb ram"])


def spreadsheet_storage_label(label):
    text = normalize(label)
    if not text:
        return False
    if any(term in text for term in ["iops", "cpu", "ocpu", "vcpu", "memory", "ram", "load balancer"]):
        return False
    if header_is_bare_disk_count(text):
        return False
    if any(
        term in text
        for term in [
            "storage",
            "database size",
            "db size",
            "allocated",
            "disk gb",
            "disk size",
            "disk capacity",
            "volume size",
            "data size",
        ]
    ):
        return True
    # Token match so "Disk in GB", "Disk (GB)", "Provisioned Disk GB" all count - a filler
    # word ("in") shouldn't stop a disk-capacity column from being recognized as storage.
    tokens = set(text.split())
    return ("disk" in tokens and ("gb" in tokens or "tb" in tokens or "mb" in tokens)) or (
        "disk" in tokens and "capacity" in tokens)


def ocpu_review_label(label):
    text = normalize(label)
    prefix = "Database Details" if any(term in text for term in ["database", " db ", "db cpu", "db cores"]) else "Application Details"
    return f"{prefix}: OCPUs"


def memory_review_label(label):
    text = normalize(label)
    prefix = "Database Details" if any(term in text for term in ["database", " db ", "db memory", "db ram"]) else "Application Details"
    return f"{prefix}: Memory per server (GB)"


def to_number(value, default=0.0):
    if value is None:
        return default
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return default
        return float(value)
    text = str(value).replace(",", "").strip()
    if not text or text == "-":
        return default
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else default


# Capacity units -> GB, largest first so "PB" is never read as the "B" of something smaller.
# Binary (IEC) spellings are treated as their decimal twins: inventories write TiB and TB for
# the same thing, and OCI bills block storage in GB where 1 TB = 1024 GB, so one scale keeps
# the estimate consistent with the invoice. Getting this wrong is silent and enormous - a
# "1.5 TiB" volume read as 1.5 GB understates by a factor of a thousand.
_CAPACITY_UNITS = (
    (r"e(?:i)?bs?|exabytes?", 1024.0 ** 3),
    (r"p(?:i)?bs?|petabytes?", 1024.0 ** 2),
    (r"t(?:i)?bs?|terabytes?", 1024.0),
    (r"g(?:i)?bs?|gigabytes?", 1.0),
    (r"m(?:i)?bs?|megabytes?", 1.0 / 1024.0),
    (r"k(?:i)?bs?|kilobytes?", 1.0 / (1024.0 ** 2)),
)


def capacity_unit_factor(text):
    """GB multiplier implied by a unit word in `text`, or None when it carries no unit."""
    text = normalize(text)
    if not text:
        return None
    for pattern, factor in _CAPACITY_UNITS:
        if re.search(r"(?:^|\d|\s)(?:%s)(?:$|\s)" % pattern, text):
            return factor
    return None


# A capacity cell is often an expression, not a number: "2 x 500 GB" for a pair of disks,
# "500 GB + 1 TB" for a boot volume plus data. Reading only the first number turned the first
# into 2 GB and the second into 500 GB, silently dropping most of the estate.
#
# normalize() can't be used here: it replaces every non-alphanumeric with a space, which
# destroys the decimal point in "12.125 TB" and the "+" that separates the terms.
_TERM_RE = re.compile(
    r"(?P<a>\d+(?:\.\d+)?)"                                   # a number
    r"(?:\s*(?:disks?|drives?|volumes?|vols?|luns?)?\s*"       # optional "disks"/"drives"
    r"[x×*]\s*(?P<b>\d+(?:\.\d+)?))?"                        # ...times a second number
    r"\s*(?P<unit>[a-z]+)?",                                    # optional unit word
    re.I,
)
_TERM_SPLIT_RE = re.compile(r"\s*(?:\+|&|\band\b|\bplus\b)\s*", re.I)


def to_gb(value, default=0.0):
    """A capacity cell -> GB, honouring units, "N x size" multipliers and "a + b" sums."""
    text = clean_text(value).lower().strip()
    if not text:
        return default
    # Strip thousands separators only. A comma is never treated as a term separator, because
    # "1,024 GB" is a single value and guessing wrong there is worse than not splitting.
    text = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text)

    terms = []
    for chunk in _TERM_SPLIT_RE.split(text):
        m = _TERM_RE.search(chunk)
        if not m:
            continue
        size = float(m.group("a"))
        if m.group("b"):
            size *= float(m.group("b"))
        terms.append((size, capacity_unit_factor(m.group("unit") or "")))
    if not terms:
        return to_number(value, default)

    # "500 + 1024 GB" states the unit once; a term without one inherits it.
    shared = next((f for _size, f in terms if f is not None), None)
    return sum(size * (factor if factor is not None else (shared if shared is not None else 1.0))
               for size, factor in terms)


_NON_STORAGE_HEADER_TERMS = (
    "uuid", "bios", "serial", "guid", "gateway", "address", "url", "folder",
    "version", "vendor", "firmware", "domain", "hostname", "mac", "ip ",
    # Columns that name a storage KIND rather than an amount - "Storage Type" holds
    # "Premium SSD", not a number. They only became reachable once a bare "storage" /
    # "disk" header was allowed to match, and they must never shadow the real capacity
    # column on a sheet that has both.
    "type", "tier", "class", "category", "protocol", "vmdk", "datastore name",
)


def plausible_storage_field(fields, key):
    """True only if `key` really looks like a storage-capacity column. Blocks identifier
    /metadata columns (SMBios UUID, VM UUID, firmware, ...) from being priced as GB."""
    if not key:
        return False
    field = next((f for f in fields or [] if isinstance(f, dict) and f.get("key") == key), None)
    if not field:
        return False
    text = normalize(" ".join(clean_text(field.get(k)) for k in ("label", "sourceHeader")))
    if any(term.strip() in text for term in _NON_STORAGE_HEADER_TERMS):
        return False
    return bool(re.search(r"storage|disk|capacity|volume|datastore|allocated|size", text))


# What the app looks for in an uploaded inventory. Header keywords -> what it unlocks.
# Needles are matched on WORD BOUNDARIES, not substrings: "Total storage capacity" must not
# register as a site column just because "capacity" ends in "city".
_DATA_CHECK_SIGNALS = [
    ("cpu",         "CPU / cores",        [["vcpu"], ["cpu"], ["core"], ["cores"]]),
    ("memory",      "Memory",             [["memory"], ["mem"], ["ram"]]),
    ("storage",     "Storage",            [["storage"], ["disk"], ["capacity"]]),
    ("os",          "OS family",          [["os", "family"], ["os", "type"], ["operating", "system"],
                                           ["os", "name"], ["os"]]),
    ("server",      "Server / VM name",   [["vm", "name"], ["server", "name"], ["machine", "name"],
                                           ["machine", "id"],
                                           ["host", "name"], ["hostname"], ["name"]]),
    ("environment", "Environment",        [["environment"], ["env"]]),
    ("application", "Application",        [["application"], ["app", "name"], ["app"]]),
]

# Columns whose header matches a signal but which never carry that signal's data.
_DATA_CHECK_EXCLUDE = {
    "application": ("details",),   # "Application Details: Memory" is a memory column
}


def _header_has_words(header, needles):
    """True when every needle appears in `header` as a whole word."""
    for n in needles:
        if not re.search(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])", header):
            return False
    return True


def inventory_data_check(fields, rows):
    """Pre-flight: what does this upload ACTUALLY contain?

    Counts POPULATED values, not just header presence - an inventory can ship a 'Domain'
    or 'vCenter URL' column that is 100% empty. Anything not found here is something the
    app must leave blank rather than invent application data.
    """
    total = len(rows or [])
    found = []
    _sizing_marker = {"cpu": "cpuSourceLabel", "memory": "memorySourceLabel",
                      "storage": "storageSourceLabel"}
    for key, label, needle_sets in _DATA_CHECK_SIGNALS:
        col_key = col_label = ""
        populated = 0
        blocked = _DATA_CHECK_EXCLUDE.get(key, ())
        # Prefer the column the parser actually uses for sizing (tagged with a *SourceLabel
        # marker) so the Data Check names the real CPU/memory/storage column rather than a
        # same-keyword neighbor (e.g. a "vCPU:Core ratio" column that sorts first).
        marker = _sizing_marker.get(key)
        if marker:
            for f in fields or []:
                if isinstance(f, dict) and f.get(marker):
                    k = f.get("key")
                    n = sum(1 for r in (rows or []) if clean_text(r.get(k)))
                    if n > populated:
                        col_key = k
                        col_label = clean_text(f.get(marker) or f.get("sourceHeader") or f.get("label"))
                        populated = n
        for needles in (() if populated else needle_sets):
            for f in fields or []:
                if not isinstance(f, dict):
                    continue
                # Match the ORIGINAL header - the parser renames CPU/memory columns.
                src = normalize(f.get("cpuSourceLabel") or f.get("memorySourceLabel")
                                or f.get("sourceHeader") or f.get("label"))
                if not _header_has_words(src, needles):
                    continue
                if any(_header_has_words(src, [b]) for b in blocked):
                    continue
                k = f.get("key")
                n = sum(1 for r in (rows or []) if clean_text(r.get(k)))
                if n > populated:
                    col_key, col_label, populated = k, clean_text(f.get("sourceHeader") or f.get("label")), n
            if populated:
                break
        found.append({
            "key": key, "label": label, "column": col_label,
            "populated": populated, "total": total,
            "present": bool(populated),
        })

    by = {f["key"]: f for f in found}
    capabilities = {
        "priceCompute": by["cpu"]["present"] and by["memory"]["present"],
        "priceStorage": by["storage"]["present"],
        # Spokes in the architecture diagram segment by environment -> OS -> application.
        "segmentBy": next((k for k in ("environment", "os", "application")
                           if by[k]["present"]), None),
        "applicationColumns": by["application"]["present"],
    }
    return {"signals": found, "capabilities": capabilities}


def header_unit_factor_to_gb(label):
    """Scale factor to GB implied by a COLUMN HEADER's unit, e.g. 'Memory (MB)' -> 1/1024
    or 'Storage (TB)' -> 1024. Inventories (RVTools-style) usually put the unit in the
    header and leave the cells as bare numbers, which to_gb() alone can't see."""
    factor = capacity_unit_factor(label)
    return factor if factor is not None else 1.0


def to_gb_with_header(value, factor=1.0):
    """to_gb(), but when the CELL carries no unit, fall back to the header's unit."""
    raw = to_number(value, 0)
    converted = to_gb(value)
    # to_gb returns the bare number when the cell had no unit text - apply the header's.
    if converted == raw and factor != 1.0:
        return raw * factor
    return converted


def _inventory_context_key(fields, kind):
    for field in fields or []:
        label = normalize(
            field.get("sourceHeader")
            or field.get("memorySourceLabel")
            or field.get("storageSourceLabel")
            or field.get("label")
        )
        if kind == "virtual" and "virtual" in label and "physical" in label:
            return field.get("key")
        if kind == "model" and label in {"model", "hardware model", "machine model", "server model"}:
            return field.get("key")
    return None


def _looks_virtual_inventory_row(row, virtual_key, model_key):
    virtual_text = normalize(row.get(virtual_key)) if virtual_key else ""
    model_text = normalize(row.get(model_key)) if model_key else ""
    if "physical" in virtual_text and "virtual" not in virtual_text:
        return False
    return (
        "virtual" in virtual_text
        or virtual_text in {"vm", "guest"}
        or any(term in model_text for term in ["vmware", "vxrail", "hyper v", "virtual machine"])
    )


def normalize_mixed_inventory_units(fields, rows, memory_keys=None, storage_keys=None):
    """Repair mixed-unit CMDB exports whose headers say GB but some source populations
    contain MiB values. Conversion is limited to repeated virtual-machine patterns so
    legitimate large physical servers and ordinary GB values are preserved."""
    if not rows:
        return []

    memory_keys = set(memory_keys or [])
    storage_keys = set(storage_keys or [])
    virtual_key = _inventory_context_key(fields, "virtual")
    model_key = _inventory_context_key(fields, "model")
    notes = []

    for key in memory_keys:
        candidates = []
        for row in rows:
            value = to_number(row.get(key), 0)
            if not _looks_virtual_inventory_row(row, virtual_key, model_key):
                continue
            mib_block = value >= 4096 and abs((value / 1024.0) - round(value / 1024.0)) < 1e-9
            if mib_block:
                candidates.append(row)
        if len(candidates) < 6:
            continue
        for row in candidates:
            row[key] = compact_number(to_number(row.get(key), 0) / 1024.0)
        notes.append(
            {
                "fieldKey": key,
                "kind": "memory",
                "fromUnit": "MiB",
                "toUnit": "GB",
                "rowCount": len(candidates),
                "message": (
                    f"Normalized {len(candidates)} virtual-machine memory values from MiB to GB "
                    "after detecting repeated 1024-MiB allocation blocks in a column labeled GB."
                ),
            }
        )

    for key in storage_keys:
        groups = collections.defaultdict(list)
        for row in rows:
            value = to_number(row.get(key), 0)
            if value <= 0 or not _looks_virtual_inventory_row(row, virtual_key, model_key):
                continue
            model = normalize(row.get(model_key)) if model_key else ""
            if not model:
                continue
            groups[model].append((row, value))

        converted = []
        converted_models = []
        for model, entries in groups.items():
            values = [value for _, value in entries]
            if len(values) < 6:
                continue
            high_ratio = sum(value >= 10240 for value in values) / len(values)
            integer_ratio = sum(abs(value - round(value)) < 1e-6 for value in values) / len(values)
            median_value = statistics.median(values)
            converted_median = median_value / 1024.0
            if (
                high_ratio < 0.8
                or integer_ratio < 0.8
                or median_value < 10240
                or not 1 <= converted_median <= 262144
            ):
                continue
            for row, value in entries:
                row[key] = compact_number(value / 1024.0)
                converted.append(row)
            converted_models.append(model)

        if converted:
            models = ", ".join(sorted(converted_models)[:4])
            model_suffix = f" for virtual model groups: {models}" if models else ""
            notes.append(
                {
                    "fieldKey": key,
                    "kind": "storage",
                    "fromUnit": "MiB",
                    "toUnit": "GB",
                    "rowCount": len(converted),
                    "message": (
                        f"Normalized {len(converted)} virtual-machine storage values from MiB to GB"
                        f"{model_suffix} after detecting a repeated MiB-scale population in a column labeled GB."
                    ),
                }
            )

    return notes


def header_has_database_signal(label):
    text = normalize(label)
    return bool(re.search(r"\b(database|db|sql|oracle|postgres|mysql|mssql|rds)\b", text))


def header_has_storage_capacity_signal(label):
    text = normalize(label)
    return any(
        term in text
        for term in [
            "storage",
            "allocated",
            "capacity",
            "disk gb",
            "disk size",
            "disk capacity",
            "volume size",
            "provisioned",
            "used gb",
            "total gb",
        ]
    )


def header_is_bare_disk_count(label):
    text = normalize(label)
    if not text:
        return False
    if header_has_storage_capacity_signal(text):
        return False
    return text in {"disk", "disks", "disk count", "number of disks", "num disks", "drive count", "drives"}


def column_numeric_profile(raw, col_idx, data_start_row=None, max_rows=80):
    start_idx = max(0, int(to_number(data_start_row, 0) or 1) - 1)
    numbers = []
    end_idx = min(len(raw.index), start_idx + max_rows)
    for row_idx in range(start_idx, end_idx):
        if col_idx >= len(raw.columns):
            continue
        value = raw.iat[row_idx, col_idx]
        if clean_text(value) == "":
            continue
        number = to_number(value, None)
        if number is not None:
            numbers.append(number)
    if not numbers:
        return {"count": 0, "max": 0, "p95": 0, "integerRatio": 0, "smallIntegerRatio": 0}
    ordered = sorted(abs(number) for number in numbers)
    p95_index = min(len(ordered) - 1, int(math.ceil(len(ordered) * 0.95)) - 1)
    integer_count = sum(1 for number in numbers if float(number).is_integer())
    small_integer_count = sum(1 for number in numbers if float(number).is_integer() and 0 <= abs(number) <= 64)
    return {
        "count": len(numbers),
        "max": max(ordered),
        "p95": ordered[p95_index],
        "integerRatio": integer_count / len(numbers),
        "smallIntegerRatio": small_integer_count / len(numbers),
    }


def column_looks_like_disk_count(raw, col_idx, label, data_start_row=None):
    if not header_is_bare_disk_count(label):
        return False
    profile = column_numeric_profile(raw, col_idx, data_start_row)
    if profile["count"] < 3:
        return True
    return profile["smallIntegerRatio"] >= 0.8 and profile["p95"] <= 32


def storage_mapping_disallowed(raw, col_idx, key, label, data_start_row=None):
    if key not in {
        "application_details_local_storage_gb",
        "application_details_shared_storage_gb",
        "database_details_total_allocated_storage_gb",
    }:
        return False
    return column_looks_like_disk_count(raw, col_idx, label, data_start_row)


def compact_number(value):
    if value == "":
        return ""
    number = float(value)
    if number.is_integer():
        return int(number)
    return round(number, 4)


def normalize_inventory_value(key, value):
    if clean_text(value) == "":
        return ""
    if key in CPU_FIELD_KEYS:
        if key == "resource_ocpus":
            return compact_number(to_number(value))
        return compact_number(to_number(value) / 2)
    if key in SIZE_FIELD_KEYS:
        return compact_number(to_gb(value))
    if key in NUMERIC_FIELD_KEYS:
        return compact_number(to_number(value))
    return clean_cell(value)


def parse_json_cell(value):
    text = clean_text(value)
    if not text or text[0] not in "[{":
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def add_json_pair(pairs, key, value):
    key_text = clean_text(key)
    value_text = clean_cell(value)
    if key_text and value_text != "":
        pairs[key_text] = value_text


def flatten_json_tags(value):
    parsed = parse_json_cell(value)
    if parsed is None:
        return {}

    pairs = {}

    def visit(node, prefix=""):
        if isinstance(node, dict):
            tag_key = node.get("key") or node.get("Key") or node.get("tagKey")
            tag_value = node.get("value") or node.get("Value") or node.get("tagValue")
            tag_text = node.get("tag") or node.get("Tag")
            if tag_key is not None and tag_value is not None:
                add_json_pair(pairs, tag_key, tag_value)
            if tag_text and "=" in clean_text(tag_text):
                left, right = clean_text(tag_text).split("=", 1)
                add_json_pair(pairs, left, tag_value if tag_value is not None else right)

            for key, child in node.items():
                child_key = clean_text(key)
                path = f"{prefix}.{child_key}" if prefix else child_key
                if isinstance(child, (dict, list)):
                    visit(child, path)
                elif child_key not in {"key", "Key", "value", "Value", "tag", "Tag"}:
                    add_json_pair(pairs, path, child)
        elif isinstance(node, list):
            for child in node:
                visit(child, prefix)

    visit(parsed)
    return pairs


def json_key_match_score(candidate, target):
    candidate_norm = normalize(candidate)
    target_norm = normalize(target)
    if not candidate_norm or not target_norm:
        return 0
    if candidate_norm == target_norm:
        return 100
    if candidate_norm.endswith(f" {target_norm}") or candidate_norm.endswith(target_norm):
        return 80
    if target_norm in candidate_norm:
        return 60
    if candidate_norm in target_norm and len(candidate_norm) >= 3:
        return 40
    return 0


def value_from_json_cell(value, json_key):
    target = clean_text(json_key)
    if not target:
        return ""
    pairs = flatten_json_tags(value)
    if not pairs:
        return ""
    best_key = ""
    best_score = 0
    for key in pairs:
        score = json_key_match_score(key, target)
        if score > best_score:
            best_key = key
            best_score = score
    return pairs.get(best_key, "") if best_score >= 40 else ""


def summarize_json_cell(value, max_items=8):
    pairs = flatten_json_tags(value)
    if not pairs:
        return None
    return {
        "keys": list(pairs.keys())[:max_items],
        "preview": {key: pairs[key] for key in list(pairs.keys())[:max_items]},
    }


def json_column_header(raw, col_idx):
    parts = []
    for row_idx in range(min(8, len(raw.index))):
        value = raw.iat[row_idx, col_idx]
        text = clean_text(value)
        if text and parse_json_cell(value) is None and text not in parts:
            parts.append(text)
    return " ".join(parts[:3])


def canonical_fields_payload(full_service_beta=False, intake_mode=INTAKE_MODE_ON_PREM):
    return [
        {
            "key": field["key"],
            "label": field["label"],
            "sourceColumn": None,
            "important": True,
        }
        for field in inventory_fields(full_service_beta, intake_mode)
    ]


def canonical_field_prompt(full_service_beta=False, intake_mode=INTAKE_MODE_ON_PREM):
    return [
        {
            "key": field["key"],
            "label": field["label"],
            "description": field["description"],
            "aliases": field["aliases"],
        }
        for field in inventory_fields(full_service_beta, intake_mode)
    ]


def resolve_shape(shape_key=None):
    return SHAPE_LOOKUP.get(shape_key or DEFAULT_SHAPE_KEY, SHAPE_LOOKUP[DEFAULT_SHAPE_KEY])


def price_catalog_payload():
    return [
        {
            "key": item["key"],
            "sku": item["sku"],
            "description": item["description"],
            "unit": item["unit"],
            "rate": item["rate"],
            "category": item["category"],
            "keywords": item["keywords"],
        }
        for item in FULL_SERVICE_CATALOG_ITEMS
    ]


def build_rate_card(shape_key=None, full_service_beta=False):
    shape = resolve_shape(shape_key)
    items = [
        {
            "sku": shape.get("computeSku", "B97384"),
            "description": "OCPU-hr rate (Compute)",
            "unit": "OCPU-hour",
            "rate": shape["computeRate"],
            "notes": f"{shape['label']} OCPU-hours use each row's Hours Running value (730 by default).",
        },
        {
            "sku": shape.get("memorySku", "B97385"),
            "description": "Memory GB-hr rate",
            "unit": "GB-hour",
            "rate": shape["memoryRate"],
            "notes": f"{shape['label']} GB-hours use each row's Hours Running value (730 by default).",
        },
        *[item.copy() for item in STORAGE_RATE_ITEMS],
        WINDOWS_LICENSE_ITEM.copy(),
    ]
    if full_service_beta:
        seen_skus = {item["sku"] for item in items}
        for item in FULL_SERVICE_RATE_ITEMS:
            if item["sku"] in seen_skus:
                continue
            items.append(
                {
                    "sku": item["sku"],
                    "description": item["description"],
                    "unit": item["unit"],
                    "rate": item["rate"],
                    "notes": item["notes"],
                }
            )
            seen_skus.add(item["sku"])
    return items


def shape_payload(shape_key=None, full_service_beta=False):
    shape = resolve_shape(shape_key)
    return {
        "key": shape["key"],
        "label": shape["label"],
        "shortLabel": shape["shortLabel"],
        "family": shape["family"],
        "processorVendor": shape.get("processorVendor", "amd"),
        "summary": shape["summary"],
        "accent": shape["accent"],
        "computeSku": shape.get("computeSku", "B97384"),
        "memorySku": shape.get("memorySku", "B97385"),
        "computeRate": shape["computeRate"],
        "memoryRate": shape["memoryRate"],
        # Bare-metal shapes carry a fixed server size; the UI labels them and pricing bills
        # whole servers. Absent/False for flex shapes.
        "bareMetal": bool(shape.get("bareMetal")),
        "bmOcpu": shape.get("bmOcpu"),
        "bmMemoryGb": shape.get("bmMemoryGb"),
        "hoursPerMonth": HOURS_PER_MONTH,
        "rateCard": build_rate_card(shape["key"], full_service_beta),
    }


def all_shape_payloads(full_service_beta=False):
    return [shape_payload(shape["key"], full_service_beta) for shape in SHAPE_DEFINITIONS]


def hidden_sheet_names(source):
    """Titles of hidden / very-hidden worksheets, so parsing ignores them. `source`
    is a path to an .xlsx/.xlsm workbook; returns an empty set for other formats or on
    any error (pandas can't see sheet visibility, so this uses openpyxl)."""
    try:
        path_str = str(source)
    except Exception:
        return set()
    if not path_str.lower().endswith((".xlsx", ".xlsm")):
        return set()
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path_str, read_only=True)
        hidden = {ws.title for ws in wb.worksheets
                  if getattr(ws, "sheet_state", "visible") != "visible"}
        wb.close()
        return hidden
    except Exception:
        return set()


def visible_sheet_names(excel_file, source=None):
    """excel_file.sheet_names minus any hidden sheets (falls back to all sheets if that
    would leave nothing)."""
    hidden = hidden_sheet_names(source if source is not None else getattr(excel_file, "io", None))
    names = [n for n in excel_file.sheet_names if n not in hidden]
    return names or list(excel_file.sheet_names)


def pick_sheet(excel_file):
    names = visible_sheet_names(excel_file)
    for name in names:
        if normalize(name) == "current app db infra details":
            return name
    best_name = names[0]
    best_score = -1
    for name in names:
        raw = pd.read_excel(excel_file, sheet_name=name, header=None, dtype=object)
        text = normalize(
            " ".join(
                clean_text(raw.iat[row_idx, col_idx])
                for row_idx in range(min(30, len(raw.index)))
                for col_idx in range(min(20, len(raw.columns)))
                if clean_text(raw.iat[row_idx, col_idx])
            )
        )
        score = 0
        for term, weight in {
            "server vm inventory": 10,
            "server vm name": 8,
            "cpu vcpu": 8,
            "vcpu": 6,
            "ram gb": 6,
            "memory": 5,
            "number of cpu": 6,
            "number of servers": 5,
        }.items():
            if term in text:
                score += weight
        # Per-COLUMN header signals: evaluate each header cell on its own so a real
        # inventory sheet isn't penalized for also having a Storage column (running
        # the memory check on the whole joined row bails out on the word "storage").
        header_cells = []
        for row_idx in range(min(8, len(raw.index))):
            for value in raw.iloc[row_idx].tolist():
                c = clean_text(value)
                if c:
                    header_cells.append(c)
        has_cpu = any(spreadsheet_cpu_label(c) for c in header_cells)
        has_mem = any(spreadsheet_memory_label(c) for c in header_cells)
        has_storage = any(spreadsheet_storage_label(c) for c in header_cells)
        if has_cpu:
            score += 6
        if has_mem:
            score += 6
        if has_storage:
            score += 3
        # A real server inventory has BOTH a CPU and a Memory column AND many server
        # rows. Weight the row count heavily so a small decoy sheet can't outrank the
        # actual inventory.
        data_rows = max(0, len(raw.index) - 1)
        if has_cpu and has_mem:
            score += 10 + min(40, data_rows // 4)
        numeric_cells = 0
        for row_idx in range(min(80, len(raw.index))):
            numeric_cells += sum(1 for value in raw.iloc[row_idx].tolist() if to_number(value, 0))
        score += min(12, numeric_cells // 8)
        if score > best_score:
            best_name = name
            best_score = score
    return best_name


def inventory_header_score(values, next_values=None):
    cells = [clean_text(value) for value in values if clean_text(value)]
    text = normalize(" ".join(cells))
    if not text:
        return 0
    score = min(10, len(cells))
    terms = {
        "application name": 12,
        "server vm name": 14,
        "server name": 10,
        "vm name": 10,
        "host name": 10,
        "environment": 6,
        "env": 4,
        "operating system": 8,
        "os version": 7,
        "cpu vcpu": 14,
        "vcpu": 12,
        "number of cpu": 12,
        "cpu cores": 10,
        "ram gb": 12,
        "memory per server": 12,
        "memory": 8,
        "local disk": 8,
        "storage": 6,
    }
    for term, weight in terms.items():
        if term in text:
            score += weight
    if "customer response" in text and "guidance" in text:
        score -= 12
    if "purpose capture" in text or "questionnaire" in text:
        score -= 10
    if next_values is not None:
        next_text = normalize(" ".join(clean_text(value) for value in next_values if clean_text(value)))
        if next_text and not any(term in next_text for term in ["guidance examples", "purpose capture"]):
            score += 3
        numeric_next = sum(1 for value in next_values if to_number(value, 0))
        score += min(8, numeric_next)
    return score


def detect_header_rows(raw):
    sample_rows = min(60, len(raw.index))
    best_score = -1
    header_row = 0
    for idx in range(sample_rows):
        values = raw.iloc[idx].tolist()
        next_values = raw.iloc[idx + 1].tolist() if idx + 1 < len(raw.index) else []
        score = inventory_header_score(values, next_values)
        if score > best_score:
            best_score = score
            header_row = idx
    group_row = max(0, header_row - 1)
    return group_row, header_row, header_row + 1


def is_important(label):
    text = normalize(label)
    terms = [
        "application name",
        "environment",
        "application type",
        "number of servers",
        "number of database servers",
        "number of cpu cores per server",
        "memory per server gb",
        "local storage gb",
        "shared storage gb",
        "database type",
        "database size gb",
        "total allocated storage gb",
        "total storage gb",
        "storage iops",
    ]
    return any(term in text for term in terms)


def build_fields(raw, group_row, header_row):
    sections = {"application details", "database details", "oci details"}
    fields = []
    seen = set()
    current_section = ""

    for col_idx in range(len(raw.columns)):
        top = clean_text(raw.iat[group_row, col_idx]) if group_row < len(raw.index) else ""
        sub = clean_text(raw.iat[header_row, col_idx]) if header_row < len(raw.index) else ""
        top_norm = normalize(top)

        if top_norm in sections:
            current_section = top
            label = f"{current_section}: {sub}" if sub else current_section
        elif sub:
            label = f"{current_section}: {sub}" if current_section else sub
        elif top:
            label = top
        else:
            label = f"Column {col_idx + 1}"

        label = clean_text(label)
        fields.append(
            {
                "key": make_key(label, seen),
                "label": label,
                "sourceColumn": col_idx + 1,
                "important": is_important(label),
            }
        )

    return fields


def meaningful_inventory_value(value):
    text = normalize(value)
    return bool(text and text not in {"na", "n a", "none", "null", "tbd", "unknown"})


def rule_based_row_has_inventory_signal(row, fields):
    has_application = False
    has_name = False
    has_environment = False
    has_descriptive_detail = False
    has_resource = False

    for field in fields:
        value = row.get(field["key"])
        if not meaningful_inventory_value(value):
            continue
        label = normalize(field.get("label"))
        if "application name" in label or label in {"application", "app name"}:
            has_application = True
        elif (
            label == "name"
            or label.endswith(" name")
            or "hostname" in label
            or "host name" in label
            or "resource id" in label
            or "resource name" in label
        ):
            # Identifier-style column (Name, Server Name, Host Name, VM Name, etc.) - but not "per server" resource labels.
            has_name = True
        elif "environment" in label or label == "env":
            has_environment = True
        elif (
            "ocpu" in label
            or spreadsheet_cpu_label(label)
            or spreadsheet_memory_label(label)
            or spreadsheet_storage_label(label)
            or "number of servers" in label
            or "number of database servers" in label
        ):
            has_resource = True
        elif any(term in label for term in ["application type", "database type", "server name", "host name", "description"]):
            has_descriptive_detail = True

    if not (has_application or has_name):
        # Accept a plain inventory row that carries real sizing signals (CPU/memory/storage)
        # even without a name/application column - e.g. a per-server DB table where each row is
        # one server and there is no hostname column.
        return has_resource
    return has_resource or has_environment or has_descriptive_detail


# Sheet names that only appear in a finished AWS->OCI comparison / BOM workbook (an OUTPUT),
# never in a raw cloud bill. Two or more present => it's a comparison, not a bill.
_COMPARISON_BOM_SHEET_SIGNATURES = (
    "product breakdown", "service mapping", "facts figures", "service comp list",
    "product groupings", "ax compute mapping", "obs management",
    "comparison", "standard pricing", "ax shapes", "expansion commit",
)


def looks_like_comparison_bom(sheet_names):
    """True when the workbook's sheets match the signature of a built comparison/BOM output
    (as opposed to a raw cloud-bill export)."""
    if not sheet_names:
        return False
    norm = [re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip() for s in sheet_names]
    hits = sum(1 for sig in _COMPARISON_BOM_SHEET_SIGNATURES
               if any(sig in n for n in norm))
    return hits >= 2


def parse_workbook_rule_based(path, full_service_beta=False):
    excel_file = pd.ExcelFile(path)
    sheet = pick_sheet(excel_file)
    raw = pd.read_excel(path, sheet_name=sheet, header=None, dtype=object)
    group_row, header_row, data_start = detect_header_rows(raw)
    fields = build_fields(raw, group_row, header_row)
    cpu_field_keys = set()
    memory_field_keys = set()
    storage_field_keys = set()
    unit_factor = {}  # field key -> GB scale implied by the source header (MB/TB/KB)
    # When several CPU columns exist (e.g. raw vCPUs AND a "Rationalized Cores" column), size
    # on ONE primary: a rationalized-cores column (the customer's already-right-sized core
    # count) wins over raw vCPUs. Only the primary becomes the OCPU sizing column; the others
    # stay as plain info columns so the app doesn't double-count or grab the wrong one.
    _cpu_cands = [f for f in fields if spreadsheet_cpu_label(f["label"])]

    def _cpu_rank(f):
        t = normalize(f["label"])
        if "rationalized" in t:
            return 3
        if "core" in t and "vcpu" not in t:      # physical cores
            return 2
        return 1                                 # vcpu / cpu
    _primary_cpu_key = max(_cpu_cands, key=_cpu_rank)["key"] if _cpu_cands else None

    for field in fields:
        if spreadsheet_cpu_label(field["label"]):
            if field["key"] != _primary_cpu_key:
                continue   # secondary CPU column -> keep as an info column, don't size on it
            cpu_field_keys.add(field["key"])
            # Preserve the original CPU header so auto-detection can tell whether the
            # column was labeled vCPU vs OCPU vs rationalized cores (label renamed below).
            field["cpuSourceLabel"] = field["label"]
            field["label"] = ocpu_review_label(field["label"])
        elif spreadsheet_memory_label(field["label"]):
            memory_field_keys.add(field["key"])
            # Honor the header's unit (e.g. "Memory (MB)") before renaming the label.
            unit_factor[field["key"]] = header_unit_factor_to_gb(field["label"])
            field["memorySourceLabel"] = field["label"]
            field["label"] = memory_review_label(field["label"])
        elif spreadsheet_storage_label(field["label"]):
            storage_field_keys.add(field["key"])
            unit_factor[field["key"]] = header_unit_factor_to_gb(field["label"])
            # Recognized storage sits alongside CPU/memory as a core inventory signal, so
            # surface it on the review page instead of leaving it as an incidental column.
            field["important"] = True
            field["storageSourceLabel"] = field["label"]

    rows = []
    for raw_idx in range(data_start, len(raw.index)):
        values = raw.iloc[raw_idx].tolist()
        if not any(clean_text(value) for value in values):
            continue
        row = {"__id": f"row-{raw_idx + 1}", "__sourceRow": raw_idx + 1, "__approved": True}
        for col_idx, field in enumerate(fields):
            value = clean_cell(values[col_idx]) if col_idx < len(values) else ""
            if field["key"] in cpu_field_keys and clean_text(value) != "":
                value = compact_number(to_number(value) / 2)
            elif field["key"] in memory_field_keys and clean_text(value) != "":
                value = compact_number(to_gb_with_header(value, unit_factor.get(field["key"], 1.0)))
            elif field["key"] in storage_field_keys and clean_text(value) != "":
                value = compact_number(to_gb_with_header(value, unit_factor.get(field["key"], 1.0)))
            row[field["key"]] = value
        if rule_based_row_has_inventory_signal(row, fields):
            rows.append(row)

    unit_normalizations = normalize_mixed_inventory_units(
        fields,
        rows,
        memory_keys=memory_field_keys,
        storage_keys=storage_field_keys,
    )

    # Express RAM in whole GB. Inventories often report fractional GB (e.g. 5.95, 15.96) that
    # just clutter the review table and BOM; round to the nearest whole GB once here so the
    # review, pricing, and export all show the same clean figure.
    for _mk in memory_field_keys:
        for _r in rows:
            if clean_text(_r.get(_mk)) == "":
                continue
            try:
                _r[_mk] = compact_number(round(float(str(_r.get(_mk)).replace(",", ""))))
            except (TypeError, ValueError):
                pass

    return {
        "fileName": Path(path).name,
        "sheetName": sheet,
        "sheets": excel_file.sheet_names,
        "fields": fields,
        "rows": rows,
        "rateCard": build_rate_card(DEFAULT_SHAPE_KEY, full_service_beta),
        "rateCards": all_shape_payloads(full_service_beta),
        "fullServiceCatalog": price_catalog_payload(),
        "selectedShape": shape_payload(DEFAULT_SHAPE_KEY, full_service_beta),
        "metadata": {
            "headerRow": header_row + 1,
            "groupRow": group_row + 1,
            "dataStartRow": data_start + 1,
            "rowCount": len(rows),
            "columnCount": len(fields),
            "parser": "rule-based",
            "intakeMode": INTAKE_MODE_ON_PREM,
            "fullServiceBeta": bool(full_service_beta),
            "unitNormalizations": unit_normalizations,
            "extractionNotes": [item["message"] for item in unit_normalizations],
        },
    }


def workbook_digest(path):
    excel_file = pd.ExcelFile(path)
    sheets = []
    for sheet in visible_sheet_names(excel_file, path):
        raw = pd.read_excel(path, sheet_name=sheet, header=None, dtype=object)
        max_rows = min(45, len(raw.index))
        max_cols = min(35, len(raw.columns))
        sample_rows = []
        row_density = []
        json_columns = {}

        for row_idx in range(min(100, len(raw.index))):
            values = raw.iloc[row_idx].tolist()
            non_blank = [clean_text(value) for value in values if clean_text(value)]
            if non_blank:
                row_density.append(
                    {
                        "row": row_idx + 1,
                        "nonBlank": len(non_blank),
                        "preview": non_blank[:12],
                    }
                )

        for row_idx in range(max_rows):
            cells = []
            for col_idx in range(max_cols):
                value = clean_text(raw.iat[row_idx, col_idx])
                if value:
                    cell = {"column": col_idx + 1, "value": value[:140]}
                    json_summary = summarize_json_cell(value)
                    if json_summary:
                        cell["jsonKeys"] = json_summary["keys"]
                        cell["jsonPreview"] = json_summary["preview"]
                        column_info = json_columns.setdefault(
                            col_idx + 1,
                            {
                                "column": col_idx + 1,
                                "sampleRows": [],
                                "keys": {},
                            },
                        )
                        column_info["sampleRows"].append(
                            {
                                "row": row_idx + 1,
                                "preview": json_summary["preview"],
                            }
                        )
                        for key, item in json_summary["preview"].items():
                            column_info["keys"].setdefault(key, clean_text(item)[:80])
                    cells.append(cell)
            if cells:
                sample_rows.append({"row": row_idx + 1, "cells": cells[:24]})

        json_column_summaries = []
        for col_idx, info in json_columns.items():
            json_column_summaries.append(
                {
                    "column": col_idx,
                    "header": json_column_header(raw, col_idx - 1),
                    "keys": list(info["keys"].keys())[:30],
                    "sampleValues": dict(list(info["keys"].items())[:12]),
                    "sampleRows": info["sampleRows"][:3],
                }
            )

        sheets.append(
            {
                "name": sheet,
                "rowCount": int(len(raw.index)),
                "columnCount": int(len(raw.columns)),
                "sampleRows": sample_rows,
                "jsonColumns": json_column_summaries,
                "likelyHeaderRows": sorted(row_density, key=lambda item: item["nonBlank"], reverse=True)[:8],
            }
        )

    return {"sheets": sheets}


def carried_section_label(raw, row_idx, col_idx):
    sections = {"application details", "database details", "oci details"}
    for scan_idx in range(col_idx, -1, -1):
        candidate = clean_text(raw.iat[row_idx, scan_idx])
        if normalize(candidate) in sections:
            return candidate
    return ""


def header_label(raw, header_rows, col_idx):
    parts = []
    for row_number in header_rows:
        row_idx = int(row_number) - 1
        if 0 <= row_idx < len(raw.index):
            part = clean_text(raw.iat[row_idx, col_idx])
            if not part:
                part = carried_section_label(raw, row_idx, col_idx)
            if part and part not in parts:
                parts.append(part)
    return " ".join(parts)


def alias_score(label, field):
    label_norm = normalize(label)
    if not label_norm:
        return 0
    if field["key"] in {"application_name", "machine_name"} and "database" in label_norm and "application" not in label_norm:
        return 0
    if header_is_bare_disk_count(label_norm) and field["key"] in {
        "application_details_local_storage_gb",
        "application_details_shared_storage_gb",
        "database_details_total_allocated_storage_gb",
    }:
        return 0
    aliases = [field["label"], *field["aliases"]]
    label_tokens = set(label_norm.split())
    score = 0
    for alias in aliases:
        alias_norm = normalize(alias)
        if not alias_norm:
            continue
        if label_norm == alias_norm:
            score = max(score, 100 + len(alias_norm))
        elif alias_norm in label_norm:
            score = max(score, 60 + len(alias_norm))
        elif label_norm in alias_norm and len(label_norm) >= 4:
            score = max(score, 30 + len(label_norm))
        else:
            # Token-subset match: every word of a multi-word alias is present in the
            # header, in any order and with filler words between - so "disk gb" matches
            # "Disk in GB", "storage gb" matches "Storage (GB)". Guards against single-token
            # aliases (too loose) and against the alias being just one word.
            alias_tokens = alias_norm.split()
            if len(alias_tokens) >= 2 and set(alias_tokens) <= label_tokens:
                score = max(score, 55 + len(alias_norm))

    is_database = any(term in label_norm for term in ["database", "db ", " db", "sql", "oracle db"])
    field_is_database = field["key"].startswith("database_details")
    if is_database and field_is_database:
        score += 14
    elif is_database and not field_is_database:
        score -= 12
    elif field_is_database:
        score -= 18

    is_shared = any(term in label_norm for term in ["shared", "nas", "nfs", "file"])
    if is_shared and field["key"] == "application_details_shared_storage_gb":
        score += 16
    elif is_shared and field["key"] == "application_details_local_storage_gb":
        score -= 10

    return score


def infer_column_mappings(raw, header_rows, full_service_beta=False, intake_mode=INTAKE_MODE_ON_PREM, data_start_row=None):
    mappings = {}
    for col_idx in range(len(raw.columns)):
        label = header_label(raw, header_rows, col_idx)
        best = None
        best_score = 0
        for field in inventory_fields(full_service_beta, intake_mode):
            if storage_mapping_disallowed(raw, col_idx, field["key"], label, data_start_row):
                continue
            score = alias_score(label, field)
            if score > best_score:
                best = field
                best_score = score
        if best and best_score >= 45:
            existing = mappings.get(best["key"])
            if existing and existing.get("_score", 0) >= best_score:
                continue
            mappings[best["key"]] = {
                "canonicalKey": best["key"],
                "sourceColumn": col_idx + 1,
                "sourceHeader": label,
                "confidence": min(0.98, best_score / 130),
                "_score": best_score,
            }
    return mappings


JSON_TAG_FIELD_ALIASES = {
    "application_name": ["appName", "application", "applicationName", "appId", "appID"],
    "machine_name": ["Name", "name", "machineName", "serverName", "hostname", "hostName", "vmName", "instanceName"],
    "environment": ["environment", "env", "stage", "lifecycle"],
    "application_details": ["role", "description", "appId", "costCenter", "owner"],
    "application_details_application_version": ["applicationVersion", "appVersion", "version", "release"],
    "application_details_operating_system": ["os", "operatingSystem", "operating_system", "platform"],
}


def infer_json_mappings(raw, header_rows, data_start_row):
    mappings = {}
    start_idx = max(0, data_start_row - 1)
    end_idx = min(len(raw.index), start_idx + 30)
    for col_idx in range(len(raw.columns)):
        label = header_label(raw, header_rows, col_idx)
        column_pairs = {}
        for row_idx in range(start_idx, end_idx):
            for key, value in flatten_json_tags(raw.iat[row_idx, col_idx]).items():
                column_pairs.setdefault(key, clean_text(value))
        if not column_pairs:
            continue

        for canonical_key, aliases in JSON_TAG_FIELD_ALIASES.items():
            if canonical_key in mappings:
                continue
            best_key = ""
            best_score = 0
            for candidate in column_pairs:
                for alias in aliases:
                    score = json_key_match_score(candidate, alias)
                    if score > best_score:
                        best_key = candidate
                        best_score = score
            if best_score >= 60:
                mappings[canonical_key] = {
                    "canonicalKey": canonical_key,
                    "sourceColumn": col_idx + 1,
                    "sourceHeader": label,
                    "jsonKey": best_key,
                    "confidence": min(0.95, best_score / 100),
                    "transform": f"Read '{best_key}' from JSON/tag data.",
                }
    return mappings


def validated_column_mappings(raw, header_rows, mappings, full_service_beta=False, intake_mode=INTAKE_MODE_ON_PREM, data_start_row=None):
    validated = {}
    field_lookup = {field["key"]: field for field in inventory_fields(full_service_beta, intake_mode)}
    for key, mapping in mappings.items():
        field = field_lookup.get(key)
        if not field:
            continue
        source_column = int(to_number(mapping.get("sourceColumn"), 0))
        if source_column <= 0:
            continue
        actual_header = header_label(raw, header_rows, source_column - 1) or clean_text(mapping.get("sourceHeader"))
        if key == "application_details_number_of_servers" and column_looks_like_disk_count(
            raw, source_column - 1, actual_header, data_start_row
        ):
            continue
        if storage_mapping_disallowed(raw, source_column - 1, key, actual_header, data_start_row):
            continue
        if key == "database_details_total_allocated_storage_gb" and not header_has_database_signal(actual_header):
            continue
        has_json_source = clean_text(mapping.get("jsonKey") or mapping.get("jsonPath"))
        if has_json_source:
            json_aliases = [field["label"], *field["aliases"], *JSON_TAG_FIELD_ALIASES.get(key, [])]
            if max((json_key_match_score(has_json_source, alias) for alias in json_aliases), default=0) < 40:
                continue
        if actual_header and not has_json_source and alias_score(actual_header, field) < 35:
            continue
        validated[key] = {
            **mapping,
            "sourceHeader": actual_header,
            "sourceColumn": source_column,
        }
    return validated


def normalize_workbook_plan(plan, excel_file, full_service_beta=False, intake_mode=INTAKE_MODE_ON_PREM):
    if not isinstance(plan, dict):
        return None
    sheet_name = clean_text(plan.get("sheetName"))
    if sheet_name not in excel_file.sheet_names:
        normalized_target = normalize(sheet_name)
        matches = [name for name in excel_file.sheet_names if normalize(name) == normalized_target]
        sheet_name = matches[0] if matches else ""
    if not sheet_name:
        return None

    header_rows = plan.get("headerRows") or plan.get("headerRow") or []
    if isinstance(header_rows, (str, int, float)):
        header_rows = [header_rows]
    header_rows = [int(to_number(item)) for item in header_rows if to_number(item)]
    header_rows = sorted({row for row in header_rows if row > 0})

    data_start = int(to_number(plan.get("dataStartRow"), 0))
    if data_start <= 0 and header_rows:
        data_start = max(header_rows) + 1
    if data_start <= 0:
        data_start = 2

    data_end = int(to_number(plan.get("dataEndRow"), 0))
    raw_mappings = plan.get("columnMappings", [])
    if isinstance(raw_mappings, dict):
        raw_mappings = [
            {"canonicalKey": key, **value} if isinstance(value, dict) else {"canonicalKey": key, "sourceColumn": value}
            for key, value in raw_mappings.items()
        ]

    mappings = {}
    field_lookup = {field["key"]: field for field in inventory_fields(full_service_beta, intake_mode)}
    for item in raw_mappings:
        if not isinstance(item, dict):
            continue
        key = clean_text(item.get("canonicalKey") or item.get("key"))
        source_column = int(to_number(item.get("sourceColumn"), 0))
        if key in field_lookup and source_column > 0:
            mappings[key] = {
                "canonicalKey": key,
                "sourceColumn": source_column,
                "sourceHeader": clean_text(item.get("sourceHeader")),
                "jsonKey": clean_text(item.get("jsonKey") or item.get("tagKey")),
                "jsonPath": clean_text(item.get("jsonPath")),
                "sourceUnit": clean_text(item.get("sourceUnit")) or "unknown",
                "confidence": to_number(item.get("confidence"), 0),
                "transform": clean_text(item.get("transform")),
            }

    return {
        "sheetName": sheet_name,
        "headerRows": header_rows,
        "dataStartRow": data_start,
        "dataEndRow": data_end or None,
        "serverGrain": normalize(plan.get("serverGrain")) or "unknown",
        "confidence": to_number(plan.get("confidence"), 0),
        "columnMappings": mappings,
        "notes": plan.get("notes", []),
    }


def should_keep_inventory_row(row):
    identity = (
        clean_text(row.get("application_name"))
        or clean_text(row.get("machine_name"))
        or clean_text(row.get("environment"))
    )
    resources = [
        to_number(row.get("application_details_number_of_servers")),
        to_number(row.get("application_details_number_of_cpu_cores_per_server")),
        to_number(row.get("application_details_memory_per_server_gb")),
        to_number(row.get("application_details_local_storage_gb")),
        to_number(row.get("database_details_number_of_database_servers")),
        to_number(row.get("database_details_number_of_cpu_cores_per_server")),
        to_number(row.get("database_details_memory_per_server_gb")),
        to_number(row.get("database_details_total_allocated_storage_gb")),
    ]
    populated_fields = sum(
        1
        for field in CANONICAL_FIELD_BY_KEY.values()
        if clean_text(row.get(field["key"])) not in {"", "0", "0.0"}
    )
    full_service_signal = any(clean_text(row.get(key)) for key in SOURCE_SERVICE_FIELD_KEYS)
    resource_signal = any(value for value in resources)
    if full_service_signal:
        return True
    if not identity:
        return False
    return bool(resource_signal or populated_fields >= 2)


def normalize_planned_inventory_value(key, value, mapping):
    """Apply only unit conversions declared in the validated structured plan."""
    if clean_text(value) == "":
        return ""
    unit = clean_text((mapping or {}).get("sourceUnit")).lower()
    if key in SIZE_FIELD_KEYS and unit in {"mb", "mib", "gb", "gib", "tb", "tib"}:
        number = to_number(value)
        factors = {
            "mb": 1 / 1000,
            "mib": 1 / 1024,
            "gb": 1,
            "gib": GIB_TO_GB,
            "tb": 1000,
            "tib": 1024 * GIB_TO_GB,
        }
        return compact_number(number * factors[unit])
    return normalize_inventory_value(key, value)


def parse_workbook_from_plan(path, plan, full_service_beta=False, intake_mode=INTAKE_MODE_ON_PREM):
    excel_file = pd.ExcelFile(path)
    raw = pd.read_excel(path, sheet_name=plan["sheetName"], header=None, dtype=object)
    header_rows = plan["headerRows"] or [max(1, plan["dataStartRow"] - 1)]
    mappings = validated_column_mappings(
        raw,
        header_rows,
        dict(plan["columnMappings"]),
        full_service_beta,
        intake_mode,
        plan["dataStartRow"],
    )
    inferred_json = infer_json_mappings(raw, header_rows, plan["dataStartRow"])
    for key, mapping in inferred_json.items():
        mappings.setdefault(key, mapping)
    inferred = infer_column_mappings(raw, header_rows, full_service_beta, intake_mode, plan["dataStartRow"])
    for key, mapping in inferred.items():
        existing = mappings.get(key)
        if (
            key == "database_details_total_allocated_storage_gb"
            and existing
            and "total allocated" in normalize(mapping.get("sourceHeader"))
            and "total allocated" not in normalize(existing.get("sourceHeader"))
        ):
            mappings[key] = mapping
        else:
            mappings.setdefault(key, mapping)

    fields = canonical_fields_payload(full_service_beta, intake_mode)
    for field in fields:
        mapping = mappings.get(field["key"])
        if mapping:
            field["sourceColumn"] = mapping["sourceColumn"]
            field["sourceHeader"] = mapping.get("sourceHeader") or header_label(raw, header_rows, mapping["sourceColumn"] - 1)
            field["sourceUnit"] = mapping.get("sourceUnit") or "unknown"
            if field["key"] in CPU_FIELD_KEYS:
                field["cpuSourceLabel"] = (
                    mapping.get("sourceUnit")
                    if mapping.get("sourceUnit") in {"vCPU", "OCPU"}
                    else field["sourceHeader"]
                )
            elif field["key"] in SIZE_FIELD_KEYS:
                field["sizeSourceLabel"] = field["sourceHeader"]
            if mapping.get("jsonKey"):
                field["sourceJsonKey"] = mapping["jsonKey"]

    def build_rows(data_start_row, data_end_row):
        parsed_rows = []
        row_end = min(data_end_row or len(raw.index), len(raw.index))
        data_start_idx = max(0, data_start_row - 1)

        for raw_idx in range(data_start_idx, row_end):
            values = raw.iloc[raw_idx].tolist()
            if not any(clean_text(value) for value in values):
                continue

            row = {"__id": f"row-{raw_idx + 1}", "__sourceRow": raw_idx + 1, "__approved": True}
            for field in fields:
                mapping = mappings.get(field["key"])
                value = ""
                if mapping:
                    col_idx = mapping["sourceColumn"] - 1
                    if 0 <= col_idx < len(values):
                        value = values[col_idx]
                        if mapping.get("jsonKey") or mapping.get("jsonPath"):
                            value = value_from_json_cell(value, mapping.get("jsonKey") or mapping.get("jsonPath"))
                row[field["key"]] = normalize_planned_inventory_value(
                    field["key"], value, mapping
                )

            if plan["serverGrain"] in {"server", "vm", "host", "asset", "inventory row"}:
                has_resource_shape = (
                    clean_text(row.get("application_name"))
                    or clean_text(row.get("machine_name"))
                    or to_number(
                    row.get("application_details_number_of_cpu_cores_per_server")
                    )
                    or to_number(row.get("application_details_memory_per_server_gb"))
                )
                if not row.get("application_details_number_of_servers") and has_resource_shape:
                    row["application_details_number_of_servers"] = 1

            if should_keep_inventory_row(row):
                parsed_rows.append(row)
        return parsed_rows, row_end

    data_start_row = plan["dataStartRow"]
    rows, row_end = build_rows(data_start_row, plan.get("dataEndRow"))
    fallback_start_row = max(header_rows) + 1 if header_rows else 2
    if not rows and data_start_row != fallback_start_row:
        data_start_row = fallback_start_row
        rows, row_end = build_rows(data_start_row, None)

    if not rows:
        raise ValueError("The OpenAI workbook plan did not produce inventory rows.")

    memory_keys = {
        field["key"]
        for field in fields
        if field["key"] in SIZE_FIELD_KEYS and "memory" in normalize(field.get("label"))
    }
    storage_keys = {
        field["key"]
        for field in fields
        if field["key"] in SIZE_FIELD_KEYS and field["key"] not in memory_keys
    }
    unit_normalizations = normalize_mixed_inventory_units(
        fields,
        rows,
        memory_keys=memory_keys,
        storage_keys=storage_keys,
    )
    plan_notes = [clean_text(note) for note in plan.get("notes", []) if clean_text(note)]
    extraction_notes = [
        *plan_notes,
        *[item["message"] for item in unit_normalizations],
    ]

    return {
        "fileName": Path(path).name,
        "sheetName": plan["sheetName"],
        "sheets": excel_file.sheet_names,
        "fields": fields,
        "rows": rows,
        "rateCard": build_rate_card(DEFAULT_SHAPE_KEY, full_service_beta),
        "rateCards": all_shape_payloads(full_service_beta),
        "fullServiceCatalog": price_catalog_payload(),
        "selectedShape": shape_payload(DEFAULT_SHAPE_KEY, full_service_beta),
        "metadata": {
            "headerRows": header_rows,
            "dataStartRow": data_start_row,
            "dataEndRow": row_end,
            "rowCount": len(rows),
            "columnCount": len(fields),
            "parser": "llm-assisted",
            "intakeMode": intake_mode,
            "fullServiceBeta": bool(full_service_beta),
            "confidence": plan.get("confidence", 0),
            "serverGrain": plan.get("serverGrain", "unknown"),
            "unitNormalizations": unit_normalizations,
            "extractionNotes": extraction_notes,
            "reviewSchema": [
                "Application Name",
                "Machine Name",
                "Environment",
                "OCPUs",
                "RAM (GB)",
                "Storage (GB)",
                "Hours Running",
            ],
        },
    }


CLOUD_PROVIDER_SIGNATURES = {
    "aws": [
        "lineitem",
        "line item",
        "productcode",
        "usageaccountid",
        "unblendedcost",
        "netunblendedcost",
        "aws",
        "amazon",
        "cur",
    ],
    "azure": [
        "metercategory",
        "metersubcategory",
        "metername",
        "costinbillingcurrency",
        "resourcelocation",
        "subscriptionid",
        "azure",
        "microsoft",
    ],
    "gcp": [
        "service description",
        "sku description",
        "usage amount",
        "usage unit",
        "project id",
        "location region",
        "billing account",
        "gcp",
        "google",
    ],
}

CLOUD_COLUMN_ALIASES = {
    "source_account": {
        "aws": ["lineitem usageaccountid", "line item usage account id", "bill payeraccountid", "usage account id"],
        "azure": ["subscriptionid", "subscription id", "subscriptionname", "subscription name"],
        "gcp": ["project id", "project name", "project number", "billing account id"],
        "common": ["account id", "account name", "project id", "subscription id", "billing account"],
    },
    "source_service": {
        "aws": ["product productname", "product product name", "lineitem productcode", "productcode", "service"],
        "azure": ["metercategory", "meter category", "consumedservice", "consumed service", "service name"],
        "gcp": ["service description", "service id", "service"],
        "common": ["service", "service name", "product name", "meter category"],
    },
    "source_product": {
        "aws": [
            "usagetype",
            "lineitem usagetype",
            "line item usage type",
            "itemdescription",
            "item description",
            "lineitem lineitemdescription",
            "line item description",
            "product servicename",
            "operation",
        ],
        "azure": ["metername", "meter name", "metersubcategory", "meter subcategory", "productname", "product name"],
        "gcp": ["sku description", "sku id", "sku"],
        "common": ["sku", "meter", "meter name", "usage type", "description", "line item description"],
    },
    "source_region": {
        "aws": ["product region", "region", "lineitem availabilityzone", "availability zone"],
        "azure": ["resourcelocation", "resource location", "location"],
        "gcp": ["location region", "location location", "region"],
        "common": ["region", "resource location", "location", "availability zone"],
    },
    "usage_quantity": {
        "aws": ["lineitem usageamount", "line item usage amount", "usageamount", "usage amount"],
        "azure": ["quantity", "consumedquantity", "consumed quantity"],
        "gcp": ["usage amount", "usage amount in pricing units", "usage pricing unit quantity"],
        "common": ["usage amount", "usage quantity", "quantity", "qty", "consumed quantity"],
    },
    "usage_unit": {
        "aws": ["pricing unit", "pricing/unit", "usage unit", "unit"],
        "azure": ["unitofmeasure", "unit of measure", "unit"],
        "gcp": ["usage unit", "usage pricing unit"],
        "common": ["unit", "usage unit", "unit of measure", "pricing unit"],
    },
    "resource_ocpus": {
        "aws": ["vcpu", "vcpus", "cpu", "cpus", "core count"],
        "azure": ["vcpu", "vcpus", "cpu", "cpus", "core count"],
        "gcp": ["vcpu", "vcpus", "cpu", "cpus", "core count"],
        "common": ["ocpu", "ocpus", "vcpu", "vcpus", "cpu", "cpus", "cores", "core count"],
    },
    "resource_memory_gb": {
        "aws": ["memory", "memory gb", "ram", "ram gb"],
        "azure": ["memory", "memory gb", "ram", "ram gb"],
        "gcp": ["memory", "memory gb", "ram", "ram gb"],
        "common": ["memory", "memory gb", "ram", "ram gb", "mem gb", "gb ram"],
    },
    "source_monthly_cost": {
        "aws": [
            "totalcost", "total cost",
            "costbeforetax", "cost before tax",
            "lineitem netunblendedcost", "lineitem unblendedcost", "net unblended cost", "unblended cost",
            "blendedcost", "blended cost",
        ],
        "azure": ["costinbillingcurrency", "cost in billing currency", "pretaxcost", "pretax cost", "cost"],
        "gcp": ["cost", "net cost"],
        "common": ["cost", "source cost", "amount", "charge"],
    },
    "source_currency": {
        "aws": ["pricing currency", "currency"],
        "azure": ["billingcurrencycode", "billing currency code", "currency"],
        "gcp": ["currency"],
        "common": ["currency", "billing currency"],
    },
    "source_period": {
        "aws": ["lineitem usagestartdate", "usage start date", "bill billingperiodstartdate", "billing period"],
        "azure": ["date", "usagedate", "usage date"],
        "gcp": ["usage start time", "invoice month", "export time"],
        "common": ["date", "month", "billing period", "usage start date"],
    },
    "source_tags": {
        "aws": ["resource tags", "resource tag", "user tag", "tag"],
        "azure": ["tags", "additionalinfo", "additional info"],
        "gcp": ["labels", "system labels"],
        "common": ["tags", "labels"],
    },
}


def read_bill_table(path, sheet_name=None):
    suffix = Path(path).suffix.lower()
    if suffix in {".csv", ".tsv"}:
        separator = "\t" if suffix == ".tsv" else None
        return pd.read_csv(path, header=None, dtype=object, sep=separator, engine="python")
    return pd.read_excel(path, sheet_name=sheet_name, header=None, dtype=object)


def cloud_header_score(values):
    text = normalize(" ".join(clean_text(value) for value in values if clean_text(value)))
    if not text:
        return 0
    score = 0
    terms = {
        "service": 3,
        "sku": 3,
        "meter": 3,
        "usage": 4,
        "quantity": 3,
        "cost": 4,
        "currency": 2,
        "lineitem": 4,
        "subscription": 3,
        "project": 3,
        "region": 2,
    }
    for term, weight in terms.items():
        if term in text:
            score += weight
    return score


def detect_cloud_header_row(raw):
    sample_rows = min(25, len(raw.index))
    best_score = 0
    best_idx = 0
    for row_idx in range(sample_rows):
        values = raw.iloc[row_idx].tolist()
        score = cloud_header_score(values)
        if score > best_score:
            best_score = score
            best_idx = row_idx
    return best_idx


def unique_headers(values):
    headers = []
    seen = {}
    for col_idx, value in enumerate(values):
        label = clean_text(value) or f"Column {col_idx + 1}"
        count = seen.get(label, 0) + 1
        seen[label] = count
        headers.append(label if count == 1 else f"{label} {count}")
    return headers


def detect_cloud_provider(headers, sample_rows=None, provider_hint=PROVIDER_AUTO):
    provider_names = {"aws": "AWS", "azure": "Azure", "gcp": "GCP"}
    hint = normalize_provider_hint(provider_hint)
    if hint != PROVIDER_AUTO:
        return provider_names[hint], 1.0

    parts = [*headers]
    for row in (sample_rows or [])[:12]:
        parts.extend(clean_text(value) for value in row if clean_text(value))
    text = normalize(" ".join(parts))

    scores = {}
    for provider, terms in CLOUD_PROVIDER_SIGNATURES.items():
        scores[provider] = sum(1 for term in terms if normalize(term) in text)
    provider = max(scores, key=scores.get)
    score = scores.get(provider, 0)
    if score <= 0:
        return "Unknown", 0.0
    confidence = min(0.98, 0.34 + (score * 0.11))
    return provider_names[provider], round(confidence, 2)


def header_alias_score(header, aliases):
    header_norm = normalize(header)
    if not header_norm:
        return 0
    best = 0
    for alias in aliases:
        alias_norm = normalize(alias)
        if not alias_norm:
            continue
        if header_norm == alias_norm:
            best = max(best, 120 + len(alias_norm))
        elif header_norm.endswith(alias_norm):
            best = max(best, 88 + len(alias_norm))
        elif alias_norm in header_norm:
            best = max(best, 64 + len(alias_norm))
        elif header_norm in alias_norm and len(header_norm) >= 4:
            best = max(best, 42 + len(header_norm))
    return best


def infer_cloud_bill_mappings(headers, detected_provider):
    provider = normalize_provider_hint(detected_provider)
    if provider == PROVIDER_AUTO and normalize(detected_provider) == "unknown":
        provider = "common"
    mappings = {}
    for field in CLOUD_BILL_FIELDS:
        aliases_by_provider = CLOUD_COLUMN_ALIASES.get(field["key"], {})
        aliases = [*aliases_by_provider.get(provider, []), *aliases_by_provider.get("common", []), *field["aliases"]]
        best_idx = None
        best_score = 0
        for idx, header in enumerate(headers):
            score = header_alias_score(header, aliases)
            if score > best_score:
                best_idx = idx
                best_score = score
        minimum_score = 64 if field["key"] in {"resource_ocpus", "resource_memory_gb"} else 48
        if best_idx is not None and best_score >= minimum_score:
            mappings[field["key"]] = {
                "canonicalKey": field["key"],
                "sourceColumn": best_idx + 1,
                "sourceHeader": headers[best_idx],
                "confidence": min(0.98, best_score / 150),
            }
    return mappings


def cloud_bill_value(key, value):
    if key in {"usage_quantity", "source_monthly_cost", "mapping_confidence"}:
        if not clean_text(value):
            return ""
        num = to_number(value, 0)
        # Cost and usage can't be negative; a negative here is a parse artifact.
        if key in {"usage_quantity", "source_monthly_cost"} and num < 0:
            num = 0
        return compact_number(num)
    if key == "resource_ocpus":
        return compact_number(to_number(value, 0)) if clean_text(value) else ""
    if key == "resource_memory_gb":
        return compact_number(to_gb(value)) if clean_text(value) else ""
    return clean_cell(value)


def value_at(values, index):
    return values[index] if 0 <= index < len(values) else ""


def text_at(values, index):
    return clean_text(value_at(values, index))


def numeric_text_only(value):
    text = clean_text(value)
    return bool(text and re.fullmatch(r"[\-$€£¥0-9,.\s%()]+", text))


def oci_mapping_text(value):
    text = clean_text(value)
    if not text:
        return ""
    normalized = normalize(text)
    if normalized in {"service", "services", "sku", "skus", "oci cost", "hrs", "hours", "instances", "bandwidth"}:
        return ""
    if "no direct mapping" in normalized:
        return ""
    if numeric_text_only(text):
        return ""
    return text


def embedded_azure_region(text):
    value = clean_text(text)
    if not value:
        return ""
    region_terms = [
        "US Central",
        "US East 2",
        "US East",
        "US North Central",
        "US South Central",
        "US West 3",
        "US West 2",
        "US West",
        "Canada Central",
        "Canada East",
        "Brazil South",
        "North Europe",
        "West Europe",
        "UK South",
        "UK West",
        "East Asia",
        "Southeast Asia",
        "Australia East",
        "Australia Southeast",
        "Central India",
        "South India",
        "West India",
        "Japan East",
        "Japan West",
        "Korea Central",
        "Korea South",
        "France Central",
        "Germany West Central",
        "Norway East",
        "Sweden Central",
        "Switzerland North",
        "UAE North",
    ]
    value_norm = normalize(value)
    for region in sorted(region_terms, key=len, reverse=True):
        if normalize(region) in value_norm:
            return region
    match = re.search(r"(?:-|,)\s*((?:US|Canada|Brazil|North|West|East|South|Central|Southeast|Australia|Japan|Korea|France|Germany|Norway|Sweden|Switzerland|UAE|UK|India)[A-Za-z ]*(?:\s\d)?)\b", value)
    return clean_text(match.group(1)) if match else ""


def azure_mapping_header_indexes(values):
    indexes = {}
    for idx, value in enumerate(values):
        normalized = normalize(value)
        if not normalized:
            continue
        if normalized in {"quantity", "qty"}:
            indexes["quantity"] = idx
        elif "unit" in normalized and "measure" in normalized:
            indexes["unit"] = idx
        elif normalized in {"vcpu", "vcpus", "cpu", "cpus"}:
            indexes["source_vcpu"] = idx
        elif normalized in {"ram", "memory", "memory gb", "ram gb"}:
            if idx >= 13:
                indexes["oci_ram"] = idx
            else:
                indexes["source_ram"] = idx
        elif normalized in {"ocpu or ecpu", "ocpu", "ecpu", "ocpus"}:
            indexes["oci_ocpu"] = idx
        elif normalized == "service":
            indexes["oci_service"] = idx
        elif normalized in {"skus", "sku"}:
            indexes["oci_sku"] = idx
        elif normalized == "oci cost":
            indexes["oci_cost"] = idx
        elif "cost" in normalized or clean_text(value).startswith("$"):
            indexes.setdefault("source_cost", idx)
    return indexes


def looks_like_azure_service_mapping_sheet(raw):
    preview = normalize(
        " ".join(
            clean_text(raw.iat[row_idx, col_idx])
            for row_idx in range(min(20, len(raw.index)))
            for col_idx in range(min(24, len(raw.columns)))
            if clean_text(raw.iat[row_idx, col_idx])
        )
    )
    return bool(
        "azure" in preview
        and "oci equivalent" in preview
        and "unit of measure" in preview
        and "skus" in preview
    )


def apply_cloud_field_source(fields, key, source_column, source_header):
    for field in fields:
        if field.get("key") == key:
            field["sourceColumn"] = source_column
            field["sourceHeader"] = source_header
            return


def parse_azure_service_mapping_table(path, sheet_name, raw, provider_hint=PROVIDER_AUTO, sheet_names=None):
    if normalize_provider_hint(provider_hint) not in {PROVIDER_AUTO, "azure"}:
        return None
    if "service mapping" not in normalize(sheet_name) and not looks_like_azure_service_mapping_sheet(raw):
        return None
    if not looks_like_azure_service_mapping_sheet(raw):
        return None

    fields = canonical_fields_payload(True, INTAKE_MODE_CLOUD_BILL)
    source_columns = {
        "source_service": (2, "Azure service group"),
        "source_product": (2, "Azure SKU / meter"),
        "usage_quantity": (3, "Quantity"),
        "usage_unit": (4, "Unit of Measure"),
        "resource_ocpus": (19, "OCI OCPU or ECPU"),
        "resource_memory_gb": (20, "OCI RAM"),
        "oci_service_category": (15, "OCI Service"),
        "oci_product": (16, "OCI SKUs"),
    }
    for key, (source_column, source_header) in source_columns.items():
        apply_cloud_field_source(fields, key, source_column, source_header)

    rows = []
    rate_card = build_rate_card(DEFAULT_SHAPE_KEY, True)
    current_service = ""
    current_target = ""
    current_header = {}
    first_header_row = None

    for raw_idx in range(len(raw.index)):
        values = raw.iloc[raw_idx].tolist()
        source_text = text_at(values, 1)
        quantity_text = text_at(values, 2)
        unit_text = text_at(values, 3)
        row_header = azure_mapping_header_indexes(values)
        is_section_header = bool(
            source_text
            and (
                normalize(quantity_text) in {"quantity", "qty"}
                or normalize(text_at(values, 14)) == "service"
                or normalize(text_at(values, 15)) in {"skus", "sku"}
            )
        )
        if is_section_header:
            current_service = source_text
            current_target = oci_mapping_text(text_at(values, 13))
            current_header = row_header
            first_header_row = first_header_row or raw_idx + 1
            continue

        if not source_text or normalize(source_text) in {"azure", "oci equivalent"}:
            continue
        if normalize(quantity_text) in {"quantity", "qty"} or normalize(unit_text) == "unit of measure":
            continue
        if not any(clean_text(value) for value in values):
            continue

        quantity = cloud_bill_value("usage_quantity", value_at(values, current_header.get("quantity", 2)))
        source_product = clean_cell(source_text)
        if not source_product and not quantity:
            continue

        source_cost = ""
        if "source_cost" in current_header:
            source_cost = cloud_bill_value("source_monthly_cost", value_at(values, current_header["source_cost"]))

        left_vcpu = to_number(value_at(values, current_header.get("source_vcpu", -1)), 0) if "source_vcpu" in current_header else 0
        left_ram = to_number(value_at(values, current_header.get("source_ram", -1)), 0) if "source_ram" in current_header else 0
        target_ocpus = to_number(value_at(values, current_header.get("oci_ocpu", -1)), 0) if "oci_ocpu" in current_header else 0
        target_ram = to_number(value_at(values, current_header.get("oci_ram", -1)), 0) if "oci_ram" in current_header else 0
        if not target_ram and "source_ram" in current_header:
            target_ram = left_ram
        if not target_ocpus and left_vcpu:
            target_ocpus = left_vcpu / 2

        row_target = oci_mapping_text(text_at(values, current_header.get("oci_service", 14))) or current_target
        row_sku = oci_mapping_text(text_at(values, current_header.get("oci_sku", 15)))
        no_direct_mapping = "no direct mapping" in normalize(text_at(values, 13)) or "no direct mapping" in normalize(current_target)

        row = {
            "__id": f"azure-map-row-{raw_idx + 1}",
            "__sourceRow": raw_idx + 1,
            "__approved": True,
            "source_provider": "Azure",
            "source_account": "",
            "source_service": current_service or source_product.split(" - ")[0],
            "source_product": source_product,
            "source_region": embedded_azure_region(source_product),
            "usage_quantity": quantity,
            "usage_unit": cloud_bill_value("usage_unit", value_at(values, current_header.get("unit", 3))),
            "resource_ocpus": compact_number(target_ocpus) if target_ocpus else "",
            "resource_memory_gb": compact_number(target_ram) if target_ram else "",
            "source_monthly_cost": source_cost,
            "source_currency": "USD",
            "source_period": "",
            "source_tags": f"Workbook sheet: {sheet_name}; source row {raw_idx + 1}",
            "oci_service_category": "" if no_direct_mapping else row_target,
            "oci_product": "" if no_direct_mapping else row_sku,
            "mapping_confidence": "",
        }
        seed_cloud_bill_mapping(row, fields, rate_card)
        if cloud_row_has_signal(row):
            rows.append(row)

    if not rows:
        return None

    mapped_count = sum(1 for row in rows if row_mapping_is_confident(row))
    return {
        "fileName": Path(path).name,
        "sheetName": sheet_name,
        "sheets": sheet_names or [sheet_name],
        "fields": fields,
        "rows": rows,
        "rateCard": build_rate_card(DEFAULT_SHAPE_KEY, True),
        "rateCards": all_shape_payloads(True),
        "fullServiceCatalog": price_catalog_payload(),
        "selectedShape": shape_payload(DEFAULT_SHAPE_KEY, True),
        "metadata": {
            "intakeMode": INTAKE_MODE_CLOUD_BILL,
            "providerHint": normalize_provider_hint(provider_hint),
            "detectedProvider": "Azure",
            "providerConfidence": 1,
            "parser": "azure-service-mapping-workbook",
            "sourceCurrency": "USD",
            "mappedCount": mapped_count,
            "unmappedCount": len(rows) - mapped_count,
            "headerRows": [first_header_row] if first_header_row else [],
            "dataStartRow": (first_header_row + 1) if first_header_row else 1,
            "rowCount": len(rows),
            "columnCount": len(fields),
            "extractionNotes": [
                "Detected side-by-side Azure-to-OCI service mapping workbook.",
                "Azure source rows were read from the left side and OCI target service/SKU/resource values from the right side.",
            ],
        },
    }


def _load_cloud_shape_map():
    """Load the AWS/Azure/GCP -> OCI instance sizing reference (extracted from the mapping workbook)."""
    path = Path(__file__).resolve().parent / "data" / "cloud_shape_map.json"
    index = {}
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return index
    for entry in payload.get("shapes", []):
        key = entry.get("key") or re.sub(r"[^a-z0-9]", "", str(entry.get("instance", "")).lower())
        if key:
            index.setdefault(key, entry)
    return index


# Exact per-instance sizing from the provided cloud mapping doc (authoritative over heuristics).
CLOUD_SHAPE_MAP = _load_cloud_shape_map()
# Longest keys first so e.g. "e2standard16" wins over a shorter accidental substring.
CLOUD_SHAPE_KEYS_BY_LEN = sorted(CLOUD_SHAPE_MAP.keys(), key=len, reverse=True)


@functools.lru_cache(maxsize=32768)
def _cloud_shape_for_collapsed(collapsed):
    """Pure substring scan over the instance-type keys. Memoized on the collapsed
    alphanumeric string so the same workload context isn't re-scanned thousands of
    times on a large bill. Returns the exact same shared CLOUD_SHAPE_MAP record the
    uncached scan would, so results are byte-for-byte identical."""
    for key in CLOUD_SHAPE_KEYS_BY_LEN:
        if len(key) >= 4 and key in collapsed:
            return CLOUD_SHAPE_MAP[key]
    return None


def lookup_cloud_shape(context):
    """Return the mapping-doc record whose instance type appears in the bill context, else None."""
    collapsed = re.sub(r"[^a-z0-9]", "", str(context).lower())
    if not collapsed:
        return None
    return _cloud_shape_for_collapsed(collapsed)


def _load_oci_shapes():
    path = Path(__file__).resolve().parent / "data" / "oci_shapes.json"
    try:
        return json.loads(path.read_text())
    except Exception:
        return {"vendorTiers": {}, "allShapes": []}


OCI_SHAPES = _load_oci_shapes()
OCI_VENDOR_TIERS = OCI_SHAPES.get("vendorTiers", {})


def _load_oci_gpu_shapes():
    path = Path(__file__).resolve().parent / "data" / "oci_gpu_shapes.json"
    index = {}
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return index
    for s in payload.get("shapes", []):
        index[s["shape"]] = s
    return index


OCI_GPU_SHAPES = _load_oci_gpu_shapes()
GPU_HOURS_PER_MONTH = 730


def _load_source_cloud_rates():
    path = Path(__file__).resolve().parent / "data" / "source_cloud_rates.json"
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


SOURCE_CLOUD_RATES = _load_source_cloud_rates()


def _instance_generation(provider, name):
    """Best-effort generation number for an instance name (higher = newer).
    AWS: the digit in the family token (m7a -> 7). Azure: the v-number (D16ads v6 -> 6)."""
    if not name:
        return 0
    text = str(name).lower()
    if provider == "azure":
        # Azure version is a standalone token like " v6" - must not match the "v"
        # inside a family name such as "NV72ads".
        match = re.search(r"(?:\s|_)v(\d+)\b", text)
        return int(match.group(1)) if match else 1
    match = re.match(r"[a-z]+(\d+)", text.split(".")[0])
    return int(match.group(1)) if match else 0


def _is_specialty_instance(provider, name, family=None):
    """True for GPU / accelerator / HPC specialty instances that must NOT be used as the
    general-purpose equivalent for a normal server. They have huge RAM/price and would otherwise
    win the 'smallest instance that fits' match (e.g. an Azure ND96 A100 GPU box at $32/hr, or an
    AWS hpc7a) for a plain memory workload, massively inflating the cross-cloud estimate."""
    n = (name or "").lower().strip()
    fam = (family or "").lower()
    if "gpu" in fam or "hpc" in fam or "accelerat" in fam or "fpga" in fam:
        return True
    if provider == "azure":
        # N-series = GPU/FPGA (NC/ND/NV/NG/NP); H/HB/HC/HX = HPC.
        if re.match(r"^n[cdvgp]", n) or re.match(r"^h(b|c|x)?\d", n):
            return True
    if provider == "aws":
        fam0 = n.split(".")[0]
        # GPU/accelerator families (p/g/dl/trn/inf/vt/f) and HPC (hpc*).
        if re.match(r"^(p\d|g\d|dl\d|trn\d|inf\d|vt\d|f\d)", fam0) or fam0.startswith("hpc"):
            return True
    return False


def _index_real_priced_instances():
    """Index every real-priced cloud instance (name + size + price + generation) by
    cloud and by cloud+OCI-vendor, so the cross-cloud estimate can match real shapes."""
    from collections import defaultdict

    by_cloud_vendor = defaultdict(list)
    by_cloud = defaultdict(list)
    max_gen = {}
    for s in CLOUD_SHAPE_MAP.values():
        if s.get("isGpu"):
            continue
        if _is_specialty_instance(s.get("provider"), s.get("instance"), s.get("family")):
            continue
        vcpu = s.get("vcpu")
        mem = s.get("memoryGb")
        hourly = s.get("approxSourceHourly")
        if not (s.get("sourcePriceReal") and vcpu and mem and hourly):
            continue
        provider = s.get("provider")
        gen = _instance_generation(provider, s.get("instance"))
        rec = {
            "instance": s.get("instance"),
            "vcpu": float(vcpu),
            "mem": float(mem),
            "hourly": float(hourly),
            "monthly": s.get("approxSourceMonthly"),
            "gen": gen,
        }
        by_cloud_vendor[(provider, s.get("ociVendor"))].append(rec)
        by_cloud[provider].append(rec)
    for key, lst in by_cloud_vendor.items():
        lst.sort(key=lambda r: (r["vcpu"], r["mem"]))
        # "Newest generation" = the highest gen that actually has a family of sizes
        # (guards against a single mislabeled/outlier instance like "E64i v31").
        from collections import Counter as _Counter

        gen_counts = _Counter(r["gen"] for r in lst)
        real_gens = [g for g, c in gen_counts.items() if c >= 3]
        max_gen[key] = max(real_gens) if real_gens else max((r["gen"] for r in lst), default=0)
    for lst in by_cloud.values():
        lst.sort(key=lambda r: (r["vcpu"], r["mem"]))
    return by_cloud_vendor, by_cloud, max_gen


REAL_PRICED_BY_CLOUD_VENDOR, REAL_PRICED_BY_CLOUD, MAX_GEN_BY_CLOUD_VENDOR = _index_real_priced_instances()


def equivalent_instance(cloud, vendor, vcpus, mem, top_of_line=False):
    """Find the equivalent named instance on `cloud` for the OCI shape's vendor,
    sized to the workload. Prefers an exact match, then the smallest instance that
    fits both vCPU and memory, then the nearest size. When top_of_line is set, only
    the newest-generation family for that vendor is considered."""
    pool = REAL_PRICED_BY_CLOUD_VENDOR.get((cloud, vendor)) or REAL_PRICED_BY_CLOUD.get(cloud) or []
    if not pool:
        return None
    if top_of_line:
        newest = MAX_GEN_BY_CLOUD_VENDOR.get((cloud, vendor))
        if newest is not None:
            top_pool = [rec for rec in pool if rec["gen"] == newest]
            if top_pool:
                pool = top_pool
    # Exact size match (same shape footprint).
    for rec in pool:
        if abs(rec["vcpu"] - vcpus) < 0.5 and abs(rec["mem"] - mem) <= max(2.0, 0.1 * mem):
            return rec
    # Smallest instance that fits both dimensions (never undersize).
    fits = [rec for rec in pool if rec["vcpu"] >= vcpus and rec["mem"] >= mem]
    if fits:
        return min(fits, key=lambda r: (r["vcpu"], r["mem"]))
    # Otherwise the nearest available by size.
    return min(pool, key=lambda r: abs(r["vcpu"] - vcpus) + abs(r["mem"] - mem) / 8.0)


# Re-price OCI-mapped non-compute services on Azure from the SAME usage (map AWS -> OCI ->
# equal Azure usage) instead of carrying the source bill 1:1. Keyed by the OCI line-item SKU:
# (factor, azure_rate) so azure_cost = quantity * factor * azure_rate in that line's unit.
# Azure list rates (US, pay-as-you-go): Blob Hot LRS $0.018/GB-mo, Files Standard $0.05/GB-mo,
# Standard-SSD managed disk ~$0.075/GB-mo (disk IOPS are bundled, so OCI performance-units map
# to $0), NVads A10 v5 ~$3.20/GPU-hr, D-series ~$0.048/vCPU-hr + ~$0.006/GB-hr RAM, PostgreSQL
# Flexible General Purpose ~$0.101/vCore-hr + ~$0.115/GB-mo storage. 1 OCPU = 2 vCPU, so
# OCPU-hour lines use factor 2.
AZURE_RATE_BY_OCI_SKU = {
    "B91628": (1.0, 0.018),       # OCI Object Storage GB-mo -> Azure Blob Hot LRS
    "B89057": (1.0, 0.05),        # OCI File Storage GB-mo -> Azure Files Standard
    "B91961": (1.0, 0.075),       # OCI Block Volume GB-mo -> Azure Standard SSD managed disk
    "B91962": (0.0, 0.0),         # OCI Block performance-units -> bundled in Azure disk GB
    "VM.GPU.A10.1": (1.0, 3.20),  # OCI A10 GPU-hr -> Azure NVads A10 v5 (per A10-hr)
    "B111129": (2.0, 0.048),      # OCI E6 OCPU-hr -> Azure D-series vCPU-hr
    "B111130": (1.0, 0.006),      # OCI compute memory GB-hr -> Azure memory GB-hr
    "B99060": (2.0, 0.101),       # OCI PostgreSQL OCPU-hr -> Azure PG Flexible GP vCore-hr
    "B99062": (1.0, 0.115),       # OCI PostgreSQL storage GB-mo -> Azure PG storage GB-mo
}
# Same idea in reverse (Azure-sourced bill -> AWS estimate). AWS list rates (US East, PAYG):
# S3 Standard $0.023/GB-mo, EFS Standard $0.30/GB-mo, EBS gp3 $0.08/GB-mo (3,000 IOPS baseline
# bundled so perf-units -> $0), g5 (A10G) ~$1.006/GPU-hr, EC2 ~$0.048/vCPU-hr + ~$0.006/GB-hr,
# RDS PostgreSQL ~$0.12/vCore-hr + $0.115/GB-mo storage.
AWS_RATE_BY_OCI_SKU = {
    "B91628": (1.0, 0.023),       # -> S3 Standard
    "B89057": (1.0, 0.30),        # -> EFS Standard
    "B91961": (1.0, 0.08),        # -> EBS gp3
    "B91962": (0.0, 0.0),         # -> bundled in EBS gp3 baseline IOPS
    "VM.GPU.A10.1": (1.0, 1.006), # -> g5 (A10G) per GPU-hr
    "B111129": (2.0, 0.048),      # -> EC2 vCPU-hr
    "B111130": (1.0, 0.006),      # -> EC2 memory GB-hr
    "B99060": (2.0, 0.12),        # -> RDS PostgreSQL vCore-hr
    "B99062": (1.0, 0.115),       # -> RDS PostgreSQL gp storage GB-mo
}
_CLOUD_RATE_TABLE = {"azure": AZURE_RATE_BY_OCI_SKU, "aws": AWS_RATE_BY_OCI_SKU}

# Networking is special: OCI's model is largely FREE (free VCN/DRG/gateways + 10 TB/mo egress),
# so mapping AWS/Azure networking THROUGH OCI zeroes it out. But AWS and Azure both charge for
# egress, NAT gateways, load balancers and endpoints at comparable rates, so the source bill's
# networking spend is the honest cross-cloud estimate - not the near-zero OCI re-price.
_NETWORKING_SOURCE_SVCS = {
    "amazonvpc", "awsdatatransfer", "awselb", "elasticloadbalancing", "awsdirectconnect",
    "amazonroute53", "amazoncloudfront", "awsglobalaccelerator", "awsamplify",
    "microsoft.network", "azurefrontdoor", "azuredns",
}


def _is_networking_row(row):
    if "networking" in str(row.get("ociServiceCategory") or "").lower():
        return True
    svc = re.sub(r"[^a-z0-9.]+", "", str(row.get("sourceService") or "").lower())
    return svc in _NETWORKING_SOURCE_SVCS


# Per-component networking list rates (US, PAYG) applied to the SOURCE usage quantity in its
# native unit (GB for data, hours for hourly resources). AWS and Azure charge networking
# comparably, so most components are close; the notable gaps are egress and load balancers.
_NET_RATE = {
    "azure": {"egress": 0.087, "lb_hr": 0.025, "vpn_hr": 0.05, "tgw_hr": 0.05,
              "tgw_gb": 0.02, "nat_hr": 0.045, "nat_gb": 0.045},   # egress=Blob-out; lb=Standard LB
    "aws":   {"egress": 0.09, "lb_hr": 0.0225, "vpn_hr": 0.05, "tgw_hr": 0.05,
              "tgw_gb": 0.02, "nat_hr": 0.045, "nat_gb": 0.045},   # egress=DTO; lb=ALB
}


def _net_component(usage_type, product):
    """Classify a networking line into a priceable component from its usage-type token."""
    t = (str(usage_type or "") + " " + str(product or "")).lower()
    if "datatransfer-out" in t or "-out-bytes" in t or "data transfer out" in t or "egress" in t:
        return "egress"
    if "natgateway-hours" in t or ("nat" in t and "hour" in t):
        return "nat_hr"
    if "natgateway-bytes" in t or ("nat" in t and "byte" in t):
        return "nat_gb"
    if "transitgateway-hours" in t or ("transitgateway" in t and "hour" in t):
        return "tgw_hr"
    if "transitgateway-bytes" in t or ("transitgateway" in t and "byte" in t):
        return "tgw_gb"
    if "loadbalancer" in t or "lcuusage" in t or "lcu-hours" in t:
        return "lb_hr"
    if "vpn-usage" in t or "vpn-connection" in t or ("vpn" in t and "hour" in t):
        return "vpn_hr"
    return None


def _reprice_networking_row(row, cloud):
    """Re-price a networking row on the TARGET cloud's specific rate per component (egress GB,
    NAT/LB/VPN/transit-gateway hours...). Components with no clean cross-cloud match (Verified
    Access, DirectConnect/ExpressRoute, VPC endpoints, etc.) carry the source spend, since AWS
    and Azure charge them comparably."""
    rates = _NET_RATE.get(cloud) or {}
    comp = _net_component(row.get("sourceUsageType"), row.get("sourceProduct"))
    if comp and comp in rates:
        return money(to_number(row.get("sourceUsageQty"), 0) * rates[comp])
    return money(to_number(row.get("sourceMonthlyCost"), 0))


def _reprice_row(row, rate_table):
    """Estimate a non-compute row on the TARGET cloud by re-pricing its OCI line items at that
    cloud's list rates for the SAME usage. Line items whose OCI SKU is in `rate_table` use
    quantity x factor x rate; the rest fall back to their OCI cost (still usage-based math, not
    a carried source-cloud price). Compute-unit line items (OCPU/ECPU/GB-hour) are scaled back
    to the INPUT footprint so OCI rightsizing never leaks in. Returns (monthly, priced_any)."""
    specs = row.get("specs") or {}
    orig, cur = row.get("originalOcpus"), specs.get("ocpus")
    scale = 1.0
    try:
        if orig and cur and float(cur) > 0:
            scale = float(orig) / float(cur)   # un-rightsize: back to the original OCPU count
    except (TypeError, ValueError):
        scale = 1.0
    total = 0.0
    priced_any = False
    for li in (row.get("lineItems") or []):
        unit = (li.get("unit") or "").lower()
        s = scale if any(u in unit for u in ("ocpu", "ecpu", "gb-hour", "gb hour")) else 1.0
        m = (rate_table or {}).get(li.get("sku") or "")
        if m is not None:
            factor, rate = m
            total += to_number(li.get("quantity"), 0) * s * factor * rate
            priced_any = True
        else:
            total += to_number(li.get("monthly"), 0) * s   # usage-based OCI proxy for the long tail
    return money(total), priced_any


def _estimate_source_cost(row, cloud, hide_windows=False):
    """App Estimate: reconstruct what a single bill row would cost on its OWN source cloud
    from its usage, for bills that arrive with SKUs/usage but no pricing (e.g. an Azure export
    with the cost columns stripped). Mirrors the cross-cloud best-match per-row math so the
    "<Cloud> Cost (App Estimate)" figure ties to the same engine that estimates the other cloud.
    Returns a monthly dollar figure."""
    if (row.get("costAction") or "") == "remove":
        return 0.0
    specs = row.get("specs") or {}
    oc_in = row.get("originalOcpus")
    if oc_in is None:
        oc_in = specs.get("ocpus") or 0
    mem = row.get("originalMemoryGb")
    if mem is None:
        mem = specs.get("memoryGb") or 0
    vcpus = (oc_in or 0) * 2
    is_compute = vcpus > 0 or mem > 0
    if not is_compute:
        # Non-compute services: networking re-priced per component, everything else on the
        # cloud's list rates for the same usage (OCI proxy for the long tail).
        if _is_networking_row(row):
            return _reprice_networking_row(row, cloud)
        est, _priced = _reprice_row(row, _CLOUD_RATE_TABLE.get(cloud) or {})
        return est
    # Compute: equivalent instance on the source cloud priced on the row's ACTUAL billed hours
    # (not a full month per line), plus Windows licensing prorated to those hours.
    hours = row.get("hoursPerMonth") or HOURS_PER_MONTH
    uh = to_number(row.get("computeUsageHours"), 0)
    if uh > 0:
        hours = uh
    windows = 0
    if not hide_windows:
        # windowsLicenseMonthly is already billed on actual usage hours; only un-rightsize it.
        windows = row.get("windowsLicenseMonthly") or 0
        cur = specs.get("ocpus")
        if windows and oc_in and cur and float(cur) > 0:
            windows = windows * (float(oc_in) / float(cur))
    inst = equivalent_instance(cloud, (row.get("shapeUsed") or {}).get("vendor"), vcpus, mem, False)
    base = inst["hourly"] * hours if inst else 0.0
    return money(base + windows)


def _apply_bare_metal_packing(priced_rows, totals, shape_key, hours):
    """Bare metal is sold as an indivisible physical box, but one box is SHARED by many
    workloads - you pack them onto it. So the right unit of rounding is the whole estate, not
    the individual row: size the total OCPU/RAM demand, work out how many servers that needs,
    and charge for the unused remainder of the last box.

    Rounding each row up to its own full server (the previous behaviour) multiplied the estimate
    enormously - a single t3a.large bill line became a whole 192-OCPU server at ~$88k/mo."""
    try:
        h = float(hours or HOURS_PER_MONTH) or HOURS_PER_MONTH
        # Group by the shape each row ACTUALLY uses, so a per-workload override to bare metal is
        # packed just like a globally selected bare-metal shape - and two different BM shapes are
        # packed into their own pools rather than pretending they share a box.
        pools = {}
        for r in priced_rows:
            key = ((r.get("shapeUsed") or {}).get("key")) or shape_key
            if not _selected_bare_metal(key):
                continue
            specs = r.get("specs") or {}
            p = pools.setdefault(key, {"ocpu": 0.0, "mem": 0.0, "rows": []})
            p["ocpu"] += float(specs.get("ocpus") or 0)
            p["mem"] += float(specs.get("memoryGb") or 0)
            p["rows"].append(r)
        if not pools:
            return None
        out = []
        for key, p in pools.items():
            bm = _selected_bare_metal(key)
            if not bm:
                continue
            bm_ocpu, bm_mem = bm
            shape = SHAPE_LOOKUP.get(key) or {}
            used_ocpu, used_mem = p["ocpu"], p["mem"]
            if used_ocpu <= 0 and used_mem <= 0:
                continue
            need = max(used_ocpu / bm_ocpu if bm_ocpu else 0,
                       used_mem / bm_mem if bm_mem else 0)
            servers = max(1, int(math.ceil(round(need, 6))))
            spare_ocpu = max(0.0, servers * bm_ocpu - used_ocpu)
            spare_mem = max(0.0, servers * bm_mem - used_mem)
            extra = money(spare_ocpu * float(shape.get("computeRate") or 0) * h
                          + spare_mem * float(shape.get("memoryRate") or 0) * h)
            label = shape.get("label") or key
            out.append({
                "shape": label, "shapeKey": key, "servers": servers,
                "serverOcpu": bm_ocpu, "serverMemoryGb": bm_mem,
                "usedOcpu": round(used_ocpu, 4), "usedMemoryGb": round(used_mem, 4),
                "spareOcpu": round(spare_ocpu, 4), "spareMemoryGb": round(spare_mem, 4),
                "workloads": len(p["rows"]), "unusedMonthly": extra,
            })
            if extra > 0 and p["rows"]:
                host = max(p["rows"], key=lambda r: float(r.get("monthly") or 0))
                host.setdefault("lineItems", []).append({
                    "sku": shape.get("computeSku"),
                    "description": f"Bare metal unused capacity ({label})",
                    "quantity": round(spare_ocpu, 4),
                    "unit": "OCPU-hour",
                    "rate": float(shape.get("computeRate") or 0),
                    "monthly": extra,
                    "mapping": (f"{servers} x {label} ({bm_ocpu:g} OCPU / {bm_mem:g} GB each) host "
                                f"the {len(p['rows'])} workload(s) mapped to this shape "
                                f"({used_ocpu:,.0f} OCPU / {used_mem:,.0f} GB). Bare metal is an "
                                f"indivisible box, so the unused remainder is still billed. Change "
                                f"a workload's OCI Shape to move it off bare metal."),
                })
                host["monthly"] = money(float(host.get("monthly") or 0) + extra)
                host["annual"] = money(float(host["monthly"]) * 12)
                totals["monthly"] = money(float(totals.get("monthly") or 0) + extra)
                totals["annual"] = money(float(totals.get("annual") or 0) + extra * 12)
        if not out:
            return None
        return out[0] if len(out) == 1 else {"pools": out}
    except Exception:
        return None


ZFS_HA_IMAGE_SKU = "B95410"
ZFS_HA_IMAGE_RATE = 1.85          # $/instance-hour, Oracle ZFS Storage HA marketplace image


def _apply_zfs_appliance(priced_rows, totals, hours):
    """AWS FSx maps to the Oracle ZFS Storage HA appliance. Per Oracle's ZFS HA documentation the
    cost is three parts: the marketplace IMAGE fee (per compute instance-hour), the COMPUTE shape
    it runs on, and standard BLOCK VOLUME for the capacity provisioned. The capacity is already
    priced on the FSx rows themselves (block-volume rates), so add the image fee here - once for
    the estate, not per bill line, since one appliance serves the whole file estate.

    The compute shape the appliance runs on is a deployment choice, so it is called out as an
    assumption rather than guessed at."""
    try:
        fsx_rows = [r for r in priced_rows
                    if "fsx" in normalize(str(r.get("sourceService") or ""))]
        if not fsx_rows:
            return
        # Only bill the appliance when there is real capacity behind it.
        if not any(float(r.get("monthly") or 0) > 0 for r in fsx_rows):
            return
        h = float(hours or HOURS_PER_MONTH) or HOURS_PER_MONTH
        monthly = money(ZFS_HA_IMAGE_RATE * h)
        host = fsx_rows[0]
        host.setdefault("lineItems", []).append({
            "sku": ZFS_HA_IMAGE_SKU,
            "description": "ZFS Storage HA - marketplace image (instance-hour)",
            "quantity": round(h, 4),
            "unit": "instance-hour",
            "rate": ZFS_HA_IMAGE_RATE,
            "monthly": monthly,
            "mapping": ("Oracle ZFS Storage HA marketplace image, one appliance for the file "
                        "estate. Capacity is priced as block volume on the FSx rows. The compute "
                        "shape the appliance runs on is additional and depends on the deployment."),
        })
        host["monthly"] = money(float(host.get("monthly") or 0) + monthly)
        host["annual"] = money(float(host["monthly"]) * 12)
        totals["monthly"] = money(float(totals.get("monthly") or 0) + monthly)
        totals["annual"] = money(float(totals.get("annual") or 0) + monthly * 12)
    except Exception:
        return


def _apply_source_cost_estimate(priced_rows, totals, source_cloud, hide_windows=False):
    """When a cloud bill carries usage/SKUs but essentially no pricing, fill each row's source
    cost with an App Estimate reconstructed from usage x the source cloud's rates, and flag it so
    the UI/BOM label the column "<Cloud> Cost (App Estimate)". Returns True when applied.
    Trigger: cloud-bill, a known source cloud, and the billed source cost is ~0 vs the OCI total."""
    if source_cloud not in _CLOUD_RATE_TABLE:
        return False
    oci_month = sum(to_number(r.get("monthly"), 0) for r in priced_rows)
    src_total = to_number(totals.get("sourceMonthlyCost"), 0)
    # "No pricing" = billed source cost is under 1% of the OCI monthly (and under $1 in absolute
    # terms), i.e. the bill's cost column was blank/negligible.
    if src_total >= max(1.0, 0.01 * oci_month):
        return False
    new_total = 0.0
    for r in priced_rows:
        est = money(_estimate_source_cost(r, source_cloud, hide_windows))
        r["sourceMonthlyCost"] = est
        r["sourceCostEstimated"] = True
        # The results table + comparison sheets read the nested mapping's cost, so mirror it.
        fsm = r.get("fullServiceMapping")
        if isinstance(fsm, dict):
            fsm["sourceMonthlyCost"] = est
            fsm["sourceCostEstimated"] = True
        new_total += est
    totals["sourceMonthlyCost"] = money(new_total)
    totals["mappedSourceMonthlyCost"] = money(new_total)
    totals["unmappedSourceMonthlyCost"] = 0.0
    return True


def _cross_cloud_one_mode(priced_rows, hide_windows, top_of_line, cloud_bill_mode=False, source_cloud=None):
    """Compute the AWS/Azure totals for one matching mode.

    On-prem inventory: every row is a compute workload, so match a real named
    instance on each cloud (best fit, or newest generation when top_of_line) and
    use its on-demand price, plus the Windows add-on rule used for OCI.

    Cloud-bill mode: we already know the real bill, so don't re-estimate it.
      - The source cloud (the cloud the bill came from) is reported at its ACTUAL
        billed cost for every line item - no estimate needed.
      - The other cloud estimates only the compute line items (swapping in an
        equivalent instance) and carries every non-compute service (storage,
        data transfer, managed services) at its actual source cost, since
        cross-cloud per-service pricing isn't modelled.
    GCP is intentionally sizing-only (no estimated pricing)."""
    out = {}
    for cloud in ("aws", "azure"):
        total = 0.0
        storage_total = 0.0
        sql_total = 0.0
        gpu_total = 0.0
        actual_rows = 0
        estimated_rows = 0
        live_rows = 0
        carried_rows = 0
        for row in priced_rows:
            # "Remove from BOM" pulls the line out of both sides, so it must not
            # count toward the source-cloud (actual bill) or other-cloud totals.
            if (row.get("costAction") or "") == "remove":
                continue
            sce = row.get("sourceCloudEstimate") or {}
            vendor = (row.get("shapeUsed") or {}).get("vendor")
            specs = row.get("specs") or {}
            # Size the cross-cloud estimate against the INPUT footprint - never OCI's
            # rightsized (trimmed) OCPU/RAM. So the other clouds reflect the original bill's
            # workload, not OCI's optimization. (OCI discount can't leak here - it's applied
            # after pricing, not inside it.)
            _ocpus_in = row.get("originalOcpus")
            if _ocpus_in is None:
                _ocpus_in = specs.get("ocpus") or 0
            mem = row.get("originalMemoryGb")
            if mem is None:
                mem = specs.get("memoryGb") or 0
            vcpus = (_ocpus_in or 0) * 2
            is_compute = vcpus > 0 or mem > 0
            src_cost = to_number(row.get("sourceMonthlyCost"), 0)

            if cloud_bill_mode:
                source_is_this = bool(source_cloud and cloud == source_cloud)
                # Best-match: the source cloud is your real bill - actual billed cost.
                # Top-of-the-line: re-estimate even the source cloud on newest-gen
                # shapes (a "what-if" - what the bill would cost re-shaped), so the
                # toggle visibly moves the source card too.
                if source_is_this and not top_of_line:
                    total += src_cost
                    actual_rows += 1
                    continue
                # Top-of-the-line re-prices the SOURCE cloud's compute at list. A Savings Plan /
                # Reserved-Instance charge is how that same compute was paid for, so carrying it
                # as well double-counts: the AWS card came out at $210,488 against a $138,833
                # bill, 52% ABOVE the real invoice, purely because $23,486 of commitment charges
                # sat alongside compute that had just been re-priced from ~$0 to list.
                if (source_is_this and top_of_line
                        and normalize(str(row.get("sourceService") or "")).replace(" ", "")
                        in _BILLING_CONSTRUCT_SERVICES):
                    continue
                # Non-compute services: on the TARGET cloud re-price the mapped OCI services from
                # the same usage at that cloud's list rates instead of carrying the source bill.
                # Works both ways (AWS-sourced -> Azure estimate, Azure-sourced -> AWS estimate).
                # The source cloud always stays at its actual billed cost.
                if not is_compute:
                    if not source_is_this and cloud in _CLOUD_RATE_TABLE:
                        if _is_networking_row(row):
                            # Networking is re-priced per component on the target cloud's rates
                            # (egress/NAT/LB/VPN/transit-gateway), not OCI's near-free model.
                            total += _reprice_networking_row(row, cloud)
                        else:
                            est, _priced = _reprice_row(row, _CLOUD_RATE_TABLE[cloud])
                            total += est             # usage re-priced on target-cloud rates (OCI proxy for the tail)
                        estimated_rows += 1
                    else:
                        total += src_cost
                        carried_rows += 1
                    continue
                # Compute line items fall through to be estimated below.
            else:
                # On-prem inventory: only compute workloads are priced here.
                if not is_compute:
                    continue
                # Attached block storage: OCI's total includes each VM's disk, so the other
                # clouds must too - otherwise OCI looks artificially expensive (its block volume
                # is far cheaper than AWS EBS / Azure managed disk). Price the VM's disk at this
                # cloud's block-volume rate (OCI B91961 -> EBS gp3 / Azure Standard SSD; the
                # performance-unit SKU B91962 is bundled into that per-GB rate, matching cloud-bill mode).
                _blk_gb = to_number(specs.get("blockStorageGb"), 0)
                if _blk_gb > 0:
                    _blk_rate = (_CLOUD_RATE_TABLE.get(cloud, {}).get("B91961") or (1.0, 0.0))[1]
                    storage_total += _blk_gb * _blk_rate
                # File/NAS storage (rare in VM inventory) priced at the cloud's file-share rate.
                _file_gb = to_number(specs.get("fileStorageGb"), 0)
                if _file_gb > 0:
                    _file_rate = (_CLOUD_RATE_TABLE.get(cloud, {}).get("B89057") or (1.0, 0.0))[1]
                    storage_total += _file_gb * _file_rate

            hours = row.get("hoursPerMonth") or HOURS_PER_MONTH
            # Cloud bills are often billed at daily/hourly granularity, so a single VM shows up
            # as many line items that each cover only part of the month. Price the equivalent
            # instance on the row's ACTUAL billed hours instead of a full 730-hour month, or
            # every daily row would count as a whole month (a ~30x over-estimate). Falls back to
            # hoursPerMonth when the meter isn't hour-based (computeUsageHours == 0).
            usage_hours = to_number(row.get("computeUsageHours"), 0) if cloud_bill_mode else 0
            if usage_hours > 0:
                hours = usage_hours
            # Windows licensing is per-OCPU, so scale it back to the INPUT OCPU count too -
            # otherwise OCI rightsizing trims the license add-on into the cross-cloud estimate.
            windows = 0
            if not hide_windows:
                # windowsLicenseMonthly is already billed on the row's actual usage hours; here we
                # only un-rightsize it back to the INPUT OCPU count so OCI's trim doesn't leak in.
                windows = row.get("windowsLicenseMonthly") or 0
                _cur = specs.get("ocpus")
                if windows and _ocpus_in and _cur and float(_cur) > 0:
                    windows = windows * (float(_ocpus_in) / float(_cur))

            # SQL Server licensing and the GPU premium are in OCI's total (and in the source
            # cloud's actual bill), so mirror them onto the target-cloud estimate for compute
            # rows too - otherwise SQL / GPU workloads make OCI look artificially expensive, the
            # same class of bug as the storage/Windows omissions. Runs for every compute row that
            # reaches here (the source-cloud-at-actual row already returned above, so no
            # double-count). On-prem GPU is unmodeled (gpu_info is cloud-bill-only), so it's 0 there.
            sql_total += to_number(row.get("sqlLicenseMonthly"), 0)
            _gpu_li = next((li for li in (row.get("lineItems") or [])
                            if li.get("isGpu") and to_number(li.get("monthly"), 0) > 0), None)
            if _gpu_li:
                _gpu_hours = to_number(_gpu_li.get("quantity"), 0)  # already gpuCount x 730
                _gpu_rate = (_CLOUD_RATE_TABLE.get(cloud, {}).get(_gpu_li.get("sku"))
                             or _CLOUD_RATE_TABLE.get(cloud, {}).get("VM.GPU.A10.1") or (1.0, 0.0))[1]
                gpu_total += _gpu_hours * _gpu_rate

            # AWS: try the live Price List API for this workload's instance type
            # (its own source instance when marked AWS, otherwise the equivalent),
            # Linux on-demand, in the workload's region.
            if cloud == "aws" and aws_pricing.available():
                inst_type = None
                if not top_of_line and sce.get("provider") == "aws" and sce.get("instance"):
                    inst_type = sce.get("instance")
                if top_of_line or not inst_type:
                    eq = equivalent_instance("aws", vendor, vcpus, mem, top_of_line)
                    inst_type = (eq or {}).get("instance") or inst_type
                live = aws_pricing.ondemand_linux_rate(inst_type, row.get("region")) if inst_type else None
                if live is not None:
                    total += live * hours + windows
                    live_rows += 1
                    if not top_of_line and sce.get("provider") == "aws":
                        actual_rows += 1
                    else:
                        estimated_rows += 1
                    continue

            # Marked source-cloud instance with a known price (best-match only).
            if not top_of_line and sce.get("provider") == cloud and sce.get("totalMonthly") is not None:
                total += sce["totalMonthly"]
                actual_rows += 1
                continue
            # Equivalent named instance from the bundled price data.
            inst = equivalent_instance(cloud, vendor, vcpus, mem, top_of_line)
            if not inst:
                continue
            total += inst["hourly"] * hours + windows
            estimated_rows += 1
        if cloud_bill_mode and source_cloud and cloud == source_cloud and not top_of_line:
            basis = "actual bill"
        elif cloud_bill_mode and source_cloud and cloud == source_cloud and top_of_line:
            basis = "what-if: bill re-shaped on newest-gen"
        elif cloud_bill_mode and source_cloud and cloud != source_cloud:
            basis = "compute + services re-priced on %s usage rates" % ("Azure" if cloud == "azure" else "AWS")
        elif cloud_bill_mode and carried_rows:
            basis = "compute estimated · other services at source cost"
        elif live_rows:
            basis = "live"
        elif actual_rows and estimated_rows:
            basis = "mixed"
        elif actual_rows:
            basis = "actual"
        else:
            basis = "equivalent"
        total += storage_total + sql_total + gpu_total
        out[cloud] = {
            "label": "AWS" if cloud == "aws" else "Microsoft Azure",
            "monthlyTotal": money(total),
            "annualTotal": money(total * 12),
            "storageMonthly": money(storage_total),
            "sqlLicenseMonthly": money(sql_total),
            "gpuMonthly": money(gpu_total),
            "priced": True,
            "basis": basis,
            "actualRows": actual_rows,
            "estimatedRows": estimated_rows,
            "liveRows": live_rows,
            "carriedRows": carried_rows,
        }
    out["gcp"] = {
        "label": "Google Cloud",
        "priced": False,
        "note": "Sizing only - no estimated pricing",
    }
    return out


def _dominant_source_cloud(pricing, source_provider=None):
    """Which cloud the workloads came from (aws/azure), or None.
    A known bill provider wins; otherwise infer from the priced rows' markers."""
    known = normalize_provider_hint(source_provider) if source_provider else PROVIDER_AUTO
    if known in ("aws", "azure"):
        return known
    counts = {"aws": 0, "azure": 0}
    for row in pricing.get("rows", []):
        prov = (row.get("sourceCloudEstimate") or {}).get("provider")
        if prov in counts:
            counts[prov] += 1
    if counts["aws"] == 0 and counts["azure"] == 0:
        return None
    return "aws" if counts["aws"] >= counts["azure"] else "azure"


def cross_cloud_estimate(priced_rows, hide_windows=False, cloud_bill_mode=False, source_cloud=None):
    """Both estimate modes so the UI can toggle without a re-price:
      - bestMatch: closest equivalent shape (uses actual source-cloud price when known).
      - topTier:   newest-generation equivalent shape on each cloud.
    In cloud-bill mode the source cloud is reported at its actual billed cost."""
    return {
        "bestMatch": _cross_cloud_one_mode(priced_rows, hide_windows, False, cloud_bill_mode, source_cloud),
        "topTier": _cross_cloud_one_mode(priced_rows, hide_windows, True, cloud_bill_mode, source_cloud),
        "sourceCloud": source_cloud,
        "cloudBillMode": cloud_bill_mode,
    }


# Source services that are BILLING CONSTRUCTS or support, not workloads: a Savings Plan /
# Reserved-Instance charge is a payment mechanism for compute that is already priced separately,
# so it correctly contributes no OCI cost of its own.
_BILLING_CONSTRUCT_SERVICES = {
    "computesavingsplans", "savingsplans", "ec2savingsplans", "machinelearningsavingsplans",
    "awssupportbusiness", "awssupportdeveloper", "awsdevelopersupport", "awssupportenterprise",
    "awssystemsmanager", "awsmarketplace", "refund", "credit", "tax",
}
# OCI services that carry NO charge, so mapping them to $0 is correct and is a genuine OCI
# advantage - not a pricing gap. Matched as substrings of the normalized product name
# (normalize() strips punctuation, so "OCI Virtual Cloud Network (VCN)" -> "...network vcn").
# Deliberately excludes OCI services that DO bill (Object Storage, File Storage, Base Database,
# Load Balancer, WAF, Vault, DNS, FastConnect, OIC, Secure Desktops): a zero there is a gap.
_FREE_ON_OCI_PRODUCTS = (
    "virtual cloud network",          # VCN itself is free
    "dynamic routing gateway",        # DRG (AWS Transit Gateway equivalent) is free
    "internet gateway",
    "service gateway",
    "oci audit",                      # Audit is free
    "identity and access management",
    "cloud guard",                    # Cloud Guard / Security Zones are free
    "security zones",
    "outbound data transfer",         # first 10 TB/mo egress is free
    # Confirmed against the reference AWS->OCI mapping another team produced for this same bill
    # ("Data Transfer (10TB egress FREE)", "Key Management Service (First 20 Keys FREE)",
    # "Secrets Management with OCI Vault - FREE"). Oracle does not charge for the Site-to-Site
    # VPN service itself either - only the data that crosses it, which is metered separately as
    # outbound data transfer. These were previously counted as unpriced gaps.
    "vpn connect",                    # OCI Site-to-Site VPN: no charge for the service
    "site-to-site vpn",
    "networking data transfer",       # metered as outbound data transfer, 10 TB/mo free
    "oci monitoring",                 # free tier covers standard metric ingestion
    "oci vault",                      # software keys / secrets management
    "key management",
)


def _is_free_on_oci(row, priced):
    """True when a zero OCI cost is CORRECT - a billing construct, or an OCI service that is
    genuinely free - rather than a mapping/pricing gap that understates the estimate."""
    svc = normalize(str(row.get("source_service") or "")).replace(" ", "")
    if svc in _BILLING_CONSTRUCT_SERVICES:
        return True
    meter = normalize(" ".join(str(priced.get(k) or "") for k in
                               ("sourceUsageType", "sourceProduct")))
    prod = normalize(str(priced.get("ociProduct") or ""))
    # Per-REQUEST API charges. S3 bills Tier1/Tier2 requests; OCI Object Storage does not meter
    # requests separately, so these correctly contribute nothing. (Cross-checked: our S3 total
    # lands within $13 of an independent mapping of the same bill, which also carries no
    # request charge.) Pricing the request COUNT as GB would be a massive over-charge, which is
    # why the capacity pricer already refuses these rows.
    if "request" in meter and ("object storage" in prod or "storage" in prod):
        return True
    # Storage I/O: AWS meters database and EBS I/O separately; OCI includes I/O in the storage
    # price, so a zero here is the OCI model, not a missed rate.
    if ("storage io" in meter or "storage i o" in meter or "iops" in meter
            or "requests" in meter) and ("database" in prod or "block volume" in prod):
        return True
    # A mapped SKU whose OCI rate is genuinely $0 (BYOL / bundled software). The line item is
    # present and visible - the customer simply brings the licence - so it is not a pricing gap.
    items = priced.get("lineItems") or []
    if items and all(float(x.get("rate") or 0) == 0 for x in items):
        blob = normalize(" ".join(str(x.get("description") or "") for x in items))
        if "byol" in blob or "bundled" in blob or "license" in blob or "included" in blob:
            return True
    prod = normalize(str(priced.get("ociProduct") or ""))
    if not prod:
        return False
    if any(term in prod for term in _FREE_ON_OCI_PRODUCTS):
        return True
    if "free on oci" in prod or "included" in prod or "no charge" in prod:
        return True
    return False


_BANDWIDTH_ATTR_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:m|g|k)bps\b", re.I)


_THROUGHPUT_METER_RE = re.compile(
    r"(mb|mib|gb|gib)[ -]?per[ -]?second|mbps[ -]?month|throughput[ -]?capacity|provisioned[ -]?throughput",
    re.I)


def _is_provisioned_throughput_line(row):
    """True for a meter that bills PROVISIONED THROUGHPUT rather than capacity - e.g. FSx's
    "$2.53 per MB-per-second-Month of provisioned Windows File Server throughput".

    OCI has no equivalent meter: on the ZFS Storage appliance, throughput is a property of the
    compute shape the appliance runs on, which is already priced (marketplace image + shape +
    block volume). Carrying AWS's throughput charge on top would double-count something OCI
    delivers as part of the appliance."""
    txt = " ".join(str(row.get(k) or "") for k in
                   ("source_product", "__usageType", "__meterName", "usage_unit"))
    return bool(_THROUGHPUT_METER_RE.search(txt))


def _is_bandwidth_attribute_line(row, fields):
    """True for a bill line that only describes an instance's NETWORK ENTITLEMENT rather than
    billing the instance itself - e.g. AWS's "$0.00 for 800 Mbps per g4ad.2xlarge". These name
    an instance type (so shape lookups match them) but bill nothing, and must not be priced as
    compute/GPU on OCI."""
    try:
        text = " ".join(str(row.get(f.get("key")) or "") for f in (fields or [])
                        if isinstance(f, dict))[:4000]
    except Exception:
        return False
    if not _BANDWIDTH_ATTR_RE.search(text):
        return False
    # Only treat it as an attribute line when it genuinely bills nothing.
    return to_number(row.get("source_monthly_cost"), 0) == 0


def gpu_pricing_for_context(context):
    """If a cloud-bill row maps to a GPU instance, return its OCI GPU shape pricing, else None."""
    rec = lookup_cloud_shape(context)
    if not rec or not rec.get("isGpu"):
        return None
    shape_name = rec.get("ociShape")
    cat = OCI_GPU_SHAPES.get(shape_name)
    if not cat:
        return None
    return {
        "shape": shape_name,
        "gpuModel": cat.get("gpuModel"),
        "gpuCount": cat.get("gpuCount"),
        "pricePerGpuHour": cat.get("pricePerGpuHour"),
        # The GPU shape's own OCPU count. Windows licensing is per OCPU of the instance you
        # actually run, so a Windows GPU workload is licensed on the OCI GPU shape (e.g.
        # VM.GPU.A10.1 = 15 OCPU), NOT on the smaller source instance it came from.
        "ocpu": cat.get("ocpu"),
        "cpuMemGb": cat.get("cpuMemGb"),
        "mappable": rec.get("mappable", True),
        "flag": rec.get("mapFlag", ""),
    }

# Map each app flex shape to its OCI shape name, per-VM max OCPU/memory, and CPU vendor.
SHAPE_KEY_TO_OCI = {
    "e6-standard-ax": ("VM.Standard.E6.Ax.Flex", 94, 712, "amd"),
    "e6-standard": ("VM.Standard.E6.Flex", 126, 1454, "amd"),
    "e5-standard": ("VM.Standard.E5.Flex", 126, 1049, "amd"),
    "e4-standard": ("VM.Standard.E4.Flex", 64, 1024, "amd"),
    "x9-standard": ("VM.Standard3.Flex", 32, 512, "intel"),
    "x12-standard-ax": ("VM.Standard4.Ax.Flex", 39, 360, "intel"),
    # Arm limits must be explicit: A1 uses one core per OCPU, while A2/A4 use two cores
    # per OCPU. Falling back to the first generic Arm flex tier made A1/A2 capacity checks
    # incorrectly inherit A4 Ax's smaller 45-OCPU limit.
    "a1-standard": ("VM.Standard.A1.Flex", 76, 472, "arm"),
    "a2-standard": ("VM.Standard.A2.Flex", 78, 946, "arm"),
    "a4-standard": ("VM.Standard.A4.Flex", 45, 700, "arm"),
    "a4-standard-ax": ("VM.Standard.A4.Ax.Flex", 45, 720, "arm"),
}


def oci_size_check(shape_key, ocpus, memory_gb):
    """Classify a single VM's size against the selected OCI shape.

    Returns dict: status = ok | baremetal | impossible, with the fitting shape and a message.
    'baremetal' means it overflows the selected flex shape but fits an OCI bare-metal shape;
    'impossible' means it exceeds every OCI shape for that CPU vendor.
    """
    # A bare-metal shape was selected for the estimate: the workload is placed on whole
    # physical servers (as many as it needs), so there's nothing to overflow.
    _bm_sel = SHAPE_LOOKUP.get(shape_key) or {}
    if _bm_sel.get("bareMetal"):
        return {"status": "ok", "shape": _bm_sel.get("label")}
    info = SHAPE_KEY_TO_OCI.get(shape_key)
    if not info:
        # Derive limits from the vendor's flex tier so shapes without an explicit
        # entry (the Ampere/ARM shapes: A4 Ax, A4, A2, A1) are still size-checked.
        vendor = (SHAPE_LOOKUP.get(shape_key) or {}).get("processorVendor")
        flex_tier = next((t for t in OCI_VENDOR_TIERS.get(vendor, []) if t.get("tier") == "flex"), None)
        if flex_tier:
            info = (flex_tier["shape"], flex_tier["maxOcpu"], flex_tier["maxMem"], vendor)
    if not info or (ocpus <= 0 and memory_gb <= 0):
        return {"status": "ok"}
    flex_shape, max_ocpu, max_mem, vendor = info
    if ocpus <= max_ocpu and memory_gb <= max_mem:
        return {"status": "ok", "shape": flex_shape}
    # Overflows the SELECTED flex shape. Two escape hatches, in preference order:
    #   (a) a LARGER flex shape (same CPU vendor) that still fits - no bare metal needed;
    #   (b) the vendor's bare-metal tier - a whole physical server.
    _sel_gen = str(shape_key).split("-")[0]  # e.g. "e6" from "e6-standard-ax"
    flex_alt = None
    _flex_alt_key = None
    for _k, (_sh, _mo, _mm, _vend) in SHAPE_KEY_TO_OCI.items():
        if _vend != vendor or _sh == flex_shape or ocpus > _mo or memory_gb > _mm:
            continue
        # Prefer a flex shape of the SAME generation as the one the user picked (e.g. stay on
        # E6 rather than dropping to an older E4), then the smallest RAM that still fits.
        if flex_alt is None:
            flex_alt, _flex_alt_key = {"shape": _sh, "maxOcpu": _mo, "maxMem": _mm}, _k
        else:
            cur_same = _flex_alt_key.split("-")[0] == _sel_gen
            cand_same = _k.split("-")[0] == _sel_gen
            if (cand_same and not cur_same) or (cand_same == cur_same and _mm < flex_alt["maxMem"]):
                flex_alt, _flex_alt_key = {"shape": _sh, "maxOcpu": _mo, "maxMem": _mm}, _k
    bm = None
    for tier in OCI_VENDOR_TIERS.get(vendor, []):
        if tier.get("tier") == "flex":
            continue
        if ocpus <= tier["maxOcpu"] and memory_gb <= tier["maxMem"]:
            bm = tier
            break
    if bm:
        if flex_alt:
            message = (
                f"{ocpus:g} OCPU / {memory_gb:g} GB exceeds {flex_shape}. It fits the larger flex "
                f"shape {flex_alt['shape']} ({flex_alt['maxOcpu']:g} OCPU / {flex_alt['maxMem']:g} GB), "
                f"or bare metal {bm['shape']} ({bm['maxOcpu']:g} OCPU / {bm['maxMem']:g} GB)."
            )
        else:
            message = (
                f"{ocpus:g} OCPU / {memory_gb:g} GB exceeds {flex_shape} and every larger flex shape, "
                f"so it needs bare metal {bm['shape']} ({bm['maxOcpu']:g} OCPU / {bm['maxMem']:g} GB) "
                f"- a full physical server, billed in full."
            )
        return {
            "status": "baremetal",
            "shape": bm["shape"],
            "bmMaxOcpu": bm["maxOcpu"],
            "bmMaxMem": bm["maxMem"],
            "flexAlt": flex_alt,
            "message": message,
        }
    biggest = (OCI_VENDOR_TIERS.get(vendor) or [{}])[-1]
    return {
        "status": "impossible",
        "shape": None,
        "message": (
            f"{ocpus:g} OCPU / {memory_gb:g} GB exceeds the largest OCI {vendor} shape "
            f"({biggest.get('shape')}: {biggest.get('maxOcpu')} OCPU / {biggest.get('maxMem')} GB)."
        ),
    }


def _selected_bare_metal(shape_key):
    """The selected shape's bare-metal capacity, or None when a flex shape is selected."""
    s = SHAPE_LOOKUP.get(shape_key) or {}
    if s.get("bareMetal") and s.get("bmOcpu"):
        return float(s["bmOcpu"]), float(s.get("bmMemoryGb") or 0)
    return None


def _billed_bm_size(shape_key, vm_ocpu, vm_mem):
    """Pricing size for ONE VM. A VM that fits a flex shape bills its own OCPU/RAM. A VM that can
    ONLY run on bare metal (it overflows every flex shape) bills the FULL bare-metal server -
    bare metal is sold as a whole physical box, so you pay for all its cores/RAM even if the
    workload needs fewer. A VM that overflows the selected flex but fits a LARGER flex shape is
    NOT inflated (it can just use the bigger flex). Returns (billed_ocpu, billed_mem)."""
    # A bare-metal shape was chosen for the whole estimate: bare metal is sold as a complete
    # physical box, so round the workload UP to whole servers and bill every one in full.
    bm = _selected_bare_metal(shape_key)
    if bm:
        bm_ocpu, bm_mem = bm
        o = float(vm_ocpu or 0)
        m = float(vm_mem or 0)
        if o <= 0 and m <= 0:
            return vm_ocpu, vm_mem
        need = max(o / bm_ocpu if bm_ocpu else 0, m / bm_mem if bm_mem else 0)
        servers = max(1, int(math.ceil(round(need, 6))))
        return servers * bm_ocpu, servers * bm_mem
    try:
        chk = oci_size_check(shape_key, float(vm_ocpu or 0), float(vm_mem or 0))
        if chk.get("status") == "baremetal" and not chk.get("flexAlt") and chk.get("bmMaxOcpu"):
            return float(chk["bmMaxOcpu"]), float(chk["bmMaxMem"])
    except Exception:
        pass
    return vm_ocpu, vm_mem


AWS_INSTANCE_SIZE_SHAPES = {
    "nano": (2, 0.5),
    "micro": (2, 1),
    "small": (2, 2),
    "medium": (2, 4),
    "large": (2, 8),
    "xlarge": (4, 16),
    "2xlarge": (8, 32),
    "3xlarge": (12, 48),
    "4xlarge": (16, 64),
    "6xlarge": (24, 96),
    "8xlarge": (32, 128),
    "9xlarge": (36, 144),
    "10xlarge": (40, 160),
    "12xlarge": (48, 192),
    "16xlarge": (64, 256),
    "18xlarge": (72, 288),
    "24xlarge": (96, 384),
    "32xlarge": (128, 512),
}

GCP_MEMORY_RATIO_BY_CLASS = {
    "standard": 4,
    "highmem": 8,
    "highcpu": 0.9,
}

AZURE_MEMORY_RATIO_BY_FAMILY = {
    "b": 4,
    "d": 4,
    "e": 8,
    "f": 2,
}


def bill_usage_capacity_factor(quantity, unit_context):
    quantity_value = to_number(quantity, 0)
    if quantity_value <= 0:
        return 1.0
    has_instance_shape = bool(
        re.search(
            r"\b(?:[a-z]\d[a-z0-9]*|[a-z]{1,4}\d[a-z0-9]*)\.(?:nano|micro|small|medium|large|xlarge|[0-9]+xlarge)\b",
            unit_context,
        )
    )
    if re.search(r"\binstance ?(?:hours?|used|usage)\b|\bbox ?usage\b|\brunning ?hours?\b|\bvm ?hours?\b", unit_context):
        return quantity_value / HOURS_PER_MONTH
    if re.search(r"\binstance\b", unit_context) and re.search(r"\bhrs?\b|\bhours?\b", unit_context):
        return quantity_value / HOURS_PER_MONTH
    if has_instance_shape and re.search(r"\bhrs?\b|\bhours?\b", unit_context):
        return quantity_value / HOURS_PER_MONTH
    return quantity_value


def meter_capacity_quantity(quantity, unit_context, is_vcpu=False):
    quantity_value = to_number(quantity, 0)
    if quantity_value <= 0:
        return 0.0
    if re.search(r"\b(?:ocpu|vcpu|cpu|core|gb|gib)? ?hours?\b|gbhr|gibhr", unit_context) and quantity_value > HOURS_PER_MONTH:
        quantity_value = quantity_value / HOURS_PER_MONTH
    return quantity_value / 2 if is_vcpu else quantity_value


def infer_instance_shape_resources(context, usage_quantity="", usage_unit=""):
    unit_context = normalize(f"{usage_unit} {context}")
    capacity_factor = bill_usage_capacity_factor(usage_quantity, unit_context)

    # Authoritative: exact instance match from the cloud mapping reference doc.
    mapped = lookup_cloud_shape(context)
    if mapped:
        ocpus = to_number(mapped.get("ocpus"), 0)
        if not ocpus:
            ocpus = to_number(mapped.get("vcpu"), 0) / 2
        memory_gb = to_number(mapped.get("ramGb"), 0) or to_number(mapped.get("memoryGb"), 0)
        if ocpus or memory_gb:
            return ocpus * capacity_factor, memory_gb * capacity_factor

    aws_match = re.search(
        r"\b(?:[a-z]\d[a-z0-9]*|[a-z]{1,4}\d[a-z0-9]*)\.(nano|micro|small|medium|large|xlarge|[0-9]+xlarge)\b",
        context,
    )
    if aws_match:
        vcpus, memory_gb = AWS_INSTANCE_SIZE_SHAPES.get(aws_match.group(1), (0, 0))
        return (vcpus / 2) * capacity_factor, memory_gb * capacity_factor

    gcp_match = re.search(r"\b(?:e2|n1|n2|n2d|c2|c3|m1|m2|m3)-(standard|highmem|highcpu)-(\d+)\b", context)
    if gcp_match:
        machine_class = gcp_match.group(1)
        vcpus = to_number(gcp_match.group(2), 0)
        memory_gb = vcpus * GCP_MEMORY_RATIO_BY_CLASS.get(machine_class, 4)
        return (vcpus / 2) * capacity_factor, memory_gb * capacity_factor

    azure_match = re.search(r"\bstandard[_ -]([a-z]+)(\d+)[a-z0-9]*", context)
    if azure_match:
        family = azure_match.group(1)[:1]
        vcpus = to_number(azure_match.group(2), 0)
        memory_gb = vcpus * AZURE_MEMORY_RATIO_BY_FAMILY.get(family, 4)
        return (vcpus / 2) * capacity_factor, memory_gb * capacity_factor

    return 0.0, 0.0


def bill_instance_compute_specs(text):
    """From an instance type / usageType string, return (ocpus, memoryGb) using the
    offline cloud shape map (vCPU/2 = OCPU). None if not found."""
    if not text:
        return None
    match = _AWS_INSTANCE_RE.search(str(text).lower())
    if not match:
        return None
    key = re.sub(r"[^a-z0-9]", "", match.group(1))
    rec = CLOUD_SHAPE_MAP.get(key)
    if not rec or not rec.get("vcpu"):
        return None
    return (rec["vcpu"] / 2.0, rec.get("memoryGb") or 0)


# ---------------------------------------------------------------------------
# OCI Base Database (DBaaS) pricing for AWS RDS / Aurora bill lines.
#
# AWS RDS lines today fall through to the generic catalog/storage fallback and
# price to ~$0 ("Block Volume Storage"). We instead map RDS -> OCI Base Database
# and price each line directly off the OCI price list:
#   - Open-source engines (MySQL / PostgreSQL / MariaDB / Aurora) instance hours
#     -> OCI Database with PostgreSQL OCPU rate (SKU B99060, $0.098/OCPU-hour).
#     OCPU = instance vCPU / 2.
#   - Aurora Serverless v2 (ACU-hours): 1 ACU ~= 0.25 vCPU = 0.125 OCPU (an ACU is
#     ~2 GiB memory + a fractional vCPU), so OCPU-hours ~= ACU-hours * 0.125,
#     priced at $0.098/OCPU-hour.
#   - SQL Server RDS has NO managed equivalent on OCI -> CARRY the line (OCI cost
#     = source cost), since it would have to be self-managed on a VM at comparable
#     spend. Detected via "SQL Server" in the description/usage type.
#   - Provisioned DB storage (RDS GP2/GP3 storage, Aurora StorageUsage) -> priced
#     at the OCI Block Volume storage rate ($0.0255/GB-month).
#   - Aurora StorageIOUsage (per-I/O) and Backup/ChargedBackupUsage are I/O and
#     backup charges with no clean OCI line-item equivalent; OCI Base Database
#     bundles I/O and includes a backup allowance, but to avoid understating cost
#     we conservatively CARRY these (OCI cost = source cost) rather than zeroing
#     them.
OCI_BASE_DB_OCPU_RATE = 0.098       # OCI Database with PostgreSQL, SKU B99060, $/OCPU-hour
OCI_BASE_DB_OCPU_SKU = "B99060"
# OCI Database with PostgreSQL bills the managed-service OCPU (B99060) PLUS the
# underlying E5 compute (OCPU B97384 + memory B97385) + Database Optimized Storage.
# Constraints: OCPU 1-64 (1 OCPU = 2 vCPU); memory min 16 GB, max 64 GB/OCPU, max 1024.
OCI_PG_E5_OCPU_RATE = 0.03          # Compute Standard E5 - OCPU, SKU B97384
OCI_PG_E5_OCPU_SKU = "B97384"
OCI_PG_E5_MEM_RATE = 0.002          # Compute Standard E5 - Memory, SKU B97385
OCI_PG_E5_MEM_SKU = "B97385"
OCI_PG_STORAGE_RATE = 0.072         # Database Optimized Storage, SKU B99062, $/GB-month
OCI_PG_STORAGE_SKU = "B99062"
OCI_PG_STORAGE_PERF_PER_GB = 30     # PostgreSQL Optimized Storage: 30 perf units/GB (B91962)
OCI_PG_MIN_OCPU, OCI_PG_MAX_OCPU = 1, 64
OCI_PG_MIN_MEM, OCI_PG_MAX_MEM, OCI_PG_MAX_MEM_PER_OCPU = 16, 1024, 64
OCI_BASE_DB_ORACLE_OCPU_RATE = 0.215  # Oracle Base Database Standard Edition, SKU B88293 (unused default; open-source rate preferred)
OCI_BASE_DB_STORAGE_RATE = 0.0255   # OCI Block Volume GB-month (reused for DB storage)
OCI_BASE_DB_STORAGE_SKU = "B91961"
OCI_BASE_DB_PRODUCT = "OCI Base Database"
# OCI MySQL Database Service (separate service, priced per ECPU; 1 ECPU = 1 vCPU).
OCI_MYSQL_ECPU_RATE = 0.0366        # MySQL Database - ECPU, SKU B108030, $/ECPU-hour
OCI_MYSQL_ECPU_SKU = "B108030"
OCI_MYSQL_STORAGE_RATE = 0.040      # MySQL Database - Storage, SKU B92426, $/GB-month
OCI_MYSQL_STORAGE_SKU = "B92426"
OCI_MYSQL_PRODUCT = "OCI MySQL Database Service"
# Database BACKUP storage uses dedicated SKUs (not generic Object Storage):
#   - Oracle Database Backup Cloud -> Object Storage: B90230, $0.0051/GB-month
#     (PostgreSQL / Aurora-PG / Oracle / Base DB backups).
#   - MySQL Database - Backup Storage: B92483, $0.04/GB-month (Aurora-MySQL / RDS MySQL).
OCI_DB_BACKUP_SKU = "B90230"
OCI_DB_BACKUP_RATE = 0.0051
OCI_MYSQL_BACKUP_SKU = "B92483"
OCI_MYSQL_BACKUP_RATE = 0.040
# Aurora Serverless v2: 1 ACU ~= 0.25 vCPU = 0.125 OCPU (ACU ~= 2 GiB + fractional vCPU).
ACU_TO_OCPU = 0.125


def _is_mysql_engine(row):
    """True when an RDS/Aurora row's engine is MySQL (or MariaDB, MySQL-compatible)."""
    blob = normalize(" ".join(clean_text(row.get(k)) for k in
                              ["source_product", "__usageType", "source_service"]))
    return ("mysql" in blob or "mariadb" in blob) and "postgre" not in blob


# OCI MySQL Database Service shapes come in discrete ECPU sizes (1 ECPU = 1 vCPU).
# RAM is fixed at 8 GB per ECPU (16 GB per 2 ECPU) and bundled in the ECPU price.
MYSQL_ECPU_SIZES = [2, 4, 8, 16, 32, 48, 64, 96]
MYSQL_GB_PER_ECPU = 8


def mysql_ecpu(vcpus):
    """Floor the source vCPU count to the nearest valid OCI MySQL shape ECPU size,
    minimum 2, capped at the largest shape (96)."""
    v = to_number(vcpus, 0) or 0
    chosen = MYSQL_ECPU_SIZES[0]
    for s in MYSQL_ECPU_SIZES:
        if v >= s:
            chosen = s
    return chosen


def postgres_compute_items(ocpu_raw, ram_raw, hours):
    """OCI Database with PostgreSQL compute line items: managed-service OCPU
    (B99060 $0.098) + underlying E5 compute (OCPU B97384 $0.03 + memory B97385
    $0.002/GB). OCPU floored to 1-64; memory min 16, max 64/OCPU, max 1024."""
    ocpu = int(math.floor(to_number(ocpu_raw, 0) or 0))
    ocpu = max(OCI_PG_MIN_OCPU, min(OCI_PG_MAX_OCPU, ocpu))
    ram = to_number(ram_raw, 0) or 0
    ram = max(OCI_PG_MIN_MEM, ram)
    ram = min(ram, OCI_PG_MAX_MEM_PER_OCPU * ocpu, OCI_PG_MAX_MEM)
    return [
        {"sku": OCI_BASE_DB_OCPU_SKU, "description": "OCI Database with PostgreSQL - OCPU",
         "quantity": round(ocpu * hours, 4), "unit": "OCPU-hour", "rate": OCI_BASE_DB_OCPU_RATE,
         "monthly": money(ocpu * OCI_BASE_DB_OCPU_RATE * hours),
         "mapping": f"OCI Database with PostgreSQL: {ocpu} OCPU x {hours:,.0f} hrs @ ${OCI_BASE_DB_OCPU_RATE}/OCPU-hr."},
        {"sku": OCI_PG_E5_OCPU_SKU, "description": "OCI Compute E5 - OCPU (PostgreSQL)",
         "quantity": round(ocpu * hours, 4), "unit": "OCPU-hour", "rate": OCI_PG_E5_OCPU_RATE,
         "monthly": money(ocpu * OCI_PG_E5_OCPU_RATE * hours),
         "mapping": f"Underlying E5 compute: {ocpu} OCPU x {hours:,.0f} hrs @ ${OCI_PG_E5_OCPU_RATE}/OCPU-hr."},
        {"sku": OCI_PG_E5_MEM_SKU, "description": "OCI Compute E5 - Memory (PostgreSQL)",
         "quantity": round(ram * hours, 4), "unit": "GB-hour", "rate": OCI_PG_E5_MEM_RATE,
         "monthly": money(ram * OCI_PG_E5_MEM_RATE * hours),
         "mapping": f"Underlying E5 memory: {ram:g} GB (min 16, max 64/OCPU) x {hours:,.0f} hrs @ ${OCI_PG_E5_MEM_RATE}/GB-hr."},
    ]

# Microsoft SQL Server "license-included" on OCI (Marketplace), $/OCPU-hour.
# Applied on top of OCI compute for SQL Server workloads (RDS SQL Server instance
# runs and EC2 "Windows with SQL" instances). Standard vs Enterprise edition.
OCI_SQL_LICENSE_STD_RATE = 0.37
OCI_SQL_LICENSE_ENT_RATE = 1.47
SQL_NO_MANAGED_NOTE = ("SQL Server has no managed OCI equivalent; OCI cost carried equal to the "
                       "source AWS cost (would be self-managed on a VM).")


def sql_license_rate(text):
    """Return the OCI SQL Server license $/OCPU-hour for a bill line, by edition.
    Express is free ($0); Enterprise if the text names Enterprise (or "EE"); Standard maps
    to Standard; anything else defaults to Standard."""
    blob = normalize(clean_text(text))
    if "express" in blob:
        return 0.0  # SQL Server Express is free
    if "enterprise" in blob or " ee" in blob or "(ee)" in blob or blob.endswith(" ee"):
        return OCI_SQL_LICENSE_ENT_RATE  # Enterprise -> Enterprise
    return OCI_SQL_LICENSE_STD_RATE      # Standard (and default) -> Standard


def sql_server_ocpu(vcpus):
    """SQL Server OCI OCPU sizing: OCPU = vCPU / 2, FLOORED to the nearest even
    integer (when between sizes), minimum 2 (yields 2, 4, 6, 8, 10 ...)."""
    base = (to_number(vcpus, 0) or 0) / 2.0
    n = int(math.floor(base))
    if n % 2 != 0:
        n -= 1  # floor to the next even size down
    if n < 2:
        n = 2
    return n

# RDS size suffixes are abbreviated in this bill (e.g. "db.t3.xl" = db.t3.xlarge,
# "db.r5.2xl" = db.r5.2xlarge). Expand the suffix so the type resolves in CLOUD_SHAPE_MAP.
_RDS_SIZE_ABBREV = {
    "xl": "xlarge",
    "2xl": "2xlarge",
    "3xl": "3xlarge",
    "4xl": "4xlarge",
    "6xl": "6xlarge",
    "8xl": "8xlarge",
    "9xl": "9xlarge",
    "10xl": "10xlarge",
    "12xl": "12xlarge",
    "16xl": "16xlarge",
    "18xl": "18xlarge",
    "24xl": "24xlarge",
    "32xl": "32xlarge",
}


def _expand_rds_instance_type(text):
    """From an RDS usageType like 'InstanceUsage:db.t3.xl' or 'HeavyUsage:db.r5.2xl',
    return a normalized full instance type ('t3.xlarge', 'r5.2xlarge'). The leading
    'db.' is stripped and abbreviated size suffixes are expanded. None if no match."""
    if not text:
        return None
    m = re.search(r"db\.([a-z0-9]+\.[a-z0-9]+)", str(text).lower())
    if not m:
        return None
    inst = m.group(1)  # e.g. "t3.xl", "t4g.micro", "r5.2xl"
    family, _, size = inst.partition(".")
    size = _RDS_SIZE_ABBREV.get(size, size)
    return f"{family}.{size}"


def _is_rds_row(row):
    """True if this cloud-bill row is an AWS RDS / Aurora line."""
    blob = normalize(" ".join(clean_text(row.get(k)) for k in
                             ["source_service", "source_product", "__usageType"]))
    return ("amazonrds" in blob.replace(" ", "")
            or "relational database" in blob
            or blob.startswith("aurora")
            or ":aurora" in normalize(row.get("__usageType")))


def _rds_is_sql_server(row):
    # Match "sql server" as words, but NOT the "postgresql serverless" case:
    # normalize() turns "PostgreSQL Serverless" into "postgresql serverless",
    # which contains the substring "sql server". Exclude that with a lookbehind.
    blob = normalize(" ".join(clean_text(row.get(k)) for k in
                              ["source_product", "__usageType", "source_service"]))
    return bool(re.search(r"(?<!postgre)sql server\b", blob))


def collect_sql_server_rds_instances(rows):
    """Scan all bill rows once and return the set of normalized instance types
    (e.g. 't3.xlarge') whose RDS engine is SQL Server. Lets license / reserved-
    instance upfront fees (which don't name the engine, e.g. 'Sign up charge for
    subscription') be carried as SQL Server too."""
    out = set()
    for r in rows:
        if not _is_rds_row(r) or not _rds_is_sql_server(r):
            continue
        inst = _expand_rds_instance_type(r.get("__usageType")) or _expand_rds_instance_type(r.get("source_product"))
        if inst:
            out.add(inst)
    return out


def price_rds_row(row, sql_server_instances=None):
    """Price one AWS RDS / Aurora bill row as OCI Base Database (DBaaS).

    Returns (line_items, oci_product_label, carried, flag) or None if the row
    isn't an RDS line that we can price/carry here. 'carried' means OCI cost ==
    source cost (Aurora I/O, backups) and the line items reflect that carry.
    'flag' is a non-empty mapping-flag string for rows that should be surfaced for
    review (e.g. SQL Server license-included rows), otherwise "".

    SQL Server instance-run lines are now PRICED as OCI compute + a per-OCPU SQL
    Server license (license-included), not carried. The companion AWS license-fee
    and reserved-instance-fee lines are priced to $0 on OCI (the license is already
    counted in the per-OCPU SQL license above).

    sql_server_instances is the set of instance types known (from a full-bill scan)
    to run SQL Server, so license / reserved-instance upfront fees that don't name
    the engine still get recognized as SQL Server."""
    if not _is_rds_row(row):
        return None

    ut = clean_text(row.get("__usageType"))
    ut_n = normalize(ut)
    qty = to_number(row.get("usage_quantity"), 0)
    src_cost = to_number(row.get("source_monthly_cost"), 0)
    _row_inst = _expand_rds_instance_type(ut) or _expand_rds_instance_type(row.get("source_product"))
    _is_sqlserver_inst = bool(sql_server_instances and _row_inst and _row_inst in sql_server_instances)
    is_mysql = _is_mysql_engine(row)

    SQL_FLAG = "SQL Server - license-included (review)"

    def carry_items(reason, label):
        return ([{
            "sku": "",
            "description": "Carried over from source AWS cost",
            "quantity": 0,
            "unit": "",
            "rate": 0,
            "monthly": money(src_cost),
            "mapping": reason,
            "carriedOver": True,
        }], label, True, "")

    # 1) SQL Server (license-included): PRICE the instance run as OCI Base Database
    #    compute + a per-OCPU SQL Server license. The companion license-fee and
    #    reserved-instance-fee lines are priced to $0 (license is already counted in
    #    the per-OCPU SQL license below; AWS source cost still counts on the source side).
    if _rds_is_sql_server(row) or _is_sqlserver_inst:
        sql_label = "OCI Compute E6 (SQL Server, license-included)"
        edition_rate = sql_license_rate(row.get("source_product"))
        edition = "Enterprise" if edition_rate == OCI_SQL_LICENSE_ENT_RATE else "Standard"

        # 1a) HeavyUsage:db.<type> -> reserved-instance / license hourly fee. On OCI
        #     the license is already in the per-OCPU SQL license, so price to $0.
        if "heavyusage" in ut_n:
            return ([{
                "sku": "",
                "description": "SQL Server reserved-instance / license fee (included on OCI)",
                "quantity": 0,
                "unit": "",
                "rate": 0,
                "monthly": 0.0,
                "mapping": "AWS SQL Server license / reserved-instance fee; on OCI the SQL license is bundled in the per-OCPU license on the instance-run line, so this line is $0 to avoid double-counting.",
            }], sql_label, False, SQL_FLAG)

        inst = _row_inst
        specs = bill_instance_compute_specs(inst) if inst else None
        if inst and specs and specs[0] > 0:
            vcpu = specs[0] * 2.0  # specs[0] is vCPU/2
            ram = specs[1] or 0.0
            ocpu = sql_server_ocpu(vcpu)
            # Instance-run hours: usage_quantity if it's instance-hours, else 744.
            hours = qty if qty and qty > 0 else 744.0

            # Reserved-instance-applied compute lines (qty>0 but $0 source) are the
            # reservation usage; the matching run line carries the cost. Price $0 to
            # avoid double-counting, but still flag and label as SQL Server.
            if src_cost <= 0:
                return ([{
                    "sku": "",
                    "description": f"SQL Server reserved-instance usage ({inst}, included on OCI)",
                    "quantity": 0,
                    "unit": "",
                    "rate": 0,
                    "monthly": 0.0,
                    "mapping": "SQL Server reserved-instance-applied usage; compute is priced on the matching on-demand instance-run line. $0 here to avoid double-counting.",
                }], sql_label, False, SQL_FLAG)

            # Map the SQL Server DB to an OCI E6 Standard compute shape and price it
            # there (self-managed VM), + the SQL Server license-included image.
            _e6 = SHAPE_LOOKUP.get("e6-standard") or {}
            e6_ocpu_rate = _e6.get("computeRate", 0.03)
            e6_ram_rate = _e6.get("memoryRate", 0.002)
            compute_hours = ocpu * hours
            ram_hours = ram * hours
            license_hours = ocpu * hours
            items = [{
                "sku": _e6.get("computeSku", "B111129"),
                "description": f"OCI Compute E6 Standard - OCPU (SQL Server VM, {inst})",
                "quantity": round(compute_hours, 4),
                "unit": "OCPU-hour",
                "rate": e6_ocpu_rate,
                "monthly": money(compute_hours * e6_ocpu_rate),
                "mapping": (f"RDS SQL Server {inst} mapped to E6 Standard: {ocpu} OCPU "
                            f"(vCPU {vcpu:g}/2 floored to even, min 2) x {hours:,.0f} hrs @ ${e6_ocpu_rate}/OCPU-hr. {SQL_NO_MANAGED_NOTE}"),
            }, {
                "sku": _e6.get("memorySku", "B111130"),
                "description": "OCI Compute E6 Standard - Memory (SQL Server VM)",
                "quantity": round(ram_hours, 4),
                "unit": "GB-hour",
                "rate": e6_ram_rate,
                "monthly": money(ram_hours * e6_ram_rate),
                "mapping": f"SQL Server VM memory {ram:g} GB x {hours:,.0f} hrs @ ${e6_ram_rate}/GB-hr (E6 Standard).",
            }, {
                "sku": "",
                "description": f"Microsoft SQL Server {edition} license-included ({ocpu} OCPU)",
                "quantity": round(license_hours, 4),
                "unit": "OCPU-hour",
                "rate": edition_rate,
                "monthly": money(license_hours * edition_rate),
                "mapping": f"SQL Server {edition} license-included image on OCI: {ocpu} OCPU x {hours:,.0f} hrs x ${edition_rate}/OCPU-hr.",
            }]
            return (items, sql_label, False, SQL_FLAG)

        # Storage / backup / other SQL Server lines: price like other engines below
        # (fall through) but keep the SQL Server label + flag. Provisioned storage:
        if "storage" in ut_n or "gp2" in ut_n or "gp3" in ut_n:
            return ([{
                "sku": OCI_BASE_DB_STORAGE_SKU,
                "description": "OCI Base Database - Storage (GB-mo)",
                "quantity": round(qty, 4),
                "unit": "GB-month",
                "rate": OCI_BASE_DB_STORAGE_RATE,
                "monthly": money(qty * OCI_BASE_DB_STORAGE_RATE),
                "mapping": f"SQL Server DB provisioned storage re-priced at the OCI block/DB storage rate (${OCI_BASE_DB_STORAGE_RATE}/GB-month).",
            }], sql_label, False, SQL_FLAG)
        # Backup storage: SQL Server runs self-managed on a VM, so its backups go to
        # standard OCI Object Storage ($0.0255/GB-month), not the Oracle Database
        # Backup Cloud (RMAN) rate. Not carried.
        if "backup" in ut_n:
            _obj = OCI_SERVICE_PRICES.get("OCI Object Storage", {})
            _obj_rate = _obj.get("rate", 0.0255)
            return ([{
                "sku": _obj.get("sku", "B91628"),
                "description": "OCI Object Storage - SQL Server backup (GB-mo)",
                "quantity": round(qty, 4),
                "unit": "GB-month",
                "rate": _obj_rate,
                "monthly": money(qty * _obj_rate),
                "mapping": f"Self-managed SQL Server VM backup stored in standard OCI Object Storage (${_obj_rate}/GB-month).",
                "ociServiceUsage": True,
            }], sql_label, False, SQL_FLAG)
        # Storage I/O: bundled (free) on OCI.
        if "iousage" in ut_n:
            return ([{
                "sku": "", "description": "Database storage I/O (included on OCI)",
                "quantity": round(qty, 4), "unit": "I/O requests", "rate": 0.0, "monthly": 0.0,
                "mapping": "OCI database storage bundles I/O; free on OCI.",
                "ociServiceUsage": True,
            }], sql_label, False, SQL_FLAG)
        # Any other SQL Server charge: carry conservatively but keep SQL label + flag.
        if src_cost > 0:
            return ([{
                "sku": "",
                "description": "Carried over from source AWS cost",
                "quantity": 0,
                "unit": "",
                "rate": 0,
                "monthly": money(src_cost),
                "mapping": "SQL Server other charge: carried conservatively at the source cost.",
                "carriedOver": True,
            }], sql_label, True, SQL_FLAG)
        return ([], sql_label, False, SQL_FLAG)

    # 2) Aurora Serverless v2 (ACU-hours).
    if "serverlessv2" in ut_n:
        if is_mysql:
            # MySQL: 1 ECPU = 1 vCPU; 1 ACU ~= 0.25 vCPU -> ECPU = ACU x 0.25.
            ecpu_hours = qty * (ACU_TO_OCPU * 2)
            return ([{
                "sku": OCI_MYSQL_ECPU_SKU,
                "description": "OCI MySQL Database Service - ECPU",
                "quantity": round(ecpu_hours, 4),
                "unit": "ECPU-hour",
                "rate": OCI_MYSQL_ECPU_RATE,
                "monthly": money(ecpu_hours * OCI_MYSQL_ECPU_RATE),
                "mapping": f"Aurora Serverless v2 (MySQL): {qty:,.1f} ACU-hours x {ACU_TO_OCPU*2} ECPU/ACU (1 ECPU = 1 vCPU) priced at ${OCI_MYSQL_ECPU_RATE}/ECPU-hour.",
            }], OCI_MYSQL_PRODUCT, False, "")
        # Aurora PostgreSQL Serverless v2 -> OCI Database with PostgreSQL: managed
        # OCPU + E5 OCPU on the ACU-derived OCPU, + memory (~2 GB per ACU).
        ocpu_hours = qty * ACU_TO_OCPU
        ram_hours = qty * 2  # ~2 GiB per ACU
        return ([{
            "sku": OCI_BASE_DB_OCPU_SKU,
            "description": "OCI Database with PostgreSQL - OCPU (Aurora Serverless v2)",
            "quantity": round(ocpu_hours, 4), "unit": "OCPU-hour", "rate": OCI_BASE_DB_OCPU_RATE,
            "monthly": money(ocpu_hours * OCI_BASE_DB_OCPU_RATE),
            "mapping": f"Aurora Serverless v2: {qty:,.1f} ACU-hours x {ACU_TO_OCPU} OCPU/ACU @ ${OCI_BASE_DB_OCPU_RATE}/OCPU-hr.",
        }, {
            "sku": OCI_PG_E5_OCPU_SKU,
            "description": "OCI Compute E5 - OCPU (PostgreSQL, Aurora Serverless v2)",
            "quantity": round(ocpu_hours, 4), "unit": "OCPU-hour", "rate": OCI_PG_E5_OCPU_RATE,
            "monthly": money(ocpu_hours * OCI_PG_E5_OCPU_RATE),
            "mapping": f"Underlying E5 compute on the ACU-derived OCPU @ ${OCI_PG_E5_OCPU_RATE}/OCPU-hr.",
        }, {
            "sku": OCI_PG_E5_MEM_SKU,
            "description": "OCI Compute E5 - Memory (PostgreSQL, Aurora Serverless v2)",
            "quantity": round(ram_hours, 4), "unit": "GB-hour", "rate": OCI_PG_E5_MEM_RATE,
            "monthly": money(ram_hours * OCI_PG_E5_MEM_RATE),
            "mapping": f"Aurora memory ~2 GB/ACU @ ${OCI_PG_E5_MEM_RATE}/GB-hr.",
        }], OCI_BASE_DB_PRODUCT, False, "")

    # 3) Aurora / RDS storage I/O: OCI Base Database storage is capacity-priced and
    #    does NOT meter I/O (it is bundled), so AWS per-I/O-request charges are free
    #    on OCI.
    if "storageiousage" in ut_n:
        return ([{
            "sku": "",
            "description": "Database storage I/O (included on OCI)",
            "quantity": round(qty, 4),
            "unit": "I/O requests",
            "rate": 0.0,
            "monthly": 0.0,
            "mapping": "AWS meters Aurora storage I/O per request; OCI Base Database storage bundles I/O (capacity-priced), so it is free on OCI.",
            "ociServiceUsage": True,
        }], OCI_BASE_DB_PRODUCT, False, "")
    # 3b) Aurora / RDS backup storage beyond the free allocation -> OCI database
    #     backup storage SKUs (NOT generic Object Storage): MySQL uses MySQL Backup
    #     Storage (B92483, $0.04/GB-mo); all other engines use Oracle Database Backup
    #     Cloud -> Object Storage (B90230, $0.0051/GB-mo). Rates are per GB-month.
    if "backupusage" in ut_n:
        if is_mysql:
            _bk_sku, _bk_rate, _bk_prod = OCI_MYSQL_BACKUP_SKU, OCI_MYSQL_BACKUP_RATE, OCI_MYSQL_PRODUCT
            _bk_desc = "OCI MySQL Database - Backup Storage (GB-mo)"
        else:
            _bk_sku, _bk_rate, _bk_prod = OCI_DB_BACKUP_SKU, OCI_DB_BACKUP_RATE, OCI_BASE_DB_PRODUCT
            _bk_desc = "OCI Database Backup - Object Storage (GB-mo)"
        return ([{
            "sku": _bk_sku,
            "description": _bk_desc,
            "quantity": round(qty, 4),
            "unit": "GB-month",
            "rate": _bk_rate,
            "monthly": money(qty * _bk_rate),
            "mapping": f"Database backup storage re-priced on the OCI database backup SKU {_bk_sku} (${_bk_rate}/GB-month).",
            "ociServiceUsage": True,
        }], _bk_prod, False, "")

    # 4) Provisioned DB storage (RDS GP2/GP3, Aurora StorageUsage) -> OCI DB storage.
    if "storage" in ut_n or "gp2" in ut_n or "gp3" in ut_n:
        if is_mysql:
            return ([{
                "sku": OCI_MYSQL_STORAGE_SKU,
                "description": "OCI MySQL Database Service - Storage (GB-mo)",
                "quantity": round(qty, 4),
                "unit": "GB-month",
                "rate": OCI_MYSQL_STORAGE_RATE,
                "monthly": money(qty * OCI_MYSQL_STORAGE_RATE),
                "mapping": f"MySQL DB storage re-priced at the OCI MySQL Database Service storage rate (${OCI_MYSQL_STORAGE_RATE}/GB-month).",
            }], OCI_MYSQL_PRODUCT, False, "")
        # PostgreSQL: Database Optimized Storage ($0.072/GB) + 30 perf units/GB.
        perf_qty = qty * OCI_PG_STORAGE_PERF_PER_GB
        return ([{
            "sku": OCI_PG_STORAGE_SKU,
            "description": "OCI Database with PostgreSQL - Optimized Storage (GB-mo)",
            "quantity": round(qty, 4),
            "unit": "GB-month",
            "rate": OCI_PG_STORAGE_RATE,
            "monthly": money(qty * OCI_PG_STORAGE_RATE),
            "mapping": f"PostgreSQL DB storage re-priced at the OCI Database Optimized Storage rate (${OCI_PG_STORAGE_RATE}/GB-month).",
        }, {
            "sku": "B91962",
            "description": "OCI Database with PostgreSQL - Storage Performance Units",
            "quantity": round(perf_qty, 4),
            "unit": "Performance Units per month",
            "rate": 0.0017,
            "monthly": money(perf_qty * 0.0017),
            "mapping": f"PostgreSQL Optimized Storage performance units ({OCI_PG_STORAGE_PERF_PER_GB} per GB) at $0.0017/unit.",
        }], OCI_BASE_DB_PRODUCT, False, "")

    # 5a) HeavyUsage:db.<type> is a reserved-instance / license hourly FEE that
    #     accompanies the matching InstanceUsage compute line. Pricing it as OCPU
    #     hours too would double-count the instance, so carry it at its (small)
    #     source cost. (SQL Server HeavyUsage was already carried above.)
    if "heavyusage" in ut_n:
        if src_cost > 0:
            return carry_items(
                "RDS reserved-instance / license fee carried at the source cost (instance compute is priced on the matching InstanceUsage line; not double-counted).",
                OCI_BASE_DB_PRODUCT,
            )
        return ([], OCI_BASE_DB_PRODUCT, False, "")

    # 5b) Engine instance hours (InstanceUsage:db.<type>).
    inst = _row_inst
    if inst:
        specs = bill_instance_compute_specs(inst)
        if specs and specs[0] > 0:
            ocpus = specs[0]  # already vCPU/2
            vcpu = ocpus * 2
            # MySQL -> OCI MySQL Database Service, ECPU (1 ECPU = 1 vCPU) floored to a
            # valid MySQL shape size (2,4,8,16,32,48,64,96), priced per ECPU-hour.
            if is_mysql:
                ecpu = mysql_ecpu(vcpu)
                ecpu_hours = ecpu * qty
                mysql_ram = ecpu * MYSQL_GB_PER_ECPU  # fixed 8 GB per ECPU (16 GB per 2 ECPU)
                return ([{
                    "sku": OCI_MYSQL_ECPU_SKU,
                    "description": f"OCI MySQL Database Service - {ecpu} ECPU / {mysql_ram:g} GB ({inst})",
                    "quantity": round(ecpu_hours, 4),
                    "unit": "ECPU-hour",
                    "rate": OCI_MYSQL_ECPU_RATE,
                    "monthly": money(ecpu_hours * OCI_MYSQL_ECPU_RATE),
                    "mapping": f"RDS MySQL {inst} ({vcpu:g} vCPU -> {ecpu} ECPU floored to shape, 1 ECPU=1 vCPU; RAM fixed at {mysql_ram:g} GB = {ecpu} x 8 GB, included) x {qty:,.0f} hrs @ ${OCI_MYSQL_ECPU_RATE}/ECPU-hr.",
                }], OCI_MYSQL_PRODUCT, False, "")
            # PostgreSQL (default engine): managed service OCPU + E5 compute + memory.
            pg_items = postgres_compute_items(ocpus, specs[1], qty)
            pg_items[0]["description"] = f"OCI Database with PostgreSQL - OCPU ({inst})"
            return (pg_items, OCI_BASE_DB_PRODUCT, False, "")
        # Reserved-instance / $0 instance lines (qty>0 but cost 0) still resolve to
        # an instance type but carry no cost; price as 0 OCPU so they don't fall
        # through to the storage/catalog fallback.
        if qty <= 0 or src_cost <= 0:
            return ([], OCI_BASE_DB_PRODUCT, False, "")

    # 6) Anything else RDS (Data-API requests, unrecognized) -> carry if it has a
    #    cost, otherwise treat as included (no OCI line) so it isn't re-priced as storage.
    if src_cost > 0:
        return carry_items(
            "RDS charge with no direct OCI line-item equivalent; carried at the source cost.",
            OCI_BASE_DB_PRODUCT,
        )
    return ([], OCI_BASE_DB_PRODUCT, False, "")


# ---------------------------------------------------------------------------
# AWS networking services -> OCI networking products.
# Priced here (before the generic storage/catalog fallback) so each line is
# priced once on its own usage and never relabeled as Storage / Data Transfer.
# ---------------------------------------------------------------------------

OCI_LOAD_BALANCER_PRODUCT = "OCI Load Balancer"
OCI_FASTCONNECT_PRODUCT = "OCI FastConnect"
OCI_TRANSFER_FAMILY_PRODUCT = "SFTP / SOA Suite (Marketplace)"

# AWS Transfer Family has no per-endpoint-hour equivalent on OCI. The SFTP service is
# re-hosted on OCI Compute (SFTPGo on an Ampere VM) writing to Object Storage via the
# S3-compatible API, so the AWS $0.30/endpoint-hour charge is replaced by the cost of a
# small VM. Each AWS endpoint (744 hrs/month) maps to its own small Ampere SFTP host
# (isolation-preserving / conservative); consolidating endpoints onto shared hosts lowers
# it further. SFTP upload/download has no separate OCI charge (landed GB are priced as
# Object Storage; transfer is free within 10 TB/month).
OCI_SFTP_AMPERE_OCPU = 1
OCI_SFTP_AMPERE_MEM_GB = 8
OCI_AMPERE_OCPU_RATE = 0.01      # $/OCPU-hour (Ampere A1, standard list)
OCI_AMPERE_MEM_RATE = 0.0015     # $/GB-hour
OCI_SFTP_VM_HOURLY = (OCI_SFTP_AMPERE_OCPU * OCI_AMPERE_OCPU_RATE
                      + OCI_SFTP_AMPERE_MEM_GB * OCI_AMPERE_MEM_RATE)   # ~$0.022/VM-hour
AWS_TF_ENDPOINT_HOURS = 744      # 1 AWS Transfer Family SFTP endpoint = 744 hrs/month
# Managed alternative: Oracle SOA Suite / MFT on Marketplace (license-included) at
# $0.7231/OCPU-hour ($0.36155 per vCPU). Not used for the default SFTP repricing; kept
# for reference / a future "managed gateway" mode.
OCI_SOA_SUITE_OCPU_RATE = 0.7231


def _bill_source_code(row):
    """Lowercase, space-stripped blob of the row's source-service / product / usageType,
    used to detect which AWS service a bill line belongs to (e.g. 'awselb')."""
    return normalize(" ".join(clean_text(row.get(k)) for k in
                              ["source_service", "source_product", "__usageType"])).replace(" ", "")


def _is_elb_row(row):
    return "awselb" in _bill_source_code(row)


def _is_direct_connect_row(row):
    return "awsdirectconnect" in _bill_source_code(row)


def _is_transfer_family_row(row):
    return "awstransfer" in _bill_source_code(row)


def is_networking_service_row(row):
    return _is_elb_row(row) or _is_direct_connect_row(row) or _is_transfer_family_row(row)


# ---------------------------------------------------------------------------
# AWS messaging + managed file transfer -> Oracle Integration Cloud (OIC).
# AWS SQS (Simple Queue Service), SNS (Simple Notification Service) and Transfer
# Family consolidate onto a single Oracle Integration Cloud instance. OIC bills per
# "message pack" (5,000 messages/hour) per hour: SKU B89639, $0.6452/pack-hour,
# 744 hrs/month. The whole workload is sized at 1 message pack, so the pack is
# charged ONCE across all these rows (one anchor line carries the cost; every other
# OIC row is consolidated at $0 so nothing is double-counted). Transfer Family keeps
# the SFTPGo-on-Compute mapping described in its note as an alternate option.
# ---------------------------------------------------------------------------
OCI_OIC_PRODUCT = "Oracle Integration Cloud"
OCI_OIC_SKU = "B89639"
OCI_OIC_PACK_RATE = 0.6452       # $ per message pack (5K msg/hr) per hour
OCI_OIC_PACK_HOURS = 744         # hours/month used for OIC message-pack billing
OCI_OIC_MESSAGE_PACKS = 1        # fallback size when the workload can't be measured
OCI_OIC_MSG_PER_PACK_HR = 5000   # 1 pack = 5,000 messages/hour (payload <= 50KB each)
OCI_OIC_MSG_BYTES = 50 * 1024    # 50KB message payload cap (larger files -> more messages)


def _oic_auto_packs(rows, hours=HOURS_PER_MONTH):
    """Auto-size the Oracle Integration Cloud message packs from the actual bill workload:
    SQS/SNS request lines are message counts; Transfer Family byte volume becomes messages
    at <=50KB each. One pack = 5,000 msg/hr -> 3.65M messages/month (730 hrs). Packs =
    ceil(total messages / 3.65M), floored at 1 when any OIC workload exists (0 if none).
    Same message-pack math as the Add-OCI-services OIC card."""
    hours = float(hours or HOURS_PER_MONTH)
    msgs = 0.0
    seen = False
    for r in rows:
        if not _is_oic_row(r) or (r.get("costAction") or "") == "remove":
            continue
        seen = True
        ut = normalize(r.get("__usageType"))
        unit = normalize(r.get("usage_unit"))
        qty = to_number(r.get("usage_quantity"), 0)
        if _is_transfer_family_row(r):
            # File-transfer bytes -> messages (each <=50KB). Bill quantity is in GB.
            if "byte" in ut or unit in ("gb", "gib", "gigabyte", "gigabytes"):
                msgs += (qty * 1_000_000_000.0) / OCI_OIC_MSG_BYTES
        elif "request" in ut or "message" in ut:
            msgs += qty
    if not seen:
        return 0
    if msgs <= 0:
        return OCI_OIC_MESSAGE_PACKS
    return max(1, math.ceil(msgs / (hours * OCI_OIC_MSG_PER_PACK_HR)))


def _is_sqs_row(row):
    code = _bill_source_code(row)
    return "awsqueueservice" in code or "simplequeueservice" in code


def _is_sns_row(row):
    code = _bill_source_code(row)
    return "amazonsns" in code or "simplenotificationservice" in code


def _is_oic_row(row):
    """SQS / SNS / Transfer Family -> consolidated onto Oracle Integration Cloud."""
    return _is_sqs_row(row) or _is_sns_row(row) or _is_transfer_family_row(row)


def collect_oic_anchor_row(rows):
    """The single row that carries the one Oracle Integration Cloud message-pack
    charge (highest source cost among the OIC-eligible rows; ties -> first)."""
    best_id, best_cost = None, None
    for r in rows:
        if not _is_oic_row(r):
            continue
        c = to_number(r.get("source_monthly_cost"), 0)
        if best_id is None or c > best_cost:
            best_id, best_cost = r.get("__id"), c
    return best_id


def price_oic_row(row, oic_anchor_row_id=None, packs=None):
    """Map an SQS / SNS / Transfer Family row to Oracle Integration Cloud. The whole
    workload is sized at `packs` message pack(s) (defaults to OCI_OIC_MESSAGE_PACKS,
    user-editable); the anchor row carries that single charge and every other OIC row
    is consolidated at $0.
    Returns (line_items, oci_product_label, oci_category, sku, carried)."""
    packs = packs if (packs and packs > 0) else OCI_OIC_MESSAGE_PACKS
    alt = ("  Alternate mapping (also shown in the app): re-host SFTP as SFTPGo on OCI "
           "Compute (Ampere ~$0.022/VM-hour) writing to Object Storage via the "
           "S3-compatible API." if _is_transfer_family_row(row) else "")
    if row.get("__id") == oic_anchor_row_id:
        monthly = packs * OCI_OIC_PACK_RATE * OCI_OIC_PACK_HOURS
        _pk = int(packs) if float(packs).is_integer() else packs
        mapping = (f"AWS SQS + SNS messaging and Transfer Family consolidated onto "
                   f"Oracle Integration Cloud - Standard: {_pk} message "
                   f"pack(s) (5K messages/hour) x {OCI_OIC_PACK_HOURS} hrs x "
                   f"${OCI_OIC_PACK_RATE}/pack-hour (SKU {OCI_OIC_SKU})." + alt)
        return ([{
            "sku": OCI_OIC_SKU,
            "description": "Oracle Integration Cloud Service - Standard (5K msg/hr message pack)",
            "quantity": _pk,
            "unit": f"message pack x {OCI_OIC_PACK_HOURS} hrs",
            "rate": OCI_OIC_PACK_RATE,
            "monthly": money(monthly),
            "mapping": mapping,
        }], OCI_OIC_PRODUCT, "Application Integration", OCI_OIC_SKU, False)
    # non-anchor OIC rows -> consolidated into the single message pack (no extra cost)
    return ([{
        "sku": OCI_OIC_SKU,
        "description": "Consolidated into Oracle Integration Cloud message pack",
        "quantity": 0,
        "unit": "",
        "rate": 0,
        "monthly": 0.0,
        "mapping": ("Consolidated into the single Oracle Integration Cloud message pack "
                    "(counted once on the anchor line)." + alt),
    }], OCI_OIC_PRODUCT, "Application Integration", OCI_OIC_SKU, False)


# ---- Amazon Redshift -> Oracle Autonomous Data Warehouse (ADW) ----
# Sizing: Redshift Serverless is USAGE-based (1 RPU = 2 vCPU / 16 GB, billed per RPU-hour,
# scales to zero when idle). ADW ECPU is always-on (4 ECPU = 1 OCPU = 1 core = 2 vCPU, so
# 1 vCPU = 2 ECPU). To avoid pricing an always-on ADW against pay-per-use Serverless, we size
# the ADW BASE to the amortized average capacity - avg RPU = total RPU-hours / 744 - as
# ECPU = max(2, ROUNDUP(avg RPU)), and rely on ADW auto-scaling (up to 3x the base, billed
# only when used) to absorb bursts, mirroring Serverless elasticity. Priced $0.336/ECPU-hr x
# 744; Redshift Managed Storage -> $0.024/GB-month. (Was ROUNDUP(avg RPU) x 2, which sized an
# always-on ADW at ~2x the actual Serverless spend.)
OCI_ADW_PRODUCT = "Oracle Autonomous Data Warehouse"
OCI_ADW_ECPU_RATE = 0.336
OCI_ADW_STORAGE_RATE = 0.024
OCI_ADW_HOURS = 744


def _is_redshift_row(row):
    return "amazonredshift" in _bill_source_code(row) or "redshift" in _bill_source_code(row)


def _is_redshift_rpu_row(row):
    utn = normalize(row.get("__usageType"))
    return _is_redshift_row(row) and ("rpu" in utn or "serverlessusage" in utn) and "storage" not in utn and "rms" not in utn


def collect_redshift_compute(rows):
    """Aggregate Redshift Serverless RPU-hours across the bill into a single ADW ECPU
    figure: base ECPU = max(2, ROUNDUP(total RPU-hrs / 744)) - i.e. the amortized average
    RPU rounded up (min 2 ECPU), with ADW auto-scaling covering peaks. Picks the largest RPU
    line to carry the whole compute cost so per-line rounding doesn't inflate it."""
    total_rpu = 0.0
    best_id, best_qty = None, 0.0
    for r in rows or []:
        if not _is_redshift_rpu_row(r):
            continue
        q = to_number(r.get("usage_quantity"), 0)
        total_rpu += q
        if q > best_qty:
            best_qty, best_id = q, r.get("__id")
    ecpu = max(2, math.ceil(total_rpu / OCI_ADW_HOURS)) if total_rpu > 0 else 0
    return {"ecpu": ecpu, "total_rpu": total_rpu, "row_id": best_id}


def price_redshift_row(row, compute_ctx=None):
    """Price an Amazon Redshift bill line on Oracle Autonomous Data Warehouse.
    compute_ctx carries the aggregated Serverless ECPU; only the designated row
    bills the compute, so the total isn't inflated by per-line rounding."""
    if not _is_redshift_row(row):
        return None
    ut_n = normalize(row.get("__usageType"))
    qty = to_number(row.get("usage_quantity"), 0)
    src = to_number(row.get("source_monthly_cost"), 0)
    # Serverless compute (RPU-hours) -> ADW ECPU (aggregated; billed on one line).
    if ("rpu" in ut_n or "serverlessusage" in ut_n) and "storage" not in ut_n and "rms" not in ut_n:
        ctx = compute_ctx or {}
        if row.get("__id") == ctx.get("row_id"):
            ecpu = ctx.get("ecpu", 0)
            monthly = ecpu * OCI_ADW_ECPU_RATE * OCI_ADW_HOURS
            return ([{
                "sku": "", "description": f"Autonomous Data Warehouse ({ecpu} ECPU)",
                "quantity": ecpu, "unit": "ECPU", "rate": OCI_ADW_ECPU_RATE,
                "monthly": money(monthly),
                "mapping": (f"Redshift Serverless {ctx.get('total_rpu', qty):,.0f} RPU-hr (total; avg "
                            f"{ctx.get('total_rpu', qty) / OCI_ADW_HOURS:,.1f} RPU) -> ADW base {ecpu} ECPU "
                            f"(max(2, ROUNDUP(RPU-hr/744)); auto-scaling covers peaks) x "
                            f"${OCI_ADW_ECPU_RATE}/ECPU-hr x {OCI_ADW_HOURS}."),
            }], OCI_ADW_PRODUCT, "Database", "", False)
        # Other RPU lines fold into the aggregated compute -> $0 here.
        return ([{"sku": "", "description": "Autonomous Data Warehouse (ECPU folded into aggregate)",
                  "quantity": 0, "unit": "ECPU", "rate": OCI_ADW_ECPU_RATE, "monthly": 0.0,
                  "mapping": "RPU-hours aggregated into the ADW ECPU compute line."}],
                OCI_ADW_PRODUCT, "Database", "", False)
    # Redshift Managed Storage -> ADW storage (GB-month).
    if "rms" in ut_n or "storage" in ut_n:
        monthly = qty * OCI_ADW_STORAGE_RATE
        return ([{
            "sku": "", "description": "Autonomous Data Warehouse - Storage (GB-mo)",
            "quantity": round(qty, 4), "unit": "GB per month", "rate": OCI_ADW_STORAGE_RATE,
            "monthly": money(monthly),
            "mapping": f"Redshift Managed Storage re-priced on ADW storage at ${OCI_ADW_STORAGE_RATE}/GB-mo.",
        }], OCI_ADW_PRODUCT, "Database", "", False)
    # Other Redshift lines -> carry conservatively.
    if src > 0:
        return ([{
            "sku": "", "description": "Carried over from source AWS cost",
            "quantity": 0, "unit": "", "rate": 0, "monthly": money(src),
            "mapping": "Redshift line with no direct ADW equivalent; carried at the source cost.",
            "carriedOver": True,
        }], OCI_ADW_PRODUCT, "Database", "", True)
    return ([{"sku": "", "description": "Included on ADW", "quantity": 0, "unit": "", "rate": 0,
              "monthly": 0.0, "mapping": "Redshift line included on ADW."}], OCI_ADW_PRODUCT, "Database", "", False)


# OCI FastConnect per-port-hour rates by provisioned port speed (OCI price list).
OCI_FASTCONNECT_PORT_RATES = {"1G": 0.2125, "10G": 1.275, "100G": 10.75, "400G": 20.00}
OCI_FASTCONNECT_DEFAULT_RATE = 0.2125  # default to 1 Gbps when the speed token isn't recognized
# OCI Load Balancer per-load-balancer-hour rate (OCI price list SKU B93031).
OCI_LOAD_BALANCER_SKU = "B93031"
OCI_LOAD_BALANCER_RATE = 0.0113
# OCI Flexible Load Balancer pricing, matching the reference workbook's logic:
#   instances = ROUNDUP(LB-hours / 744); bandwidth = instances x 100 Mbps;
#   OCI = instances x $0.0113 x 744 + bandwidth x $0.0001 x 744;
#   the OCI always-free tier (1 instance + 10 Mbps) is subtracted once per bill.
OCI_LB_HOURS = 744
OCI_LB_BANDWIDTH_RATE = 0.0001   # per Mbps-hour
OCI_LB_MBPS_PER_INSTANCE = 100
OCI_LB_FREE_INSTANCES = 1
OCI_LB_FREE_MBPS = 10


def _elb_instances(qty):
    import math as _m
    q = to_number(qty, 0)
    return int(_m.ceil(q / OCI_LB_HOURS)) if q > 0 else 0


def collect_elb_free_tier_row(rows):
    """Pick the ELB bill row that should carry the one-time OCI Load Balancer
    free tier (the largest LoadBalancerUsage line), so the free tier is applied
    exactly once across the whole bill (matching the reference)."""
    best_id = None
    best_qty = 0.0
    for r in rows or []:
        if not _is_elb_row(r):
            continue
        if "loadbalancerusage" not in normalize(r.get("__usageType")):
            continue
        q = to_number(r.get("usage_quantity"), 0)
        if q > best_qty:
            best_qty = q
            best_id = r.get("__id")
    return best_id


def _fastconnect_speed_token(usagetype_norm):
    """Extract the FastConnect port speed token (1G/10G/100G/400G) from a Direct
    Connect PortUsage usageType, e.g. 'use1-eqdc2-portusage:1g' -> '1G'."""
    # normalize() collapses the ':' separator to a space, so match either form
    # (e.g. 'portusage:1g' or 'portusage 1g').
    m = re.search(r"portusage[:\s]+(\d+)g\b", usagetype_norm)
    if m:
        return f"{m.group(1)}G"
    return None


def price_networking_row(row, elb_free_tier_row_id=None):
    """Price one AWS networking bill row (ELB / Direct Connect / Transfer Family)
    as its OCI equivalent.

    Returns (line_items, oci_product_label, oci_category, sku, carried) or None if
    the row isn't a networking line handled here. Each line is priced on its OWN
    usage_quantity / usageType. elb_free_tier_row_id marks the single ELB line
    that gets the one-time OCI Load Balancer free tier."""
    ut = clean_text(row.get("__usageType"))
    ut_n = normalize(ut)
    qty = to_number(row.get("usage_quantity"), 0)
    src_cost = to_number(row.get("source_monthly_cost"), 0)

    def free_item(reason):
        return ([{
            "sku": "",
            "description": "Included (free on OCI)",
            "quantity": 0,
            "unit": "",
            "rate": 0,
            "monthly": 0.0,
            "mapping": reason,
        }], None, None, "", False)

    # 1) Elastic Load Balancing -> OCI Flexible Load Balancer.
    # Reference logic: each LB/LCU line -> instances=ROUNDUP(hours/744),
    # bandwidth=instances*100 Mbps, OCI = instances*0.0113*744 + bandwidth*0.0001*744,
    # with the always-free tier (1 instance + 10 Mbps) subtracted once per bill.
    if _is_elb_row(row):
        if "loadbalancerusage" in ut_n:
            # Blended OCI Flexible LB rate per LB-hour = instance ($0.0113) +
            # 100 Mbps x $0.0001 = $0.0213/LB-hour (matches the reference's
            # instance + 100 Mbps-per-instance model without per-line rounding).
            blended = OCI_LOAD_BALANCER_RATE + OCI_LB_MBPS_PER_INSTANCE * OCI_LB_BANDWIDTH_RATE
            monthly = qty * blended
            if row.get("__id") == elb_free_tier_row_id:
                # Subtract the OCI always-free tier (1 instance + 10 Mbps) once.
                free_credit = (OCI_LB_FREE_INSTANCES * OCI_LOAD_BALANCER_RATE
                               + OCI_LB_FREE_MBPS * OCI_LB_BANDWIDTH_RATE) * OCI_LB_HOURS
                monthly = max(0.0, monthly - free_credit)
            return ([{
                "sku": OCI_LOAD_BALANCER_SKU,
                "description": "OCI Load Balancer (LB-hour, incl. 100 Mbps)",
                "quantity": round(qty, 4),
                "unit": "Load-Balancer-hour",
                "rate": round(blended, 4),
                "monthly": money(monthly),
                "mapping": (f"ELB -> OCI Flexible Load Balancer at ${blended:.4f}/LB-hour "
                            f"(instance ${OCI_LOAD_BALANCER_RATE} + 100 Mbps x ${OCI_LB_BANDWIDTH_RATE}/Mbps-hr)"
                            + (" less the always-free 1 instance + 10 Mbps." if row.get("__id") == elb_free_tier_row_id else ".")),
            }], OCI_LOAD_BALANCER_PRODUCT, "Networking", OCI_LOAD_BALANCER_SKU, False)
        if "lcuusage" in ut_n:
            items, _, _, sku, carried = free_item(
                "ELB capacity units (LCU) are included (free) on OCI Load Balancer; not priced.")
            return (items, OCI_LOAD_BALANCER_PRODUCT, "Networking", sku, carried)
        # Other ELB lines (e.g. data processed) -> carry conservatively if priced.
        if src_cost > 0:
            return ([{
                "sku": "", "description": "Carried over from source AWS cost",
                "quantity": 0, "unit": "", "rate": 0, "monthly": money(src_cost),
                "mapping": "ELB charge with no direct OCI line-item equivalent; carried at the source cost.",
                "carriedOver": True,
            }], OCI_LOAD_BALANCER_PRODUCT, "Networking", "", True)
        items, _, _, sku, carried = free_item("ELB line with no OCI cost; included on OCI Load Balancer.")
        return (items, OCI_LOAD_BALANCER_PRODUCT, "Networking", sku, carried)

    # 2) AWS Direct Connect -> OCI FastConnect.
    if _is_direct_connect_row(row):
        # FastConnect does not meter traffic: Direct Connect data-transfer lines are free.
        if "dataxfer" in ut_n:
            items, _, _, sku, carried = free_item(
                "FastConnect does not meter traffic; Direct Connect data transfer is free on OCI.")
            return (items, OCI_FASTCONNECT_PRODUCT, "Networking", sku, carried)
        if "portusage" in ut_n:
            speed = _fastconnect_speed_token(ut_n)
            rate = OCI_FASTCONNECT_PORT_RATES.get(speed, OCI_FASTCONNECT_DEFAULT_RATE)
            speed_label = speed or "1G (default)"
            monthly = qty * rate
            return ([{
                "sku": OCI_FASTCONNECT_PRODUCT,
                "description": f"OCI FastConnect - {speed_label} port (port-hour)",
                "quantity": round(qty, 4),
                "unit": "port-hour",
                "rate": rate,
                "monthly": money(monthly),
                "mapping": f"Direct Connect {speed_label} port-hours re-priced at the OCI FastConnect rate (${rate}/port-hour).",
            }], OCI_FASTCONNECT_PRODUCT, "Networking", "", False)
        # Other Direct Connect lines -> carry if priced, else free.
        if src_cost > 0:
            return ([{
                "sku": "", "description": "Carried over from source AWS cost",
                "quantity": 0, "unit": "", "rate": 0, "monthly": money(src_cost),
                "mapping": "Direct Connect charge with no direct OCI line-item equivalent; carried at the source cost.",
                "carriedOver": True,
            }], OCI_FASTCONNECT_PRODUCT, "Networking", "", True)
        items, _, _, sku, carried = free_item("Direct Connect line with no OCI cost; included on OCI FastConnect.")
        return (items, OCI_FASTCONNECT_PRODUCT, "Networking", sku, carried)

    # 3) AWS Transfer Family -> OCI Compute SFTP (SFTPGo) writing to Object Storage.
    #    The AWS per-endpoint-hour charge ($0.30) is replaced by the cost of a small
    #    OCI Compute (Ampere) host; data transfer over SFTP has no separate OCI charge.
    if _is_transfer_family_row(row):
        # 3a) Per-endpoint protocol hours -> small Ampere SFTP VM-hours (no per-endpoint fee).
        if "protocolhours" in ut_n:
            endpoints = (qty / AWS_TF_ENDPOINT_HOURS) if qty else 0
            monthly = qty * OCI_SFTP_VM_HOURLY
            return ([{
                "sku": "",
                "description": "OCI Compute (Ampere) hosting SFTP (SFTPGo) - VM-hour",
                "quantity": round(qty, 4),
                "unit": "endpoint-hour -> VM-hour",
                "rate": round(OCI_SFTP_VM_HOURLY, 4),
                "monthly": money(monthly),
                "mapping": (f"AWS Transfer Family endpoint-hours (~{endpoints:.1f} endpoint(s)) re-hosted as "
                            f"SFTPGo on OCI Compute at ${OCI_SFTP_VM_HOURLY:.4f}/VM-hour "
                            f"(Ampere {OCI_SFTP_AMPERE_OCPU} OCPU/{OCI_SFTP_AMPERE_MEM_GB} GB) vs AWS $0.30/endpoint-hour. "
                            f"One VM per endpoint (isolation-preserving); consolidating endpoints lowers this further. "
                            f"Files land in OCI Object Storage via the S3-compatible API."),
            }], OCI_TRANSFER_FAMILY_PRODUCT, "Networking", "", False)
        # 3b) SFTP data uploaded/downloaded -> no separate OCI charge (storage priced elsewhere).
        if "bytes" in ut_n:
            items, _, _, sku, carried = free_item(
                "SFTP upload/download has no separate OCI charge; landed GB are priced as OCI "
                "Object Storage and transfer is free within 10 TB/month.")
            return (items, OCI_TRANSFER_FAMILY_PRODUCT, "Networking", sku, carried)
        # 3c) Any other Transfer Family line -> carry if priced, else free.
        if src_cost > 0:
            return ([{
                "sku": "", "description": "Carried over from source AWS cost",
                "quantity": 0, "unit": "", "rate": 0, "monthly": money(src_cost),
                "mapping": "Transfer Family charge with no direct OCI line-item equivalent; carried at the source cost.",
                "carriedOver": True,
            }], OCI_TRANSFER_FAMILY_PRODUCT, "Networking", "", True)
        items, _, _, sku, carried = free_item("Transfer Family line with no OCI cost.")
        return (items, OCI_TRANSFER_FAMILY_PRODUCT, "Networking", sku, carried)

    return None


FREE_OCI_SOURCE_TERMS = [
    "virtual private cloud", "amazonvpc", " vpc", "cloudtrail",
    "aws support", "savings plan", "savingsplan",
    # EBS direct API requests are API-call charges (not capacity) and free on OCI.
    "ebs direct",
    # OCI NAT Gateway has no per-GB data-processing or hourly charge -> free.
    "nat gateway", "natgateway",
    # gp3 provisioned IOPS / throughput: OCI Block Volume bundles performance,
    # so these extra-performance lines are free (the gp3 storage GB is still priced).
    "volumep iops gp3", "volumep throughput gp3",
    # Regional / intra-region (cross-AZ) data transfer is free on OCI: VCN traffic
    # within a region and across availability domains is not metered.
    "datatransfer regional", "regional data transfer",
]


def is_free_oci_service(row):
    """Services the migration treats as free on OCI (VPC, CloudTrail, Support,
    Savings Plans, plus every service the reference Service Comp List flags as
    FREE: Config, Systems Manager, Cost Explorer, GuardDuty, Security Hub,
    CloudFormation, Certificate Manager, etc.), matching the reference."""
    text = normalize(" ".join(clean_text(row.get(k)) for k in ["source_service", "source_product", "__usageType"]))
    svc_n = normalize(row.get("source_service"))
    if "support" in svc_n:
        return True
    if any(t.strip() in text for t in FREE_OCI_SOURCE_TERMS):
        return True
    # AWS KMS -> OCI Vault: software-protected keys and their operations are free on
    # OCI (only HSM-protected / dedicated / external key management is paid).
    if "kms" in svc_n or "key management service" in text:
        return True
    # AWS CloudWatch metrics, alarms, dashboards, and metric API requests are free on
    # OCI Monitoring. CloudWatch *Logs* (VendedLog / DataProcessing / TimedStorage
    # byte-hours) map to OCI Logging, which is metered, so those are NOT free.
    if "cloudwatch" in svc_n:
        ut_cw = normalize(row.get("__usageType"))
        is_logs = any(k in ut_cw for k in [
            "vendedlog", "dataprocessing", "timedstorage", "logbytes", "putlogevents", "egress"])
        is_metrics = ut_cw.startswith("cw") or any(k in ut_cw for k in [
            "metricmonitor", "alarmmonitor", "dashboard", "gmd metrics", "metricstorage", "requests"])
        if is_metrics and not is_logs:
            return True
    ref = ref_service_lookup(row.get("source_service"), row.get("source_product"), row.get("__usageType"))
    return bool(ref and ref.get("free"))


# OCI Web Application Firewall: first instance free, then $5/instance-month; first
# 10M incoming requests/month free, then $0.60 per 1,000,000 requests. OCI WAF has no
# separate per-rule / per-WebACL charge (rules and bot management are bundled).
OCI_WAF_PRODUCT = "OCI Web Application Firewall"
OCI_WAF_INSTANCE_SKU = "B94579"
OCI_WAF_REQUEST_SKU = "B94277"
OCI_WAF_INSTANCE_RATE = 5.00          # per instance per month (after first free)
OCI_WAF_REQUEST_RATE = 0.60           # per 1,000,000 incoming requests (after free pool)
OCI_WAF_FREE_INSTANCES = 1            # first WAF instance free (once per bill)
OCI_WAF_FREE_REQUESTS = 10_000_000    # first 10M incoming requests/month free (once per bill)


def _is_waf_row(row):
    return "waf" in normalize(row.get("source_service"))


def price_waf_row(row, waf_instance_pool, waf_request_pool):
    """Price an AWS WAF bill row on OCI Web Application Firewall.

    waf_instance_pool / waf_request_pool are one-element lists holding the remaining
    free instances / free requests, shared once per bill. Web ACLs map to OCI WAF
    instances (first free, then $5/mo each); request meters price at $0.60/1M after
    the 10M free pool; rule / bot-management lines are bundled (free) on OCI.
    Returns (line_items, label, category, sku, carried) or None."""
    if not _is_waf_row(row):
        return None
    ut_n = normalize(row.get("__usageType"))
    qty = to_number(row.get("usage_quantity"), 0)
    # 1) Request meters -> $0.60 per 1M after the 10M/month free pool.
    if "request" in ut_n:
        free_here = min(waf_request_pool[0], qty)
        waf_request_pool[0] -= free_here
        chargeable = max(qty - free_here, 0.0)
        millions = chargeable / 1_000_000.0
        return ([{
            "sku": OCI_WAF_REQUEST_SKU,
            "description": "OCI WAF - Requests (per 1M incoming requests)",
            "quantity": round(qty, 4),
            "unit": "request",
            "rate": OCI_WAF_REQUEST_RATE,
            "monthly": money(millions * OCI_WAF_REQUEST_RATE),
            "mapping": ("OCI WAF requests: first 10M/month free"
                        + (f" ({free_here:,.0f} free here)" if free_here > 0 else "")
                        + f"; ${OCI_WAF_REQUEST_RATE} per 1,000,000 over the free pool."),
            "ociServiceUsage": True,
        }], OCI_WAF_PRODUCT, "Security", OCI_WAF_REQUEST_SKU, False)
    # 2) Web ACLs -> OCI WAF instances: first instance free, then $5/instance-month.
    if "webacl" in ut_n:
        free_here = min(waf_instance_pool[0], qty)
        waf_instance_pool[0] -= free_here
        chargeable = max(qty - free_here, 0.0)
        return ([{
            "sku": OCI_WAF_INSTANCE_SKU,
            "description": "OCI WAF - Instance (per WAF instance/month)",
            "quantity": round(qty, 4),
            "unit": "instance per month",
            "rate": OCI_WAF_INSTANCE_RATE,
            "monthly": money(chargeable * OCI_WAF_INSTANCE_RATE),
            "mapping": ("Web ACLs map to OCI WAF instances: first instance free"
                        + (f" ({free_here:,.0f} free here)" if free_here > 0 else "")
                        + f"; ${OCI_WAF_INSTANCE_RATE}/instance-month after."),
            "ociServiceUsage": True,
        }], OCI_WAF_PRODUCT, "Security", OCI_WAF_INSTANCE_SKU, False)
    # 3) Rules / bot-management / everything else -> bundled (free) on OCI WAF.
    return ([{
        "sku": OCI_WAF_INSTANCE_SKU,
        "description": "OCI WAF - Rules / bot management (bundled)",
        "quantity": round(qty, 4),
        "unit": "",
        "rate": 0.0,
        "monthly": 0.0,
        "mapping": "OCI Web Application Firewall has no separate per-rule / per-WebACL or bot-management charge; bundled with the WAF instance and requests.",
        "ociServiceUsage": True,
    }], OCI_WAF_PRODUCT, "Security", OCI_WAF_INSTANCE_SKU, False)


# OCI Logging: first 10 GB/month free, then $0.05/GB-month. CloudWatch Logs
# (VendedLog / DataProcessing / TimedStorage) map here (CloudWatch metrics are free
# on OCI Monitoring and handled in is_free_oci_service).
OCI_LOGGING_PRODUCT = "OCI Logging"
OCI_LOGGING_SKU = "B92707"
OCI_LOGGING_RATE = 0.05
OCI_LOGGING_FREE_GB = 10.0


def _is_cloudwatch_logs_row(row):
    if "cloudwatch" not in normalize(row.get("source_service")):
        return False
    ut = normalize(row.get("__usageType"))
    return any(k in ut for k in ["vendedlog", "dataprocessing", "timedstorage", "logbytes", "putlogevents"])


def price_cloudwatch_logs_row(row, logging_pool):
    """Price a CloudWatch Logs bill row on OCI Logging: first 10 GB/month free
    (shared once per bill), then $0.05/GB-month. logging_pool is [remaining_free_gb].
    Returns (line_items, label, category, sku, carried) or None."""
    if not _is_cloudwatch_logs_row(row):
        return None
    qty = to_number(row.get("usage_quantity"), 0)  # GB
    free_here = min(logging_pool[0], qty)
    logging_pool[0] -= free_here
    chargeable = max(qty - free_here, 0.0)
    return ([{
        "sku": OCI_LOGGING_SKU,
        "description": "OCI Logging - Log Storage (GB-mo)",
        "quantity": round(qty, 4),
        "unit": "GB-month",
        "rate": OCI_LOGGING_RATE,
        "monthly": money(chargeable * OCI_LOGGING_RATE),
        "mapping": ("CloudWatch Logs re-priced on OCI Logging: first 10 GB/month free"
                    + (f" ({free_here:,.1f} GB free here)" if free_here > 0 else "")
                    + f"; ${OCI_LOGGING_RATE}/GB-month after."),
        "ociServiceUsage": True,
    }], OCI_LOGGING_PRODUCT, "Observability & Management", OCI_LOGGING_SKU, False)


# OCI DNS: $0.85 per 1,000,000 queries; zones are free. Route 53 maps here. (OCI DNS
# can run higher than Route 53, so these rows are flagged as a non-ideal mapping.)
OCI_DNS_PRODUCT = "OCI DNS"
OCI_DNS_SKU = "B88516"
OCI_DNS_RATE = 0.85  # per 1,000,000 queries


def _is_route53_row(row):
    s = normalize(row.get("source_service"))
    return "route53" in s or "route 53" in s


def price_route53_row(row):
    """Price an AWS Route 53 bill row on OCI DNS: $0.85 per 1,000,000 queries;
    hosted zones and intra-VCN/internal resolver queries are free on OCI.
    Returns (line_items, label, category, sku, carried) or None."""
    if not _is_route53_row(row):
        return None
    ut = normalize(row.get("__usageType"))
    qty = to_number(row.get("usage_quantity"), 0)
    # Internal / intra-VCN resolver queries are free on OCI.
    if "intra" in ut or "internal" in ut:
        return ([{
            "sku": OCI_DNS_SKU, "description": "OCI DNS - internal resolver queries (free)",
            "quantity": round(qty, 4), "unit": "query", "rate": 0.0, "monthly": 0.0,
            "mapping": "Internal / intra-VCN DNS resolution is free on OCI.",
            "ociServiceUsage": True,
        }], OCI_DNS_PRODUCT, "Networking", OCI_DNS_SKU, False)
    # Public DNS queries -> $0.85 per 1,000,000.
    if "quer" in ut:
        millions = qty / 1_000_000.0
        return ([{
            "sku": OCI_DNS_SKU, "description": "OCI DNS - Queries (per 1M)",
            "quantity": round(qty, 4), "unit": "query", "rate": OCI_DNS_RATE,
            "monthly": money(millions * OCI_DNS_RATE),
            "mapping": f"Route 53 DNS queries re-priced on OCI DNS at ${OCI_DNS_RATE} per 1,000,000 queries.",
            "ociServiceUsage": True,
        }], OCI_DNS_PRODUCT, "Networking", OCI_DNS_SKU, False)
    # Hosted zones and everything else: OCI DNS zones are free.
    return ([{
        "sku": OCI_DNS_SKU, "description": "OCI DNS - Hosted zone (free)",
        "quantity": round(qty, 4), "unit": "zone", "rate": 0.0, "monthly": 0.0,
        "mapping": "OCI DNS does not charge per hosted zone; only per-query usage is billed.",
        "ociServiceUsage": True,
    }], OCI_DNS_PRODUCT, "Networking", OCI_DNS_SKU, False)


# ---- AWS WorkSpaces -> OCI Secure Desktops (full stack) ---------------------
# A real OCI Secure Desktop is the per-desktop service fee (B95518) PLUS the
# underlying E6 compute (B111129 OCPU / B111130 memory) and its boot volume
# (B91961 storage + B91962 performance units). This mirrors the "Secure Desktops"
# add-in card exactly, so a mapped WorkSpaces fleet ties out to the same rates
# instead of being under-priced at a flat $20/desktop.
WS_DESKTOP_SKU = "B95518"; WS_DESKTOP_RATE = 20.00     # Secure Desktop / month
WS_OCPU_SKU = "B111129"; WS_OCPU_RATE = 0.03           # E6 OCPU / hour
WS_MEM_SKU = "B111130"; WS_MEM_RATE = 0.002            # E6 memory / GB-hour
WS_BLOCK_SKU = "B91961"; WS_BLOCK_RATE = 0.0255        # Block storage / GB-mo
WS_VPU_SKU = "B91962"; WS_VPU_RATE = 0.0017            # Block perf units / (GB*VPU)-mo
WS_BOOT_VPU = 10                                       # Balanced boot volume (10 VPU/GB)
WS_DEFAULT_BOOT_GB = 100
WS_PRODUCT = "OCI Secure Desktops"
WS_CATEGORY = "Other Services"


def _is_workspaces_row(row):
    blob = normalize(" ".join(clean_text(row.get(k)) for k in
                              ["source_service", "source_product", "__usageType"]))
    return "workspace" in blob


def _parse_ws_bundle(text):
    """Pull (vcpu, mem_gb, boot_gb) from a WorkSpaces hardware line description,
    e.g. 'Power-4vCPU,16GB Memory,175GB Root,100GB User' or
    'General Purpose (16 vCPU, 64GB RAM), Root:175 GB,User:100 GB'."""
    t = clean_text(text)
    vcpu = 0
    mem = 0.0
    m = re.search(r"(\d+)\s*vcpu", t, re.I)
    if m:
        vcpu = int(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*gb\s*(?:memory|ram)", t, re.I)
    if m:
        mem = float(m.group(1))
    root = user = 0
    m = re.search(r"(\d+)\s*gb\s*root", t, re.I) or re.search(r"root:?\s*(\d+)\s*gb", t, re.I)
    if m:
        root = int(m.group(1))
    m = re.search(r"(\d+)\s*gb\s*user", t, re.I) or re.search(r"user:?\s*(\d+)\s*gb", t, re.I)
    if m:
        user = int(m.group(1))
    boot = (root + user) or WS_DEFAULT_BOOT_GB
    return vcpu, mem, boot


def price_workspaces_row(row, hours=None):
    """Price an AWS WorkSpaces bill row on OCI Secure Desktops.

    AWS bills WorkSpaces as separate hardware (compute) and software (Office/
    utilities) lines. Only the hardware desktop lines represent provisioned
    desktops; software is BYOL on OCI, and AutoStop hourly-usage lines are just
    runtime of a desktop already counted on its monthly base line. Hardware
    desktop lines emit the full Secure Desktop stack sized from the bundle specs.
    Returns (line_items, label, category, sku, carried) or None."""
    if not _is_workspaces_row(row):
        return None
    full_month = hours if hours and hours > 0 else HOURS_PER_MONTH
    ut = (clean_text(row.get("__usageType")) or "").upper()
    prod = clean_text(row.get("source_product"))
    qty = to_number(row.get("usage_quantity"), 0)
    is_software = ("-AW-SW" in ut) or ("(software)" in prod.lower())
    # Software lines are BYOL on OCI Secure Desktops - no added charge.
    if is_software or "-AW-HW" not in ut:
        return ([{
            "sku": WS_DESKTOP_SKU, "description": "OCI Secure Desktops - bundled software (BYOL)",
            "quantity": round(qty, 4), "unit": "line", "rate": 0.0, "monthly": 0.0,
            "mapping": "WorkSpaces bundled software (Office/utilities) is BYOL on OCI "
                       "Secure Desktops - no added charge.",
            "ociServiceUsage": True,
        }], WS_PRODUCT, WS_CATEGORY, WS_DESKTOP_SKU, False)
    vcpu, mem, boot = _parse_ws_bundle(prod)
    ocpu = max(1, math.ceil(vcpu / 2)) if vcpu else 2
    if not mem:
        mem = float(ocpu)
    if ocpu > mem:            # OCPU can never exceed RAM GB
        ocpu = int(mem)
    is_autostop_usage = "AUTOSTOP-USAGE" in ut
    is_autostop_user = "AUTOSTOP-USER" in ut

    # AutoStop hourly-usage line: qty = aggregate desktop-hours actually run. Price ONLY
    # the underlying E6 compute at those real running hours (the desktop fee and boot
    # volume are billed monthly on the matching -AutoStop-User line, below).
    if is_autostop_usage:
        run_hours = qty
        return ([{
            "sku": WS_OCPU_SKU, "description": f"OCI Secure Desktops - E6 Compute OCPU ({ocpu} OCPU, running hrs)",
            "quantity": round(ocpu * run_hours, 4), "unit": "OCPU-hour", "rate": WS_OCPU_RATE,
            "monthly": money(ocpu * run_hours * WS_OCPU_RATE),
            "mapping": f"AutoStop desktops: {ocpu} OCPU × {run_hours:,.0f} actual running hrs (from the bill).",
            "ociServiceUsage": True,
        }, {
            "sku": WS_MEM_SKU, "description": f"OCI Secure Desktops - E6 Compute Memory ({mem:g} GB, running hrs)",
            "quantity": round(mem * run_hours, 4), "unit": "GB-hour", "rate": WS_MEM_RATE,
            "monthly": money(mem * run_hours * WS_MEM_RATE),
            "mapping": f"AutoStop desktops: {mem:g} GB × {run_hours:,.0f} actual running hrs (from the bill).",
            "ociServiceUsage": True,
        }], WS_PRODUCT, WS_CATEGORY, WS_OCPU_SKU, False)

    # Desktop provisioning line: qty = provisioned desktops (fractional = partial month).
    # Always bill the $20 desktop fee and the boot volume per desktop-month.
    n = qty
    items = [{
        "sku": WS_DESKTOP_SKU, "description": "OCI Secure Desktops - Secure Desktop",
        "quantity": round(n, 4), "unit": "desktop per month", "rate": WS_DESKTOP_RATE,
        "monthly": money(n * WS_DESKTOP_RATE),
        "mapping": f"AWS WorkSpaces desktop → OCI Secure Desktop at ${WS_DESKTOP_RATE}/desktop-mo (B95518).",
        "ociServiceUsage": True,
    }, {
        "sku": WS_BLOCK_SKU, "description": f"OCI Secure Desktops - Boot Volume ({boot:g} GB)",
        "quantity": round(boot * n, 4), "unit": "GB per month", "rate": WS_BLOCK_RATE,
        "monthly": money(boot * n * WS_BLOCK_RATE),
        "mapping": f"Boot volume: {boot:g} GB (root+user) × {n:g} desktops.",
        "ociServiceUsage": True,
    }, {
        "sku": WS_VPU_SKU, "description": "OCI Secure Desktops - Boot Volume Performance Units",
        "quantity": round(boot * WS_BOOT_VPU * n, 4), "unit": "Performance Units per month", "rate": WS_VPU_RATE,
        "monthly": money(boot * WS_BOOT_VPU * n * WS_VPU_RATE),
        "mapping": f"Boot volume performance ({WS_BOOT_VPU} VPU/GB) × {boot:g} GB × {n:g} desktops.",
        "ociServiceUsage": True,
    }]
    # AlwaysOn / monthly desktops run the full month, so their compute is billed here.
    # AutoStop -User lines add NO compute here - it comes from the -AutoStop-Usage line
    # at the real running hours.
    if not is_autostop_user:
        items.insert(1, {
            "sku": WS_OCPU_SKU, "description": f"OCI Secure Desktops - E6 Compute OCPU ({ocpu} OCPU)",
            "quantity": round(ocpu * n * full_month, 4), "unit": "OCPU-hour", "rate": WS_OCPU_RATE,
            "monthly": money(ocpu * n * full_month * WS_OCPU_RATE),
            "mapping": f"Always-on E6 compute: {ocpu} OCPU (from {vcpu or ocpu * 2} vCPU) × {n:g} desktops × {full_month:,.0f} hrs.",
            "ociServiceUsage": True,
        })
        items.insert(2, {
            "sku": WS_MEM_SKU, "description": f"OCI Secure Desktops - E6 Compute Memory ({mem:g} GB)",
            "quantity": round(mem * n * full_month, 4), "unit": "GB-hour", "rate": WS_MEM_RATE,
            "monthly": money(mem * n * full_month * WS_MEM_RATE),
            "mapping": f"Always-on E6 memory: {mem:g} GB × {n:g} desktops × {full_month:,.0f} hrs.",
            "ociServiceUsage": True,
        })
    return (items, WS_PRODUCT, WS_CATEGORY, WS_DESKTOP_SKU, False)


# ---- AWS AppStream 2.0 -> OCI Secure Desktops -------------------------------
# AppStream is Windows app/desktop streaming - the same Secure Desktops target as
# WorkSpaces. AWS bills it as fleet streaming hours + per-user RDS-CAL license +
# image-builder hours + stopped-fleet hours. Map: user seats -> Secure Desktop fee,
# running/streaming hours -> underlying E6 compute sized from the fleet instance type,
# stopped hours -> $0 (no running compute on OCI).
def _is_appstream_row(row):
    blob = normalize(" ".join(clean_text(row.get(k)) for k in
                              ["source_service", "source_product", "__usageType"]))
    return "appstream" in blob


def _appstream_specs(text):
    """OCPU / memory (GB) for an AppStream fleet instance from its size keyword
    (stream.standard/compute.small|medium|large|xlarge|2xlarge). Defaults to 2 OCPU / 8 GB."""
    t = clean_text(text).lower()
    if "2xlarge" in t or "2xl" in t:
        v, m = 8, 32
    elif "xlarge" in t or "xl" in t:
        v, m = 4, 16
    elif "large" in t:
        v, m = 2, 8
    else:                                    # small / medium / unknown
        v, m = 2, 4
    if "compute" in t or "memory" in t:      # compute/memory-optimized fleets carry more RAM
        m = max(m, v * 4)
    return max(1, math.ceil(v / 2)), float(m)


def price_appstream_row(row, hours=None):
    """Price an AWS AppStream 2.0 bill row on OCI Secure Desktops (same target as WorkSpaces).
    Returns (line_items, label, category, sku, carried) or None."""
    if not _is_appstream_row(row):
        return None
    prod = clean_text(row.get("source_product"))
    qty = to_number(row.get("usage_quantity"), 0)
    p = prod.lower()
    # Per-user seat (RDS-CAL license / "per user per month") -> Secure Desktop fee.
    if "per user" in p or "rds-cal" in p or "user per month" in p:
        return ([{
            "sku": WS_DESKTOP_SKU, "description": "OCI Secure Desktops - Secure Desktop (AppStream user)",
            "quantity": round(qty, 4), "unit": "desktop per month", "rate": WS_DESKTOP_RATE,
            "monthly": money(qty * WS_DESKTOP_RATE),
            "mapping": f"AWS AppStream user seat -> OCI Secure Desktop at ${WS_DESKTOP_RATE}/desktop-mo (B95518).",
            "ociServiceUsage": True,
        }], WS_PRODUCT, WS_CATEGORY, WS_DESKTOP_SKU, False)
    # Stopped fleet -> no running compute on OCI Secure Desktops.
    if "stopped" in p:
        return ([{
            "sku": WS_OCPU_SKU, "description": "OCI Secure Desktops - stopped fleet (no running compute)",
            "quantity": 0, "unit": "OCPU-hour", "rate": WS_OCPU_RATE, "monthly": 0.0,
            "mapping": "AppStream stopped fleet: no running compute charge on OCI Secure Desktops.",
            "ociServiceUsage": True,
        }], WS_PRODUCT, WS_CATEGORY, WS_OCPU_SKU, False)
    # Running fleet / image-builder streaming hours -> E6 compute at the fleet size x hours.
    ocpu, mem = _appstream_specs(prod)
    run = qty
    return ([{
        "sku": WS_OCPU_SKU, "description": f"OCI Secure Desktops - E6 Compute OCPU ({ocpu} OCPU, streaming hrs)",
        "quantity": round(ocpu * run, 4), "unit": "OCPU-hour", "rate": WS_OCPU_RATE,
        "monthly": money(ocpu * run * WS_OCPU_RATE),
        "mapping": f"AWS AppStream fleet streaming: {ocpu} OCPU × {run:,.0f} hrs (from the bill).",
        "ociServiceUsage": True,
    }, {
        "sku": WS_MEM_SKU, "description": f"OCI Secure Desktops - E6 Compute Memory ({mem:g} GB, streaming hrs)",
        "quantity": round(mem * run, 4), "unit": "GB-hour", "rate": WS_MEM_RATE,
        "monthly": money(mem * run * WS_MEM_RATE),
        "mapping": f"AWS AppStream fleet streaming: {mem:g} GB × {run:,.0f} hrs (from the bill).",
        "ociServiceUsage": True,
    }], WS_PRODUCT, WS_CATEGORY, WS_OCPU_SKU, False)


# ---- Azure usage-details resolvers -----------------------------------------
# Azure VM families carry a fixed RAM-per-vCPU ratio, so the VM size token
# (E16as v5, D8as v5, F8s v2 ...) fully determines vCPU and RAM. Suffix 'a' = AMD.
AZURE_VM_RAM_PER_VCPU = {
    "b": 4, "d": 4, "e": 8, "f": 2, "l": 8, "m": 14, "a": 2, "h": 4, "g": 15, "n": 6,
}
# Azure managed-disk / snapshot tier number -> provisioned GB (P/S/E share one ladder).
AZURE_DISK_TIER_GB = {
    1: 4, 2: 8, 3: 16, 4: 32, 6: 64, 10: 128, 15: 256, 20: 512, 30: 1024,
    40: 2048, 50: 4096, 60: 8192, 70: 16384, 80: 32768,
}


def azure_vm_specs(*texts):
    """Resolve an Azure VM size to (vcpu, ram_gb, vendor).
    Reads the size token from MeterName / AdditionalInfo ServiceType, e.g.
    'E16as v5', 'Standard_D8as_v5', 'E8-4as v5'. Returns None if not a VM size."""
    blob = " ".join(clean_text(t) for t in texts if t)
    if not blob:
        return None
    norm = blob.replace("Standard_", " ").replace("_", " ")
    # family(1-2 letters) + size digits + optional -constrained + feature letters + vN
    m = re.search(r"\b([A-Za-z]{1,2})(\d+)(?:-(\d+))?([a-z]*)\s*v?(\d+)?\b", norm)
    if not m:
        return None
    family = m.group(1).lower()
    size = int(m.group(2))
    suffix = (m.group(4) or "").lower()
    if size <= 0 or size > 512:
        return None
    ratio = AZURE_VM_RAM_PER_VCPU.get(family[0], 4)
    vcpu = size
    ram = size * ratio
    vendor = "amd" if "a" in suffix else "intel"
    return (vcpu, float(ram), vendor)


def azure_disk_gb(*texts):
    """Provisioned GB for one Azure managed disk / snapshot from its tier code
    (P10/S15/E20 -> 128/256/512). Returns 0 if no tier code is present."""
    blob = clean_text(" ".join(clean_text(t) for t in texts if t))
    m = re.search(r"\b([PSE])(\d+)\b", blob)
    if not m:
        return 0
    return AZURE_DISK_TIER_GB.get(int(m.group(2)), 0)


def price_azure_vm_license_row(row):
    """Azure 'Virtual Machines Licenses' lines are SQL Server / Windows licenses, NOT
    compute. Left alone they match the generic Compute catalog item and get a bogus
    inferred OCPU line. Map them the way the reference does: SQL Server -> Microsoft
    SQL Server license-included (Standard $0.37 / Enterprise $1.47 per OCPU-hr, Express
    $0); Windows -> BYOL ($0). Returns (items, label, category, sku, carried) or None."""
    if "virtual machines licenses" not in normalize(row.get("__meterCategory")):
        return None
    blob = normalize(" ".join(clean_text(row.get(k)) for k in
                              ["source_product", "__meterName", "__meterSub", "__azureInfo"]))
    qty = to_number(row.get("usage_quantity"), 0)
    if "sql" in blob:
        rate = sql_license_rate(blob)  # express -> 0, enterprise -> 1.47, else 0.37
        edition = ("Express" if rate == 0 else
                   "Enterprise" if rate == OCI_SQL_LICENSE_ENT_RATE else "Standard")
        ocpu_hr = qty / 2.0            # Azure license vCPU-hours -> OCPU-hours (2 vCPU = 1 OCPU)
        sku = "B91372" if rate == OCI_SQL_LICENSE_ENT_RATE else "B91373"
        return ([{
            "sku": sku,
            "description": f"Microsoft SQL Server {edition} license-included",
            "quantity": round(ocpu_hr, 4), "unit": "OCPU-hour", "rate": rate,
            "monthly": money(ocpu_hr * rate),
            "mapping": f"Azure SQL Server {edition} license re-priced on OCI: {qty:g} vCPU-hr / 2 "
                       f"= {ocpu_hr:g} OCPU-hr x ${rate}/OCPU-hr. Express edition is $0. {SQL_NO_MANAGED_NOTE}",
            "ociServiceUsage": True,
        }], "Microsoft SQL Server License", "Database", sku, False)
    # Windows (or other) VM license -> BYOL on OCI, no added charge.
    return ([{
        "sku": "", "description": "Windows Server license (BYOL on OCI)",
        "quantity": round(qty, 4), "unit": "hour", "rate": 0.0, "monthly": 0.0,
        "mapping": "Azure Windows Server VM license is BYOL on OCI - no added license charge.",
        "ociServiceUsage": True,
    }], "Windows Server License", "Licensing", "", False)


def remap_snapshot_storage(row):
    """Snapshots / volume backups are backup data that lives in object storage, not an
    attachable block volume. Route the STORAGE (capacity) lines to OCI Object Storage
    (Standard, $0.0255/GB, no performance units), or Archive ($0.0026/GB) when the tier
    is explicitly cold (EBS Snapshot Archive, Azure archive tier, Glacier/Coldline).
    Snapshot API/list/copy meters are left as-is (handled as requests, not capacity)."""
    blob = normalize(" ".join(clean_text(row.get(k)) for k in
                              ["__usageType", "__meterName", "source_product", "source_service", "__meterSub"]))
    if "snapshot" not in blob and "backup" not in blob:
        return
    # API / list / copy / operation meters are request counts, not stored GB.
    if any(k in blob for k in ["list", "copy", "operation", "transaction", "apicall",
                               "api call", "directapi", "request"]):
        return
    cold = any(k in blob for k in ["archive", "cold", "glacier", "deep archive", "coldline"])
    row["oci_product"] = "OCI Archive Storage" if cold else "OCI Object Storage"
    row["oci_service_category"] = "Storage"


def price_azure_storage_ops_row(row):
    """Azure disk/blob operation (transaction) meters bill in '10K' units. Left alone
    they map to Block Volume and get mis-priced as GB. Managed-disk I/O is bundled into
    OCI Block Volume performance units (no charge); blob/object operations map to OCI
    Object Requests (B91627, $0.0034 / 10,000). Returns (items, label, cat, sku, carried)
    or None."""
    meter = normalize(row.get("__meterName"))
    if "operation" not in meter and "transaction" not in meter:
        return None
    if not (clean_text(row.get("__meterCategory")) or clean_text(row.get("__consumedService"))):
        return None
    qty = to_number(row.get("usage_quantity"), 0)  # in 10,000-operation units
    blob = normalize(" ".join(clean_text(row.get(k)) for k in
                              ["__meterName", "source_product", "__meterSub"]))
    if "disk" in blob:
        return ([{
            "sku": "", "description": "Block Volume I/O (included on OCI)",
            "quantity": round(qty, 4), "unit": "10,000 operations", "rate": 0.0, "monthly": 0.0,
            "mapping": "Azure managed-disk operations are bundled into OCI Block Volume "
                       "performance units - no per-operation charge.",
            "ociServiceUsage": True,
        }], "OCI Block Volumes", "Storage", "", False)
    rate = 0.0034
    return ([{
        "sku": "B91627", "description": "OCI Object Storage - Requests (per 10K)",
        "quantity": round(qty, 4), "unit": "10,000 requests", "rate": rate, "monthly": money(qty * rate),
        "mapping": "Azure storage operations re-priced on OCI Object Requests at $0.0034 per 10,000.",
        "ociServiceUsage": True,
    }], "OCI Object Storage", "Storage", "B91627", False)


def normalize_azure_storage_units(row):
    """Azure managed-disk lines bill in disk-months; convert to provisioned GB
    (disk-months x tier GB, e.g. 29 P10 disks x 128 GB) so the OCI Block Volume
    pricer sees the real capacity. Snapshots already bill in GB, and disk-operation
    lines are request counts - both are left untouched."""
    meter = clean_text(row.get("__meterName"))
    if not meter:
        return
    low = meter.lower()
    if "operation" in low or "snapshot" in low or "transaction" in low or "disk" not in low:
        return
    gb = azure_disk_gb(meter)
    qty = to_number(row.get("usage_quantity"), 0)
    if gb <= 0 or qty <= 0:
        return
    row["usage_quantity"] = compact_number(qty * gb)
    row["usage_unit"] = "GB"
    row["__azureDiskGb"] = gb


def is_azure_vm_row(row):
    """True for an Azure Virtual Machine compute-hour line (not a disk/license/network)."""
    cat = normalize(row.get("__meterCategory"))
    consumed = normalize(row.get("__consumedService"))
    info = clean_text(row.get("__azureInfo"))
    if "virtual machines licenses" in cat:
        return False
    if cat == "virtual machines" or "computehr" in normalize(info):
        return True
    return "microsoft.compute" in consumed and normalize(row.get("usage_unit")) in ("1hour", "hour", "hours")


def enrich_cloud_bill_resource_fields(row):
    # Azure usage export: VM size comes from MeterName / AdditionalInfo, not a spec
    # column. Size compute from the Azure family ratio (vCPU -> OCPU, RAM by family);
    # every OTHER Azure line (storage, network, license, snapshot) is not OCI compute,
    # so clear its specs - otherwise the meter-inference fallback invents bogus OCPU/RAM.
    if clean_text(row.get("__meterCategory")) or clean_text(row.get("__consumedService")):
        if is_azure_vm_row(row):
            specs = azure_vm_specs(row.get("__meterName"), row.get("__azureInfo"),
                                   row.get("source_product"))
            if specs:
                vcpu, ram, _vendor = specs
                row["resource_ocpus"] = compact_number(max(1, math.ceil(vcpu / 2)))
                row["resource_memory_gb"] = compact_number(ram)
                return
        row["resource_ocpus"] = ""
        row["resource_memory_gb"] = ""
        return

    # Detailed bills carry a usageType - use it to decide what is real compute.
    # Only EC2 instance-run lines (BoxUsage/SpotUsage/DedicatedUsage) are OCI compute;
    # everything else (EBS-optimized surcharges, CPU credits, RDS instance hours, etc.)
    # must not be priced as compute. OCPU/memory come from the instance's REAL specs
    # in the shape data repository (cloud_shape_map) - never inferred from the meter.
    inst = extract_aws_instance_type(row)
    specs = bill_instance_compute_specs(row.get("__usageType")) or (bill_instance_compute_specs(inst) if inst else None)
    ut = normalize(row.get("__usageType"))
    svc = normalize(row.get("source_service"))

    if ut:
        # Detailed bill: compute only for EC2 instance-running lines, sized from the repo.
        is_ec2_run = "ec2" in svc and any(k in ut for k in ["boxusage", "spotusage", "dedicatedusage", "reservedhostusage", "heavyusage"])
        if is_ec2_run and specs:
            row["resource_ocpus"] = compact_number(specs[0])
            row["resource_memory_gb"] = compact_number(specs[1])
        else:
            row["resource_ocpus"] = ""
            row["resource_memory_gb"] = ""
        return

    # Any other cloud row that names a known instance type: use the repo's real specs.
    if specs:
        row["resource_ocpus"] = compact_number(specs[0])
        row["resource_memory_gb"] = compact_number(specs[1])
        return

    # Last resort ONLY when no instance type is identifiable anywhere (e.g. a bill
    # format with no instance column). Meter inference is the fallback, not the default.
    raw_context = " ".join(
        clean_text(row.get(key))
        for key in [
            "source_provider",
            "source_service",
            "source_product",
            "usage_unit",
            "source_tags",
            "oci_service_category",
            "oci_product",
        ]
    )
    context = normalize(raw_context)
    quantity = row.get("usage_quantity")
    unit_context = normalize(f"{row.get('usage_unit')} {raw_context}")
    usage_unit_only = normalize(row.get("usage_unit"))
    if re.fullmatch(r"(mb|mib|gb|gib|tb|tib)", usage_unit_only):
        return

    if not to_number(row.get("resource_ocpus"), 0):
        if context_has_any(context, ["ocpu", "ocpu per hour", "ocpu hour"]):
            inferred = meter_capacity_quantity(quantity, unit_context)
            if inferred:
                row["resource_ocpus"] = compact_number(inferred)
        elif context_has_any(context, ["vcpu", "v cpu", "cpu hour", "cpu per hour", "core hour", "core per hour"]):
            inferred = meter_capacity_quantity(quantity, unit_context, is_vcpu=True)
            if inferred:
                row["resource_ocpus"] = compact_number(inferred)

    if not to_number(row.get("resource_memory_gb"), 0) and context_has_any(
        context,
        ["memory gb", "memory per hour", "gb per hour", "gb hour memory", "ram gb", "ram per hour"],
    ):
        inferred = meter_capacity_quantity(quantity, unit_context)
        if inferred:
            row["resource_memory_gb"] = compact_number(inferred)

    inferred_ocpus, inferred_memory_gb = infer_instance_shape_resources(
        clean_text(raw_context).lower(),
        quantity,
        row.get("usage_unit"),
    )
    if inferred_ocpus and not to_number(row.get("resource_ocpus"), 0):
        row["resource_ocpus"] = compact_number(inferred_ocpus)
    if inferred_memory_gb and not to_number(row.get("resource_memory_gb"), 0):
        row["resource_memory_gb"] = compact_number(inferred_memory_gb)


def detect_tag_columns(headers):
    tag_columns = []
    for idx, header in enumerate(headers):
        text = normalize(header)
        if "tag" in text or "label" in text:
            tag_columns.append((idx, header))
    return tag_columns


def summarize_source_tags(values, tag_columns, existing=""):
    parts = []
    if clean_text(existing):
        parts.append(clean_text(existing))
    for col_idx, header in tag_columns:
        if col_idx >= len(values):
            continue
        value = clean_text(values[col_idx])
        if not value:
            continue
        label = clean_text(header)
        parts.append(f"{label}={value}")
    return "; ".join(dict.fromkeys(parts))


def cloud_row_has_signal(row):
    return bool(
        clean_text(row.get("source_service"))
        or clean_text(row.get("source_product"))
        or clean_text(row.get("source_monthly_cost"))
        or clean_text(row.get("usage_quantity"))
        or clean_text(row.get("resource_ocpus"))
        or clean_text(row.get("resource_memory_gb"))
    )


def row_mapping_is_confident(row):
    return bool(clean_text(row.get("oci_product")) and "needs review" not in normalize(row.get("mapping_confidence")))


def row_maps_to_storage_meter(row):
    target = normalize(
        " ".join(
            [
                clean_text(row.get("oci_service_category")),
                clean_text(row.get("oci_product")),
                clean_text(row.get("source_service")),
                clean_text(row.get("source_product")),
                clean_text(row.get("usage_unit")),
            ]
        )
    )
    storage_terms = [
        "storage",
        "block volume",
        "volume storage",
        "object storage",
        "archive storage",
        "file storage",
        "managed disk",
        "persistent disk",
        "cold hdd",
        "snapshot",
        "gb month",
        "gb mo",
        "bytehrs",
    ]
    return context_has_any(target, storage_terms) and not context_has_any(target, ["memory", "ram"])


def clear_resource_fields_for_storage(row):
    if not row_maps_to_storage_meter(row):
        return
    row["resource_ocpus"] = ""
    row["resource_memory_gb"] = ""


def _load_service_mapping():
    path = Path(__file__).resolve().parent / "data" / "service_mapping.json"
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


SERVICE_MAPPING = _load_service_mapping()


def _load_service_comp_list():
    """Reference AWS->OCI->group lookup (extracted from the reference's Service
    Comp List). Used as a fallback so recognized AWS services get an OCI target +
    group (and a free flag) instead of being left as 'Needs review'."""
    path = Path(__file__).resolve().parent / "data" / "service_comp_list.json"
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {}, []
    by_norm = {}
    for e in data.get("entries", []):
        key = e.get("awsNorm")
        if key and key not in by_norm:
            by_norm[key] = e
    keys_by_len = sorted(by_norm.keys(), key=len, reverse=True)
    return by_norm, keys_by_len


SERVICE_COMP_BY_NORM, SERVICE_COMP_KEYS = _load_service_comp_list()


def ref_service_lookup(*texts):
    """Match AWS source text against the reference Service Comp List.
    Exact normalized match first, then longest known service name that is a
    substring (also tolerant of space-removed concatenated bill names like
    'AmazonRDS'). Returns the entry dict or None."""
    norm = normalize(" ".join(clean_text(t) for t in texts if t))
    if not norm:
        return None
    hit = SERVICE_COMP_BY_NORM.get(norm)
    if hit:
        return hit
    nospace = norm.replace(" ", "")
    for key in SERVICE_COMP_KEYS:
        if len(key) < 4:
            continue
        if key in norm or key.replace(" ", "") in nospace:
            return SERVICE_COMP_BY_NORM[key]
    return None


def _load_oci_service_prices():
    path = Path(__file__).resolve().parent / "data" / "oci_service_prices.json"
    try:
        return json.loads(path.read_text()).get("services", {})
    except Exception:
        return {}


OCI_SERVICE_PRICES = _load_oci_service_prices()


# OCI Outbound Data Transfer: each origin region group gets its own 10 TB/month free
# allowance, then a per-group over-10TB rate (from the OCI price list).
DT_REGION_RATES = {
    "na_eu_uk": {"rate": 0.0085, "label": "North America / Europe / UK"},
    "apac_sa": {"rate": 0.025, "label": "APAC / Japan / South America"},
    "me_africa": {"rate": 0.05, "label": "Middle East / Africa"},
}
DT_FREE_GB_PER_REGION = 10000.0


def dt_region_group(region, usagetype=""):
    """Classify a source region (or AWS usageType prefix like 'APS2-') into one of the
    three OCI outbound-data-transfer pricing groups."""
    code = normalize(region)
    if not code:
        match = re.match(r"([a-z]{2})", normalize(usagetype))
        code = match.group(1) if match else ""
    head = code[:2]
    if head in ("ap", "sa") or "japan" in code:
        return "apac_sa"
    if head in ("me", "af"):
        return "me_africa"
    return "na_eu_uk"  # us, ca, eu, uk and default


def block_perf_units_per_gb(*texts):
    """OCI Block Volume performance units per GB, by source volume type:
    gp3 -> 20, gp2 -> 15, everything else -> 10 (default Balanced). Snapshots /
    backups carry no provisioned performance, so they get 0 VPU (capacity only)."""
    blob = normalize(" ".join(clean_text(t) for t in texts if t))
    if "snapshot" in blob or "backup" in blob:
        return 0
    if "gp3" in blob:
        return 20
    if "gp2" in blob:
        return 15
    return BLOCK_PERFORMANCE_UNITS_PER_GB


def oci_service_usage_items(oci_product, usage_quantity, transfer_pools=None, region="", usagetype="", perf_units_per_gb=None):
    """Re-price a source-cloud usage quantity on the equivalent OCI service
    (same quantity x OCI rate). Returns a list of priced line items, or [].

    transfer_pools is a dict {region_group: [remaining_free_gb]} for OCI Outbound
    Data Transfer, giving each origin region its own 10 TB/month free allowance and
    per-region over-10TB rate. perf_units_per_gb sets Block Volume performance
    units per GB (gp3=20, gp2=15, else 10)."""
    perf_per_gb = perf_units_per_gb if perf_units_per_gb is not None else BLOCK_PERFORMANCE_UNITS_PER_GB
    base = clean_text(oci_product).split(" (approximate")[0].strip()
    svc = OCI_SERVICE_PRICES.get(base)
    qty = to_number(usage_quantity, 0)
    # WAF / Logging / DNS are priced by their dedicated handlers (pools / per-query),
    # never by this generic per-unit pricer.
    if svc and svc.get("basis") in ("waf", "logging", "dns", "reference"):
        return []
    if not svc or qty <= 0 or not svc.get("rate"):
        return []
    chargeable = qty
    rate = svc["rate"]
    free_note = ""
    if svc.get("basis") == "transfer" and transfer_pools is not None:
        group = dt_region_group(region, usagetype)
        rate = DT_REGION_RATES[group]["rate"]
        pool = transfer_pools.setdefault(group, [DT_FREE_GB_PER_REGION])
        free_here = min(pool[0], qty)
        pool[0] -= free_here
        chargeable = qty - free_here
        free_note = f" {DT_REGION_RATES[group]['label']}: first 10 TB/mo free" + (f" ({free_here:,.0f} GB free here)" if free_here > 0 else "") + f"; over-10TB ${rate}/GB."
    items = [{
        "sku": svc.get("sku", ""),
        "description": f"{base} ({svc.get('unit', '')})",
        "quantity": round(qty, 4),
        "unit": svc.get("unit", ""),
        "rate": rate,
        "monthly": money(chargeable * rate),
        "mapping": f"Source usage re-priced on {base} at the OCI rate (same quantity x ${rate}/{svc.get('unit', 'unit')}).{free_note}",
        "ociServiceUsage": True,
    }]
    # Block Volume also carries performance units (per-GB by volume type) like the BOM.
    if svc.get("perfUnitsRate"):
        perf_qty = qty * perf_per_gb
        items.append({
            "sku": svc.get("perfUnitsSku", ""),
            "description": f"{base} - Performance Units",
            "quantity": round(perf_qty, 4),
            "unit": "Performance Units per month",
            "rate": svc["perfUnitsRate"],
            "monthly": money(perf_qty * svc["perfUnitsRate"]),
            "mapping": f"Block Volume performance units ({perf_per_gb} per GB) at the OCI rate.",
            "ociServiceUsage": True,
        })
    return items


def _keyword_hit(kw, haystack):
    """Match a mapping keyword against a bill row's text.

    Short acronyms must match as WHOLE WORDS. A plain substring test let "emr" match inside
    "ConfigurationItemRecorded" ("It-emR-ecorded"), so 248 AWS Config lines were mapped to OCI
    Big Data Service. Longer keywords stay substring-matched so multi-word product names still
    hit inside a longer meter description."""
    if not kw:
        return False
    if len(kw) <= 4 and " " not in kw:
        return re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(kw), haystack) is not None
    return kw in haystack


# Storage tiers are the one mapping a bill's SERVICE name cannot express: every S3 line says
# "Amazon S3" whether it is Standard, Standard-IA, One Zone-IA or Glacier, and the tier appears
# only in the product description or usage type (TimedStorage-SIA-ByteHrs). These are the only
# refinements a detail match is allowed to make over a service match - keeping it to an explicit
# ladder is what stops generic detail text ("data transfer out") from overriding a named service.
STORAGE_TIER_REFINEMENTS = {
    "OCI Object Storage": (
        "OCI Infrequent Access Storage",   # Standard-IA, One Zone-IA, SIA/ZIA usage types
        "OCI Archive Storage",             # Glacier, Glacier Deep Archive, GDA usage types
    ),
}


def map_service_comparison(provider, *texts):
    """Map a source-cloud service to its OCI equivalent using Oracle's Service
    Comparison guides. provider is 'aws'/'azure'; texts are the bill's service/
    product strings. Returns {'category', 'product'} or None."""
    prov = normalize_provider_hint(provider)
    entries = SERVICE_MAPPING.get(prov)
    if not entries:
        return None
    haystack = normalize(" ".join(clean_text(t) for t in texts if t))
    if not haystack:
        return None

    def _best_match(text):
        """Longest keyword hit in `text`, not the first entry in file order.

        File order is arbitrary, so "amazon s3 standard-ia" matched whichever of the S3 rules
        happened to be listed first. Scoring by keyword length makes the most specific rule win:
        "standard-ia" (11 chars) beats "s3" (2).
        """
        if not text:
            return None
        best = None
        for entry in entries:
            for kw in entry.get("keywords", []):
                nkw = normalize(kw)
                if _keyword_hit(nkw, text) and (best is None or len(nkw) > best[0]):
                    best = (len(nkw), {
                        "category": entry.get("category", ""),
                        "product": entry.get("ociProduct", ""),
                        "note": entry.get("note", ""),
                    })
        return best

    # The SERVICE name is authoritative for WHICH service, and it stays that way - matching the
    # joined text let "regional data transfer - in/out/between EC2 AZs" hit the EC2 rule, and
    # letting generic detail text win flips AmazonCloudFront onto plain egress.
    #
    # The one thing the service name CANNOT carry is the storage TIER: an S3 line is always
    # service "Amazon S3", with Standard / Standard-IA / Glacier living only in the product or
    # usage-type detail. Mapping on the service alone priced every S3 line as Standard
    # ($0.0255/GB) instead of Infrequent Access ($0.0100) or Archive ($0.0026).
    #
    # So the override is deliberately narrow: a detail match may refine a service match only
    # along the tier ladder declared below. Everything else keeps the service name's answer.
    service_text = normalize(clean_text(texts[0])) if texts else ""
    detail_text = normalize(" ".join(clean_text(t) for t in texts[1:] if t))
    svc_best = _best_match(service_text)
    detail_best = _best_match(detail_text)
    if svc_best and detail_best:
        tiers = STORAGE_TIER_REFINEMENTS.get(svc_best[1]["product"]) or ()
        if detail_best[1]["product"] in tiers:
            return detail_best[1]
    if svc_best:
        return svc_best[1]
    if detail_best:
        return detail_best[1]
    full_best = _best_match(haystack)
    return full_best[1] if full_best else None


def seed_cloud_bill_mapping(row, fields, rate_card):
    # Oracle Service Comparison guide is authoritative for OCI service identity, so it
    # sets the displayed OCI Service/Product even when a priceable catalog item also
    # matches (e.g. EFS -> File Storage, Redshift -> Autonomous Lakehouse). Pricing
    # line items are computed separately, so this only relabels the mapping.
    svc = map_service_comparison(
        row.get("source_provider"),
        row.get("source_service"),
        row.get("source_product"),
        row.get("__usageType"),
    )

    # Approximate maps carry a disclaimer (e.g. AWS FSx, AWS WorkSpaces).
    svc_note = (svc or {}).get("note")
    svc_product = svc.get("product") if svc else ""
    if svc_product and svc_note:
        svc_product = f"{svc_product} (approximate match)"
    svc_conf = ("Service guide - approximate" if svc_note else "Service guide") if svc else ""

    # Reference Service Comp List as a higher-priority target than the generic
    # catalog fallback (so e.g. RDS -> DBaaS instead of "Block Volume Storage").
    _ref = ref_service_lookup(row.get("source_service"), row.get("source_product"), row.get("__usageType"))
    # A reference entry flagged free ("OCI doesn't charge for this") still names the right OCI
    # service, and that NAME must be kept. Discarding it let the generic price-list guess win
    # instead - AWS Config, whose reference target is Cloud Guard / Security Zones, was being
    # labelled "OCI Big Data Service" on 248 lines. Free affects PRICE, not identity.
    ref_product = ((_ref.get("ociEquivalent") or "").lstrip("*").strip() if _ref else "")
    ref_group = (_ref.get("group", "") if _ref else "")
    # For non-infra services (Database, Networking, Security, DevOps, etc.) the
    # reference target wins over a generic storage/compute catalog guess so e.g.
    # RDS -> DBaaS, not "Block Volume Storage". Storage/Compute ref matches stay
    # low priority so working storage/compute pricing labels aren't disturbed.
    noninfra_product = ref_product if (ref_product and ref_group not in ("Storage", "Compute")) else ""

    item, confidence = classify_full_service_item(row, fields)
    if item:
        row["oci_service_category"] = (svc["category"] if svc else "") or (ref_group if noninfra_product else "") or row.get("oci_service_category") or ref_group or item.get("category", "")
        row["oci_product"] = svc_product or noninfra_product or row.get("oci_product") or ref_product or item.get("description", "")
        if svc_note:
            row["mapping_note"] = svc_note
        if not clean_text(row.get("mapping_confidence")):
            row["mapping_confidence"] = svc_conf if svc else f"{round(confidence * 100)}%"
        clear_resource_fields_for_storage(row)
        return

    if svc and svc_product:
        row["oci_service_category"] = svc["category"]
        row["oci_product"] = svc_product
        if svc_note:
            row["mapping_note"] = svc_note
        row["mapping_confidence"] = row.get("mapping_confidence") or svc_conf
        clear_resource_fields_for_storage(row)
        return

    target = infer_oci_service_target(row, fields)
    if target:
        row["oci_service_category"] = row.get("oci_service_category") or target["category"]
        row["oci_product"] = row.get("oci_product") or target["product"]
        row["mapping_confidence"] = row.get("mapping_confidence") or confidence_label(target["confidence"], target.get("reviewRequired", True))
        clear_resource_fields_for_storage(row)
        return

    # Fallback: the reference Service Comp List recognizes far more AWS services
    # than the keyword maps above. If a recognized service still has no OCI target,
    # use the reference's OCI equivalent + product group so it stops showing as
    # "Needs review" (pricing for non-compute/non-storage stays as-is).
    if not clean_text(row.get("oci_product")):
        ref = ref_service_lookup(row.get("source_service"), row.get("source_product"), row.get("__usageType"))
        if ref and not ref.get("free"):
            row["oci_service_category"] = row.get("oci_service_category") or ref.get("group", "")
            row["oci_product"] = (ref.get("ociEquivalent") or "").lstrip("*").strip() or row.get("oci_product")
            row["mapping_confidence"] = row.get("mapping_confidence") or "Service comparison"
            return

    row["mapping_confidence"] = row.get("mapping_confidence") or "Needs review"


def catalog_items_for_keys(keys):
    requested = set(keys or [])
    return [
        {
            "key": item["key"],
            "sku": item["sku"],
            "description": item["description"],
            "unit": item["unit"],
            "category": item["category"],
            "rate": item["rate"],
        }
        for item in FULL_SERVICE_CATALOG_ITEMS
        if not requested or item["key"] in requested
    ]


def provider_mapping_context(provider):
    provider_norm = normalize(provider)
    if provider_norm in {"aws", "amazon", "amazon web services"}:
        provider_terms = {"aws", "amazon"}
    elif provider_norm in {"azure", "microsoft azure", "microsoft"}:
        provider_terms = {"azure", "microsoft"}
    elif provider_norm in {"gcp", "google", "google cloud", "google cloud platform"}:
        provider_terms = {"gcp", "google"}
    else:
        provider_terms = set()

    context = []
    for mapping in OCI_SOURCE_SERVICE_MAPPINGS:
        source_text = normalize(" ".join(mapping["sourceServices"]))
        if not provider_terms or any(term in source_text for term in provider_terms):
            context.append({**mapping, "localCatalogCandidates": catalog_items_for_keys(mapping.get("catalogKeys"))})
    return context


def cloud_bill_pattern_key(row):
    parts = [
        normalize(row.get("source_provider")),
        normalize(row.get("source_service")),
        normalize(row.get("source_product")),
        normalize(row.get("usage_unit")),
    ]
    return "|".join(parts)


def compact_cloud_bill_patterns(rows, max_patterns=140):
    grouped = {}
    for row_index, row in enumerate(rows, start=1):
        key = cloud_bill_pattern_key(row)
        if not key.strip("|"):
            continue
        pattern = grouped.setdefault(
            key,
            {
                "patternId": f"pattern-{len(grouped) + 1}",
                "rowIds": [],
                "sampleRows": [],
                "provider": clean_text(row.get("source_provider")),
                "sourceService": clean_text(row.get("source_service")),
                "sourceProduct": clean_text(row.get("source_product")),
                "usageUnit": clean_text(row.get("usage_unit")),
                "sourceRegions": [],
                "sourceAccounts": [],
                "totalUsageQuantity": 0.0,
                "totalSourceMonthlyCost": 0.0,
            },
        )
        pattern["rowIds"].append(row.get("__id"))
        if len(pattern["sampleRows"]) < 3:
            pattern["sampleRows"].append(
                {
                    "rowId": row.get("__id"),
                    "sourceRow": row.get("__sourceRow"),
                    "usageQuantity": clean_cell(row.get("usage_quantity")),
                    "sourceMonthlyCost": clean_cell(row.get("source_monthly_cost")),
                    "sourceTags": clean_text(row.get("source_tags"))[:260],
                }
            )
        if clean_text(row.get("source_region")) and clean_text(row.get("source_region")) not in pattern["sourceRegions"]:
            pattern["sourceRegions"].append(clean_text(row.get("source_region")))
        if clean_text(row.get("source_account")) and clean_text(row.get("source_account")) not in pattern["sourceAccounts"]:
            pattern["sourceAccounts"].append(clean_text(row.get("source_account")))
        pattern["totalUsageQuantity"] += to_number(row.get("usage_quantity"), 0)
        pattern["totalSourceMonthlyCost"] += to_number(row.get("source_monthly_cost"), 0)

    patterns = sorted(
        grouped.values(),
        key=lambda item: (item["totalSourceMonthlyCost"], len(item["rowIds"]), item["totalUsageQuantity"]),
        reverse=True,
    )
    compacted = []
    pattern_rows = {}
    for pattern in patterns[:max_patterns]:
        pattern["rowCount"] = len(pattern["rowIds"])
        pattern["totalUsageQuantity"] = round(pattern["totalUsageQuantity"], 4)
        pattern["totalSourceMonthlyCost"] = money(pattern["totalSourceMonthlyCost"])
        pattern["sourceRegions"] = pattern["sourceRegions"][:6]
        pattern["sourceAccounts"] = pattern["sourceAccounts"][:6]
        pattern_rows[pattern["patternId"]] = pattern["rowIds"]
        compacted.append({key: value for key, value in pattern.items() if key != "rowIds"})
    return compacted, pattern_rows, len(patterns) > len(compacted)


def sanitized_bill_patterns(patterns):
    safe_patterns = []
    for pattern in patterns:
        safe_patterns.append(
            {
                "patternId": pattern["patternId"],
                "provider": pattern.get("provider"),
                "sourceService": pattern.get("sourceService"),
                "sourceProduct": pattern.get("sourceProduct"),
                "usageUnit": pattern.get("usageUnit"),
                "rowCount": pattern.get("rowCount"),
            }
        )
    return safe_patterns


def confidence_label(confidence, review_required=False):
    if isinstance(confidence, (int, float)):
        percent = max(0, min(100, round(float(confidence) * 100 if confidence <= 1 else float(confidence))))
        return "Needs review" if review_required and percent < 60 else f"{percent}%"
    text = clean_text(confidence)
    if not text:
        return "Needs review" if review_required else ""
    if review_required and "review" not in normalize(text):
        return f"{text} - Needs review"
    return text


def parse_quantity_multiplier(multiplier):
    if multiplier in {None, ""}:
        return None
    if isinstance(multiplier, (int, float)):
        return float(multiplier)
    text = clean_text(multiplier).lower()
    try:
        return float(text)
    except ValueError:
        pass
    compact = text.replace(" ", "")
    if compact in {"1/730", "1÷730"}:
        return 1 / HOURS_PER_MONTH
    if "1024" in compact and "730" in compact and compact.startswith("1/"):
        return 1 / (1024**3) / HOURS_PER_MONTH
    if compact in {"1/1024", "1÷1024"}:
        return 1 / 1024
    return None


def apply_quantity_multiplier(row, multiplier, target_unit):
    if multiplier in {None, ""}:
        if clean_text(target_unit):
            row["usage_unit"] = clean_text(target_unit)
        return
    factor = parse_quantity_multiplier(multiplier)
    if factor is None:
        if clean_text(target_unit):
            row["usage_unit"] = clean_text(target_unit)
        return
    quantity = to_number(row.get("usage_quantity"), 0)
    if quantity > 0:
        row["usage_quantity"] = compact_number(quantity * factor)
    if clean_text(target_unit):
        row["usage_unit"] = clean_text(target_unit)


def append_mapping_rationale(row, rationale):
    text = clean_text(rationale)
    if not text:
        return
    existing = clean_text(row.get("source_tags"))
    note = f"OCI mapping: {text[:260]}"
    if note in existing:
        return
    row["source_tags"] = "; ".join(part for part in [existing, note] if part)


def apply_cloud_bill_llm_mapping(parsed, llm_payload, pattern_rows):
    if not isinstance(llm_payload, dict):
        return 0, []
    rows_by_id = {row.get("__id"): row for row in parsed.get("rows", [])}
    applied = 0
    warnings_list = [clean_text(item) for item in llm_payload.get("warnings", []) if clean_text(item)]

    for mapping in llm_payload.get("mappings", []):
        if not isinstance(mapping, dict):
            continue
        row_ids = []
        pattern_id = clean_text(mapping.get("patternId"))
        if pattern_id:
            row_ids.extend(pattern_rows.get(pattern_id, []))
        row_id = clean_text(mapping.get("rowId"))
        if row_id:
            row_ids.append(row_id)
        row_ids = list(dict.fromkeys(row_id for row_id in row_ids if row_id in rows_by_id))
        if not row_ids:
            warnings_list.append(f"Skipped OCI mapping for unknown pattern or row: {pattern_id or row_id}.")
            continue

        category = clean_text(mapping.get("ociServiceCategory") or mapping.get("oci_service_category"))
        product = clean_text(mapping.get("ociProduct") or mapping.get("oci_product"))
        target_unit = clean_text(mapping.get("targetUsageUnit") or mapping.get("usageUnit") or mapping.get("usage_unit"))
        multiplier = mapping.get("quantityMultiplier")
        confidence = confidence_label(mapping.get("confidence"), bool(mapping.get("reviewRequired")))
        rationale = clean_text(mapping.get("rationale") or mapping.get("reasoning"))

        for row_id in row_ids:
            row = rows_by_id[row_id]
            if category:
                row["oci_service_category"] = category
            if product:
                row["oci_product"] = product
            if confidence:
                row["mapping_confidence"] = confidence
            elif mapping.get("reviewRequired"):
                row["mapping_confidence"] = "Needs review"
            apply_quantity_multiplier(row, multiplier, target_unit)
            append_mapping_rationale(row, rationale)
            clear_resource_fields_for_storage(row)
            applied += 1

    return applied, warnings_list


def cloud_bill_mapping_schema():
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {"type": "string"},
            "mappings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "patternId": {"type": "string"},
                        "ociServiceCategory": {"type": "string"},
                        "ociProduct": {"type": "string"},
                        "targetUsageUnit": {"type": "string"},
                        "quantityMultiplier": {
                            "anyOf": [
                                {"type": "number"},
                                {"type": "null"},
                            ]
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                        "reviewRequired": {"type": "boolean"},
                        "rationale": {"type": "string"},
                    },
                    "required": [
                        "patternId",
                        "ociServiceCategory",
                        "ociProduct",
                        "targetUsageUnit",
                        "quantityMultiplier",
                        "confidence",
                        "reviewRequired",
                        "rationale",
                    ],
                },
            },
            "warnings": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["summary", "mappings", "warnings"],
    }


def call_llm_cloud_bill_mapping(parsed):
    rows = [
        row
        for row in (parsed.get("rows", []) or [])
        if not row_mapping_is_confident(row)
    ]
    if not rows:
        parsed.setdefault("metadata", {})["llmBillMappingNeeded"] = False
        return parsed
    metadata = parsed.get("metadata", {})
    metadata["llmBillMappingNeeded"] = True
    default_max_patterns = 20 if metadata.get("parser") == "cloud-bill-pdf" else 40
    max_patterns = int(os.environ.get("OPENAI_BILL_MAX_PATTERNS", default_max_patterns))
    patterns, pattern_rows, truncated = compact_cloud_bill_patterns(rows, max_patterns=max_patterns)
    if not patterns:
        return parsed

    provider = metadata.get("detectedProvider") or metadata.get("providerHint") or "Unknown"
    include_private_context = clean_text(os.environ.get("OPENAI_BILL_INCLUDE_PRIVATE_CONTEXT")).lower() in {"1", "true", "yes", "on"}
    system = (
        "You are an Oracle Cloud Infrastructure cloud-bill mapper. Return compact JSON only. "
        "Your primary job is to recognize source-cloud or document lines that imply compute/core usage, RAM/memory, and storage capacity or requests, then map those rows to OCI services and meters. "
        "Think through each source bill-line pattern using source provider, service, SKU/meter name, and usage unit. "
        "Map the source line to the closest OCI service and OCI price-list meter using the provided Oracle service mapping guide and metering rules. "
        "Use localCatalogCandidates when there is a trustworthy exact meter. If the local catalog does not include the needed OCI meter, still identify the OCI service/product but set reviewRequired true. "
        "Never use source-cloud cost as the OCI rate. Preserve separate meters; do not merge compute, memory, storage, request, and network rows. "
        "For vCPU/core-hour source usage mapped to OCI compute, use quantityMultiplier 0.5 and targetUsageUnit 'OCPU-hour'. "
        "For TB-month storage, use multiplier 1024 and targetUsageUnit 'GB-month'. For GB-hour storage, use multiplier 1/730 and targetUsageUnit 'GB-month'. "
        "For byte-hours object storage, use multiplier 1/(1024^3*730) and targetUsageUnit 'GB-month'. "
        "For noisy PDF invoices, map only the supplied high-impact patterns and omit anything you are not confident about. "
        "Keep the summary, rationale, and warning strings concise. "
        "Return this exact shape: {summary:string, mappings:[{patternId:string, ociServiceCategory:string, ociProduct:string, "
        "targetUsageUnit:string, quantityMultiplier:number|null, confidence:number, reviewRequired:boolean, rationale:string}], warnings:[string]}."
    )
    payload = {
        "workflowContract": LLM_WORKFLOW_CONTRACT,
        "officialReferences": OCI_OFFICIAL_REFERENCES,
        "meteringGuidance": OCI_METERING_GUIDANCE,
        "sourceServiceMappings": provider_mapping_context(provider),
        "localOciPriceCatalog": price_catalog_payload(),
        "billMetadata": {
            "detectedProvider": metadata.get("detectedProvider"),
            "providerConfidence": metadata.get("providerConfidence"),
            "patternCount": len(patterns),
            "patternsTruncated": truncated,
            "privateContextIncluded": include_private_context,
        },
        "billLinePatterns": patterns if include_private_context else sanitized_bill_patterns(patterns),
    }
    llm_payload, warning = call_openai_json(
        system,
        payload,
        max_output_tokens=5000,
        timeout=45,
        model_env="OPENAI_BILL_MODEL",
        reasoning_effort_env="OPENAI_BILL_REASONING_EFFORT",
        default_reasoning_effort="low",
        schema_name="oci_cloud_bill_mapping",
        response_schema=cloud_bill_mapping_schema(),
    )
    metadata["llmBillMappingAttempted"] = True
    metadata["llmBillPatternCount"] = len(patterns)
    metadata["llmBillPatternsTruncated"] = truncated
    if warning:
        mapped_count = sum(1 for row in parsed.get("rows", []) if row_mapping_is_confident(row))
        metadata["mappedCount"] = mapped_count
        metadata["unmappedCount"] = max(0, len(parsed.get("rows", [])) - mapped_count)
        metadata["llmBillMappingWarning"] = warning
        metadata.setdefault("extractionNotes", []).append(
            "Used deterministic OCI bill mapping because OpenAI API calls are disconnected."
            if warning == OPENAI_DISABLED_MESSAGE
            else "Used deterministic OCI bill mapping because the OpenAI bill-mapping pass did not complete."
        )
        if mapped_count == 0:
            parsed["llmWarning"] = (
                "OpenAI API calls are temporarily disabled; used deterministic bill mapping."
                if warning == OPENAI_DISABLED_MESSAGE
                else f"Cloud bill OpenAI mapping did not complete; used deterministic bill mapping. Detail: {warning}"
            )
        return parsed

    applied, warnings_list = apply_cloud_bill_llm_mapping(parsed, llm_payload, pattern_rows)
    remapped_count = sum(1 for row in parsed.get("rows", []) if row_mapping_is_confident(row))
    metadata["llmBillMappedRows"] = applied
    metadata["mappedCount"] = remapped_count
    metadata["unmappedCount"] = max(0, len(parsed.get("rows", [])) - remapped_count)
    if clean_text(llm_payload.get("summary")):
        metadata.setdefault("extractionNotes", []).append(clean_text(llm_payload.get("summary")))
    if warnings_list:
        metadata.setdefault("extractionNotes", []).extend(warnings_list[:4])
    return parsed


PDF_SERVICE_KEYWORDS = [
    "amazon simple storage service",
    "simple storage service",
    "elastic block store",
    "ebs",
    "ec2",
    "elastic compute",
    "rds",
    "glacier",
    "efs",
    "azure storage",
    "blob storage",
    "managed disk",
    "virtual machines",
    "azure files",
    "gcp",
    "google cloud",
    "cloud storage",
    "compute engine",
    "persistent disk",
    "filestore",
    "bigquery",
    "object storage",
    "block storage",
    "file storage",
    "archive",
    "requests",
    "cloudtrail",
    "cloudwatch",
    "config",
    "data transfer",
    "elastic load balancing",
    "key management service",
    "simple email service",
    "simple notification service",
    "simple queue service",
    "simpledb",
    "virtual private cloud",
    "support business",
]


def extract_pdf_text(path):
    reader = PdfReader(str(path))
    pages = []
    for index, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        if clean_text(text):
            pages.append({"page": index + 1, "text": text})
    if not pages:
        raise ValueError("No selectable text was found in the PDF. Scanned image PDFs need OCR before upload.")
    return pages


def pdf_lines(path):
    lines = []
    for page in extract_pdf_text(path):
        raw_lines = re.split(r"[\r\n]+", page["text"])
        if len(raw_lines) <= 1:
            raw_lines = re.split(r"(?<=[.])\s+(?=[A-Z0-9$])", page["text"])
        for raw_line in raw_lines:
            line = clean_text(raw_line)
            if line:
                lines.append({"page": page["page"], "text": line})
    return lines


def pdf_money_amount(line):
    text = clean_text(line)
    matches = []
    for pattern in [
        r"(?:\$|USD\s+|US\$\s*)(-?\d[\d,]*(?:\.\d{2})?)",
        r"(-?\d[\d,]*(?:\.\d{2})?)\s*(?:USD|US dollars?)\b",
    ]:
        matches.extend(re.findall(pattern, text, flags=re.IGNORECASE))
    if not matches and context_has_any(normalize(text), PDF_SERVICE_KEYWORDS):
        end_match = re.search(r"(-?\d[\d,]*\.\d{2})\s*$", text)
        if end_match:
            matches.append(end_match.group(1))
    if not matches:
        return ""
    return compact_number(to_number(matches[-1], 0))


def pdf_usage(line):
    patterns = [
        r"(-?\d[\d,]*(?:\.\d+)?)\s*(GB[-\s]?(?:mo|month)|GiB[-\s]?(?:mo|month)|TB[-\s]?(?:mo|month))\b",
        r"(-?\d[\d,]*(?:\.\d+)?)\s*(ByteHrs?|Byte[-\s]?hours?|Bytes?[-\s]?hours?)\b",
        r"(-?\d[\d,]*(?:\.\d+)?)\s*((?:vCPU|OCPU|CPU|core)[-\s]?hours?)\b",
        r"(-?\d[\d,]*(?:\.\d+)?)\s*(GB[-\s]?hours?|GiB[-\s]?hours?)\b",
        r"(-?\d[\d,]*(?:\.\d+)?)\s*(TB|TiB|GB|GiB|MB|MiB)\b",
        r"(-?\d[\d,]*(?:\.\d+)?)\s*(requests?|API requests?)\b",
        r"(-?\d[\d,]*(?:\.\d+)?)\s*(events?|messages?|IOs?|operations?|metrics?|keys?|counts?|LCU[-\s]?hrs?|LoadBalancer[-\s]?hours?)\b",
        r"(-?\d[\d,]*(?:\.\d+)?)\s*(hours?|hrs?)\b",
        r"(-?\d[\d,]*(?:\.\d+)?)(ConfigurationItemRecorded)\b",
    ]
    matches = []
    for pattern in patterns:
        for match in re.finditer(pattern, line, flags=re.IGNORECASE):
            matches.append((match.start(), match.group(1), match.group(2)))
    if matches:
        _, quantity, unit = sorted(matches, key=lambda item: item[0])[-1]
        return compact_number(to_number(quantity, 0)), clean_text(unit)
    return "", ""


def pdf_region(line):
    match = re.search(r"\b([a-z]{2,}-[a-z]+-\d+[a-z]?|[a-z]+[a-z ]+(?:us|eu|asia|uk|india|japan|korea|australia)\b)\b", line, flags=re.IGNORECASE)
    return clean_text(match.group(1)) if match else ""


def pdf_service_context_for_line(line):
    text = normalize(line)
    rules = [
        (["cloudtrail"], "AWS CloudTrail"),
        (["cloudwatch", "putlogevents"], "Amazon CloudWatch"),
        (["aws config", "configurationitemrecorded"], "AWS Config"),
        (["data transfer", "bandwidth"], "AWS Data Transfer"),
        (["elastic load balancing", "loadbalancer", "load balancer"], "Elastic Load Balancing"),
        (["elastic compute cloud", "ec2", "linux unix", "windows amazon vpc", "instance hour"], "Amazon EC2"),
        (["elastic block store", "ebs"], "EBS"),
        (["aws iot", "registryandshadowoperations"], "AWS IoT"),
        (["key management service", "kms requests", "kms keys"], "AWS Key Management Service"),
        (["simple email service"], "Amazon Simple Email Service"),
        (["simple notification service", "sns api", "sns requests"], "Amazon Simple Notification Service"),
        (["simple queue service", "sqs requests", "sqs"], "Amazon Simple Queue Service"),
        (["simple storage service", "timedstorage bytehrs", "requests tier"], "Amazon Simple Storage Service"),
        (["simpledb"], "Amazon SimpleDB"),
        (["virtual private cloud", "createvpnconnection"], "Amazon Virtual Private Cloud"),
        (["support business", "aws support"], "AWS Support"),
        (["efs", "elastic file system"], "Amazon EFS"),
        (["rds", "relational database"], "Amazon RDS"),
    ]
    for terms, service in rules:
        if context_has_any(text, terms):
            return service
    return ""


def pdf_service_for_line(line):
    service = pdf_service_context_for_line(line)
    text = normalize(line)
    if service == "Amazon Simple Storage Service" or context_has_any(text, ["object storage", "cloud storage"]):
        return "Object Storage", "Object storage usage"
    if context_has_any(text, ["glacier", "archive", "coldline"]):
        return "Archive Storage", "Archive storage usage"
    if service == "EBS" or context_has_any(text, ["elastic block store", "managed disk", "persistent disk", "block storage"]):
        return "Block Storage", "Block storage usage"
    if service == "Amazon EFS" or context_has_any(text, ["azure files", "filestore", "file storage"]):
        return "File Storage", "File storage usage"
    if service == "Amazon EC2" or context_has_any(text, ["elastic compute", "virtual machines", "compute engine", "vcpu", "ocpu", "cpu hour"]):
        return "Compute", "Compute usage"
    if context_has_term(text, "bigquery"):
        return "Analytics", "BigQuery usage"
    return service, clean_text(line)[:180] if service else ""


def pdf_has_bill_signal(line):
    text = normalize(line)
    return context_has_any(text, PDF_SERVICE_KEYWORDS)


def pdf_boilerplate_or_region(line):
    text = normalize(line)
    if not text:
        return True
    if re.fullmatch(r"\$?\d[\d,]*(?:\.\d{2})?", clean_text(line)):
        return True
    boilerplate_terms = [
        "billing management console",
        "console aws amazon com",
        "linked account can t download reports",
        "contact your payer account",
        "billing statement",
        "date printed",
        "account number",
        "payer account id",
        "details",
        "aws service charges",
        "summary usd",
    ]
    if any(term in text for term in boilerplate_terms):
        return True
    region_terms = [
        "asia pacific",
        "canada central",
        "eu frankfurt",
        "eu ireland",
        "eu london",
        "eu paris",
        "south america sao paulo",
        "us east",
        "us west",
        "any",
    ]
    return any(text == term or text.startswith(f"{term} ") for term in region_terms)


def pdf_candidate_lines(lines):
    candidates = []
    current_service = ""
    consumed = set()
    for index, item in enumerate(lines):
        if index in consumed:
            continue
        line = item["text"]
        if pdf_boilerplate_or_region(line):
            continue

        explicit_service = pdf_service_context_for_line(line)
        if explicit_service:
            current_service = explicit_service

        usage_quantity, _ = pdf_usage(line)
        has_money = pdf_money_amount(line) != ""
        has_meter_text = " per " in f" {normalize(line)} " or usage_quantity != ""

        if explicit_service:
            if not has_money and not has_meter_text:
                continue
            combined = line
        elif current_service and (has_meter_text or has_money):
            combined = f"{current_service} {line}"
        else:
            continue

        if index + 1 < len(lines):
            next_line = lines[index + 1]["text"]
            next_explicit_service = pdf_service_context_for_line(next_line)
            next_usage, _ = pdf_usage(next_line)
            next_has_detail = (" per " in f" {normalize(next_line)} " or next_usage != "") and not next_explicit_service
            if next_has_detail and not pdf_boilerplate_or_region(next_line):
                combined = f"{combined} {next_line}"
                consumed.add(index + 1)
        if not pdf_has_bill_signal(combined) and not current_service:
            continue
        candidates.append({"page": item["page"], "line": index + 1, "text": clean_text(combined)})
    return candidates


def parse_pdf_cloud_bill(path, provider_hint=PROVIDER_AUTO):
    lines = pdf_lines(path)
    detected_provider, provider_confidence = detect_cloud_provider([], [[item["text"]] for item in lines], provider_hint)
    fields = canonical_fields_payload(True, INTAKE_MODE_CLOUD_BILL)
    rows = []
    rate_card = build_rate_card(DEFAULT_SHAPE_KEY, True)
    for index, item in enumerate(pdf_candidate_lines(lines)):
        service, product = pdf_service_for_line(item["text"])
        source_cost = pdf_money_amount(item["text"])
        usage_quantity, usage_unit = pdf_usage(item["text"])
        row = {"__id": f"pdf-line-{index + 1}", "__sourceRow": f"p{item['page']} l{item['line']}", "__approved": True}
        for field in fields:
            row[field["key"]] = ""
        row["source_provider"] = detected_provider if detected_provider != "Unknown" else ""
        row["source_service"] = service or "PDF bill line"
        row["source_product"] = product or item["text"][:180]
        row["source_region"] = pdf_region(item["text"])
        row["usage_quantity"] = usage_quantity
        row["usage_unit"] = usage_unit
        row["source_monthly_cost"] = source_cost
        row["source_currency"] = "USD" if "$" in item["text"] or "usd" in normalize(item["text"]) else ""
        row["source_tags"] = f"PDF page {item['page']}, line {item['line']}: {item['text'][:220]}"
        enrich_cloud_bill_resource_fields(row)
        seed_cloud_bill_mapping(row, fields, rate_card)
        if cloud_row_has_signal(row):
            rows.append(row)

    if not rows:
        amounts = [pdf_money_amount(item["text"]) for item in lines]
        amounts = [amount for amount in amounts if amount != ""]
        row = {"__id": "pdf-summary-1", "__sourceRow": "PDF summary", "__approved": True}
        for field in fields:
            row[field["key"]] = ""
        row["source_provider"] = detected_provider if detected_provider != "Unknown" else ""
        row["source_service"] = "PDF bill summary"
        row["source_product"] = "No line-item table was detected. Review this PDF summary before pricing."
        row["source_monthly_cost"] = amounts[-1] if amounts else ""
        row["source_currency"] = "USD"
        row["mapping_confidence"] = "Needs review"
        enrich_cloud_bill_resource_fields(row)
        rows.append(row)

    mapped_count = sum(1 for row in rows if row_mapping_is_confident(row))
    currency_values = [clean_text(row.get("source_currency")) for row in rows if clean_text(row.get("source_currency"))]
    source_currency = currency_values[0] if currency_values else "USD"
    parsed = {
        "fileName": Path(path).name,
        "sheetName": "PDF bill",
        "sheets": ["PDF bill"],
        "fields": fields,
        "rows": rows,
        "rateCard": build_rate_card(DEFAULT_SHAPE_KEY, True),
        "rateCards": all_shape_payloads(True),
        "fullServiceCatalog": price_catalog_payload(),
        "selectedShape": shape_payload(DEFAULT_SHAPE_KEY, True),
        "metadata": {
            "intakeMode": INTAKE_MODE_CLOUD_BILL,
            "providerHint": normalize_provider_hint(provider_hint),
            "detectedProvider": detected_provider,
            "providerConfidence": provider_confidence,
            "parser": "cloud-bill-pdf",
            "sourceCurrency": source_currency,
            "mappedCount": mapped_count,
            "unmappedCount": len(rows) - mapped_count,
            "headerRows": [],
            "dataStartRow": 1,
            "rowCount": len(rows),
            "columnCount": len(fields),
        },
    }
    # AI is deliberately limited to inventory scrubbing and architecture planning.
    # Cloud bill mappings remain deterministic and auditable.
    return parsed


_AWS_INSTANCE_RE = re.compile(r"\b([a-z][0-9][a-z\-]*\.(?:nano|micro|small|medium|metal|[0-9]*xlarge|large))\b")


def extract_aws_instance_type(row):
    """Pull an EC2 instance type (e.g. 't2.medium', 'm5.xlarge') from a bill row's
    SKU/meter/description text, so we can look up its specs via the Price List API."""
    for key in ("source_product", "source_service"):
        text = clean_text(row.get(key)).lower()
        match = _AWS_INSTANCE_RE.search(text)
        if match:
            return match.group(1)
    blob = " ".join(clean_text(v) for v in row.values() if isinstance(v, str)).lower()
    match = _AWS_INSTANCE_RE.search(blob)
    return match.group(1) if match else None


def looks_like_inventory(raw, mappings=None, max_scan=25):
    """True when a 'cloud bill' upload actually looks like a server inventory:
    short header-like cells mention CPU/RAM/instance-type but there is no cost
    column. Scans short cells only so JSON tag blobs (e.g. 'costCenter=...') and
    descriptions don't trip the cost check, and works even if the header row was
    mis-detected."""
    inv_signals = ["vcpu", "ocpu", "cpu", "cores", "memory", "ram", "gib",
                   "instancetype", "instance type"]
    cost_signals = ["cost", "charge", "amount", "price", "unblended", "blended",
                    "lineitem", "line item", "meter", "billed", "spend", "invoice"]
    inv_hits = set()
    cost_hit = False
    try:
        n = min(len(raw.index), max_scan)
        for i in range(n):
            for cell in raw.iloc[i].tolist():
                t = normalize(cell)
                if not t or len(t) > 40:  # skip long cells (JSON tags, descriptions)
                    continue
                for sig in inv_signals:
                    if sig in t:
                        inv_hits.add(sig)
                if any(sig in t for sig in cost_signals):
                    cost_hit = True
    except Exception:
        return False
    # Note: a column mapping to cost isn't trusted here - a mis-detected header row
    # can fabricate one. The short-cell cost-signal scan is the reliable signal.
    return len(inv_hits) >= 2 and not cost_hit


def enrich_aws_skus_from_api(rows, max_lookups=400):
    """Fill the AWS product SKU for bill lines via the Price List API, keyed by
    ServiceCode (ProductCode) + usageType. De-duplicated and cached, so only distinct
    combinations hit the API. No-op when the API isn't available."""
    if not rows or not aws_pricing.available():
        return
    combos = {}
    for row in rows:
        svc = clean_text(row.get("source_service"))
        ut = clean_text(row.get("__usageType"))
        if svc and ut:
            combos.setdefault((svc, ut), None)
    looked_up = 0
    for combo in list(combos):
        if looked_up >= max_lookups:
            break
        combos[combo] = aws_pricing.service_sku(combo[0], combo[1])
        looked_up += 1
    for row in rows:
        info = combos.get((clean_text(row.get("source_service")), clean_text(row.get("__usageType"))))
        if not info or not info.get("sku"):
            continue
        row["aws_sku"] = info["sku"]
        current = clean_text(row.get("source_product"))
        if not current or current == clean_text(row.get("__usageType")):
            row["source_product"] = info["sku"]
        elif info["sku"] not in current:
            row["source_product"] = f"{info['sku']} · {current}"


def filter_bill_record_types(rows):
    """AWS detailed billing reports stack record-type levels (LineItem, PayerLineItem,
    AccountTotal, InvoiceTotal, StatementTotal, ...) that each re-total the whole bill.
    Summing all of them multiplies the cost. Keep only the most granular line-item
    level and drop rollup/total rows. No-op when there's no RecordType column."""
    types = [clean_text(r.get("__recordType")) for r in rows if clean_text(r.get("__recordType"))]
    if not types:
        return rows
    # Drop any rollup/total rows outright.
    rollup = ("total", "rounding", "invoice", "statement", "account")
    kept = [r for r in rows if not any(k in normalize(r.get("__recordType")) for k in rollup)]
    # Among the remaining line-item levels, prefer the single most granular (most rows),
    # e.g. LinkedLineItem over PayerLineItem when both are present.
    remaining = collections.Counter(normalize(r.get("__recordType")) for r in kept if clean_text(r.get("__recordType")))
    if len(remaining) > 1:
        best = max(remaining, key=lambda k: remaining[k])
        kept = [r for r in kept if not clean_text(r.get("__recordType")) or normalize(r.get("__recordType")) == best]
    return kept or rows


def parse_cloud_bill(path, provider_hint=PROVIDER_AUTO):
    suffix = Path(path).suffix.lower()
    if suffix == ".pdf":
        return parse_pdf_cloud_bill(path, provider_hint)

    candidate_tables = []
    if suffix in {".csv", ".tsv"}:
        raw = read_bill_table(path)
        header_row = detect_cloud_header_row(raw)
        headers = unique_headers(raw.iloc[header_row].tolist())
        candidate_tables.append(("Cloud bill", raw, header_row, headers))
    else:
        excel_file = pd.ExcelFile(path)
        dedicated_parsed = None
        visible_sheets = visible_sheet_names(excel_file, path)
        for sheet in visible_sheets:
            raw = read_bill_table(path, sheet)
            if dedicated_parsed is None:
                dedicated_parsed = parse_azure_service_mapping_table(path, sheet, raw, provider_hint, visible_sheets)
            header_row = detect_cloud_header_row(raw)
            headers = unique_headers(raw.iloc[header_row].tolist())
            candidate_tables.append((sheet, raw, header_row, headers))
        if dedicated_parsed:
            return dedicated_parsed

    if not candidate_tables:
        raise ValueError("No bill rows were found.")

    sheet_name, raw, header_row, headers = max(candidate_tables, key=lambda item: cloud_header_score(item[3]))
    data_start_idx = header_row + 1
    sample_values = [raw.iloc[idx].tolist() for idx in range(data_start_idx, min(len(raw.index), data_start_idx + 12))]
    detected_provider, provider_confidence = detect_cloud_provider(headers, sample_values, provider_hint)
    mappings = infer_cloud_bill_mappings(headers, detected_provider)
    fields = canonical_fields_payload(True, INTAKE_MODE_CLOUD_BILL)
    for field in fields:
        mapping = mappings.get(field["key"])
        if mapping:
            field["sourceColumn"] = mapping["sourceColumn"]
            field["sourceHeader"] = mapping["sourceHeader"]

    tag_columns = detect_tag_columns(headers)
    # Raw usageType column (used to resolve AWS SKUs via the Price List API).
    usagetype_idx = next((i for i, h in enumerate(headers) if normalize(h).replace(" ", "") == "usagetype"), None)
    # RecordType column (AWS detailed billing reports stack rollup levels that each
    # re-total the whole bill - we keep only the most granular line-item level).
    recordtype_idx = next((i for i, h in enumerate(headers) if normalize(h).replace(" ", "") == "recordtype"), None)
    # Azure usage-details exports carry the VM size, vCPU count, Windows flag, and disk
    # tier in these columns (not in the generic mapped fields), so capture them raw for
    # Azure compute sizing and disk-tier -> GB resolution.
    def _hidx(*names):
        want = {normalize(n).replace(" ", "") for n in names}
        return next((i for i, h in enumerate(headers) if normalize(h).replace(" ", "") in want), None)
    az_addinfo_idx = _hidx("additionalinfo")
    az_meter_idx = _hidx("metername")
    az_metercat_idx = _hidx("metercategory")
    az_metersub_idx = _hidx("metersubcategory")
    az_consumed_idx = _hidx("consumedservice")
    rows = []
    rate_card = build_rate_card(DEFAULT_SHAPE_KEY, True)
    provider_label = detected_provider if detected_provider != "Unknown" else ""
    for raw_idx in range(data_start_idx, len(raw.index)):
        values = raw.iloc[raw_idx].tolist()
        if not any(clean_text(value) for value in values):
            continue
        row = {"__id": f"bill-row-{raw_idx + 1}", "__sourceRow": raw_idx + 1, "__approved": True}
        for field in fields:
            mapping = mappings.get(field["key"])
            value = ""
            if mapping:
                col_idx = mapping["sourceColumn"] - 1
                if 0 <= col_idx < len(values):
                    value = values[col_idx]
            row[field["key"]] = cloud_bill_value(field["key"], value)
        row["source_provider"] = row.get("source_provider") or provider_label
        row["source_currency"] = row.get("source_currency") or "USD"
        row["source_tags"] = summarize_source_tags(values, tag_columns, row.get("source_tags"))
        if usagetype_idx is not None and usagetype_idx < len(values):
            row["__usageType"] = clean_text(values[usagetype_idx])
        if recordtype_idx is not None and recordtype_idx < len(values):
            row["__recordType"] = clean_text(values[recordtype_idx])
        for _key, _idx in (("__azureInfo", az_addinfo_idx), ("__meterName", az_meter_idx),
                           ("__meterCategory", az_metercat_idx), ("__meterSub", az_metersub_idx),
                           ("__consumedService", az_consumed_idx)):
            if _idx is not None and _idx < len(values):
                row[_key] = clean_text(values[_idx])
        normalize_azure_storage_units(row)
        enrich_cloud_bill_resource_fields(row)
        seed_cloud_bill_mapping(row, fields, rate_card)
        remap_snapshot_storage(row)
        if cloud_row_has_signal(row):
            rows.append(row)

    if not rows:
        raise ValueError("The cloud bill parser did not find usable bill line rows.")

    rows = filter_bill_record_types(rows)
    enrich_aws_skus_from_api(rows)

    mapped_count = sum(1 for row in rows if row_mapping_is_confident(row))
    currency_values = [clean_text(row.get("source_currency")) for row in rows if clean_text(row.get("source_currency"))]
    source_currency = currency_values[0] if currency_values else "USD"
    parsed = {
        "fileName": Path(path).name,
        "sheetName": sheet_name,
        "sheets": [item[0] for item in candidate_tables],
        "fields": fields,
        "rows": rows,
        "rateCard": build_rate_card(DEFAULT_SHAPE_KEY, True),
        "rateCards": all_shape_payloads(True),
        "fullServiceCatalog": price_catalog_payload(),
        "selectedShape": shape_payload(DEFAULT_SHAPE_KEY, True),
        "metadata": {
            "intakeMode": INTAKE_MODE_CLOUD_BILL,
            "providerHint": normalize_provider_hint(provider_hint),
            "detectedProvider": detected_provider,
            "providerConfidence": provider_confidence,
            "parser": "cloud-bill-adapter",
            "sourceCurrency": source_currency,
            "mappedCount": mapped_count,
            "unmappedCount": len(rows) - mapped_count,
            "inventorySuspected": looks_like_inventory(raw, mappings),
            "headerRows": [header_row + 1],
            "dataStartRow": data_start_idx + 1,
            "rowCount": len(rows),
            "columnCount": len(fields),
        },
    }
    return parsed


FIXED_REVIEW_SCHEMA = [
    "Application Name",
    "Machine Name",
    "Environment",
    "OCPUs",
    "RAM (GB)",
    "Storage (GB)",
    "Hours Running",
]


def inventory_scrub_quality(parsed):
    rows = (parsed or {}).get("rows") or []
    fields = (parsed or {}).get("fields") or []
    checks = inventory_data_check(fields, rows) if rows and fields else {"signals": []}
    present = {item["key"] for item in checks.get("signals", []) if item.get("present")}
    return {
        "rowCount": len(rows),
        "hasIdentity": bool({"application", "server"} & present),
        "hasCpu": "cpu" in present,
        "hasMemory": "memory" in present,
        "hasStorage": "storage" in present,
    }


def validate_ai_inventory_scrub(candidate, baseline=None):
    quality = inventory_scrub_quality(candidate)
    if not quality["rowCount"]:
        raise ValueError("The AI scrub returned no usable inventory rows.")
    if not quality["hasIdentity"]:
        raise ValueError("The AI scrub did not identify an application or machine name.")
    if not (quality["hasCpu"] and quality["hasMemory"]):
        raise ValueError("The AI scrub did not identify both CPU and memory.")
    baseline_rows = len((baseline or {}).get("rows") or [])
    if baseline_rows:
        ratio = quality["rowCount"] / baseline_rows
        if ratio < 0.65 or ratio > 1.35:
            raise ValueError(
                f"The AI scrub selected {quality['rowCount']} rows while the deterministic "
                f"parser found {baseline_rows}; used the deterministic result."
            )
    metadata = candidate.setdefault("metadata", {})
    metadata["reviewSchema"] = list(FIXED_REVIEW_SCHEMA)
    metadata["aiValidation"] = quality
    metadata["aiAssisted"] = True
    return candidate


def _baseline_sizing_complete(baseline):
    """True when the rule-based parser confidently read the core sizing columns — CPU (marked
    with cpuSourceLabel), memory, AND storage — each with actual data in the rows. Used to decide
    whether the deterministic parse is reliable enough to prefer over the AI plan for on-prem."""
    try:
        rows = baseline.get("rows") or []
        fields = baseline.get("fields") or []
        if not rows:
            return False

        def marker_has_data(marker):
            for f in fields:
                if isinstance(f, dict) and f.get(marker):
                    k = f.get("key")
                    if k and any(clean_text(r.get(k)) for r in rows):
                        return True
            return False

        return (marker_has_data("cpuSourceLabel") and marker_has_data("memorySourceLabel")
                and marker_has_data("storageSourceLabel"))
    except Exception:
        return False


def _reconcile_onprem_sizing(result, baseline):
    """Safety net (in addition to the early return in parse_workbook): if the AI result is used
    but the deterministic parser had complete CPU/memory/storage sizing, prefer the baseline."""
    try:
        if not baseline or result is baseline:
            return result
        if _baseline_sizing_complete(baseline):
            baseline.setdefault("metadata", {})["preferredOverAI"] = True
            baseline["llmWarning"] = (
                "Used the validated rule-based parser for CPU / memory / storage sizing "
                "(it detected the intended columns, including any rationalized-cores column)."
            )
            return baseline
    except Exception:
        return result
    return result


def parse_workbook(path, full_service_beta=False, intake_mode=INTAKE_MODE_ON_PREM, provider_hint=PROVIDER_AUTO):
    if intake_mode == INTAKE_MODE_CLOUD_BILL:
        parsed = parse_cloud_bill(path, provider_hint)
        metadata = parsed.setdefault("metadata", {})
        metadata["llmBillMappingNeeded"] = bool(metadata.get("unmappedCount"))
        if not metadata["llmBillMappingNeeded"]:
            return parsed
        if not openai_api_enabled():
            metadata["llmBillMappingWarning"] = OPENAI_DISABLED_MESSAGE
            return parsed
        if not openai_api_configured():
            metadata["llmBillMappingWarning"] = "OPENAI_API_KEY is not set."
            return parsed
        try:
            return call_llm_cloud_bill_mapping(parsed)
        except Exception as exc:
            metadata["llmBillMappingWarning"] = clean_text(exc)
            metadata.setdefault("extractionNotes", []).append(
                "Used deterministic OCI bill mapping because the OpenAI fallback did not complete."
            )
            return parsed

    baseline = None
    baseline_error = None
    try:
        baseline = parse_workbook_rule_based(path, full_service_beta)
        baseline.setdefault("metadata", {})["reviewSchema"] = list(FIXED_REVIEW_SCHEMA)
        baseline["metadata"]["aiAssisted"] = False
    except Exception as exc:
        baseline_error = exc
    # If the deterministic rule-based parser confidently read the core sizing columns — CPU
    # (incl. a rationalized-cores column, treated as OCPU 1:1), memory, AND storage, each with
    # data — use it and skip the AI plan entirely. It's reliable for well-formed inventories,
    # and the AI plan sometimes maps a raw-vCPU / vCPU:Core-ratio column for CPU or misses storage.
    if baseline is not None and _baseline_sizing_complete(baseline):
        baseline.setdefault("metadata", {})["ruleBasedSizingComplete"] = True
        baseline["llmWarning"] = (
            "Used the validated rule-based parser — it read CPU, memory, and storage "
            "(including any rationalized-cores column)."
        )
        return baseline
    if not openai_api_enabled():
        if baseline is None:
            raise baseline_error
        baseline["llmWarning"] = (
            "OpenAI inventory scrubbing is disabled; used validated rule-based spreadsheet parsing."
        )
        return baseline
    if not openai_api_configured():
        if baseline is None:
            raise baseline_error
        baseline["llmWarning"] = (
            "OPENAI_API_KEY is not set; used validated rule-based spreadsheet parsing."
        )
        return baseline

    try:
        plan, llm_warning = call_llm_workbook_plan(path, full_service_beta)
        if plan:
            candidate = parse_workbook_from_plan(
                path, plan, full_service_beta, intake_mode
            )
            result = validate_ai_inventory_scrub(candidate, baseline)
            # The AI plan sometimes maps the wrong CPU column (e.g. a raw-vCPU or vCPU:Core-ratio
            # column instead of a "Rationalized Cores" column) or misses storage. The rule-based
            # parser is deterministic and gets these right, so overlay its CPU/memory/storage
            # sizing columns onto the AI result — keeping the AI's row cleanup for everything else.
            result = _reconcile_onprem_sizing(result, baseline)
            # AGENT_AUTHORITY: inventory parsing is advisory. If the agent raised a major error
            # the deterministic parse still stands, but the upload is flagged for review rather
            # than the objection being dropped.
            decision = resolve_agent_result("inventory_scrub", baseline, plan)
            if decision.get("review"):
                meta = result.setdefault("metadata", {}) if isinstance(result, dict) else {}
                meta["agentReview"] = True
                meta["agentNote"] = decision.get("note", "")
                if isinstance(result, dict):
                    result["llmWarning"] = decision.get("note", "")
            return result
        if baseline is None:
            raise ValueError(
                llm_warning or "Neither AI nor deterministic parsing found a usable inventory table."
            )
        baseline["llmWarning"] = llm_warning or (
            "OpenAI did not identify a safe inventory table; used validated rule-based parsing."
        )
    except Exception as exc:
        if baseline is None:
            raise
        baseline["llmWarning"] = (
            "OpenAI inventory scrubbing did not pass validation; used validated rule-based "
            f"spreadsheet parsing. Detail: {exc}"
        )
    return baseline


def find_key(fields, contains, section=None):
    needles = [normalize(item) for item in contains]
    section_norm = normalize(section) if section else ""
    for field in fields:
        label_norm = normalize(field["label"])
        if section_norm and not label_norm.startswith(section_norm):
            continue
        if all(needle in label_norm for needle in needles):
            return field["key"]
    return None


def find_key_exact(fields, labels):
    targets = {normalize(label) for label in labels}
    for field in fields:
        label_norm = normalize(field.get("label"))
        source_norm = normalize(field.get("sourceHeader"))
        if label_norm in targets or source_norm in targets:
            return field["key"]
    return None


def value_for(row, key, default=0.0):
    return to_number(row.get(key), default) if key else default


def row_operating_system(row):
    """Scan all source values of a row for an OS hint (matches the BOM script).

    An explicit per-row override from the Review table wins outright. Detection is a guess -
    it reads any cell containing "windows" or "linux", so a comment like "migrating off
    Windows" flips a Linux box - and the person reviewing the inventory knows better than the
    scan does. Stored under a "__" key so the scan below can't see it and re-derive it.
    """
    override = str(row.get("__os") or "").strip().lower()
    if override in {"windows", "linux"}:
        return override
    detected = ""
    for key, value in row.items():
        if isinstance(key, str) and key.startswith("__"):
            continue
        text = str(value).lower()
        if "windows" in text:
            return "windows"
        if "linux" in text:
            detected = "linux"
    return detected


def field_by_key(fields, key):
    return next((field for field in fields if field.get("key") == key), None)


def field_is_ocpu(fields, key):
    field = field_by_key(fields, key)
    if not field:
        return False
    label = normalize(field.get("label"))
    # OCPU columns, or physical-CORE columns (in OCI 1 OCPU = 1 physical core, so a "CPU cores
    # per server" / "physical cores" count is OCPUs 1:1 and must NOT be halved like vCPUs).
    return "ocpu" in label or ("core" in label and "vcpu" not in label)


def field_text(fields, key):
    field = field_by_key(fields, key)
    if not field:
        return ""
    return normalize(" ".join(clean_text(field.get(item)) for item in ["label", "sourceHeader", "description"]))


GIB_TO_GB = 1.073741824


def field_is_gib(fields, key):
    """True when a storage column is expressed in GiB (binary) rather than GB."""
    if not key:
        return False
    text = field_text(fields, key)
    return "gib" in text or "gibibyte" in text


def storage_gb_value(fields, key, value):
    """Return storage in GB. If the column is GiB, convert (x1.073741824) and floor;
    if it is already GB, use the given value as-is."""
    if not value:
        return 0.0
    if field_is_gib(fields, key):
        return float(math.floor(value * GIB_TO_GB))
    return value


def storage_field_is_row_total(fields, key):
    text = field_text(fields, key)
    if not key or not text:
        return False
    if any(term in text for term in ["per server", "per vm", "per host", "per instance", "per node"]):
        return False
    return any(term in text for term in ["total storage", "total allocated", "allocated storage", "storage total"])


def ocpus_for_review_value(fields, key, value):
    if not key or not value:
        return 0.0
    return value if field_is_ocpu(fields, key) else value / 2


# Only the AmpereOne A1/A2 shapes can be priced in 0.5-OCPU increments.
FRACTIONAL_OCPU_SHAPES = {"a1-standard", "a2-standard"}


def round_ocpu_for_shape(ocpu, shape_key):
    """OCI OCPU granularity rules (per VM):
      - A1 / A2 (Ampere) allow half OCPUs -> snap to the nearest 0.5.
      - Every other shape needs whole OCPUs: anything below 1 rounds UP to 1,
        and 1 or more rounds DOWN to the whole number (e.g. 1.5 -> 1)."""
    value = float(ocpu or 0)
    if value <= 0:
        return 0.0
    if shape_key in FRACTIONAL_OCPU_SHAPES:
        return round(value * 2) / 2
    if value < 1:
        return 1.0
    return float(math.floor(value))


def text_for(row, fields, contains, section=None):
    key = find_key(fields, contains, section)
    return clean_text(row.get(key, "")) if key else ""


def text_for_exact(row, fields, labels):
    key = find_key_exact(fields, labels)
    return clean_text(row.get(key, "")) if key else ""


def rate(sku, rate_card):
    for item in rate_card:
        if item["sku"] == sku:
            return item
    raise KeyError(sku)


def money(value):
    return round(float(value), 2)


def find_key_any(fields, groups, section=None):
    for contains in groups:
        key = find_key(fields, contains, section)
        if key:
            return key
    return None


def find_storage_key_any(fields, groups, section=None):
    """find_key_any, but skipping columns that name a storage KIND instead of an amount.

    Filtering has to happen DURING the search, not after it. A sheet with both "Storage Type"
    and "Storage" matches the bare needle on whichever column comes first; rejecting the
    winner afterwards threw away the real capacity column along with it, and the inventory
    priced at zero storage.
    """
    for contains in groups:
        for field in fields or []:
            if not isinstance(field, dict) or not field.get("key"):
                continue
            key = find_key([field], contains, section)
            if key and plausible_storage_field(fields, key):
                return key
    return None


def text_from_any(row, fields, canonical_key, groups):
    value = clean_text(row.get(canonical_key))
    if value:
        return value
    key = find_key_any(fields, groups)
    return clean_text(row.get(key, "")) if key else ""


def row_context(row, fields):
    parts = []
    for field in fields:
        key = field.get("key")
        value = clean_text(row.get(key))
        if not key or not value:
            continue
        parts.append(f"{field.get('label', key)} {value}")
    return normalize(" ".join(parts))


def detect_source_provider(provider, context):
    provider_text = clean_text(provider)
    if provider_text:
        return provider_text
    if re.search(r"\baws\b|amazon|cur|cost explorer|ec2|s3|ebs|efs|rds", context):
        return "AWS"
    if re.search(r"\bazure\b|microsoft|managed disk|blob|azure files|meter category", context):
        return "Azure"
    if re.search(r"\bgcp\b|google cloud|cloud storage|persistent disk|filestore|bigquery", context):
        return "GCP"
    if re.search(r"on prem|on premise|on premises|vmware|vcenter|esxi|hyper v|san|nas|nfs|smb", context):
        return "On-prem"
    return ""


def context_has_term(context, term):
    term_norm = normalize(term)
    if not term_norm:
        return False
    if len(term_norm) <= 3 or re.fullmatch(r"[a-z]+\d+|\d+[a-z]+", term_norm):
        return bool(re.search(rf"(^|\s){re.escape(term_norm)}($|\s)", context))
    return term_norm in context


def context_has_any(context, terms):
    return any(context_has_term(context, term) for term in terms)


def infer_oci_service_target(row, fields):
    context = row_context(row, fields)
    target = normalize(
        " ".join(
            [
                clean_text(row.get("source_service")),
                clean_text(row.get("source_product")),
                clean_text(row.get("source_tags")),
            ]
        )
    )
    haystack = f"{target} {context}"

    rules = [
        {
            "terms": ["cloudtrail", "freeeventsrecorded", "events recorded"],
            "category": "Security",
            "product": "OCI Audit / Logging",
            "confidence": 0.78,
            "reviewRequired": True,
        },
        {
            "terms": ["cloudwatch", "putlogevents", "metric month", "logs", "log data ingested"],
            "category": "Observability and Management",
            "product": "OCI Monitoring / Logging",
            "confidence": 0.82,
            "reviewRequired": True,
        },
        {
            "terms": ["aws config", "configurationitemrecorded", "configuration item recorded"],
            "category": "Observability and Management",
            "product": "OCI Cloud Guard / Resource Manager",
            "confidence": 0.7,
            "reviewRequired": True,
        },
        {
            "terms": ["data transfer", "bandwidth", "out bytes", "in bytes", "gb data processed"],
            "category": "Networking",
            "product": "OCI Networking data transfer",
            "confidence": 0.76,
            "reviewRequired": True,
        },
        {
            "terms": ["elastic load balancing", "loadbalancer", "load balancer", "lcu hrs", "lcu hour"],
            "category": "Networking",
            "product": "OCI Load Balancer",
            "confidence": 0.82,
            "reviewRequired": True,
        },
        {
            "terms": ["elastic compute cloud", "ec2", "instance hour", "instancehour", "boxusage", "linux unix", "windows amazon vpc"],
            "category": "Compute",
            "product": "OCI Virtual Machine Instances",
            "confidence": 0.78,
            "reviewRequired": True,
        },
        {
            "terms": ["key management service", "kms requests", "kms keys", "customer managed kms"],
            "category": "Security",
            "product": "OCI Vault",
            "confidence": 0.82,
            "reviewRequired": True,
        },
        {
            "terms": ["aws iot", "registryandshadowoperations", "device shadow", "device registry"],
            "category": "Integration",
            "product": "OCI application integration / IoT equivalent",
            "confidence": 0.62,
            "reviewRequired": True,
        },
        {
            "terms": ["simple email service", "sendemail", "sendrawemail"],
            "category": "Developer Services",
            "product": "OCI Email Delivery",
            "confidence": 0.86,
            "reviewRequired": True,
        },
        {
            "terms": ["simple notification service", "sns api", "sns requests"],
            "category": "Developer Services",
            "product": "OCI Notifications",
            "confidence": 0.86,
            "reviewRequired": True,
        },
        {
            "terms": ["simple queue service", "sqs requests", "sqs"],
            "category": "Developer Services",
            "product": "OCI Queue",
            "confidence": 0.86,
            "reviewRequired": True,
        },
        {
            "terms": ["simpledb"],
            "category": "Open Source Databases",
            "product": "Oracle NoSQL Database / Autonomous JSON Database",
            "confidence": 0.58,
            "reviewRequired": True,
        },
        {
            "terms": ["virtual private cloud", "createvpnconnection", "vpn connection"],
            "category": "Networking",
            "product": "OCI VPN Connect / Virtual Cloud Network",
            "confidence": 0.82,
            "reviewRequired": True,
        },
        {
            "terms": ["support business", "aws support"],
            "category": "Customer Success Services",
            "product": "Oracle Cloud support services",
            "confidence": 0.7,
            "reviewRequired": True,
        },
    ]
    for rule in rules:
        if context_has_any(haystack, rule["terms"]):
            return rule
    return None


def classify_full_service_item(row, fields):
    context = row_context(row, fields)
    target = normalize(
        " ".join(
            [
                clean_text(row.get("oci_product")),
                clean_text(row.get("oci_service_category")),
                clean_text(row.get("source_service")),
                clean_text(row.get("source_product")),
            ]
        )
    )
    haystack = f"{target} {context}"
    review_only_service_terms = [
        "cloudwatch",
        "cloudtrail",
        "aws config",
        "data transfer",
        "elastic load balancing",
        "instancehour",
        "key management service",
        "aws iot",
        "simple notification service",
        "simple queue service",
        "simple email service",
        "simpledb",
        "virtual private cloud",
        "support business",
    ]
    if context_has_any(haystack, review_only_service_terms):
        return None, 0.0

    for item in FULL_SERVICE_CATALOG_ITEMS:
        if item["sku"].lower() in haystack:
            return item, 0.96
        if normalize(item["description"]) and normalize(item["description"]) in haystack:
            return item, 0.92

    object_terms = ["object", "s3", "blob", "bucket", "gcs", "cloud storage", "simple storage service", "timedstorage", "bytehrs"]
    request_terms = ["request", "requests", "api", "put", "get", "list"]
    retrieval_terms = ["retrieval", "retrieved", "restore bytes", "restorebyte"]
    infrequent_terms = [
        "infrequent access",
        "standard ia",
        "standard-ia",
        "one zone ia",
        "one zone-ia",
        "onezone ia",
        "s3 ia",
        "cool blob",
        "cool tier",
        "nearline",
        "ia byte",
        "ia-byte",
    ]
    archive_terms = ["archive", "glacier", "deep archive", "coldline", "cold storage"]
    file_terms = ["efs", "azure files", "file share", "filestore", "nfs", "smb", "nas", "file storage"]
    block_terms = [
        "ebs",
        "managed disk",
        "persistent disk",
        "block",
        "volume",
        "san",
        "disk",
        "gp2",
        "gp3",
        "ssd",
        "magnetic provisioned storage",
        "cold hdd",
        "snapshot data stored",
        "provisioned storage",
        "optimized storage",
        "postgresql optimized storage",
        "gb month",
        "gb mo",
    ]
    compute_terms = ["ocpu", "vcpu", "cpu hour", "cpu-hour", "core hour", "core-hour", "compute unit"]
    memory_terms = ["memory gb hour", "memory gb-hour", "ram gb hour", "ram gb-hour", "gb hour memory", "gb-hour memory"]
    plain_data_unit = bool(re.fullmatch(r"(mb|mib|gb|gib|tb|tib)", normalize(row.get("usage_unit"))))

    if context_has_any(haystack, compute_terms):
        return FULL_SERVICE_RATE_BY_KEY["compute_ocpu_hours"], 0.9
    if context_has_any(haystack, memory_terms):
        return FULL_SERVICE_RATE_BY_KEY["memory_gb_hours"], 0.9
    if (context_has_any(haystack, request_terms)
            and context_has_any(haystack, object_terms + infrequent_terms + archive_terms)):
        return FULL_SERVICE_RATE_BY_KEY["object_storage_requests"], 0.88
    if context_has_any(haystack, infrequent_terms) and context_has_any(haystack, retrieval_terms):
        return FULL_SERVICE_RATE_BY_KEY["object_storage_infrequent_retrieval"], 0.92
    if context_has_any(haystack, archive_terms):
        return FULL_SERVICE_RATE_BY_KEY["archive_storage"], 0.9
    if context_has_any(haystack, infrequent_terms):
        return FULL_SERVICE_RATE_BY_KEY["object_storage_infrequent"], 0.9
    if plain_data_unit and context_has_any(haystack, block_terms):
        return FULL_SERVICE_RATE_BY_KEY["block_volume_storage"], 0.86
    if plain_data_unit and context_has_any(haystack, object_terms):
        return FULL_SERVICE_RATE_BY_KEY["object_storage_standard"], 0.86
    if context_has_any(haystack, file_terms):
        return FULL_SERVICE_RATE_BY_KEY["file_storage"], 0.88
    if context_has_any(haystack, block_terms):
        return FULL_SERVICE_RATE_BY_KEY["block_volume_storage"], 0.86
    if context_has_any(haystack, object_terms):
        return FULL_SERVICE_RATE_BY_KEY["object_storage_standard"], 0.86
    return None, 0.0


def request_quantity(quantity_text, unit_text, context):
    quantity = to_number(quantity_text, 0)
    if quantity <= 0:
        return 0.0
    unit_norm = normalize(f"{unit_text} {context}")
    if re.search(r"\bbillion\b|1 000 000 000|1000000000", unit_norm):
        return quantity * 1000000000
    if re.search(r"\bmillion\b|1 000 000|1000000|1m\b", unit_norm):
        return quantity * 1000000
    if re.search(r"\b10k\b|10 000|10000|ten thousand", unit_norm):
        return quantity * 10000
    if re.search(r"\bthousand\b|1 000|1000|1k\b", unit_norm):
        return quantity * 1000
    return quantity


def storage_gb_month_quantity(quantity_text, unit_text, context):
    quantity = to_number(quantity_text, 0)
    if quantity <= 0:
        return 0.0
    unit_context = normalize(f"{unit_text} {context}")
    if re.search(r"byte ?hrs|byte ?hours|byte hour|bytehrs", unit_context):
        return quantity / (1024**3) / HOURS_PER_MONTH
    if re.search(r"byte ?seconds|byte second|bytesec|byte sec", unit_context):
        return quantity / (1024**3) / (HOURS_PER_MONTH * 3600)
    if re.search(r"\btb ?hours?\b|tib ?hours?", unit_context):
        return (quantity * 1024) / HOURS_PER_MONTH
    if re.search(r"\bgb ?hours?\b|gib ?hours?", unit_context):
        return quantity / HOURS_PER_MONTH
    if re.search(r"\bmb ?hours?\b|mib ?hours?", unit_context):
        return (quantity / 1024) / HOURS_PER_MONTH
    return to_gb(f"{quantity_text} {unit_text}", 0)


def usage_quantity_is_hours(quantity_text, unit_text, context, row=None):
    quantity = to_number(quantity_text, 0)
    if quantity <= 0:
        return False
    unit_context = normalize(f"{unit_text} {context}")
    unit_only = normalize(unit_text)
    if re.search(r"\bhrs?\b|\bhours?\b", unit_context):
        return True
    if row and unit_only in {"", "1", "unit", "units"} and (
        to_number(row.get("resource_ocpus"), 0) or to_number(row.get("resource_memory_gb"), 0)
    ):
        return True
    return False


def cloud_usage_hours(row, fields):
    context = row_context(row, fields)
    quantity_text = text_from_any(row, fields, "usage_quantity", [["usage", "quantity"], ["usage", "amount"], ["quantity"], ["consumed"]])
    unit_text = text_from_any(row, fields, "usage_unit", [["usage", "unit"], ["unit"], ["unit", "measure"], ["pricing", "unit"]])
    return to_number(quantity_text, 0) if usage_quantity_is_hours(quantity_text, unit_text, context, row) else 0.0


def source_only_context(row):
    return normalize(
        " ".join(
            clean_text(row.get(key))
            for key in [
                "source_provider",
                "source_service",
                "source_product",
                "usage_unit",
                "source_tags",
            ]
        )
    )


def quantity_for_full_service_item(item, quantity_text, unit_text, context, row=None):
    if not clean_text(quantity_text):
        return 0.0
    unit_context = normalize(f"{unit_text} {context}")
    if item["unit"] == "OCPU-hour":
        resource_ocpus = to_number(row.get("resource_ocpus"), 0) if row else 0
        if resource_ocpus and usage_quantity_is_hours(quantity_text, unit_text, context, row):
            return to_number(quantity_text, 0) * resource_ocpus
        source_context = source_only_context(row) if row else context
        inferred_ocpus, _ = infer_instance_shape_resources(source_context or context, quantity_text, unit_text)
        if inferred_ocpus and re.search(r"\bhrs?\b|\bhours?\b|instance|box ?usage", unit_context):
            return inferred_ocpus * HOURS_PER_MONTH
        is_vcpu = not re.search(r"\bocpus?\b", unit_context) and bool(
            re.search(r"\bvcpus?\b|\bv cpu\b|\bvcores?\b|\bv core\b|\bcpus?\b|\bcores?\b", unit_context)
        )
        if re.search(r"\bhrs?\b|\bhours?\b", unit_context):
            quantity = to_number(quantity_text, 0)
            return quantity / 2 if is_vcpu else quantity
        return meter_capacity_quantity(quantity_text, unit_context, is_vcpu=is_vcpu)
    if item["unit"] == "GB-hour":
        resource_memory_gb = to_number(row.get("resource_memory_gb"), 0) if row else 0
        if resource_memory_gb and usage_quantity_is_hours(quantity_text, unit_text, context, row):
            return to_number(quantity_text, 0) * resource_memory_gb
        return meter_capacity_quantity(quantity_text, unit_context)
    if item["unit"] == "GB-month":
        return storage_gb_month_quantity(quantity_text, unit_text, context)
    if item["unit"] == "10,000 requests":
        return request_quantity(quantity_text, unit_text, context) / 10000
    return to_number(quantity_text, 0)


def full_service_signal(row, fields):
    context = row_context(row, fields)
    if any(clean_text(row.get(key)) for key in SOURCE_SERVICE_FIELD_KEYS):
        return True
    return bool(
        re.search(
            r"\baws\b|amazon|azure|gcp|google cloud|on prem|on premise|vmware|s3|ebs|efs|blob|managed disk|persistent disk|filestore|glacier|archive|san|nas|nfs|smb",
            context,
        )
    )


def full_service_line_items(row, fields, rate_card=None):
    if not full_service_signal(row, fields):
        return [], None, []

    context = row_context(row, fields)
    provider = detect_source_provider(
        text_from_any(row, fields, "source_provider", [["provider"], ["cloud"], ["vendor"]]),
        context,
    )
    service = text_from_any(
        row,
        fields,
        "source_service",
        [["service"], ["meter", "category"], ["product", "code"], ["resource", "type"]],
    )
    product = text_from_any(
        row,
        fields,
        "source_product",
        [["usage", "type"], ["meter", "name"], ["sku"], ["product", "name"], ["item", "description"]],
    )
    region = text_from_any(row, fields, "source_region", [["region"], ["location"], ["datacenter"], ["data", "center"]])
    quantity_text = text_from_any(row, fields, "usage_quantity", [["usage", "quantity"], ["usage", "amount"], ["quantity"], ["consumed"]])
    unit_text = text_from_any(row, fields, "usage_unit", [["usage", "unit"], ["unit"], ["unit", "measure"], ["pricing", "unit"]])
    source_cost = text_from_any(row, fields, "source_monthly_cost", [["monthly", "cost"], ["cost"], ["charge"], ["amount"]])
    source_account = text_from_any(row, fields, "source_account", [["account"], ["subscription"], ["project"]])
    source_currency = text_from_any(row, fields, "source_currency", [["currency"], ["billing", "currency"]]) or "USD"
    source_period = text_from_any(row, fields, "source_period", [["period"], ["date"], ["month"]])
    source_tags = text_from_any(row, fields, "source_tags", [["tags"], ["labels"]])

    item, confidence = classify_full_service_item(row, fields)
    if not item:
        note = "Full-service beta saw this row but could not match it to the local OCI price-list subset."
        return [], {
            "sourceProvider": provider,
            "sourceAccount": source_account,
            "sourceService": service,
            "sourceProduct": product,
            "sourceRegion": region,
            "sourceMonthlyCost": money(to_number(source_cost, 0)) if source_cost else 0,
            "sourceCurrency": source_currency,
            "sourcePeriod": source_period,
            "sourceTags": source_tags,
            "confidence": 0,
            "reviewRequired": True,
        }, [note]

    quantity = quantity_for_full_service_item(item, quantity_text, unit_text, context, row)
    # On detailed bills, storage services have many non-capacity meters (requests,
    # retrieval, data transfer, select, notifications). Only the storage-capacity
    # meter should be priced as GB-month, otherwise storage is wildly over-counted.
    ut = normalize(row.get("__usageType"))
    if ut and "month" in normalize(item.get("unit")):
        non_capacity = ["request", "retrieval", "datatransfer", "data transfer", "select",
                        "notification", "earlydelete", "early delete", "lifecycle", "tagging",
                        "monitor", "inventory", "replication", "apicall", "api call"]
        is_capacity = any(k in ut for k in ["timedstorage", "bytehrs", "storage", "gb-mo", "gbmo", "volumeusage", "snapshotusage"])
        if not is_capacity and any(k in ut for k in non_capacity):
            quantity = 0
    if quantity <= 0:
        note = f"{item['description']} was inferred, but no usable usage quantity was present for OCI pricing."
        return [], {
            "sku": item["sku"],
            "ociProduct": item["description"],
            "sourceProvider": provider,
            "sourceAccount": source_account,
            "sourceService": service,
            "sourceProduct": product,
            "sourceRegion": region,
            "sourceMonthlyCost": money(to_number(source_cost, 0)) if source_cost else 0,
            "sourceCurrency": source_currency,
            "sourcePeriod": source_period,
            "sourceTags": source_tags,
            "confidence": round(confidence, 2),
            "reviewRequired": True,
        }, [note]

    priced_item = item
    if rate_card:
        priced_item = next((candidate for candidate in rate_card if candidate.get("sku") == item["sku"]), None)
        if not priced_item and item["unit"] in {"OCPU-hour", "GB-hour"}:
            priced_item = next((candidate for candidate in rate_card if candidate.get("unit") == item["unit"]), None)
        priced_item = priced_item or item
    monthly = money(quantity * priced_item["rate"])
    line_item = {
        "sku": priced_item["sku"],
        "description": priced_item["description"],
        "quantity": round(quantity, 4),
        "unit": item["unit"],
        "rate": priced_item["rate"],
        "monthly": monthly,
        "mapping": f"{provider or 'Source'} {service or product or 'usage'} maps to {item['description']}.",
    }
    mapping = {
        "sku": priced_item["sku"],
        "ociProduct": priced_item["description"],
        "sourceProvider": provider,
        "sourceAccount": source_account,
        "sourceService": service,
        "sourceProduct": product,
        "sourceRegion": region,
        "sourceMonthlyCost": money(to_number(source_cost, 0)) if source_cost else 0,
        "sourceCurrency": source_currency,
        "sourcePeriod": source_period,
        "sourceTags": source_tags,
        "quantity": round(quantity, 4),
        "unit": item["unit"],
        "confidence": round(confidence, 2),
        "reviewRequired": confidence < 0.9,
    }
    return [line_item], mapping, []


def detect_cpu_unit(fields):
    """Auto-detect whether the uploaded CPU column holds vCPUs or OCPUs from its
    original header text. Falls back to 'vcpu' (the app's default assumption) when the
    header is ambiguous (e.g. just 'CPUs')."""
    for f in fields or []:
        if not isinstance(f, dict):
            continue
        src = normalize(f.get("cpuSourceLabel") or "")
        if not src:
            continue
        if "ocpu" in src:
            return "ocpu"
        if "vcpu" in src or "v cpu" in src or "virtual cpu" in src:
            return "vcpu"
        # A physical-CORE column (CPU cores per server, physical cores, rationalized cores) is a
        # real core count, and in OCI 1 OCPU = 1 physical core - treat it as OCPUs (1:1), NOT
        # halved like vCPUs. Only vcpu-labelled columns (above) are halved.
        if "core" in src:
            return "ocpu"
    return "vcpu"


def calculate_pricing(fields, rows, shape_key=DEFAULT_SHAPE_KEY, full_service_beta=False, intake_mode=INTAKE_MODE_ON_PREM, bom_match=False, hide_gpu_pricing=False, hide_windows_pricing=False, rightsize=False, auto=False, hours_per_month=None, source_provider=None, auto_tier="best", shape_overrides=None, cost_overrides=None, cpu_unit="auto", hours_override=False, oic_message_packs=None, hide_sql_pricing=False):
    # cpu_unit override (on-prem uploads): the parser normalizes the source CPU column
    # to OCPUs assuming it holds vCPUs (2 vCPU = 1 OCPU). 'auto' (default) detects the
    # unit from the column header and falls back to vCPU; 'ocpu' uses the source count
    # as-is (undo the halving); 'vcpu' forces the 2:1 conversion.
    _cpu_unit = clean_text(cpu_unit).lower()
    if _cpu_unit not in ("auto", "vcpu", "ocpu"):
        _cpu_unit = "auto"
    cpu_unit_resolved = detect_cpu_unit(fields) if _cpu_unit == "auto" else _cpu_unit
    cpu_ocpu_mult = 2.0 if cpu_unit_resolved == "ocpu" else 1.0
    eff_hours = hours_per_month if (hours_per_month and hours_per_month > 0) else HOURS_PER_MONTH
    def _apply_rightsize(ocpu_value, mem_value, plan):
        # Compute optimization: shrink OCPUs and RAM by a percentage, but only when the
        # reduction actually crosses to a lower whole number - new = ceil(value*(1-pct)).
        # e.g. 4 GB at 20% -> ceil(3.2) = 4 (no change); at 25% -> ceil(3.0) = 3. Values are
        # never reduced below 2, and anything already <= 2 is left alone. Applied to all
        # compute; only the Ax shapes and the regular E6 get a plan (others: unchanged).
        if not rightsize or not plan:
            return ocpu_value, mem_value
        # Flat rate per shape (Ax 15%/20%, E6 10%/15%) - NOT compounded by how many
        # generations the source instance is behind. The Compute sheet shows this exact
        # band as the "% approximation", so the reduction must match what's displayed.
        ocpu_rate, ram_rate, _gens = plan

        def opt(v, rate):
            if not v or v <= 2:
                return v
            pct = min(0.95, max(0.0, rate))
            return max(2, math.ceil(v * (1 - pct)))

        return opt(ocpu_value, ocpu_rate), opt(mem_value, ram_rate)
    cloud_bill_mode = intake_mode == INTAKE_MODE_CLOUD_BILL
    service_catalog_enabled = bool(full_service_beta or cloud_bill_mode)
    selected_shape = shape_payload(shape_key, service_catalog_enabled)
    rate_card = selected_shape["rateCard"]
    keys = {
        "app_servers": find_key(fields, ["number of servers"], "Application Details"),
        "app_cpu": find_key_any(
            fields,
            [["ocpus per server"], ["ocpu"], ["number of cpu cores per server"], ["vcpu"], ["cpu cores"], ["cores"]],
            "Application Details",
        )
        or find_key_any(fields, [["ocpus per server"], ["ocpu"], ["number of cpu cores per server"], ["vcpu"], ["cpu cores"], ["cores"]]),
        "app_memory": find_key_any(
            fields,
            [["memory per server"], ["memory"], ["ram"], ["memory gb"], ["ram gb"]],
            "Application Details",
        )
        or find_key_any(fields, [["memory per server"], ["memory"], ["ram"], ["memory gb"], ["ram gb"]]),
        # Needle sets are AND-ed tokens, so ["disk", "gb"] matches "Disk in GB", "Disk (GB)",
        # "Disk GB" - a literal "disk gb" would miss any of those with a word in between.
        # Every set is anchored to a STORAGE word (storage/disk/provisioned). RAM is also in
        # GB, so a bare capacity+gb (or gb alone) would wrongly grab a "Memory (GB)" /
        # "RAM Capacity (GB)" column - never match on "gb" without a storage word.
        # A bare "Storage" / "Disk" header is listed LAST so the specific needles win when a
        # sheet has both, but it still resolves: plenty of inventories just write "Storage"
        # with the unit in the cell ("384 GB", "1 TB") rather than in the header, and those
        # were silently pricing at zero block storage. Safe because plausible_storage_field
        # below rejects anything that isn't a capacity column.
        "app_local_storage": (find_storage_key_any(
            fields,
            [["local storage"], ["total storage"], ["allocated storage"], ["storage", "gb"],
             ["disk", "gb"], ["disk", "size"], ["disk", "capacity"], ["provisioned", "storage"],
             ["provisioned", "disk"], ["storage"], ["disk"]],
            "Application Details",
        )
        or find_storage_key_any(fields, [["local storage"], ["total storage"], ["allocated storage"],
                                        ["storage", "gb"], ["disk", "gb"], ["disk", "size"],
                                        ["disk", "capacity"], ["provisioned", "storage"],
                                        ["provisioned", "disk"], ["storage"], ["disk"]])),
        # NOTE: needles must be specific - a bare "smb" matched "SMBios UUID" and the
        # UUID's digits were priced as file storage. Keep these anchored to storage words.
        "app_shared_storage": find_key_any(fields, [["shared storage"], ["file storage"], ["nas storage"], ["nfs"], ["smb share"], ["cifs"]], "Application Details")
        or find_key_any(fields, [["shared storage"], ["file storage"], ["nas storage"], ["nfs"], ["smb share"], ["cifs"]]),
        "db_servers": find_key(fields, ["number of database servers"], "Database Details"),
        "db_cpu": find_key_any(
            fields,
            [["ocpus per server"], ["ocpu"], ["database cpu"], ["db cpu"], ["database cores"], ["database vcpu"], ["number of cpu cores per server"]],
            "Database Details",
        )
        or find_key_any(fields, [["database ocpu"], ["database cpu"], ["db cpu"], ["database cores"], ["database vcpu"]]),
        "db_memory": find_key_any(
            fields,
            [["memory per server"], ["database memory"], ["db memory"], ["database ram"], ["db ram"], ["memory"], ["ram"]],
            "Database Details",
        )
        or find_key_any(fields, [["database memory"], ["db memory"], ["database ram"], ["db ram"]]),
        # Section-scoped first, then a section-less fallback on the field's own declared aliases
        # (database/db storage, size) so a plainly-named "Database Storage (GB)" column prices
        # too - mirroring how the application-storage keys resolve.
        "db_total_allocated": find_key(fields, ["total allocated storage"], "Database Details")
        or find_key_any(fields, [["database total allocated storage"], ["db total allocated storage"], ["database storage"], ["db storage"]]),
        "db_total_storage": find_key(fields, ["total storage"], "Database Details")
        or find_key_any(fields, [["database total storage"], ["db total storage"]]),
        "db_size": find_key(fields, ["database size"], "Database Details")
        or find_key_any(fields, [["database size"], ["db size"]]),
        "hours": find_key_any(
            fields,
            [["hours per month"], ["hours/month"], ["hours month"], ["monthly hours"], ["hours running"], ["running hours"], ["usage hours"], ["uptime hours"], ["hours"]],
        ),
        "region": find_key_any(
            fields,
            [["aws region"], ["region"], ["location"], ["datacenter"], ["data center"], ["availability domain"], ["availability zone"]],
        ),
    }
    # Guard: never price an identifier/metadata column as storage. (A bare "smb" needle
    # once matched "SMBios UUID" and the UUID's digits were billed as file storage.)
    for _sk in ("app_local_storage", "app_shared_storage", "db_total_allocated",
                "db_total_storage", "db_size"):
        if keys.get(_sk) and not plausible_storage_field(fields, keys[_sk]):
            keys[_sk] = None
    # A single-tier inventory (e.g. a DB-only per-server table with no "Application Details"
    # section) can resolve the SAME column for both an app and a db finder, because the app
    # finders fall back section-lessly. That double-counts the column as app + db. If an app key
    # landed on a column already claimed by a db key, drop the app side so it's counted once.
    _db_claimed = {keys.get(k) for k in ("db_cpu", "db_memory", "db_servers",
                                         "db_total_allocated", "db_total_storage", "db_size")
                   if keys.get(k)}
    for _ak in ("app_cpu", "app_memory", "app_local_storage", "app_shared_storage", "app_servers"):
        if keys.get(_ak) and keys.get(_ak) in _db_claimed:
            keys[_ak] = None
    data_has = {"region": False, "environment": False, "hours": bool(keys.get("hours"))}

    priced_rows = []
    totals = {
        "ocpus": 0.0,
        "memoryGb": 0.0,
        "blockStorageGb": 0.0,
        "fileStorageGb": 0.0,
        "cloudStorageGb": 0.0,
        "fullServiceMonthly": 0.0,
        "mappedServiceRows": 0,
        "unpricedServiceRows": 0,
        "oversizeRows": 0,
        "impossibleRows": 0,
        "sourceMonthlyCost": 0.0,
        "mappedSourceMonthlyCost": 0.0,
        "unmappedSourceMonthlyCost": 0.0,
        # Source spend that produces NO OCI cost. unmappedSourceMonthlyCost only counts rows with
        # no mapping at all, which badly understated the gap (it reported $384 on a bill where
        # $62,883 of spend landed at $0). Split so the UI can separate a real OCI advantage from
        # something that still needs attention:
        #   freeOnOciSourceMonthly  - billing constructs (Savings Plans, support) and services
        #                             OCI genuinely doesn't charge for (VCN, Audit, free egress)
        #   unpricedSourceMonthly   - mapped to a CHARGEABLE OCI product but priced at zero, or
        #                             not mapped at all: these understate the OCI estimate
        "zeroOciSourceMonthly": 0.0,
        "freeOnOciSourceMonthly": 0.0,
        "unpricedSourceMonthly": 0.0,
        "unpricedRows": 0,
        # Of the unpriced spend, the part with NO OCI mapping at all - nothing was chosen for
        # this line, so nobody can even say what it should have cost. Kept separate from lines
        # that DID map to a chargeable product and merely never got a rate, because only the
        # unmapped ones need a human to decide what they map to.
        "unmappedZeroSourceMonthly": 0.0,
        "unmappedRows": 0,
        # Cost CARRIED OVER from the source bill because the line couldn't be priced on an OCI
        # rate (an unmappable unit, e.g. FSx provisioned MB/s throughput). It is the source
        # figure copied across, NOT an OCI calculation, so those lines can never show a saving
        # and they pull the OCI total toward the source total. Tracked and surfaced so the
        # estimate can't quietly look more precise than it is.
        "carriedSourceMonthly": 0.0,
        "carriedRows": 0,
        "monthly": 0.0,
        "annual": 0.0,
    }

    def append_compute_memory_items(target_items, ocpus_value, memory_gb_value, mapping_prefix, hours_value=HOURS_PER_MONTH, shape=None):
        shape = shape or selected_shape
        hours = hours_value if hours_value and hours_value > 0 else HOURS_PER_MONTH
        if ocpus_value:
            qty = ocpus_value * hours
            r = shape.get("computeRate", 0)
            target_items.append(
                {
                    "sku": shape.get("computeSku", "B97384"),
                    "description": f"OCPU-hr rate ({shape.get('label', 'Compute')})",
                    "quantity": round(qty, 4),
                    "unit": "OCPU-hour",
                    "rate": r,
                    "monthly": money(qty * r),
                    "mapping": mapping_prefix,
                }
            )
        if memory_gb_value:
            qty = memory_gb_value * hours
            r = shape.get("memoryRate", 0)
            target_items.append(
                {
                    "sku": shape.get("memorySku", "B97385"),
                    "description": f"Memory GB-hr rate ({shape.get('label', 'Memory')})",
                    "quantity": round(qty, 4),
                    "unit": "GB-hour",
                    "rate": r,
                    "monthly": money(qty * r),
                    "mapping": "Memory is billed as GB-hours.",
                }
            )

    # OCI Outbound Data Transfer: each origin region group gets its own 10 TB/month
    # free allowance, consumed across rows.
    oci_transfer_pools = {}
    # OCI WAF: first instance + first 10M requests/month free, shared once per bill.
    oci_waf_instance_pool = [OCI_WAF_FREE_INSTANCES]
    oci_waf_request_pool = [OCI_WAF_FREE_REQUESTS]
    # OCI Logging: first 10 GB/month free, shared once per bill.
    oci_logging_pool = [OCI_LOGGING_FREE_GB]

    # RDS instance types known to run SQL Server (scanned once) so license /
    # reserved-instance upfront fees that don't name the engine are carried too.
    rds_sql_server_instances = collect_sql_server_rds_instances(rows) if cloud_bill_mode else set()
    elb_free_tier_row_id = collect_elb_free_tier_row(rows) if cloud_bill_mode else None
    redshift_compute_ctx = collect_redshift_compute(rows) if cloud_bill_mode else None
    # The single row that carries the Oracle Integration Cloud message-pack charge, and the
    # pack count: a manual override (oic_message_packs) wins, else auto-size from the actual
    # SQS/SNS message counts + Transfer Family byte volume (same message-pack math as the
    # Add-OCI-services OIC card).
    oic_anchor_row_id = collect_oic_anchor_row(rows) if cloud_bill_mode else None
    oic_effective_packs = (oic_message_packs if (oic_message_packs and oic_message_packs > 0)
                           else (_oic_auto_packs(rows) if cloud_bill_mode else None))

    for row_index, row in enumerate(rows, start=1):
        if row.get("__approved") is False:
            continue

        app_servers = 0.0 if cloud_bill_mode else value_for(row, keys["app_servers"])
        db_servers = 0.0 if cloud_bill_mode else value_for(row, keys["db_servers"])
        app_cpu = 0.0 if cloud_bill_mode else value_for(row, keys["app_cpu"]) * cpu_ocpu_mult
        db_cpu = 0.0 if cloud_bill_mode else value_for(row, keys["db_cpu"]) * cpu_ocpu_mult
        app_memory = 0.0 if cloud_bill_mode else value_for(row, keys["app_memory"])
        db_memory = 0.0 if cloud_bill_mode else value_for(row, keys["db_memory"])
        if not cloud_bill_mode and not keys["app_servers"] and (app_cpu or app_memory):
            app_servers = 1.0
        if not cloud_bill_mode and not keys["db_servers"] and (db_cpu or db_memory):
            db_servers = 1.0
        # Storage: convert GiB columns to GB (floored); GB columns are used as-is.
        app_local_storage = 0.0 if cloud_bill_mode else storage_gb_value(fields, keys["app_local_storage"], value_for(row, keys["app_local_storage"]))
        app_shared_storage = 0.0 if cloud_bill_mode else storage_gb_value(fields, keys["app_shared_storage"], value_for(row, keys["app_shared_storage"]))

        storage_key = keys["db_total_allocated"] or keys["db_total_storage"] or keys["db_size"]
        db_storage = 0.0 if cloud_bill_mode else storage_gb_value(fields, storage_key, value_for(row, storage_key))

        # Source instance lookup (used for Best Match mapping, rightsize gen-gap, and
        # the source-cloud estimate). Computed once per row.
        src_rec = lookup_cloud_shape(row_context(row, fields))

        # Best Match (auto) maps each row to the best shape of its detected CPU vendor;
        # otherwise every row uses the selected shape. Determined up front so OCPU
        # rounding can respect the shape's granularity rules.
        row_shape = selected_shape
        if auto:
            _v = (src_rec or {}).get("ociVendor")
            if _v in BEST_SHAPE_BY_VENDOR:
                if auto_tier == "top":
                    # Top of the line: newest OCI shape for the vendor (E6 Ax / X12 Ax / A4 Ax).
                    row_shape = SHAPE_LOOKUP[BEST_SHAPE_BY_VENDOR[_v]]
                else:
                    # Best match: equivalent-generation OCI shape for the source instance.
                    _prov = (src_rec or {}).get("provider")
                    _gen = _instance_generation(_prov, (src_rec or {}).get("instance"))
                    _key = equivalent_gen_shape_key(_prov, _v, _gen)
                    row_shape = SHAPE_LOOKUP.get(_key, SHAPE_LOOKUP[BEST_SHAPE_BY_VENDOR[_v]])
            # Bare metal is never an automatic mapping target - a whole physical server is a
            # deliberate choice, not a default. It's still SUGGESTED on the row when the sizing
            # overflows every flex shape (see oci_size_check), and remains selectable by hand.
            if row_shape.get("bareMetal"):
                row_shape = SHAPE_LOOKUP[BEST_SHAPE_BY_VENDOR.get(_v) or DEFAULT_SHAPE_KEY]
        # A per-row override (set from the editable results table) wins over everything.
        _override = (shape_overrides or {}).get(str(row.get("__id")))
        if _override and _override in SHAPE_LOOKUP:
            row_shape = SHAPE_LOOKUP[_override]
        row_shape_key = row_shape.get("key")

        # Rightsize plan: generation-gap OCPU/RAM reduction for this row's shape.
        rs_plan = rightsize_plan(row_shape_key, src_rec) if rightsize else None

        if cloud_bill_mode:
            raw_ocpu = value_for(row, "resource_ocpus")
            raw_mem = value_for(row, "resource_memory_gb")
            # Bills rarely carry vCPU/RAM. If the row names an EC2 instance type, fill
            # the missing sizing from the AWS Price List API so it can map/price on page 4.
            if (not raw_ocpu or not raw_mem) and aws_pricing.available():
                inst_type = extract_aws_instance_type(row)
                if inst_type:
                    specs = aws_pricing.instance_specs(inst_type, clean_text(row.get("source_region")))
                    if specs:
                        if not raw_ocpu and specs.get("vcpu"):
                            raw_ocpu = specs["vcpu"] / 2  # 2 vCPU = 1 OCPU
                        if not raw_mem and specs.get("memoryGb"):
                            raw_mem = specs["memoryGb"]
                        row["_apiInstanceType"] = inst_type
            app_vm_ocpu = round_ocpu_for_shape(raw_ocpu, row_shape_key)
            app_vm_mem = math.floor(raw_mem) if raw_mem else 0.0
            original_ocpus = app_vm_ocpu
            original_memory_gb = app_vm_mem
            app_vm_ocpu, app_vm_mem = _apply_rightsize(app_vm_ocpu, app_vm_mem, rs_plan)
            # OCPU can never exceed RAM: RAM (GB) is floored at the OCPU count.
            app_vm_mem = max(app_vm_mem, app_vm_ocpu)
            db_ocpus = 0.0
            # Rows always carry their REAL demand. Bare metal's indivisible-box rounding is
            # applied once across the whole estate (see _apply_bare_metal_packing), because one
            # physical server is shared by many workloads rather than dedicated to each row.
            ocpus, memory_gb = app_vm_ocpu, app_vm_mem
        else:
            # OCPU: 2 vCPU = 1 OCPU, rounded per-VM; RAM floored like the BOM script.
            # Rightsize (gen-gap) reduction is then applied per VM before aggregating.
            app_vm_ocpu = round_ocpu_for_shape(ocpus_for_review_value(fields, keys["app_cpu"], app_cpu), row_shape_key) if app_cpu else 0.0
            db_vm_ocpu = round_ocpu_for_shape(ocpus_for_review_value(fields, keys["db_cpu"], db_cpu), row_shape_key) if db_cpu else 0.0
            app_vm_mem = math.floor(app_memory) if app_memory else 0.0
            db_vm_mem = math.floor(db_memory) if db_memory else 0.0
            original_ocpus = (app_servers * app_vm_ocpu if app_servers else 0.0) + (db_servers * db_vm_ocpu if db_servers else 0.0)
            original_memory_gb = (app_servers * app_vm_mem if app_servers else 0.0) + (db_servers * db_vm_mem if db_servers else 0.0)
            app_vm_ocpu, app_vm_mem = _apply_rightsize(app_vm_ocpu, app_vm_mem, rs_plan)
            db_vm_ocpu, db_vm_mem = _apply_rightsize(db_vm_ocpu, db_vm_mem, rs_plan)
            # OCPU can never exceed RAM: floor each VM's RAM (GB) at its OCPU count.
            app_vm_mem = max(app_vm_mem, app_vm_ocpu)
            db_vm_mem = max(db_vm_mem, db_vm_ocpu)
            app_ocpus = app_servers * app_vm_ocpu if app_servers else 0.0
            db_ocpus = db_servers * db_vm_ocpu if db_servers else 0.0
            ocpus = app_ocpus + db_ocpus
            memory_gb = (app_servers * app_vm_mem if app_servers else 0.0) + (db_servers * db_vm_mem if db_servers else 0.0)

        # Per-server running hours: use an "hours running"/"hours per month" column if present,
        # otherwise the global hours setting (default 730).
        # Hours come from the data source per row (an "Hours/month" column) UNLESS the user
        # overrode the global hours field - then the override wins for every row.
        row_hours = value_for(row, keys["hours"]) if (keys.get("hours") and not hours_override) else 0
        row_hours = row_hours if row_hours and row_hours > 0 else eff_hours

        local_storage_multiplier = 1.0 if storage_field_is_row_total(fields, keys["app_local_storage"]) else app_servers
        shared_storage_multiplier = 1.0 if storage_field_is_row_total(fields, keys["app_shared_storage"]) else app_servers
        block_storage_gb = (local_storage_multiplier * app_local_storage) + db_storage
        file_storage_gb = shared_storage_multiplier * app_shared_storage

        line_items = []
        if not cloud_bill_mode:
            append_compute_memory_items(
                line_items,
                ocpus,
                memory_gb,
                f"Spreadsheet CPU values are assumed to be vCPUs, shown in review as OCPUs using 2 vCPUs = 1 OCPU, then multiplied by {row_hours:g} monthly hours.",
                row_hours,
                shape=row_shape,
            )
            # Windows OS license: OS recognition scans the row; Windows rows are licensed per OCPU-hour
            # (1 license per OCPU = 1 per 2 vCPUs). Skipped when Windows pricing is toggled off.
            if ocpus and not hide_windows_pricing and row_operating_system(row) == "windows":
                win_rc = rate(WINDOWS_LICENSE_SKU, rate_card)
                win_qty = ocpus * row_hours
                line_items.append(
                    {
                        "sku": win_rc["sku"],
                        "description": win_rc["description"],
                        "quantity": round(win_qty, 4),
                        "unit": win_rc["unit"],
                        "rate": win_rc["rate"],
                        "monthly": money(win_qty * win_rc["rate"]),
                        "mapping": f"Row detected as Windows; Windows OS licensing applied at OCPU-hours x {row_hours:g}.",
                    }
                )
        if block_storage_gb:
            rc = rate("B91961", rate_card)
            line_items.append(
                {
                    "sku": rc["sku"],
                    "description": rc["description"],
                    "quantity": round(block_storage_gb, 4),
                    "unit": rc["unit"],
                    "rate": rc["rate"],
                    "monthly": money(block_storage_gb * rc["rate"]),
                    "mapping": "Local VM storage and database allocated storage map to block volume GB-months.",
                }
            )
            # Block volume performance units: 10 units per GB of block storage (BOM script Balanced tier).
            perf_rc = rate("B91962", rate_card)
            perf_qty = BLOCK_PERFORMANCE_UNITS_PER_GB * block_storage_gb
            line_items.append(
                {
                    "sku": perf_rc["sku"],
                    "description": perf_rc["description"],
                    "quantity": round(perf_qty, 4),
                    "unit": perf_rc["unit"],
                    "rate": perf_rc["rate"],
                    "monthly": money(perf_qty * perf_rc["rate"]),
                    "mapping": "Block volume performance units = 10 x block storage GB (Balanced performance).",
                }
            )
        if file_storage_gb:
            rc = rate("B89057", rate_card)
            line_items.append(
                {
                    "sku": rc["sku"],
                    "description": rc["description"],
                    "quantity": round(file_storage_gb, 4),
                    "unit": rc["unit"],
                    "rate": rc["rate"],
                    "monthly": money(file_storage_gb * rc["rate"]),
                    "mapping": "Shared storage maps to file storage GB-months.",
                }
            )

        full_service_mapping = None
        full_service_notes = []
        row_free_on_oci = cloud_bill_mode and is_free_oci_service(row)
        if row_free_on_oci:
            # Free on OCI (VPC, CloudTrail, Support, Savings Plans) - $0 OCI cost, but the
            # AWS spend still counts toward the source total.
            row["oci_product"] = clean_text(row.get("oci_product")) or "Included (free on OCI)"
            row["mapping_confidence"] = row.get("mapping_confidence") or "Included (free)"
            sc = to_number(row.get("source_monthly_cost"), 0)
            totals["sourceMonthlyCost"] += sc
            totals["mappedSourceMonthlyCost"] += sc
            totals["mappedServiceRows"] += 1

        # AWS RDS / Aurora -> OCI Base Database (DBaaS). Priced here (before the
        # generic storage/catalog fallback) so RDS rows are priced as a database
        # service and never relabeled/priced as "Block Volume Storage".
        rds_handled = False
        if cloud_bill_mode and not row_free_on_oci:
            rds_result = price_rds_row(row, rds_sql_server_instances)
            if rds_result is not None:
                rds_items, rds_label, rds_carried, rds_flag = rds_result
                if rds_flag:
                    row["_sqlMappingFlag"] = rds_flag
                line_items.extend(rds_items)
                row["oci_product"] = rds_label
                row["oci_service_category"] = "Database"
                _rds_src = to_number(row.get("source_monthly_cost"), 0)
                full_service_mapping = {
                    "sku": OCI_BASE_DB_OCPU_SKU,
                    "ociProduct": rds_label,
                    "sourceProvider": clean_text(row.get("source_provider")),
                    "sourceService": clean_text(row.get("source_service")),
                    "sourceProduct": clean_text(row.get("source_product")),
                    "sourceMonthlyCost": money(_rds_src),
                    "sourceCurrency": clean_text(row.get("source_currency")) or "USD",
                    "quantity": round(to_number(row.get("usage_quantity"), 0), 4),
                    "unit": clean_text(row.get("usage_unit")) or (rds_items[0].get("unit") if rds_items else ""),
                    "confidence": 0.85,
                    "reviewRequired": False,
                }
                if rds_carried:
                    full_service_notes.append("OCI cost set equal to the source AWS cost (no managed OCI equivalent / bundled charge).")
                totals["sourceMonthlyCost"] += _rds_src
                totals["mappedSourceMonthlyCost"] += _rds_src
                totals["mappedServiceRows"] += 1
                totals["fullServiceMonthly"] += sum(li.get("monthly", 0) for li in rds_items)
                rds_handled = True

        # AWS networking services -> OCI networking products. Priced here (before the
        # generic storage/catalog fallback) so each ELB / Direct Connect / Transfer
        # Family line is priced once on its own usage and never relabeled as Storage
        # or Outbound Data Transfer.
        # AWS SQS / SNS / Transfer Family -> Oracle Integration Cloud. Runs before the
        # networking handler so Transfer Family maps to OIC (not the SFTPGo default),
        # while the SFTPGo option is still described in the OIC mapping note. The whole
        # workload is one message pack, charged once on the anchor row.
        oic_handled = False
        if cloud_bill_mode and not row_free_on_oci and not rds_handled and _is_oic_row(row):
            oic_items, oic_label, oic_cat, oic_sku, oic_carried = price_oic_row(row, oic_anchor_row_id, oic_effective_packs)
            line_items.extend(oic_items)
            row["oci_product"] = oic_label
            row["oci_service_category"] = oic_cat
            _oic_src = to_number(row.get("source_monthly_cost"), 0)
            full_service_mapping = {
                "sku": oic_sku,
                "ociProduct": oic_label,
                "sourceProvider": clean_text(row.get("source_provider")),
                "sourceService": clean_text(row.get("source_service")),
                "sourceProduct": clean_text(row.get("source_product")),
                "sourceMonthlyCost": money(_oic_src),
                "sourceCurrency": clean_text(row.get("source_currency")) or "USD",
                "quantity": round(to_number(row.get("usage_quantity"), 0), 4),
                "unit": clean_text(row.get("usage_unit")) or (oic_items[0].get("unit") if oic_items else ""),
                "confidence": 0.9,
                "reviewRequired": False,
            }
            totals["sourceMonthlyCost"] += _oic_src
            totals["mappedSourceMonthlyCost"] += _oic_src
            totals["mappedServiceRows"] += 1
            totals["fullServiceMonthly"] += sum(li.get("monthly", 0) for li in oic_items)
            oic_handled = True

        networking_handled = False
        if cloud_bill_mode and not row_free_on_oci and not rds_handled and not oic_handled:
            net_result = price_networking_row(row, elb_free_tier_row_id)
            if net_result is not None:
                net_items, net_label, net_category, net_sku, net_carried = net_result
                line_items.extend(net_items)
                if net_label:
                    row["oci_product"] = net_label
                if net_category:
                    row["oci_service_category"] = net_category
                _net_src = to_number(row.get("source_monthly_cost"), 0)
                full_service_mapping = {
                    "sku": net_sku,
                    "ociProduct": net_label or clean_text(row.get("oci_product")),
                    "sourceProvider": clean_text(row.get("source_provider")),
                    "sourceService": clean_text(row.get("source_service")),
                    "sourceProduct": clean_text(row.get("source_product")),
                    "sourceMonthlyCost": money(_net_src),
                    "sourceCurrency": clean_text(row.get("source_currency")) or "USD",
                    "quantity": round(to_number(row.get("usage_quantity"), 0), 4),
                    "unit": clean_text(row.get("usage_unit")) or (net_items[0].get("unit") if net_items else ""),
                    "confidence": 0.9,
                    "reviewRequired": False,
                }
                if net_carried:
                    full_service_notes.append("OCI cost set equal to the source AWS cost (no native OCI equivalent / carried).")
                totals["sourceMonthlyCost"] += _net_src
                totals["mappedSourceMonthlyCost"] += _net_src
                totals["mappedServiceRows"] += 1
                totals["fullServiceMonthly"] += sum(li.get("monthly", 0) for li in net_items)
                networking_handled = True

        # Amazon Redshift -> Oracle Autonomous Data Warehouse (ADW): price the
        # Serverless compute (ECPU) + managed storage here so it isn't reduced to
        # block storage only by the generic fallback.
        redshift_handled = False
        if cloud_bill_mode and not row_free_on_oci and not rds_handled and not networking_handled and _is_redshift_row(row):
            rs_result = price_redshift_row(row, redshift_compute_ctx)
            if rs_result is not None:
                rs_items, rs_label, rs_cat, rs_sku, rs_carried = rs_result
                line_items.extend(rs_items)
                row["oci_product"] = rs_label
                row["oci_service_category"] = rs_cat
                _rs_src = to_number(row.get("source_monthly_cost"), 0)
                full_service_mapping = {
                    "sku": rs_sku,
                    "ociProduct": rs_label,
                    "sourceProvider": clean_text(row.get("source_provider")),
                    "sourceService": clean_text(row.get("source_service")),
                    "sourceProduct": clean_text(row.get("source_product")),
                    "sourceMonthlyCost": money(_rs_src),
                    "sourceCurrency": clean_text(row.get("source_currency")) or "USD",
                    "quantity": round(to_number(row.get("usage_quantity"), 0), 4),
                    "unit": clean_text(row.get("usage_unit")) or (rs_items[0].get("unit") if rs_items else ""),
                    "confidence": 0.85,
                    "reviewRequired": False,
                }
                totals["sourceMonthlyCost"] += _rs_src
                totals["mappedSourceMonthlyCost"] += _rs_src
                totals["mappedServiceRows"] += 1
                totals["fullServiceMonthly"] += sum(li.get("monthly", 0) for li in rs_items)
                redshift_handled = True

        # AWS WAF -> OCI Web Application Firewall: instances ($5/mo after first free),
        # requests ($0.60/1M after 10M free), rules/bot management bundled.
        waf_handled = False
        if cloud_bill_mode and not row_free_on_oci and not rds_handled and not networking_handled and not redshift_handled:
            waf_result = price_waf_row(row, oci_waf_instance_pool, oci_waf_request_pool)
            if waf_result is not None:
                waf_items, waf_label, waf_cat, waf_sku, waf_carried = waf_result
                line_items.extend(waf_items)
                row["oci_product"] = waf_label
                row["oci_service_category"] = waf_cat
                _waf_src = to_number(row.get("source_monthly_cost"), 0)
                full_service_mapping = {
                    "sku": waf_sku,
                    "ociProduct": waf_label,
                    "sourceProvider": clean_text(row.get("source_provider")),
                    "sourceService": clean_text(row.get("source_service")),
                    "sourceProduct": clean_text(row.get("source_product")),
                    "sourceMonthlyCost": money(_waf_src),
                    "sourceCurrency": clean_text(row.get("source_currency")) or "USD",
                    "quantity": round(to_number(row.get("usage_quantity"), 0), 4),
                    "unit": clean_text(row.get("usage_unit")) or (waf_items[0].get("unit") if waf_items else ""),
                    "confidence": 0.9,
                    "reviewRequired": False,
                }
                totals["sourceMonthlyCost"] += _waf_src
                totals["mappedSourceMonthlyCost"] += _waf_src
                totals["mappedServiceRows"] += 1
                totals["fullServiceMonthly"] += sum(li.get("monthly", 0) for li in waf_items)
                waf_handled = True

        # CloudWatch Logs -> OCI Logging ($0.05/GB after 10 GB/month free). Route 53
        # -> OCI DNS ($0.85/1M queries; zones free), flagged as a non-ideal mapping.
        obs_handled = False
        if cloud_bill_mode and not row_free_on_oci and not rds_handled and not networking_handled and not redshift_handled and not waf_handled:
            obs_result = price_cloudwatch_logs_row(row, oci_logging_pool) or price_route53_row(row)
            if obs_result is not None:
                obs_items, obs_label, obs_cat, obs_sku, obs_carried = obs_result
                line_items.extend(obs_items)
                row["oci_product"] = obs_label
                row["oci_service_category"] = obs_cat
                _obs_src = to_number(row.get("source_monthly_cost"), 0)
                full_service_mapping = {
                    "sku": obs_sku,
                    "ociProduct": obs_label,
                    "sourceProvider": clean_text(row.get("source_provider")),
                    "sourceService": clean_text(row.get("source_service")),
                    "sourceProduct": clean_text(row.get("source_product")),
                    "sourceMonthlyCost": money(_obs_src),
                    "sourceCurrency": clean_text(row.get("source_currency")) or "USD",
                    "quantity": round(to_number(row.get("usage_quantity"), 0), 4),
                    "unit": clean_text(row.get("usage_unit")) or (obs_items[0].get("unit") if obs_items else ""),
                    "confidence": 0.9,
                    "reviewRequired": False,
                }
                totals["sourceMonthlyCost"] += _obs_src
                totals["mappedSourceMonthlyCost"] += _obs_src
                totals["mappedServiceRows"] += 1
                totals["fullServiceMonthly"] += sum(li.get("monthly", 0) for li in obs_items)
                obs_handled = True

        # AWS WorkSpaces -> OCI Secure Desktops: full stack (desktop fee + underlying
        # E6 compute + boot volume), sized from the WorkSpaces bundle specs, matching
        # the Secure Desktops add-in instead of a flat $20/desktop.
        ws_handled = False
        if cloud_bill_mode and not row_free_on_oci and not rds_handled and not networking_handled and not redshift_handled and not waf_handled and not obs_handled:
            ws_result = price_workspaces_row(row)
            if ws_result is not None:
                ws_items, ws_label, ws_cat, ws_sku, ws_carried = ws_result
                line_items.extend(ws_items)
                row["oci_product"] = ws_label
                row["oci_service_category"] = ws_cat
                _ws_src = to_number(row.get("source_monthly_cost"), 0)
                full_service_mapping = {
                    "sku": ws_sku,
                    "ociProduct": ws_label,
                    "sourceProvider": clean_text(row.get("source_provider")),
                    "sourceService": clean_text(row.get("source_service")),
                    "sourceProduct": clean_text(row.get("source_product")),
                    "sourceMonthlyCost": money(_ws_src),
                    "sourceCurrency": clean_text(row.get("source_currency")) or "USD",
                    "quantity": round(to_number(row.get("usage_quantity"), 0), 4),
                    "unit": clean_text(row.get("usage_unit")) or (ws_items[0].get("unit") if ws_items else ""),
                    "confidence": 0.9,
                    "reviewRequired": False,
                }
                totals["sourceMonthlyCost"] += _ws_src
                totals["mappedSourceMonthlyCost"] += _ws_src
                totals["mappedServiceRows"] += 1
                totals["fullServiceMonthly"] += sum(li.get("monthly", 0) for li in ws_items)
                ws_handled = True

        # AWS AppStream 2.0 -> OCI Secure Desktops (same target as WorkSpaces): user seats ->
        # Secure Desktop fee, streaming hours -> underlying E6 compute. Prices instead of
        # carrying the AppStream lines at billed cost.
        if cloud_bill_mode and not row_free_on_oci and not ws_handled:
            as_result = price_appstream_row(row)
            if as_result is not None:
                as_items, as_label, as_cat, as_sku, _as_carried = as_result
                line_items.extend(as_items)
                row["oci_product"] = as_label
                row["oci_service_category"] = as_cat
                _as_src = to_number(row.get("source_monthly_cost"), 0)
                full_service_mapping = {
                    "sku": as_sku,
                    "ociProduct": as_label,
                    "sourceProvider": clean_text(row.get("source_provider")),
                    "sourceService": clean_text(row.get("source_service")),
                    "sourceProduct": clean_text(row.get("source_product")),
                    "sourceMonthlyCost": money(_as_src),
                    "sourceCurrency": clean_text(row.get("source_currency")) or "USD",
                    "quantity": round(to_number(row.get("usage_quantity"), 0), 4),
                    "unit": clean_text(row.get("usage_unit")) or (as_items[0].get("unit") if as_items else ""),
                    "confidence": 0.85,
                    "reviewRequired": False,
                }
                totals["sourceMonthlyCost"] += _as_src
                totals["mappedSourceMonthlyCost"] += _as_src
                totals["mappedServiceRows"] += 1
                totals["fullServiceMonthly"] += sum(li.get("monthly", 0) for li in as_items)
                ws_handled = True   # reuse the Secure-Desktops handled flag downstream

        # Azure 'Virtual Machines Licenses' rows are SQL Server / Windows licenses, not
        # compute - price them as licensing so they don't match the generic Compute
        # catalog item and get a bogus inferred-OCPU line.
        azlic_handled = False
        if cloud_bill_mode and not row_free_on_oci and not ws_handled:
            az_result = price_azure_vm_license_row(row)
            if az_result is not None:
                az_items, az_label, az_cat, az_sku, _az_carried = az_result
                line_items.extend(az_items)
                row["oci_product"] = az_label
                row["oci_service_category"] = az_cat
                _az_src = to_number(row.get("source_monthly_cost"), 0)
                full_service_mapping = {
                    "sku": az_sku, "ociProduct": az_label,
                    "sourceProvider": clean_text(row.get("source_provider")),
                    "sourceService": clean_text(row.get("source_service")),
                    "sourceProduct": clean_text(row.get("source_product")),
                    "sourceMonthlyCost": money(_az_src),
                    "sourceCurrency": clean_text(row.get("source_currency")) or "USD",
                    "quantity": round(to_number(row.get("usage_quantity"), 0), 4),
                    "unit": clean_text(row.get("usage_unit")) or (az_items[0].get("unit") if az_items else ""),
                    "confidence": 0.9, "reviewRequired": False,
                }
                totals["sourceMonthlyCost"] += _az_src
                totals["mappedSourceMonthlyCost"] += _az_src
                totals["mappedServiceRows"] += 1
                totals["fullServiceMonthly"] += sum(li.get("monthly", 0) for li in az_items)
                azlic_handled = True

        # Azure disk/blob operation (transaction) meters -> Block Volume I/O (free) or
        # Object Requests, not block-volume GB.
        azops_handled = False
        if cloud_bill_mode and not row_free_on_oci and not ws_handled and not azlic_handled:
            ops_result = price_azure_storage_ops_row(row)
            if ops_result is not None:
                ops_items, ops_label, ops_cat, ops_sku, _oc = ops_result
                line_items.extend(ops_items)
                row["oci_product"] = ops_label
                row["oci_service_category"] = ops_cat
                _ops_src = to_number(row.get("source_monthly_cost"), 0)
                full_service_mapping = {
                    "sku": ops_sku, "ociProduct": ops_label,
                    "sourceProvider": clean_text(row.get("source_provider")),
                    "sourceService": clean_text(row.get("source_service")),
                    "sourceProduct": clean_text(row.get("source_product")),
                    "sourceMonthlyCost": money(_ops_src),
                    "sourceCurrency": clean_text(row.get("source_currency")) or "USD",
                    "quantity": round(to_number(row.get("usage_quantity"), 0), 4),
                    "unit": clean_text(row.get("usage_unit")) or (ops_items[0].get("unit") if ops_items else ""),
                    "confidence": 0.9, "reviewRequired": False,
                }
                totals["sourceMonthlyCost"] += _ops_src
                totals["mappedSourceMonthlyCost"] += _ops_src
                totals["mappedServiceRows"] += 1
                totals["fullServiceMonthly"] += sum(li.get("monthly", 0) for li in ops_items)
                azops_handled = True

        # Authoritative storage pricing: a storage service maps to ONE OCI storage
        # product at its real rate (S3->Object Storage, EFS/FSx->File Storage,
        # EBS->Block Volume), priced on the actual storage-capacity quantity from the
        # bill - not scattered across catalog items per line.
        storage_handled = oic_handled or rds_handled or networking_handled or redshift_handled or waf_handled or obs_handled or ws_handled or azlic_handled or azops_handled
        if cloud_bill_mode and not row_free_on_oci and not oic_handled and not rds_handled and not networking_handled and not redshift_handled and not waf_handled and not obs_handled and not ws_handled and not azlic_handled and not azops_handled:
            ut2 = normalize(row.get("__usageType"))
            base = clean_text(row.get("oci_product")).split(" (approx")[0].strip()
            # EBS volumes/snapshots bill under EC2 - route them to Block Volume.
            if base not in OCI_SERVICE_PRICES and any(k in ut2 for k in ["volumeusage", "ebs:", "snapshotusage", "ebsoptimized"]):
                if "ebsoptimized" not in ut2:  # EBS-optimized throughput is bundled/free on OCI
                    base = "OCI Block Volumes"
                    row["oci_product"] = base
                    row["oci_service_category"] = "Storage"
            svc_price = OCI_SERVICE_PRICES.get(base)
            if svc_price and svc_price.get("basis") == "storage":
                storage_meter_text = normalize(
                    " ".join(
                        clean_text(row.get(key))
                        for key in ["__usageType", "__meterName", "source_product", "usage_unit"]
                    )
                )
                ia_retrieval = (
                    base == "OCI Infrequent Access Storage"
                    and context_has_any(storage_meter_text, ["retrieval", "retrieved", "restore bytes", "restorebyte"])
                )
                is_capacity = (
                    any(k in ut2 for k in ["timedstorage", "bytehrs", "volumeusage", "snapshotusage", "storage"])
                    or normalize(row.get("usage_unit")) in ("gb", "gib", "gbmonth", "gbmo", "gigabyte", "gigabytemonth")
                    # Backup capacity meters (e.g. FSx "BackupUsage", billed per GB-month) name
                    # neither "storage" nor a unit, so 72,838 GB of Windows File Server backup
                    # went unpriced. Require the meter to be GB-based so per-request or
                    # per-operation backup meters aren't priced as capacity.
                    or ("backup" in ut2 and "gb" in storage_meter_text)
                    or not ut2
                ) and not ia_retrieval
                if ia_retrieval:
                    retrieval_qty = to_number(row.get("usage_quantity"), 0)
                    retrieval_rate = float(svc_price.get("retrievalRate") or 0)
                    retrieval_free = float(svc_price.get("freeRetrievalGb") or 0)
                    line_items.append({
                        "sku": svc_price.get("retrievalSku", "B93001"),
                        "description": "OCI Infrequent Access Storage - Data Retrieval",
                        "quantity": round(retrieval_qty, 4),
                        "unit": "GB retrieved",
                        "rate": retrieval_rate,
                        "monthly": money(max(0.0, retrieval_qty - retrieval_free) * retrieval_rate),
                        "mapping": (
                            "Infrequent Access retrieval re-priced at "
                            f"${retrieval_rate}/GB after the first {retrieval_free:g} GB/month."
                        ),
                        "ociServiceUsage": True,
                    })
                elif is_capacity:
                    _perf = block_perf_units_per_gb(row.get("__usageType"), row.get("source_product"), row.get("__meterName"))
                    line_items.extend(oci_service_usage_items(base, row.get("usage_quantity"), oci_transfer_pools, row.get("source_region"), row.get("__usageType"), _perf))
                sc = to_number(row.get("source_monthly_cost"), 0)
                # Label every line of this service with its single OCI product so the
                # results page always shows e.g. S3 -> OCI Object Storage.
                # Surface the billed usage so the Usage column isn't blank for
                # storage/transfer lines (these aren't block/file GB on specs).
                _usage_qty = to_number(row.get("usage_quantity"), 0)
                if _usage_qty <= 0:
                    _usage_qty = sum(
                        to_number(li.get("quantity"), 0)
                        for li in line_items
                        if "performance" not in str(li.get("unit", "")).lower()
                    )
                _usage_unit = clean_text(row.get("usage_unit")) or svc_price.get("unit", "")
                full_service_mapping = {
                    "sku": (
                        svc_price.get("retrievalSku", "B93001")
                        if ia_retrieval
                        else svc_price.get("sku", "")
                    ),
                    "ociProduct": base,
                    "sourceProvider": clean_text(row.get("source_provider")),
                    "sourceService": clean_text(row.get("source_service")),
                    "sourceProduct": clean_text(row.get("source_product")),
                    "sourceMonthlyCost": money(sc),
                    "sourceCurrency": clean_text(row.get("source_currency")) or "USD",
                    "quantity": round(_usage_qty, 4) if _usage_qty else 0,
                    "unit": _usage_unit,
                    "confidence": 0.9,
                    "reviewRequired": False,
                }
                totals["sourceMonthlyCost"] += sc
                totals["mappedSourceMonthlyCost"] += sc
                totals["mappedServiceRows"] += 1
                totals["fullServiceMonthly"] += sum(li.get("monthly", 0) for li in line_items)
                # Surface storage capacity (GB) for the Storage KPI, which is defined
                # as Block Volume + File Storage only (to match the label and the
                # on-prem KPI). Object/Archive storage is excluded, and request/API
                # charges (non-capacity) are excluded too.
                _base_l = base.lower()
                if (is_capacity and svc_price.get("unit", "").lower().startswith(("gb", "gib"))
                        and ("block volume" in _base_l or "file storage" in _base_l)):
                    totals["cloudStorageGb"] += _usage_qty
                storage_handled = True

        if service_catalog_enabled and not row_free_on_oci and not storage_handled:
            service_items, full_service_mapping, full_service_notes = full_service_line_items(row, fields, rate_card)
            line_items.extend(service_items)
            fallback_start = len(line_items)
            service_units = {item.get("unit") for item in service_items}
            allow_compute_memory_fallback = not service_items or bool(service_units & {"OCPU-hour", "GB-hour"})
            usage_hours = cloud_usage_hours(row, fields) if cloud_bill_mode else HOURS_PER_MONTH
            if cloud_bill_mode and allow_compute_memory_fallback and (ocpus or memory_gb):
                existing_units = {item.get("unit") for item in line_items}
                append_compute_memory_items(
                    line_items,
                    0 if "OCPU-hour" in existing_units else ocpus,
                    0 if "GB-hour" in existing_units else memory_gb,
                    "Cloud bill CPU/vCPU usage was normalized to OCPUs and priced using source usage hours when present.",
                    usage_hours,
                    shape=row_shape,
                )
                # EC2 "Windows with SQL" instances: ADD a Microsoft SQL Server
                # license-included line (license-included on OCI) on top of compute.
                _ec2_blob = normalize(" ".join(clean_text(row.get(k)) for k in
                                               ["source_product", "__usageType", "source_service"]))
                if "with sql" in _ec2_blob and ocpus:
                    _sql_vcpus = ocpus * 2  # OCI flex shape OCPU x 2 = vCPU
                    _sql_ocpu = sql_server_ocpu(_sql_vcpus)
                    _sql_rate = sql_license_rate(row.get("source_product"))
                    _sql_edition = "Enterprise" if _sql_rate == OCI_SQL_LICENSE_ENT_RATE else "Standard"
                    _sql_hours = usage_hours if usage_hours and usage_hours > 0 else HOURS_PER_MONTH
                    _sql_qty = _sql_ocpu * _sql_hours
                    line_items.append({
                        "sku": "",
                        "description": f"Microsoft SQL Server license-included ({_sql_ocpu} OCPU)",
                        "quantity": round(_sql_qty, 4),
                        "unit": "OCPU-hour",
                        "rate": _sql_rate,
                        "monthly": money(_sql_qty * _sql_rate),
                        "mapping": f"EC2 Windows with SQL ({_sql_edition}): {_sql_ocpu} OCPU (vCPU {_sql_vcpus:g}/2 floored to even, min 2) x {_sql_hours:,.0f} hours x ${_sql_rate}/OCPU-hour license-included on OCI. {SQL_NO_MANAGED_NOTE}",
                    })
                    row["_sqlMappingFlag"] = "SQL Server - license-included (review)"
            # Re-price the source usage on the mapped OCI service (same quantity x OCI
            # rate) when nothing else priced this row (e.g. S3 GB -> OCI Object Storage GB).
            if cloud_bill_mode and sum(li.get("monthly", 0) for li in line_items) == 0:
                _perf2 = block_perf_units_per_gb(row.get("__usageType"), row.get("source_product"))
                line_items.extend(oci_service_usage_items(row.get("oci_product"), row.get("usage_quantity"), oci_transfer_pools, row.get("source_region"), row.get("__usageType"), _perf2))
            fallback_items = line_items[fallback_start:]
            source_cost_value = full_service_mapping.get("sourceMonthlyCost", 0) if full_service_mapping else 0
            totals["sourceMonthlyCost"] += source_cost_value
            if service_items or fallback_items:
                totals["fullServiceMonthly"] += sum(item["monthly"] for item in [*service_items, *fallback_items])
                totals["mappedServiceRows"] += 1
                totals["mappedSourceMonthlyCost"] += source_cost_value
            elif full_service_mapping:
                totals["unpricedServiceRows"] += 1
                totals["unmappedSourceMonthlyCost"] += source_cost_value

        gpu_info = gpu_pricing_for_context(row_context(row, fields)) if cloud_bill_mode else None
        # A bill carries non-instance ATTRIBUTE lines that merely name the instance type - e.g.
        # "$0.00 for 800 Mbps per g4ad.2xlarge" is a network-bandwidth entitlement, not GPU
        # instance-hours. Those matched the GPU shape by name and each booked a whole month of
        # OCI GPU, inventing cost against a $0 source line. Only price GPU on real instance lines.
        if gpu_info and _is_bandwidth_attribute_line(row, fields):
            gpu_info = None
        if gpu_info:
            # OCI GPU bare-metal pricing replaces flex OCPU/memory (the host is bundled in the GPU price).
            line_items = [li for li in line_items if li.get("unit") not in {"OCPU-hour", "GB-hour"}]
            price = gpu_info.get("pricePerGpuHour")
            count = gpu_info.get("gpuCount") or 0
            gpu_desc = f"GPU - {gpu_info.get('gpuModel')} ({gpu_info['shape']})"
            # GPU compute rolls up under AI & Machine Learning, not Compute.
            row["oci_service_category"] = "AI & Machine Learning"
            row["oci_product"] = gpu_desc
            if price and count and not hide_gpu_pricing:
                # Bill on the line's ACTUAL metered hours, not a flat 730. A bill lists a GPU VM
                # as many partial-period lines, so a fixed month each multiplied the cost.
                _gpu_hours = cloud_usage_hours(row, fields) or 0
                if _gpu_hours <= 0:
                    _gpu_hours = GPU_HOURS_PER_MONTH
                qty = count * _gpu_hours
                line_items.append({
                    "sku": gpu_info["shape"],
                    "description": gpu_desc,
                    "quantity": round(qty, 4),
                    "unit": "GPU-hour",
                    "rate": price,
                    "monthly": money(qty * price),
                    "mapping": f"Mapped to OCI GPU shape {gpu_info['shape']} ({count}x {gpu_info.get('gpuModel')}); priced per GPU-hour x 730.",
                    "isGpu": True,
                })
            elif hide_gpu_pricing:
                line_items.append({
                    "sku": gpu_info["shape"], "description": gpu_desc, "quantity": 0,
                    "unit": "GPU-hour", "rate": price or 0, "monthly": 0.0,
                    "mapping": "GPU pricing hidden by toggle.", "isGpu": True, "gpuHidden": True,
                })

        # Carried lines keep the source cost (conservative), but they are the SOURCE figure,
        # not an OCI-rate calculation - they can never show a saving and they pull the OCI total
        # toward the source total. Tag the amount so the estimate can disclose how much of
        # itself is carried rather than priced.
        for _li in (line_items or []):
            if (_li.get("carriedOver") or "carried over" in normalize(str(_li.get("description") or ""))) \
                    and not _li.get("carriedSourceMonthly"):
                _li["carriedSourceMonthly"] = float(_li.get("monthly") or 0)

        monthly = money(sum(item["monthly"] for item in line_items))
        annual = money(monthly * 12)
        _src_cost = to_number(row.get("source_monthly_cost"), 0)
        _prod_l = (clean_text(row.get("oci_product")) or "").lower()
        _is_object_storage = "object storage" in _prod_l
        _is_approx_map = bool(clean_text(row.get("mapping_note")))  # FSx / WorkSpaces etc.
        _user_action = (cost_overrides or {}).get(str(row.get("__id"))) if cost_overrides else None

        # Auto-carry recognized services that have no clean OCI price (they came out
        # $0): carry the AWS cost over and flag as an imperfect mapping, matching the
        # reference's carry methodology (e.g. AppStream, Route 53, WAF, Glue).
        # A row that was already priced on a known OCI service (storage, networking,
        # RDS, Redshift, or a re-priced usage line) is intentionally $0 when it lands
        # inside a free allowance - e.g. Data Transfer within the 10 TB/region free
        # pool, or a free networking tier. Those must NOT be carried at the AWS cost.
        _priced_on_oci_service = bool(storage_handled) or any(
            li.get("ociServiceUsage") for li in line_items)
        _auto_carried = False
        # Provisioned-throughput meters have no OCI counterpart: throughput comes from the
        # shape the storage appliance runs on, which is already priced. Record $0 with the
        # reason instead of carrying the source charge on top of hardware we've already costed.
        if (cloud_bill_mode and monthly == 0 and _src_cost > 0
                and _is_provisioned_throughput_line(row)):
            line_items = [{
                "sku": "", "description": "Provisioned throughput (included in the OCI shape)",
                "quantity": 0, "unit": "", "rate": 0, "monthly": 0.0,
                "mapping": ("AWS bills provisioned throughput separately (${:,.2f}/mo). OCI has "
                            "no such meter - throughput comes from the compute shape the storage "
                            "appliance runs on, already priced here - so this is $0, not carried."
                            .format(_src_cost)),
                "ociServiceUsage": True,
            }]
            annual = 0.0
        elif (cloud_bill_mode and not row_free_on_oci and not _user_action
                and not clean_text(row.get("_sqlMappingFlag"))
                and not _priced_on_oci_service
                and monthly == 0 and _src_cost > 0):
            # Do NOT copy the source cost into the OCI column automatically. Asserting "OCI
            # costs whatever AWS charged" is not a price: such a line can never show a saving,
            # it drags the OCI total toward the source total, and it hides the fact that the
            # service was never actually mapped. Record it at $0 with the source amount kept
            # for context so it surfaces in the needs-review total and gets a real decision -
            # an OCI price, or an explicit "free"/"not migrating". A user can still choose to
            # carry a specific row by hand (cost_action == "carry" below).
            # No OCI price exists for this service, so the source cost is carried to keep the
            # estimate conservative (better than a $0 that understates the migration). This is
            # NOT an OCI calculation though: such a line can never show a saving and it pulls
            # the OCI total toward the source total. It is tagged and totalled into
            # carriedSourceMonthly so the estimate always discloses how much of itself is
            # carried rather than priced.
            line_items = [{
                "sku": "", "description": "Carried over from source AWS cost",
                "quantity": 0, "unit": "", "rate": 0, "monthly": money(_src_cost),
                "mapping": ("No OCI price for this service, so the source cost is carried "
                            "unchanged. This is the source figure, not an OCI-rate calculation - "
                            "it shows no saving and needs a real OCI price or a free / "
                            "not-migrating decision."),
                "carriedOver": True,
                "carriedSourceMonthly": money(_src_cost),
            }]
            monthly = money(_src_cost)
            annual = money(monthly * 12)
            if not clean_text(row.get("oci_product")):
                row["oci_product"] = "Carried - no direct OCI equivalent"
            _auto_carried = True

        # Mapping flag. Object Storage is a clean, expected mapping and is never
        # flagged. Approximate maps (FSx, WorkSpaces) and auto-carried services are
        # always flagged; otherwise flag when OCI runs >10% above the source cost.
        mapping_flag = ""
        # Transfer Family (carried) and Route 53 -> OCI DNS (can run higher than AWS)
        # are flagged as non-ideal mappings.
        _force_flag = _is_transfer_family_row(row) or _is_route53_row(row)
        if cloud_bill_mode and not _is_object_storage:
            if _is_approx_map or _auto_carried or _force_flag:
                mapping_flag = "May not be an optimal mapping"
            elif _src_cost > 0 and monthly > _src_cost * 1.10:
                mapping_flag = "May not be an optimal mapping"
        # SQL Server (license-included) rows always carry the SQL flag, overriding
        # the generic mapping-flag logic above.
        _sql_flag = clean_text(row.get("_sqlMappingFlag"))
        if _sql_flag:
            mapping_flag = _sql_flag

        # Per-row cost actions for flagged rows: carry the AWS cost over to OCI, or
        # remove the line from both sides of the BOM.
        cost_action = _user_action
        if cost_action == "carry":
            line_items = [{
                "sku": "", "description": "Carried over from source AWS cost",
                "quantity": 0, "unit": "", "rate": 0, "monthly": money(_src_cost),
                "mapping": "OCI cost set equal to the source AWS cost (mapping flagged as non-optimal).",
                "carriedOver": True,
            }]
            monthly = money(_src_cost)
            annual = money(monthly * 12)
        elif cost_action == "remove":
            line_items = []
            monthly = 0.0
            annual = 0.0
            # Pull this line's source cost back out of the totals (removed from both sides).
            totals["sourceMonthlyCost"] -= _src_cost
            totals["mappedSourceMonthlyCost"] -= _src_cost

        source_row_label = clean_text(row.get("__sourceRow"))
        fallback_name = f"Workload {source_row_label}" if source_row_label.isdigit() else f"Workload {row_index}"
        application_name = (
            text_for(row, fields, ["application name"])
            or text_for_exact(row, fields, ["application", "app"])
        )
        machine_name = (
            text_for(row, fields, ["machine name"])
            or text_for_exact(row, fields, ["name", "server name", "host name", "hostname", "vm name", "instance name"])
        )
        name = (
            application_name
            or machine_name
            or text_for(row, fields, ["database name"])
            or clean_text(row.get("source_service"))
            or clean_text(row.get("source_product"))
            or fallback_name
        )
        environment = text_for(row, fields, ["environment"]) or clean_text(row.get("source_account")) or clean_text(row.get("source_region"))
        region = ""
        if keys.get("region"):
            region = clean_text(row.get(keys["region"], ""))
        if not region:
            region = clean_text(row.get("source_region"))
        if region:
            data_has["region"] = True
        if environment:
            data_has["environment"] = True
        assumptions = [
            "Spreadsheet CPU values are assumed to be vCPUs and converted in review using 2 vCPUs = 1 OCPU.",
            "OCPU and memory prices use each row's Hours Running value, with 730 as the default.",
            "Local VM storage plus database allocated storage are treated as block volume storage.",
            "Application shared storage is treated as file storage.",
        ]
        if service_catalog_enabled:
            assumptions.append("Recognized AWS, Azure, GCP, and on-prem rows are mapped to a curated Oracle price-list subset.")
            assumptions.extend(full_service_notes)

        # Feasibility against the selected OCI shape, evaluated on the SAME total shown in the
        # row (specs.ocpus / specs.memoryGb) so the flag message matches the displayed size.
        size_check = oci_size_check(row_shape_key, float(ocpus or 0), float(memory_gb or 0))
        if size_check["status"] == "impossible":
            totals["impossibleRows"] += 1
        elif size_check["status"] == "baremetal":
            totals["oversizeRows"] += 1

        # Source-cloud cost estimate (other-cloud comparison): Linux baseline + Windows license add-on.
        # Windows add-on mirrors the OCI rule (1 license per OCPU) and is gated by the Windows toggle.
        is_windows_row = row_operating_system(row) == "windows"
        # Windows licensing is per-OCPU-hour. A cloud bill lists each resource at daily/hourly
        # granularity, so bill Windows on the row's ACTUAL usage hours - not a full 730h month -
        # or a VM split across ~30 daily lines gets charged ~30 months of licensing. On-prem
        # inventory has no per-line hours, so it keeps the full-month basis.
        _win_hours = HOURS_PER_MONTH
        if cloud_bill_mode:
            _uh = cloud_usage_hours(row, fields)
            _win_hours = _uh if _uh and _uh > 0 else HOURS_PER_MONTH
        # Windows is licensed per OCPU of the instance you actually RUN. A GPU workload lands on
        # an OCI GPU shape whose OCPU count is fixed by the shape (VM.GPU.A10.1 = 15 OCPU), not on
        # the smaller source instance, so license it on the GPU shape or Windows is undercounted.
        _win_ocpus = ocpus
        if gpu_info and gpu_info.get("ocpu"):
            _win_ocpus = float(gpu_info["ocpu"])
        windows_addon = money(_win_ocpus * WINDOWS_LICENSE_RATE * _win_hours) if (is_windows_row and not hide_windows_pricing and _win_ocpus) else 0.0
        source_cloud_estimate = None
        # Once the bill's provider is known (from filename/toggle), trust it for the
        # whole file instead of re-deciding per server.
        known_provider = normalize_provider_hint(source_provider) if source_provider else PROVIDER_AUTO
        row_provider = known_provider if known_provider != PROVIDER_AUTO else (src_rec.get("provider") if src_rec else None)
        # GCP: keep sizing/mapping only, no estimated source-cloud pricing.
        if src_rec and row_provider != "gcp" and src_rec.get("approxSourceMonthly") is not None:
            base = src_rec["approxSourceMonthly"]
            source_cloud_estimate = {
                "provider": row_provider,
                "instance": src_rec.get("instance"),
                "osDetected": "windows" if is_windows_row else "linux",
                "linuxMonthly": base,
                "windowsAddOnMonthly": windows_addon,
                "totalMonthly": money(base + windows_addon),
                "priceSource": "real" if src_rec.get("sourcePriceReal") else "estimate",
            }

        # SQL Server 3rd-party licensing is bundled as line item(s) inside the OCI cost (unlike
        # Windows, which is a separate add-on). SQL Server licensing has its OWN toggle
        # (hide_sql_pricing) so it can be removed independently of Windows - both are
        # BYOL-able 3rd-party licenses the customer may already own.
        sql_license_monthly = 0.0
        for _li in (line_items or []):
            _d = normalize(_li.get("description", ""))
            if "sql server" in _d and ("licens" in _d):
                sql_license_monthly += float(_li.get("monthly") or 0)
        sql_license_monthly = money(sql_license_monthly)
        if hide_sql_pricing and sql_license_monthly:
            for _li in line_items:
                _d = normalize(_li.get("description", ""))
                if "sql server" in _d and ("licens" in _d):
                    _li["monthly"] = 0.0
                    _li["mapping"] = (clean_text(_li.get("mapping", "")) + " SQL Server licensing hidden by the licensing toggle.").strip()
            monthly = money(monthly - sql_license_monthly)
            annual = money(monthly * 12)
            sql_license_monthly = 0.0

        priced = {
            "rowId": row["__id"],
            "sourceRow": row.get("__sourceRow"),
            "name": name,
            "applicationName": application_name,
            "machineName": machine_name,
            "environment": environment,
            "region": region,
            "sizeCheck": size_check,
            "mappingFlag": mapping_flag,
            "costAction": cost_action or "",
            "ociServiceCategory": clean_text(row.get("oci_service_category")),
            "ociProduct": clean_text(row.get("oci_product")),
            "sourceService": clean_text(row.get("source_service")),
            "sourceMonthlyCost": money(_src_cost),
            # Raw source usage kept for cross-cloud networking re-pricing (egress GB, NAT/LB/
            # VPN/transit-gateway hours, etc. classified from the usage-type token).
            "sourceUsageType": clean_text(row.get("__usageType")),
            "sourceUsageQty": to_number(row.get("usage_quantity"), 0),
            "sourceProduct": clean_text(row.get("source_product")),
            "windowsLicenseMonthly": windows_addon,
            "sqlLicenseMonthly": sql_license_monthly,
            "osDetected": "windows" if is_windows_row else "linux",
            "sourceCloudEstimate": source_cloud_estimate,
            "rightsized": bool(rightsize and ((original_memory_gb and memory_gb != original_memory_gb) or (original_ocpus and ocpus != original_ocpus))),
            "originalMemoryGb": round(original_memory_gb, 4),
            "originalOcpus": round(original_ocpus, 4),
            "hoursPerMonth": row_hours,
            # Actual billed hours for this line when the meter is hour-based (0 otherwise).
            # Cloud bills are often daily/hourly line items, so a single VM appears as many
            # rows that each cover only part of the month. The cross-cloud estimator uses this
            # to price the equivalent instance on real usage, not a full 730-hour month per row.
            "computeUsageHours": round(cloud_usage_hours(row, fields), 4) if cloud_bill_mode else 0,
            "shapeUsed": {
                "key": row_shape.get("key"),
                "label": row_shape.get("label"),
                "shortLabel": row_shape.get("shortLabel"),
                "vendor": row_shape.get("processorVendor"),
                "computeSku": row_shape.get("computeSku"),
                "memorySku": row_shape.get("memorySku", row_shape.get("computeSku")),
                "computeRate": row_shape.get("computeRate"),
                "memoryRate": row_shape.get("memoryRate"),
            },
            "specs": {
                "applicationServers": app_servers,
                "databaseServers": db_servers,
                "vcpus": round(ocpus * 2, 4),
                "ocpus": round(ocpus, 4),
                "memoryGb": round(memory_gb, 4),
                "blockStorageGb": round(block_storage_gb, 4),
                "fileStorageGb": round(file_storage_gb, 4),
            },
            "fullServiceMapping": full_service_mapping,
            "lineItems": line_items,
            "monthly": monthly,
            "annual": annual,
            "assumptions": assumptions,
        }
        priced_rows.append(priced)

        for key in ["ocpus", "memoryGb", "blockStorageGb", "fileStorageGb"]:
            totals[key] += priced["specs"][key]
        totals["monthly"] += monthly
        totals["annual"] += annual

        # Carried-over source cost is not an OCI price. Count it so the estimate can disclose
        # how much of itself is calculated vs copied from the source bill.
        for _li in (line_items or []):
            if _li.get("carriedSourceMonthly"):
                totals["carriedSourceMonthly"] += float(_li.get("carriedSourceMonthly") or 0)
                totals["carriedRows"] += 1

        # Track source spend that produced no OCI cost, split into "OCI genuinely doesn't charge
        # for this" vs "this should have cost something" so the estimate's gaps are visible.
        if cloud_bill_mode and monthly == 0 and _src_cost > 0:
            totals["zeroOciSourceMonthly"] += _src_cost
            if _is_free_on_oci(row, priced):
                totals["freeOnOciSourceMonthly"] += _src_cost
            else:
                totals["unpricedSourceMonthly"] += _src_cost
                totals["unpricedRows"] += 1
                if not clean_text(priced.get("ociProduct")):
                    totals["unmappedZeroSourceMonthly"] += _src_cost
                    totals["unmappedRows"] += 1

    for key in totals:
        if key in {"monthly", "annual", "fullServiceMonthly", "sourceMonthlyCost", "mappedSourceMonthlyCost", "unmappedSourceMonthlyCost", "zeroOciSourceMonthly", "freeOnOciSourceMonthly", "unpricedSourceMonthly", "unmappedZeroSourceMonthly"}:
            totals[key] = money(totals[key])
        elif key in {"mappedServiceRows", "unpricedServiceRows", "oversizeRows", "impossibleRows", "unpricedRows", "unmappedRows"}:
            totals[key] = int(totals[key])
        else:
            totals[key] = round(totals[key], 4)

    # App Estimate: a bill uploaded with SKUs/usage but no pricing (e.g. an Azure export with the
    # cost columns stripped) still needs a source-cost figure. Reconstruct each row's cost on its
    # own source cloud from usage, and flag it so the "<Cloud> Cost" labels become "(App Estimate)".
    source_cost_estimated = False
    source_cloud_key = _dominant_source_cloud({"rows": priced_rows}, source_provider) if cloud_bill_mode else None
    if cloud_bill_mode:
        source_cost_estimated = _apply_source_cost_estimate(
            priced_rows, totals, source_cloud_key, hide_windows_pricing)
        _apply_zfs_appliance(priced_rows, totals, eff_hours)
    # Bare metal is an indivisible box shared across workloads: round the ESTATE (not each row)
    # up to whole servers and bill the unused remainder.
    bare_metal_packing = _apply_bare_metal_packing(priced_rows, totals, shape_key, eff_hours)

    return {
        "engine": "local-rule-engine",
        "intakeMode": intake_mode,
        "fullServiceBeta": service_catalog_enabled,
        "cloudBillMode": cloud_bill_mode,
        "hoursPerMonth": eff_hours,
        "selectedShape": selected_shape,
        "cpuUnitResolved": cpu_unit_resolved,
        "rateCard": rate_card,
        "rateCards": all_shape_payloads(service_catalog_enabled),
        "totals": totals,
        "rows": priced_rows,
        "fieldMap": keys,
        "dataFlags": data_has,
        "crossCloud": cross_cloud_estimate(
            priced_rows,
            hide_windows_pricing,
            cloud_bill_mode,
            source_cloud_key,
        ),
        # True when the bill had no pricing and the source cost is an App Estimate (usage-based);
        # sourceCloud names the cloud so the UI/BOM can label "<Cloud> Cost (App Estimate)".
        "sourceCostEstimated": source_cost_estimated,
        "bareMetalPacking": bare_metal_packing,
        "sourceCloud": source_cloud_key,
        "priceCatalog": price_catalog_payload() if service_catalog_enabled else [],
    }


def extract_response_text(payload):
    if "output_text" in payload:
        return payload["output_text"]
    chunks = []
    for item in payload.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"}:
                chunks.append(content.get("text", ""))
    return "\n".join(chunks)


def parse_jsonish(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    return json.loads(text)


def compact_llm_summary(pricing):
    sample_rows = []
    for row in pricing["rows"][:12]:
        sample_rows.append(
            {
                "rowId": row["rowId"],
                "name": row["name"],
                "environment": row["environment"],
                "specs": row["specs"],
                "fullServiceMapping": row.get("fullServiceMapping"),
                "mappedSkus": [item["sku"] for item in row["lineItems"]],
                "monthly": row["monthly"],
            }
        )
    compute_item = next((item for item in pricing.get("rateCard", []) if item.get("unit") == "OCPU-hour"), {"sku": "B97384"})
    memory_item = next((item for item in pricing.get("rateCard", []) if item.get("unit") == "GB-hour"), {"sku": "B97385"})
    mapping_rules = [
        {
            "sku": compute_item["sku"],
            "rule": "Uploaded spreadsheet CPU values are assumed to be vCPUs, converted to OCPUs in review using 2 vCPUs = 1 OCPU, then multiplied by each row's Hours Running value (730 by default).",
        },
        {
            "sku": memory_item["sku"],
            "rule": "Memory GB-hours use each row's Hours Running value (730 by default) and the selected flex shape rate.",
        },
        {"sku": "B91961", "rule": "VM local storage, database allocated storage, EBS, managed disks, persistent disks, SAN, and block volume rows use block volume GB-month."},
        {"sku": "B89057", "rule": "Shared/NAS storage, EFS, Azure Files, GCP Filestore, NFS, SMB, and file-share rows use file storage GB-month."},
    ]
    if pricing.get("fullServiceBeta"):
        mapping_rules.extend(
            [
                {"sku": "B91628", "rule": "S3, Blob, GCS, bucket, and standard object storage rows use object storage GB-month."},
                {"sku": "B93000", "rule": "S3 Standard-IA/One Zone-IA, Azure Blob Cool, GCP Nearline, and equivalent rows use Infrequent Access GB-month."},
                {"sku": "B93001", "rule": "Infrequent Access retrieval rows use retrieved GB."},
                {"sku": "B91633", "rule": "Glacier, archive blob, archive/coldline, and backup archive rows use archive storage GB-month."},
                {"sku": "B91627", "rule": "S3/Blob/GCS request rows use object storage request units of 10,000 requests."},
            ]
        )
    return {
        "workflowContract": LLM_WORKFLOW_CONTRACT,
        "rowCount": len(pricing["rows"]),
        "totals": pricing["totals"],
        "sampleRows": sample_rows,
        "intakeMode": pricing.get("intakeMode", INTAKE_MODE_ON_PREM),
        "fullServiceBeta": pricing.get("fullServiceBeta", False),
        "selectedShape": pricing.get("selectedShape", shape_payload(DEFAULT_SHAPE_KEY)),
        "rateCard": pricing.get("rateCard", build_rate_card(DEFAULT_SHAPE_KEY)),
        "officialReferences": OCI_OFFICIAL_REFERENCES if pricing.get("fullServiceBeta") else [],
        "meteringGuidance": OCI_METERING_GUIDANCE if pricing.get("fullServiceBeta") else [],
        "sourceServiceMappings": provider_mapping_context("") if pricing.get("fullServiceBeta") else [],
        "priceCatalog": pricing.get("priceCatalog", []),
        "localMappingRules": mapping_rules,
    }


def describe_http_error(exc):
    payload = exc.read().decode("utf-8", errors="replace")
    try:
        error = json.loads(payload).get("error", {})
        parts = [str(exc)]
        if error.get("type"):
            parts.append(f"type={error.get('type')}")
        if error.get("code"):
            parts.append(f"code={error.get('code')}")
        if error.get("message"):
            parts.append(error.get("message"))
        return " | ".join(parts)
    except json.JSONDecodeError:
        return f"{exc} | {payload[:300]}"


def call_openai_json(
    system_content,
    user_payload,
    max_output_tokens=1600,
    timeout=45,
    model_env="OPENAI_MODEL",
    reasoning_effort_env=None,
    default_reasoning_effort=None,
    schema_name=None,
    response_schema=None,
):
    if not openai_api_enabled():
        return None, OPENAI_DISABLED_MESSAGE

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None, "OPENAI_API_KEY is not set."

    model = os.environ.get(model_env) or os.environ.get("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    reasoning_effort = clean_text(os.environ.get(reasoning_effort_env)) if reasoning_effort_env else ""
    reasoning_effort = reasoning_effort or clean_text(default_reasoning_effort)
    body = {
        "model": model,
        "max_output_tokens": max_output_tokens,
        "input": [
            {"role": "system", "content": system_content},
            {"role": "user", "content": json.dumps(user_payload)},
        ],
    }
    if reasoning_effort:
        body["reasoning"] = {"effort": reasoning_effort}
    if schema_name and isinstance(response_schema, dict):
        body["text"] = {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "schema": response_schema,
                "strict": True,
            }
        }
    api_base = clean_text(os.environ.get("OPENAI_API_BASE")) or "https://api.openai.com/v1"
    request = urllib.request.Request(
        f"{api_base.rstrip('/')}/responses",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        text = extract_response_text(payload)
        return parse_jsonish(text), None
    except urllib.error.HTTPError as exc:
        return None, describe_http_error(exc)
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        return None, str(exc)


def workbook_plan_schema(full_service_beta=False):
    canonical_keys = [field["key"] for field in inventory_fields(full_service_beta)]
    mapping_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "canonicalKey": {"type": "string", "enum": canonical_keys},
            "sourceColumn": {"type": "integer", "minimum": 1},
            "sourceHeader": {"type": "string"},
            "jsonKey": {"type": "string"},
            "sourceUnit": {
                "type": "string",
                "enum": [
                    "text", "count", "vCPU", "OCPU", "MB", "MiB", "GB", "GiB",
                    "TB", "TiB", "hours", "unknown",
                ],
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "transform": {"type": "string"},
        },
        "required": [
            "canonicalKey", "sourceColumn", "sourceHeader", "jsonKey",
            "sourceUnit", "confidence", "transform",
        ],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "sheetName": {"type": "string"},
            "headerRows": {
                "type": "array",
                "items": {"type": "integer", "minimum": 1},
                "minItems": 1,
                "maxItems": 4,
            },
            "dataStartRow": {"type": "integer", "minimum": 1},
            "dataEndRow": {
                "anyOf": [
                    {"type": "integer", "minimum": 1},
                    {"type": "null"},
                ]
            },
            "serverGrain": {
                "type": "string",
                "enum": ["server", "vm", "host", "asset", "application", "unknown"],
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "columnMappings": {
                "type": "array",
                "items": mapping_schema,
                "maxItems": len(canonical_keys),
            },
            "notes": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 10,
            },
        },
        "required": [
            "sheetName", "headerRows", "dataStartRow", "dataEndRow",
            "serverGrain", "confidence", "columnMappings", "notes",
        ],
    }


def call_llm_workbook_plan(path, full_service_beta=False):
    digest = workbook_digest(path)
    system = (
        "You interpret Excel infrastructure inventory workbooks for an Oracle Cloud Infrastructure intake app. "
        "Your primary job is to find the workload identity and the specs needed for OCI pricing: core count, RAM/memory, and storage. "
        "Given workbook sheet samples, identify the sheet and row range that contain servers, applications, VMs, "
        "hosts, databases, or other infrastructure inventory. Return compact JSON only. "
        "Use 1-based row and column numbers. Do not invent missing columns. "
        "Map source columns to the provided canonical fields when the source appears equivalent, even if headings use "
        "terms like hostname, VM, instance, vCPU, RAM, disk, storage, OS, platform, environment, or application. "
        "Treat uploaded spreadsheet CPU/core values as vCPUs. The app will normalize those values to OCPUs for review "
        "using 2 vCPUs = 1 OCPU, so CPU/vCPU source columns should map to the OCPU canonical fields. "
        "Map RAM, Memory, MemoryGB, or MemoryGB(RAM) only to memory fields. Map Total Storage, Storage GB, allocated "
        "storage, disk size, or disk capacity to storage fields. Do not map a bare Disk/Disks column to storage or "
        "server count when its values look like small disk counts. If a storage heading says Total Storage, treat it "
        "as the row's total storage, not storage-per-server that should be multiplied by a disk count. "
        "For every mapped numeric column, set sourceUnit from the values and header. A header can be wrong: when values "
        "are repeated powers or multiples of 1024, distinguish MiB from GB using the whole-column profile. Use vCPU for "
        "virtual CPU/core counts unless the source explicitly says OCPU. "
        "Some workbooks, especially AWS billing or inventory exports, store tags as JSON strings in a cell. "
        "When a JSON/tag column contains useful keys, map it with jsonKey. Map application, appName, or appId "
        "to application_name. Map Name, hostname, serverName, vmName, or instanceName to machine_name. "
        "Map 'environment' to environment and 'os' to application_details_operating_system. "
        "For application_details, prefer resource-specific values such as tag key 'appId', 'role', 'owner', resourceId, "
        "or private IP; avoid accountId or region unless nothing resource-specific exists. "
        "Do not map the full JSON blob as plain text unless no useful key exists. "
        "If each row is one server/VM/host, set serverGrain to 'server'. If each row is an application/workload "
        "that may represent many servers, set serverGrain to 'application'. "
        "Return this shape: {sheetName, headerRows, dataStartRow, dataEndRow, serverGrain, confidence, "
        "columnMappings:[{canonicalKey, sourceColumn, sourceHeader, jsonKey, confidence, transform}], notes:[string]}. "
        "For transform, briefly say unit conversions needed, such as TB to GB."
    )
    if full_service_beta:
        system += (
            " OCI full service beta is enabled. Treat AWS Cost Explorer/CUR exports, Azure cost exports, "
            "GCP billing exports, and on-prem CMDB/asset sheets as valid source workbooks even when they are not "
            "classic server inventories. Map provider, source service, source product/meter, source region, usage "
            "quantity, usage unit, and monthly cost columns when present. Prefer source_product for detailed usage "
            "type or meter names such as S3 StandardStorage, EBS VolumeUsage, Azure Blob Hot LRS, GCP Persistent Disk, "
            "NAS, SAN, backup archive, or object request rows. Use oci_service_category and oci_product only when "
            "the spreadsheet already contains target Oracle mapping columns; do not invent target values during "
            "column mapping."
        )
    payload = {
        "workflowContract": LLM_WORKFLOW_CONTRACT,
        "canonicalFields": canonical_field_prompt(full_service_beta),
        "ociFullServiceBeta": bool(full_service_beta),
        "ociPriceCatalog": price_catalog_payload() if full_service_beta else [],
        "workbook": digest,
    }
    plan, warning = call_openai_json(
        system,
        payload,
        max_output_tokens=2800,
        timeout=45,
        model_env="OPENAI_UPLOAD_MODEL",
        reasoning_effort_env="OPENAI_UPLOAD_REASONING_EFFORT",
        default_reasoning_effort="low",
        schema_name="oci_inventory_scrub",
        response_schema=workbook_plan_schema(full_service_beta),
    )
    if warning:
        if warning == OPENAI_DISABLED_MESSAGE:
            return None, "OpenAI API calls are temporarily disabled; used rule-based spreadsheet parsing."
        return None, f"OpenAI workbook interpretation did not complete; used rule-based spreadsheet parsing. Detail: {warning}"
    excel_file = pd.ExcelFile(path)
    normalized = normalize_workbook_plan(plan, excel_file, full_service_beta)
    if not normalized:
        return None, "OpenAI workbook interpretation did not identify a usable inventory table; used rule-based spreadsheet parsing."
    return normalized, None


def architecture_plan_schema():
    placement = {
        "type": "string",
        "enum": [
            "edge",
            "regional_service",
            "hub_public_subnet",
            "hub_inspection_subnet",
            "hub_shared_services_subnet",
            "private_app_subnet",
            "private_data_subnet",
            "outside_vcn",
            "dr_region",
        ],
    }
    service_placement = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "service": {"type": "string"},
            "placement": placement,
            "evidence": {
                "type": "string",
                "enum": ["priced", "user_selected", "baseline_pattern"],
            },
            "rationale": {"type": "string"},
        },
        "required": ["service", "placement", "evidence", "rationale"],
    }
    traffic_flow = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "source": {"type": "string"},
            "target": {"type": "string"},
            "protocol": {"type": "string"},
            "purpose": {"type": "string"},
        },
        "required": ["source", "target", "protocol", "purpose"],
    }
    icon_mapping = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "service": {"type": "string"},
            "iconQuery": {"type": "string"},
            "evidence": {
                "type": "string",
                "enum": ["priced", "user_selected", "baseline_pattern"],
            },
            "fallbackPolicy": {
                "type": "string",
                "enum": ["direct_only", "alias_allowed", "disclose_placeholder"],
            },
            "rationale": {"type": "string"},
        },
        "required": [
            "service",
            "iconQuery",
            "evidence",
            "fallbackPolicy",
            "rationale",
        ],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {"type": "string"},
            "pattern": {
                "type": "string",
                "enum": ["landing_zone_hub_spoke"],
            },
            "referenceBaseline": {"type": "string"},
            "referenceRationale": {"type": "string"},
            "networkPosture": {
                "type": "string",
                "enum": ["private_by_default", "mixed", "public_ingress"],
            },
            "availabilityPosture": {
                "type": "string",
                "enum": [
                    "single_region",
                    "single_region_multi_ad",
                    "cross_region_dr",
                    "multi_ad_and_cross_region_dr",
                ],
            },
            "subnetScope": {
                "type": "string",
                "enum": ["regional"],
            },
            "databaseStrategy": {"type": "string"},
            "ingressStrategy": {"type": "string"},
            "egressStrategy": {"type": "string"},
            "managementStrategy": {"type": "string"},
            "workloadGroupingRationale": {"type": "string"},
            "haDrRationale": {"type": "string"},
            "servicePlacements": {
                "type": "array",
                "items": service_placement,
                "maxItems": 18,
            },
            "trafficFlows": {
                "type": "array",
                "items": traffic_flow,
                "maxItems": 18,
            },
            "iconMappings": {
                "type": "array",
                "items": icon_mapping,
                "maxItems": 24,
            },
            "securityControls": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 12,
            },
            "assumptions": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 10,
            },
            "warnings": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 10,
            },
            "qaChecks": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 12,
            },
            "architectureReview": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 12,
            },
            "visualReview": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 12,
            },
        },
        "required": [
            "summary",
            "pattern",
            "referenceBaseline",
            "referenceRationale",
            "networkPosture",
            "availabilityPosture",
            "subnetScope",
            "databaseStrategy",
            "ingressStrategy",
            "egressStrategy",
            "managementStrategy",
            "workloadGroupingRationale",
            "haDrRationale",
            "servicePlacements",
            "trafficFlows",
            "iconMappings",
            "securityControls",
            "assumptions",
            "warnings",
            "qaChecks",
            "architectureReview",
            "visualReview",
        ],
    }


def compact_architecture_context(
    pricing,
    rows,
    fields,
    diagram_options,
    extra_services=None,
    bom_name="",
    shape_label="",
):
    import bom_diagram
    import bom_template

    keys = bom_template._resolve_inventory_keys(fields or []) if fields else {}
    segments = bom_diagram.collect_segments(pricing, rows, keys)
    priced_services = []
    seen = set()
    for row in (pricing or {}).get("rows", []):
        service = clean_text(
            row.get("ociProduct")
            or row.get("ociServiceCategory")
            or row.get("sourceService")
        )
        if service and service not in seen:
            seen.add(service)
            priced_services.append(service)
    selected_services = [
        clean_text(item.get("name") or item.get("service"))
        for item in (extra_services or [])
        if clean_text(item.get("name") or item.get("service"))
    ]
    reference_query = " ".join(
        [
            "physical OCI architecture",
            "hub spoke landing zone",
            "migration",
            "multi AD" if (diagram_options or {}).get("splitADs") else "single region",
            "disaster recovery multi region" if (diagram_options or {}).get("enableDr") else "",
            *priced_services[:24],
            *selected_services[:18],
            *[clean_text(segment.get("name")) for segment in segments[:8]],
        ]
    )
    from architecture_engine.integration import select_reference_baseline

    reference_selection = select_reference_baseline(reference_query)
    return {
        "bomName": bom_name or "Customer",
        "shape": shape_label,
        "totals": {
            key: (pricing or {}).get("totals", {}).get(key, 0)
            for key in ("monthly", "ocpus", "memoryGb", "blockStorageGb")
        },
        "segments": [
            {
                "name": segment.get("name"),
                "vms": segment.get("vms", 0),
                "ocpus": round(float(segment.get("ocpu", 0)), 2),
                "memoryGb": round(float(segment.get("ram", 0)), 2),
                "blockStorageGb": round(float(segment.get("block", 0)), 2),
            }
            for segment in segments
        ],
        "pricedServices": priced_services[:40],
        "userSelectedServices": selected_services[:30],
        "userArchitectureChoices": diagram_options or {},
        "referenceSelection": reference_selection,
        "rendererContract": {
            "pattern": (
                "Physical OCI landing zone with a hub VCN, DRG, workload spoke VCNs, "
                "regional private app/data subnets, and optional DR region."
            ),
            "numericSource": "All capacities and costs come from deterministic pricing; the model may not alter them.",
            "referencePolicy": (
                "Use the primary bundled Oracle reference for layout discipline. Use supporting "
                "references only for the specific DR, security, database, or workload patterns they cover."
            ),
            "iconPolicy": (
                "Request an official direct OCI icon first, then a trusted alias. Never silently "
                "substitute a merely similar icon; disclose an honest placeholder when needed."
            ),
            "placements": [
                "Edge and regional ingress remain outside workload subnets.",
                "Public ingress belongs in the hub public subnet.",
                "Compute and databases remain in private workload subnets.",
                "Availability-domain and DR choices from the user override model recommendations.",
                "Subnets are regional. AD boxes are placement lanes behind regional subnets, not subnet boundaries.",
                "Internet, NAT, and Service gateways mount on the VCN boundary.",
            ],
            "qualityGates": [
                "No stretched icons or missing official icon content.",
                "No connector through an unrelated node or along a container border.",
                "No shared connector lane for unrelated semantic flows.",
                "No overlapping labels, icons, arrowheads, or grouping boundaries.",
                "Public and private placement, HA/DR posture, and database scope must be visually honest.",
            ],
        },
    }


def validate_architecture_plan(plan):
    if not isinstance(plan, dict):
        raise ValueError("The architecture planner returned an invalid plan.")
    if plan.get("pattern") != "landing_zone_hub_spoke":
        raise ValueError("The architecture planner selected an unsupported topology.")
    allowed_placements = {
        "edge",
        "regional_service",
        "hub_public_subnet",
        "hub_inspection_subnet",
        "hub_shared_services_subnet",
        "private_app_subnet",
        "private_data_subnet",
        "outside_vcn",
        "dr_region",
    }
    for item in plan.get("servicePlacements", []):
        if item.get("placement") not in allowed_placements:
            raise ValueError("The architecture planner returned an unsupported placement.")
    if plan.get("subnetScope") != "regional":
        raise ValueError("The architecture planner selected unsupported AD-specific subnet framing.")
    reference = clean_text(plan.get("referenceBaseline"))
    if not reference:
        raise ValueError("The architecture planner did not select a reference baseline.")
    for item in plan.get("iconMappings", []):
        if not clean_text(item.get("service")) or not clean_text(item.get("iconQuery")):
            raise ValueError("The architecture planner returned an incomplete icon mapping.")
    return plan


def call_llm_architecture_plan(
    pricing,
    rows,
    fields,
    diagram_options,
    extra_services=None,
    bom_name="",
    shape_label="",
):
    system = (
        "You are planning a physical Oracle Cloud Infrastructure architecture using the same discipline "
        "as the bundled Boeing OCI architecture workflow. "
        "Return only the strict JSON requested. Use the supplied priced services, workload aggregates, "
        "user architecture choices, and ranked Oracle references as the source of truth. Choose the "
        "landing_zone_hub_spoke pattern. Use the primary reference as the layout baseline and supporting "
        "references only for the specific patterns they cover. "
        "Do not change quantities, costs, regions, availability-domain choices, or DR selections. "
        "Do not invent a paid OCI service. Baseline landing-zone controls may be recommended only with "
        "evidence baseline_pattern and must be described as design assumptions, not priced items. "
        "Keep edge services outside workload subnets, ingress in the public hub subnet, shared controls "
        "in hub inspection/shared-services subnets, and compute/database resources in private spoke subnets. "
        "Treat all subnets as regional. Availability Domain boxes are placement lanes behind regional "
        "subnets and must not imply AD-specific subnet scope. Resolve every service to an official icon "
        "query or alias. Never claim a direct icon when only a placeholder is honest. Call out missing "
        "evidence instead of guessing. Include separate architecture and visual reviews for public/private "
        "placement, HA/DR honesty, gateway boundaries, icon/service correspondence, connector routing, "
        "label overlap, sibling symmetry, and numeric fidelity."
    )
    context = compact_architecture_context(
        pricing,
        rows,
        fields,
        diagram_options,
        extra_services,
        bom_name,
        shape_label,
    )
    result, warning = call_openai_json(
        system,
        context,
        max_output_tokens=4000,
        timeout=60,
        model_env="OPENAI_ARCHITECTURE_MODEL",
        reasoning_effort_env="OPENAI_ARCHITECTURE_REASONING_EFFORT",
        default_reasoning_effort="low",
        schema_name="oci_architecture_plan",
        response_schema=architecture_plan_schema(),
    )
    if warning:
        return None, warning
    return validate_architecture_plan(result), None


def architecture_options_with_ai(
    pricing,
    rows,
    fields,
    diagram_options,
    extra_services=None,
    bom_name="",
    shape_label="",
):
    options = dict(diagram_options or {})
    plan = None
    warning = None
    if openai_api_enabled() and openai_api_configured():
        try:
            plan, warning = call_llm_architecture_plan(
                pricing,
                rows,
                fields,
                options,
                extra_services,
                bom_name,
                shape_label,
            )
        except Exception as exc:
            warning = str(exc)
    else:
        warning = (
            OPENAI_DISABLED_MESSAGE
            if not openai_api_enabled()
            else "OPENAI_API_KEY is not set."
        )
    # The architecture diagram is the one domain where an agent MAY override what we generated
    # (see AGENT_AUTHORITY). It is a drawing, so changing it cannot move a price or a mapping.
    if plan and agent_may_override("architecture"):
        options["aiPlan"] = plan
        options["agentAuthority"] = "override"
    elif plan:
        # Policy flipped to advisory: keep the plan visible but don't let it drive the diagram.
        options["aiPlanAdvisory"] = plan
        options["agentAuthority"] = "advisory"
    return options, {
        "status": "assisted" if plan else "deterministic_fallback",
        "authority": AGENT_AUTHORITY.get("architecture", "advisory"),
        "model": (
            os.environ.get("OPENAI_ARCHITECTURE_MODEL")
            or os.environ.get("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
        ),
        "plan": plan,
        "warning": warning,
    }


def architecture_artifact_qa(drawio, png):
    from PIL import Image
    from architecture_engine.integration import inspect_drawio_artifact

    issues = []
    metrics = {}
    drawio_path = Path(drawio) if drawio else None
    png_path = Path(png) if png else None
    if not drawio_path or not drawio_path.exists():
        issues.append("Editable draw.io output is missing.")
    else:
        metrics["drawioBytes"] = drawio_path.stat().st_size
        try:
            icon_report_path = drawio_path.with_name(
                f"{drawio_path.stem}_icon_mapping.json"
            )
            inspection = inspect_drawio_artifact(drawio_path, icon_report_path)
            metrics["drawioCells"] = inspection["cellCount"]
            metrics["drawioEdges"] = inspection["edgeCount"]
            metrics["drawioOfficialStencilCells"] = inspection["officialIconCount"]
            metrics["drawioPlaceholderCells"] = inspection["placeholderCount"]
            metrics["drawioPages"] = inspection["validation"].get("page_count", 0)
            for validation_issue in inspection["validation"].get("issues", []):
                issues.append(f"Draw.io validation: {validation_issue}")
            if metrics["drawioCells"] < 20 or metrics["drawioEdges"] < 4:
                issues.append("The draw.io graph is unexpectedly sparse.")
            if metrics["drawioOfficialStencilCells"] < 10:
                issues.append(
                    "The draw.io architecture does not contain enough official OCI icon stencils."
                )
        except Exception as exc:
            issues.append(f"The draw.io XML is invalid: {exc}")
    if not png_path or not png_path.exists():
        issues.append("Rendered PNG output is missing.")
    else:
        metrics["pngBytes"] = png_path.stat().st_size
        try:
            with Image.open(png_path) as image:
                metrics["pngWidth"], metrics["pngHeight"] = image.size
                extrema = image.convert("RGB").resize((96, 96)).getextrema()
                metrics["pngChannelRanges"] = [
                    int(high - low) for low, high in extrema
                ]
                if image.width < 800 or image.height < 600:
                    issues.append("The PNG canvas is too small for the architecture.")
                if max(metrics["pngChannelRanges"], default=0) < 8:
                    issues.append("The PNG appears blank or nearly uniform.")
        except Exception as exc:
            issues.append(f"The PNG could not be inspected: {exc}")
    return {"passed": not issues, "issues": issues, "metrics": metrics}


def call_llm_mapping(pricing):
    prompt = compact_llm_summary(pricing)
    system = (
        "You are an Oracle Cloud Infrastructure pricing mapper. "
        "The rows you receive have already passed through the editable review table; treat those edited values as the source of truth. "
        "Validate whether the SKU mapping rules, selected OCI flexible compute shape, supplied OCI rate card, and any cloud-bill/on-prem "
        "service mappings are appropriate for the approved review-table rows. "
        "Return compact JSON only with keys globalAssumptions, mappingRules, and reviewNotes. "
        "Do not price from the original upload if review-table values differ. Do not invent rates. "
        "Do not recalculate every row; validate the rules against the supplied rate card and call out mapping risks."
    )
    payload, warning = call_openai_json(
        system,
        prompt,
        max_output_tokens=1200,
        timeout=45,
        model_env="OPENAI_PRICING_MODEL",
    )
    if warning:
        if warning == OPENAI_DISABLED_MESSAGE:
            return None, "OpenAI API calls are temporarily disabled; used deterministic SKU mapping."
        if warning == "OPENAI_API_KEY is not set.":
            return None, "OPENAI_API_KEY is not set; used deterministic SKU mapping."
        return None, f"OpenAI call did not complete; used deterministic SKU mapping. Detail: {warning}"
    return payload, None


FOREIGN_BOM_KINDS = ("ocpu", "memory", "blockStorage", "fileStorage", "objectStorage",
                      "perf", "network", "database", "license", "gpu", "other")
FOREIGN_BOM_CATEGORIES = ("Compute", "Storage", "Networking", "Database", "Licensing",
                          "Security", "Disaster Recovery", "Other Services")


def foreign_bom_assist(result, min_unrecognized=3, min_share=0.2):
    """Advisory pass over the lines a foreign OCI BOM left unrecognized.

    A BOM from outside this app can use SKUs the catalog has never seen (older parts, services
    the app doesn't price, a partner's private part numbers). The deterministic converter still
    carries those lines at the cost the BOM itself states - nothing is dropped - but it can't say
    WHAT they are, so their service category and their OCPU / RAM / storage sizing stay blank.

    That blank is precisely what AGENT_POLICY.md lets an agent fill: `foreign_bom_mapping` is
    advisory, so this may only classify a line the engine couldn't. Specifically it may return a
    service category and a resource kind, and nothing else.

    It may NOT return money or quantities. Every figure stays the BOM's own: the agent says
    "this line is block storage", the file says "2000 units", and the deterministic code
    multiplies. So an agent never writes a rate and never turns a blank into a number - it only
    labels a number the document already contained. Rows it touches are marked aiAssisted and
    reviewRequired so the classification is visibly a suggestion, not a fact.

    Returns (applied_count, status_dict). The result dict is mutated in place.
    """
    rows = (result or {}).get("rows") or []
    pending = []
    for row in rows:
        mapping = row.get("fullServiceMapping") or {}
        # reviewRequired is exactly the set the deterministic converter could not identify:
        # no catalog match, and not a legitimate $0 free-tier line.
        if not mapping.get("reviewRequired"):
            continue
        sku = clean_text(mapping.get("sku"))
        desc = clean_text(mapping.get("ociProduct")) or clean_text(row.get("name"))
        if not desc and not sku:
            continue
        pending.append({"rowId": row.get("rowId"), "sku": sku, "description": desc,
                        "unit": clean_text(mapping.get("unit")),
                        "quantity": mapping.get("quantity")})

    total_lines = len(rows) or 1
    status = {"ran": False, "applied": 0, "considered": len(pending), "note": ""}
    # Only worth a round trip when a meaningful share of the BOM is unreadable. A stray
    # unrecognized line isn't a reason to call out to a model.
    if len(pending) < min_unrecognized or (len(pending) / total_lines) < min_share:
        status["note"] = "Deterministic SKU recognition covered this BOM."
        return 0, status
    if not openai_api_enabled():
        status["note"] = OPENAI_DISABLED_MESSAGE
        return 0, status
    if not openai_api_configured():
        status["note"] = ("OPENAI_API_KEY is not set, so %d unrecognized line items keep the cost "
                          "the BOM states but no service classification." % len(pending))
        return 0, status

    system = (
        "You classify line items from an Oracle Cloud Infrastructure bill of materials that an "
        "automated SKU catalog could not recognize. "
        "For each line, decide what kind of OCI resource it is from its description, unit and part number. "
        "Return compact JSON only: {\"lines\":[{\"rowId\":string,\"category\":string,\"kind\":string,"
        "\"ociProduct\":string,\"confidence\":number,\"note\":string}],\"warnings\":[string]}. "
        "category must be one of: " + ", ".join(FOREIGN_BOM_CATEGORIES) + ". "
        "kind must be one of: " + ", ".join(FOREIGN_BOM_KINDS) + ". "
        "ociProduct is the official Oracle product name you believe the line refers to. "
        "NEVER return a price, a rate, a cost or a quantity - those come from the BOM itself and "
        "are not yours to set. Omit any line you cannot classify with reasonable confidence "
        "rather than guessing. confidence is 0..1."
    )
    payload, warning = call_openai_json(
        system,
        {"lines": pending[:120], "bomSheet": clean_text(result.get("sheetName"))},
        max_output_tokens=2000,
        timeout=60,
        model_env="OPENAI_FOREIGN_BOM_MODEL",
        reasoning_effort_env="OPENAI_FOREIGN_BOM_REASONING_EFFORT",
        default_reasoning_effort="low",
    )
    if warning or not isinstance(payload, dict):
        status["note"] = ("AI assist did not complete, so %d unrecognized line items keep the "
                          "cost the BOM states. Detail: %s" % (len(pending), warning or "no result"))
        return 0, status

    by_id = {str(r.get("rowId")): r for r in rows if r.get("rowId") is not None}
    applied = 0
    for suggestion in (payload.get("lines") or []):
        if not isinstance(suggestion, dict):
            continue
        row = by_id.get(str(suggestion.get("rowId")))
        if row is None:
            continue
        category = clean_text(suggestion.get("category"))
        kind = clean_text(suggestion.get("kind"))
        # Reject anything outside the closed vocabulary - a model returning a category the app
        # doesn't have must not create one.
        if category not in FOREIGN_BOM_CATEGORIES or kind not in FOREIGN_BOM_KINDS:
            continue
        mapping = row.setdefault("fullServiceMapping", {})
        # Advisory: only fill what the engine left blank. "Other Services" IS the converter's
        # blank - it's the fallback when classify_resource recognized nothing - so that may be
        # replaced. Any real deterministic category is left exactly as it was.
        if clean_text(row.get("ociServiceCategory")) in ("", "Other Services"):
            row["ociServiceCategory"] = category
            mapping["ociServiceCategory"] = category
        product = clean_text(suggestion.get("ociProduct"))
        if product:
            mapping["aiSuggestedProduct"] = product
        mapping["aiSuggestedKind"] = kind
        mapping["aiConfidence"] = to_number(suggestion.get("confidence"), 0)
        mapping["aiNote"] = clean_text(suggestion.get("note"))[:300]
        mapping["reviewRequired"] = True
        row["aiAssisted"] = True
        applied += 1

    status.update({
        "ran": True,
        "applied": applied,
        "warnings": [clean_text(w)[:300] for w in (payload.get("warnings") or []) if clean_text(w)],
        "note": ("AI assist classified %d of %d unrecognized line items. Costs, quantities and "
                 "sizing are unchanged - they remain the BOM's own figures - and every assisted "
                 "line stays flagged for review." % (applied, len(pending))),
    })
    return applied, status


def compact_table_edit_context(fields, rows, max_rows=250):
    editable_fields = [
        {
            "key": field.get("key"),
            "label": field.get("label") or field.get("key"),
            "description": field.get("description", ""),
        }
        for field in fields
        if field.get("key")
    ]
    row_payload = []
    for index, row in enumerate(rows[:max_rows], start=1):
        values = {}
        for field in editable_fields:
            key = field["key"]
            value = clean_cell(row.get(key, ""))
            if value != "":
                values[key] = value
        row_payload.append(
            {
                "displayIndex": index,
                "rowId": row.get("__id"),
                "sourceRow": row.get("__sourceRow"),
                "approved": row.get("__approved") is not False,
                "values": values,
            }
        )
    return {
        "fields": editable_fields,
        "rows": row_payload,
        "truncated": len(rows) > max_rows,
        "rowCount": len(rows),
        "includedRowCount": len(row_payload),
    }


def call_llm_table_edit(fields, rows, instruction, full_service_beta=False):
    system = (
        "You edit a normalized Oracle Cloud Infrastructure intake review table. Return compact JSON only. "
        "This table is the user's editable source of truth before pricing. Keep core/OCPU, RAM/memory, storage, application/workload, and environment fields coherent when the user asks for changes. "
        "Use only the provided rowId values and field key values when changing existing rows. "
        "If the user refers to row numbers, use displayIndex to choose the row. "
        "Never invent field keys. Do not change a value unless the user asked for that change or it is a direct consequence. "
        "For remove, exclude, ignore, or do-not-price requests, set that row's approval to false rather than deleting it. "
        "For new rows, return addRows with a values object keyed by known field keys. "
        "Return this exact shape: {summary:string, changes:[{rowId:string, fieldKey:string, value:string|number|boolean}], "
        "rowApprovals:[{rowId:string, approved:boolean}], addRows:[{values:object, approved:boolean}], warnings:[string]}."
    )
    payload = {
        "workflowContract": LLM_WORKFLOW_CONTRACT,
        "instruction": instruction,
        "ociFullServiceBeta": bool(full_service_beta),
        "table": compact_table_edit_context(fields, rows),
    }
    result, warning = call_openai_json(
        system,
        payload,
        max_output_tokens=2600,
        timeout=60,
        model_env="OPENAI_TABLE_EDIT_MODEL",
    )
    if warning:
        if warning == OPENAI_DISABLED_MESSAGE:
            return None, "OpenAI API calls are temporarily disabled; table assistant is unavailable."
        if warning == "OPENAI_API_KEY is not set.":
            return None, "OPENAI_API_KEY is not set; table assistant is unavailable."
        return None, f"OpenAI table edit did not complete. Detail: {warning}"
    return result, None


def coerce_approved(value, default=True):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = normalize(value)
    if text in {"false", "no", "0", "exclude", "excluded", "unapprove", "unapproved"}:
        return False
    if text in {"true", "yes", "1", "include", "included", "approve", "approved"}:
        return True
    return default


def apply_table_edit_plan(fields, rows, edit_plan):
    if not isinstance(edit_plan, dict):
        raise ValueError("The table assistant returned an invalid edit plan.")

    field_keys = [field.get("key") for field in fields if field.get("key")]
    field_key_set = set(field_keys)
    next_rows = [dict(row) for row in rows]
    next_lookup = {row.get("__id"): row for row in next_rows if row.get("__id")}
    applied = []
    warnings_list = [clean_text(item) for item in edit_plan.get("warnings", []) if clean_text(item)]

    for change in edit_plan.get("changes", []):
        if not isinstance(change, dict):
            continue
        row_id = clean_text(change.get("rowId"))
        field_key = clean_text(change.get("fieldKey"))
        if row_id not in next_lookup or field_key not in field_key_set:
            warnings_list.append(f"Skipped change for unknown row or field: {row_id} / {field_key}.")
            continue
        raw_value = change.get("value", "")
        value = normalize_inventory_value(field_key, raw_value)
        next_lookup[row_id][field_key] = value
        applied.append({"rowId": row_id, "fieldKey": field_key, "value": value})

    for approval in edit_plan.get("rowApprovals", []):
        if not isinstance(approval, dict):
            continue
        row_id = clean_text(approval.get("rowId"))
        if row_id not in next_lookup:
            warnings_list.append(f"Skipped approval update for unknown row: {row_id}.")
            continue
        approved = coerce_approved(approval.get("approved"), next_lookup[row_id].get("__approved") is not False)
        next_lookup[row_id]["__approved"] = approved
        applied.append({"rowId": row_id, "fieldKey": "__approved", "value": approved})

    for index, item in enumerate(edit_plan.get("addRows", []), start=1):
        if not isinstance(item, dict):
            continue
        values = item.get("values", {})
        if not isinstance(values, dict):
            continue
        new_row = {
            "__id": f"ai-{int(time.time() * 1000)}-{index}",
            "__sourceRow": "AI edit",
            "__approved": coerce_approved(item.get("approved"), True),
        }
        for field_key in field_keys:
            new_row[field_key] = ""
        for field_key, value in values.items():
            key = clean_text(field_key)
            if key in field_key_set:
                new_row[key] = normalize_inventory_value(key, value)
        next_rows.append(new_row)
        applied.append({"rowId": new_row["__id"], "fieldKey": "__new_row", "value": True})

    return {
        "rows": next_rows,
        "summary": clean_text(edit_plan.get("summary")) or f"Applied {len(applied)} table update(s).",
        "appliedChanges": applied,
        "warnings": warnings_list,
    }


def enrich_with_llm(pricing, llm_payload):
    if not llm_payload:
        return pricing
    mapping_by_row = {item.get("rowId"): item for item in llm_payload.get("mappings", [])}
    for row in pricing["rows"]:
        match = mapping_by_row.get(row["rowId"])
        if not match:
            continue
        row["llmMappedSkus"] = match.get("mappedSkus", [])
        row["llmAssumptions"] = match.get("assumptions", [])
    pricing["engine"] = "llm-assisted"
    pricing["globalAssumptions"] = llm_payload.get("globalAssumptions", [])
    pricing["mappingRules"] = llm_payload.get("mappingRules", [])
    pricing["reviewNotes"] = llm_payload.get("reviewNotes", [])
    return pricing


class IntakeHandler(BaseHTTPRequestHandler):
    server_version = "OCIIntake/1.0"

    def send_bytes(self, status, content_type, data, filename=None, extra_headers=None):
        """Send a response body without treating a closed browser tab as a server error."""
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            # The user navigated away or closed the tab while a response was in flight.
            # That is a normal client disconnect, not a backend failure.
            return False
        return True

    def send_json(self, status, payload):
        encoded = json.dumps(payload).encode("utf-8")
        accept_encoding = clean_text(self.headers.get("Accept-Encoding")).lower()
        if len(encoded) >= 256 * 1024 and "gzip" in accept_encoding:
            encoded = gzip.compress(encoded, compresslevel=6)
            return self.send_bytes(
                status,
                "application/json",
                encoded,
                extra_headers={
                    "Content-Encoding": "gzip",
                    "Vary": "Accept-Encoding",
                },
            )
        return self.send_bytes(status, "application/json", encoded)

    def send_error_json(self, status, message):
        self.send_json(status, {"error": message})

    def read_json_body(self):
        """Read a JSON object request and report client syntax errors as HTTP 400."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            self.send_error_json(400, "Invalid Content-Length header.")
            return None
        raw_body = self.rfile.read(max(0, length))
        content_type = clean_text(self.headers.get("Content-Type")).lower()
        if content_type.startswith("application/json+gzip"):
            try:
                with gzip.GzipFile(fileobj=io.BytesIO(raw_body), mode="rb") as compressed_file:
                    raw_body = compressed_file.read(MAX_DECOMPRESSED_UPLOAD_BYTES + 1)
            except (EOFError, OSError):
                self.send_error_json(400, "The compressed JSON request could not be read.")
                return None
            if len(raw_body) > MAX_DECOMPRESSED_UPLOAD_BYTES:
                self.send_error_json(413, "The decompressed JSON request exceeds 128 MB.")
                return None
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_error_json(400, "Request body must be valid JSON.")
            return None
        if not isinstance(payload, dict):
            self.send_error_json(400, "Request body must be a JSON object.")
            return None
        return payload

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path == "/api/catalog-status":
            # Lets the UI - or an agent - see how current the Oracle SKU catalog is without
            # having to read the price-list file or know the refresh script exists.
            try:
                import bootstrap
                self.send_json(200, bootstrap.catalog_status())
            except Exception as exc:
                self.send_error_json(500, f"Could not read catalog status: {exc}")
            return
        if path == "/api/health":
            self.send_json(
                200,
                {
                    "ok": True,
                    "build": APP_BUILD_TAG,
                    "bareMetalShapes": {
                        _v: [t for t in _tiers if t.get("tier") == "baremetal"]
                        for _v, _tiers in OCI_VENDOR_TIERS.items()
                    },
                    "openaiApiEnabled": openai_api_enabled(),
                    "openaiApiConfigured": openai_api_configured(),
                    "openaiApiConnected": openai_api_enabled() and openai_api_configured(),
                    "openaiModel": os.environ.get("OPENAI_MODEL", DEFAULT_OPENAI_MODEL),
                    "openaiFeatures": list(OPENAI_ACTIVE_FEATURES),
                    "rateCard": build_rate_card(DEFAULT_SHAPE_KEY),
                    "rateCards": all_shape_payloads(),
                    "selectedShape": shape_payload(DEFAULT_SHAPE_KEY),
                    "fullServiceCatalog": price_catalog_payload(),
                },
            )
            return
        if path == "/api/catalog":
            # Searchable OCI service catalog for the "Add OCI services" panel.
            from urllib.parse import parse_qs
            qs = parse_qs(parsed.query)
            q = (qs.get("q") or [""])[0]
            group = (qs.get("group") or [""])[0]
            import oci_catalog
            self.send_json(200, {
                "groups": oci_catalog.groups_with_counts(),
                "results": oci_catalog.search(q, group),
            })
            return
        if path == "/":
            self.serve_file(STATIC_DIR / "index.html")
            return
        if path.startswith("/static/"):
            target = (STATIC_DIR / path.removeprefix("/static/")).resolve()
            if STATIC_DIR.resolve() not in target.parents and target != STATIC_DIR.resolve():
                self.send_error_json(403, "Invalid static path.")
                return
            self.serve_file(target)
            return
        self.send_error_json(404, "Not found.")

    def serve_file(self, path):
        if not path.exists() or not path.is_file():
            self.send_error_json(404, "File not found.")
            return
        content = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        # Never let the browser cache the app shell. Without this, a hard-refresh is the
        # only way to pick up a change to app.js/styles.css, and you end up debugging a
        # stale copy of the frontend against a fresh backend.
        self.send_bytes(
            200,
            mime,
            content,
            extra_headers={"Cache-Control": "no-store, must-revalidate"},
        )

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/upload":
            self.handle_upload()
            return
        if parsed.path == "/api/price":
            self.handle_price()
            return
        if parsed.path == "/api/edit-table":
            self.handle_table_edit()
            return
        if parsed.path == "/api/export":
            self.handle_export()
            return
        if parsed.path == "/api/diagram":
            self.handle_diagram()
            return
        if parsed.path == "/api/load-workflow":
            self.handle_load_workflow()
            return
        if parsed.path == "/api/convert-bom":
            self.handle_convert_bom()
            return
        self.send_error_json(404, "Not found.")

    def handle_convert_bom(self):
        """Accept an uploaded alternate OCI BOM (xlsx/csv), recognize its SKUs/line
        items against the OCI catalog, re-price + recover sizing, and return the app
        pricing-result JSON so the frontend can load it live into the results view."""
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            self.send_error_json(400, "Upload must be multipart/form-data.")
            return
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type,
                     "CONTENT_LENGTH": self.headers.get("Content-Length", "0")},
        )
        if "file" not in form:
            self.send_error_json(400, "Missing file field.")
            return
        file_item = form["file"]
        filename = clean_text(getattr(file_item, "filename", "")) or "bom.xlsx"
        if not filename.lower().endswith((".xlsx", ".xls", ".csv", ".tsv", ".json")):
            self.send_error_json(400, "Please upload an OCI BOM as .xlsx, .xls, .csv, or .json.")
            return
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", filename)
        saved_path = UPLOAD_DIR / f"{int(time.time())}_bom_{safe_name}"
        saved_path.write_bytes(file_item.file.read())
        # Workbooks routinely hold several proposals side by side; the client can name which
        # sheet to convert after seeing the options the first pass reported.
        requested_sheet = ""
        if "sheet" in form:
            requested_sheet = clean_text(form.getfirst("sheet") or "")
        try:
            import bom_convert
            result = bom_convert.convert_oci_bom(saved_path, sheet=requested_sheet or None)
            # Advisory AI pass over whatever the SKU catalog could not identify. It never touches
            # a cost, a quantity or a rate - see foreign_bom_assist and AGENT_POLICY.md.
            try:
                _assist_applied, _assist_status = foreign_bom_assist(result)
                result["aiAssist"] = _assist_status
                if _assist_status.get("ran") and _assist_status.get("note"):
                    result.setdefault("conversionWarnings", []).append(_assist_status["note"])
            except Exception as assist_exc:
                result["aiAssist"] = {"ran": False, "applied": 0,
                                      "note": f"AI assist unavailable: {assist_exc}"}
            result["fileName"] = filename
            result["selectedShape"] = shape_payload(DEFAULT_SHAPE_KEY)
            result["rateCard"] = build_rate_card(DEFAULT_SHAPE_KEY, True)
            result["rateCards"] = all_shape_payloads(True)  # shape options for per-VM remap
            self.send_json(200, result)
        except Exception as exc:
            self.send_error_json(400, f"Could not convert this OCI BOM: {exc}")

    def handle_load_workflow(self):
        """Accept an uploaded workflow file (.json, or an exported .xlsx with the
        hidden _workflow sheet) and return the embedded app state so the frontend
        can recreate the window."""
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            self.send_error_json(400, "Upload must be multipart/form-data.")
            return
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type,
                     "CONTENT_LENGTH": self.headers.get("Content-Length", "0")},
        )
        if "file" not in form:
            self.send_error_json(400, "Missing file field.")
            return
        file_item = form["file"]
        filename = clean_text(getattr(file_item, "filename", "")) or "workflow"
        if not filename.lower().endswith((".json", ".xlsx")):
            self.send_error_json(400, "Please upload a .json or exported .xlsx workflow file.")
            return
        saved_path = UPLOAD_DIR / f"{int(time.time())}_wf_{re.sub(r'[^A-Za-z0-9_.-]+', '_', filename)}"
        saved_path.write_bytes(file_item.file.read())
        try:
            state = bom_export.read_workflow_state(str(saved_path))
            if not state:
                self.send_error_json(400, "No saved workflow found in that file. Export one first (the .xlsx must include the hidden _workflow sheet).")
                return
            self.send_json(200, {"workflow": state})
        except Exception as exc:
            self.send_error_json(400, f"Could not read workflow: {exc}")

    def handle_upload(self):
        content_type = self.headers.get("Content-Type", "")
        compressed_upload = content_type.startswith(("application/gzip", "application/x-gzip"))
        uploaded_bytes = None
        if compressed_upload:
            query = parse_qs(urlparse(self.path).query)
            intake_mode = normalize_intake_mode(
                self.headers.get("X-Intake-Mode")
                or (query.get("intakeMode") or [""])[0]
            )
            provider_hint = normalize_provider_hint(
                self.headers.get("X-Provider-Hint")
                or (query.get("providerHint") or [""])[0]
            )
            full_service_beta = (
                intake_mode == INTAKE_MODE_CLOUD_BILL
                or clean_text(
                    self.headers.get("X-Full-Service-Beta")
                    or (query.get("fullServiceBeta") or [""])[0]
                ).lower()
                in {"1", "true", "yes", "on"}
            )
            filename = Path(
                unquote(
                    clean_text(self.headers.get("X-Upload-Filename"))
                    or (query.get("filename") or [""])[0]
                )
                or "upload.xlsx"
            ).name
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except (TypeError, ValueError):
                self.send_error_json(400, "Invalid compressed upload length.")
                return
            try:
                with gzip.GzipFile(
                    fileobj=io.BytesIO(self.rfile.read(max(0, content_length))),
                    mode="rb",
                ) as compressed_file:
                    uploaded_bytes = compressed_file.read(
                        MAX_DECOMPRESSED_UPLOAD_BYTES + 1
                    )
            except (EOFError, OSError):
                self.send_error_json(400, "The compressed upload could not be read.")
                return
            if len(uploaded_bytes) > MAX_DECOMPRESSED_UPLOAD_BYTES:
                self.send_error_json(413, "The decompressed upload exceeds 128 MB.")
                return
        elif content_type.startswith("multipart/form-data"):
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": content_type,
                    "CONTENT_LENGTH": self.headers.get("Content-Length", "0"),
                },
            )
            if "file" not in form:
                self.send_error_json(400, "Missing file field.")
                return
            file_item = form["file"]
            intake_mode = normalize_intake_mode(form.getvalue("intakeMode"))
            provider_hint = normalize_provider_hint(form.getvalue("providerHint"))
            full_service_beta = (
                intake_mode == INTAKE_MODE_CLOUD_BILL
                or clean_text(form.getvalue("fullServiceBeta")).lower()
                in {"1", "true", "yes", "on"}
            )
            filename = clean_text(getattr(file_item, "filename", "")) or "upload.xlsx"
            uploaded_bytes = file_item.file.read()
        else:
            self.send_error_json(
                400,
                "Upload must be multipart/form-data or a gzip-compressed file.",
            )
            return

        allowed_suffixes = (".xlsx", ".xls", ".csv", ".tsv", ".pdf") if intake_mode == INTAKE_MODE_CLOUD_BILL else (".xlsx", ".xls")
        if not filename.lower().endswith(allowed_suffixes):
            message = "Please upload a PDF, CSV, TSV, or Excel bill export." if intake_mode == INTAKE_MODE_CLOUD_BILL else "Please upload an Excel workbook."
            self.send_error_json(400, message)
            return

        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", filename)
        saved_path = UPLOAD_DIR / f"{int(time.time())}_{safe_name}"
        saved_path.write_bytes(uploaded_bytes)

        # One of OUR exports dropped onto the inventory/bill uploader. Every Full BOM carries
        # the complete saved workflow in a hidden _workflow sheet, so the right answer is to
        # restore it rather than parse it: the visible sheets are OUTPUTS, so reading them as
        # an inventory finds nothing and the upload reports "0 rows · No usable rows were
        # found". The file the user is holding does contain their whole estimate - refusing it
        # because it arrived at the wrong drop zone is a needless dead end.
        if filename.lower().endswith(".xlsx"):
            try:
                saved_workflow = bom_export.read_workflow_state(str(saved_path))
            except Exception:
                saved_workflow = None
            if isinstance(saved_workflow, dict) and (saved_workflow.get("rows") or []):
                self.send_json(200, {
                    "workflowRestore": True,
                    "workflow": saved_workflow,
                    "fileName": filename,
                })
                return

        # In cloud-bill mode, when the user hasn't forced a provider, guess from the
        # filename so parsing/mapping starts from the right cloud.
        filename_guess = guess_provider_from_filename(filename) if intake_mode == INTAKE_MODE_CLOUD_BILL else PROVIDER_AUTO
        effective_hint = provider_hint if provider_hint != PROVIDER_AUTO else filename_guess

        try:
            parsed = parse_workbook(saved_path, full_service_beta, intake_mode, effective_hint)
            parsed["fileName"] = filename
            parsed["uploadedPath"] = str(saved_path)
            # Warn when an already-built comparison/BOM workbook is dropped into cloud-bill
            # mode - its numbers are outputs, not a raw bill, so parsing them produces
            # garbage. Point the user at the "Convert an alternate OCI BOM" flow instead.
            if intake_mode == INTAKE_MODE_CLOUD_BILL and looks_like_comparison_bom(parsed.get("sheets")):
                parsed["comparisonBomWarning"] = (
                    "This looks like an already-built cost-comparison / BOM workbook, not a "
                    "raw cloud bill. Cloud-bill mode expects a raw AWS/Azure/GCP cost export; "
                    "parsing a finished comparison produces wrong totals. Use “Convert an "
                    "alternate OCI BOM” for this file, or upload the original raw bill."
                )
            # Quick pre-flight on every upload: what does this file actually contain?
            # Drives what the app will build vs. leave blank (never fabricate).
            if intake_mode != INTAKE_MODE_CLOUD_BILL:
                try:
                    parsed["dataCheck"] = inventory_data_check(parsed.get("fields"), parsed.get("rows"))
                except Exception:
                    parsed["dataCheck"] = None
            meta = parsed.get("metadata")
            if isinstance(meta, dict):
                meta["filenameGuess"] = filename_guess
                meta["providerHint"] = provider_hint
                meta["providerSource"] = (
                    "user" if provider_hint != PROVIDER_AUTO
                    else "filename" if filename_guess != PROVIDER_AUTO
                    else "content"
                )
            self.send_json(200, parsed)
        except Exception as exc:
            self.send_error_json(500, f"Could not parse workbook: {exc}")

    def handle_price(self):
        try:
            payload = self.read_json_body()
            if payload is None:
                return
            fields = payload.get("fields", [])
            rows = payload.get("rows", [])
            shape_key = payload.get("shape") or DEFAULT_SHAPE_KEY
            intake_mode = normalize_intake_mode(payload.get("intakeMode"))
            full_service_beta = bool(payload.get("fullServiceBeta")) or intake_mode == INTAKE_MODE_CLOUD_BILL
            bom_match = bool(payload.get("bomMatch"))
            hide_gpu_pricing = bool(payload.get("hideGpuPricing"))
            hide_windows_pricing = bool(payload.get("hideWindowsPricing"))
            hide_sql_pricing = bool(payload.get("hideSqlPricing"))
            rightsize = bool(payload.get("rightsize"))
            auto = bool(payload.get("auto"))
            auto_tier = "top" if str(payload.get("autoTier", "best")).lower() == "top" else "best"
            hours_per_month = to_number(payload.get("hoursPerMonth"), 0) or None
            source_provider = normalize_provider_hint(payload.get("providerHint"))
            shape_overrides = payload.get("shapeOverrides") if isinstance(payload.get("shapeOverrides"), dict) else {}
            cost_overrides = payload.get("costOverrides") if isinstance(payload.get("costOverrides"), dict) else {}
            cpu_unit = str(payload.get("cpuUnit", "auto")).lower()
            if cpu_unit not in ("auto", "vcpu", "ocpu"):
                cpu_unit = "auto"
            if shape_key not in SHAPE_LOOKUP:
                self.send_error_json(400, f"Unsupported OCI flex shape: {shape_key}")
                return
            if not isinstance(fields, list) or not fields or not isinstance(rows, list) or not rows:
                self.send_error_json(400, "Pricing requires fields and rows.")
                return
            pricing = calculate_pricing(fields, rows, shape_key, full_service_beta, intake_mode, bom_match, hide_gpu_pricing, hide_windows_pricing, rightsize, auto, hours_per_month, source_provider, auto_tier, shape_overrides, cost_overrides, cpu_unit, hours_override=bool(payload.get('hoursOverride')), oic_message_packs=to_number(payload.get('oicMessagePacks'), 0) or None, hide_sql_pricing=hide_sql_pricing)
            pricing["bomMatch"] = bom_match
            pricing["hideGpuPricing"] = hide_gpu_pricing
            pricing["hideWindowsPricing"] = hide_windows_pricing
            pricing["hideSqlPricing"] = hide_sql_pricing
            pricing["rightsize"] = rightsize
            pricing["auto"] = auto
            pricing["cpuUnit"] = cpu_unit  # requested (may be "auto")
            pricing["engine"] = "deterministic"
            self.send_json(200, pricing)
        except Exception as exc:
            self.send_error_json(500, f"Could not price inventory: {exc}")

    def handle_diagram(self):
        """Build ONLY the architecture diagram from the current pricing and return a zip
        with the generated .png and editable .drawio - no workbook."""
        try:
            payload = self.read_json_body()
            if payload is None:
                return
            fields = payload.get("fields", [])
            rows = payload.get("rows", [])
            converted_pricing = payload.get("convertedPricing")
            is_converted = (
                isinstance(converted_pricing, dict)
                and bool(converted_pricing.get("converted"))
            )
            shape_key = payload.get("shape") or DEFAULT_SHAPE_KEY
            intake_mode = normalize_intake_mode(payload.get("intakeMode"))
            if shape_key not in SHAPE_LOOKUP:
                self.send_error_json(400, f"Unsupported OCI flex shape: {shape_key}")
                return
            valid_rows = isinstance(rows, list) and bool(rows)
            valid_fields = isinstance(fields, list) and bool(fields)
            if not valid_rows or (not is_converted and not valid_fields):
                self.send_error_json(400, "Diagram needs fields and rows.")
                return
            full_service_beta = bool(payload.get("fullServiceBeta")) or intake_mode == INTAKE_MODE_CLOUD_BILL
            rightsize = bool(payload.get("rightsize"))
            auto = bool(payload.get("auto"))
            hide_sql_pricing = bool(payload.get("hideSqlPricing"))
            hours_per_month = to_number(payload.get("hoursPerMonth"), 0) or None
            source_provider = normalize_provider_hint(payload.get("providerHint"))
            auto_tier = "top" if str(payload.get("autoTier", "best")).lower() == "top" else "best"
            shape_overrides = payload.get("shapeOverrides") if isinstance(payload.get("shapeOverrides"), dict) else {}
            cost_overrides = payload.get("costOverrides") if isinstance(payload.get("costOverrides"), dict) else {}
            cpu_unit = str(payload.get("cpuUnit", "auto")).lower()
            if cpu_unit not in ("auto", "vcpu", "ocpu"):
                cpu_unit = "auto"
            bom_name = clean_text(payload.get("bomName"))
            extra_services = payload.get("extraServices") if isinstance(payload.get("extraServices"), list) else []
            diagram_options = payload.get("diagramOptions") if isinstance(payload.get("diagramOptions"), dict) else {}

            if is_converted:
                pricing = converted_pricing
            else:
                pricing = calculate_pricing(fields, rows, shape_key, full_service_beta, intake_mode, False,
                                            bool(payload.get("hideGpuPricing")), bool(payload.get("hideWindowsPricing")),
                                            rightsize, auto, hours_per_month, source_provider, auto_tier,
                                            shape_overrides, cost_overrides, cpu_unit,
                                            hours_override=bool(payload.get("hoursOverride")),
                                            oic_message_packs=to_number(payload.get("oicMessagePacks"), 0) or None,
                                            hide_sql_pricing=hide_sql_pricing)

            import bom_diagram, bom_template, tempfile, zipfile, io
            keys = bom_template._resolve_inventory_keys(fields) if fields else {}
            shp = shape_payload(shape_key)
            out_dir = tempfile.mkdtemp(prefix="ocidiag_")
            extra_priced = None
            if extra_services:
                import oci_catalog
                extra_priced, _ = oci_catalog.price_extras(
                    extra_services, hours_per_month or HOURS_PER_MONTH)
            diagram_options, architecture_plan = architecture_options_with_ai(
                pricing,
                rows,
                fields,
                diagram_options,
                extra_services,
                bom_name,
                shp.get("shortLabel") or shp.get("label") or "",
            )
            drawio, png = bom_diagram.build_architecture(
                pricing, rows, keys, bom_name,
                shp.get("shortLabel") or shp.get("label") or "",
                out_dir=out_dir,
                sites=bom_template._distinct_sites(fields, rows) if fields else None,
                extra_priced=extra_priced, diagram_options=diagram_options)
            if not drawio and not png:
                self.send_error_json(500, "Could not build the architecture diagram (no workloads found).")
                return
            artifact_qa = architecture_artifact_qa(drawio, png)
            architecture_plan["artifactQa"] = artifact_qa
            if not artifact_qa["passed"]:
                self.send_error_json(
                    500,
                    "Architecture output failed validation: "
                    + " ".join(artifact_qa["issues"]),
                )
                return

            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", bom_name).strip("_") or "OCI"
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                if png and Path(png).exists():
                    z.writestr(f"{safe}_architecture.png", Path(png).read_bytes())
                if drawio and Path(drawio).exists():
                    z.writestr(f"{safe}_architecture.drawio", Path(drawio).read_bytes())
            formats = []
            if drawio and Path(drawio).exists():
                formats.append("drawio")
            if png and Path(png).exists():
                formats.append("png")
            data = buf.getvalue()
            self.send_bytes(
                200,
                "application/zip",
                data,
                filename=f"{safe}_architecture.zip",
                extra_headers={
                    "X-Architecture-Formats": ",".join(formats),
                    "X-Architecture-AI": architecture_plan["status"],
                    "X-Architecture-Model": architecture_plan["model"],
                },
            )
        except Exception as exc:
            traceback.print_exc()
            self.send_error_json(500, f"Could not build diagram: {type(exc).__name__}: {exc}")

    def handle_export(self):
        try:
            payload = self.read_json_body()
            if payload is None:
                return
            fields = payload.get("fields", [])
            rows = payload.get("rows", [])
            shape_key = payload.get("shape") or DEFAULT_SHAPE_KEY
            intake_mode = normalize_intake_mode(payload.get("intakeMode"))
            full_service_beta = bool(payload.get("fullServiceBeta")) or intake_mode == INTAKE_MODE_CLOUD_BILL
            bom_match = bool(payload.get("bomMatch"))
            hide_gpu_pricing = bool(payload.get("hideGpuPricing"))
            hide_windows_pricing = bool(payload.get("hideWindowsPricing"))
            hide_sql_pricing = bool(payload.get("hideSqlPricing"))
            rightsize = bool(payload.get("rightsize"))
            auto = bool(payload.get("auto"))
            hours_per_month = to_number(payload.get("hoursPerMonth"), 0) or None
            source_provider = normalize_provider_hint(payload.get("providerHint"))
            auto_tier = "top" if str(payload.get("autoTier", "best")).lower() == "top" else "best"
            shape_overrides = payload.get("shapeOverrides") if isinstance(payload.get("shapeOverrides"), dict) else {}
            cost_overrides = payload.get("costOverrides") if isinstance(payload.get("costOverrides"), dict) else {}
            cpu_unit = str(payload.get("cpuUnit", "auto")).lower()
            if cpu_unit not in ("auto", "vcpu", "ocpu"):
                cpu_unit = "auto"
            bom_name = clean_text(payload.get("bomName"))
            oci_discount = to_number(payload.get("ociDiscount"), 0)
            if oci_discount < 0:
                oci_discount = 0.0
            if oci_discount > 1:
                oci_discount = oci_discount / 100.0  # accept either fraction or percent
            ramp = payload.get("ramp")
            existing_infra_cost = payload.get("existingInfraCost", 0)
            # Full app workflow state to embed (so the file can be re-imported to
            # recreate the window). Sent by the frontend as a JSON-serializable object.
            workflow_state = payload.get("workflowState")
            workflow_json = json.dumps(workflow_state) if workflow_state else None

            # A converted OCI BOM is already priced - export it in the AWS cloud-compare
            # workbook format directly from the converted pricing (no re-pricing, and it
            # has no on-prem fields, so this must run before the fields/shape checks).
            if payload.get("converted"):
                conv = payload.get("convertedPricing") or {"rows": rows, "totals": {}}
                content = bom_export.build_cloud_comparison_bytes(conv, payload.get("ramp"), bom_name, oci_discount, workflow_json, extra_services=payload.get("extraServices") or [], hours=hours_per_month or HOURS_PER_MONTH)
                safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", bom_name).strip("_") if bom_name else ""
                download_name = f"{safe_name}.xlsx" if safe_name else "OCI_BOM_Converted.xlsx"
                self.send_bytes(
                    200,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    content,
                    filename=download_name,
                )
                return

            if shape_key not in SHAPE_LOOKUP:
                self.send_error_json(400, f"Unsupported OCI flex shape: {shape_key}")
                return
            if not isinstance(fields, list) or not fields or not isinstance(rows, list) or not rows:
                self.send_error_json(400, "Export requires fields and rows.")
                return
            pricing = calculate_pricing(fields, rows, shape_key, full_service_beta, intake_mode, bom_match, hide_gpu_pricing, hide_windows_pricing, rightsize, auto, hours_per_month, source_provider, auto_tier, shape_overrides, cost_overrides, cpu_unit, hours_override=bool(payload.get('hoursOverride')), oic_message_packs=to_number(payload.get('oicMessagePacks'), 0) or None, hide_sql_pricing=hide_sql_pricing)

            # "Full BOM": the 12-sheet customer-facing deliverable (Table of Contents,
            # Assumptions, Rate Card, Pricing Overview, Compute, Storage, Networking, DR,
            # Security KMS, Consumption Ramp, Annexure, Applications Migrated to OCI).
            if str(payload.get("template", "quick")).lower() == "full":
                import bom_template
                # Cloud-bill Full BOM: the 12-sheet deliverable PLUS the AWS->OCI bill sheets
                # appended, so all service data and mappings come along.
                cloud_comparison = None
                if intake_mode == INTAKE_MODE_CLOUD_BILL:
                    cloud_comparison = {
                        "pricing": pricing,
                        "ramp": payload.get("ramp"),
                        "ociDiscount": oci_discount,
                        "extraServices": payload.get("extraServices") or [],
                        "hours": hours_per_month or HOURS_PER_MONTH,
                    }
                else:
                    # On-prem gets the same Pricing Overview treatment - editable OCI discount,
                    # 5-year projection, savings and chart - with current on-prem spend standing
                    # in for the uploaded bill. The baseline is whatever the user entered in the
                    # app; it is written as an editable cell, so leaving it at zero gives the
                    # customer a blank to fill in rather than a fabricated number.
                    cloud_comparison = {
                        "pricing": pricing,
                        "ramp": payload.get("ramp"),
                        "ociDiscount": oci_discount,
                        "extraServices": payload.get("extraServices") or [],
                        "hours": hours_per_month or HOURS_PER_MONTH,
                        "onPrem": True,
                        "baselineMonthly": to_number(existing_infra_cost, 0),
                    }
                full_diagram_options, full_architecture_plan = architecture_options_with_ai(
                    pricing,
                    rows,
                    fields,
                    payload.get("diagramOptions") or {},
                    payload.get("extraServices") or [],
                    bom_name,
                    (shape_payload(shape_key).get("shortLabel")
                     or shape_payload(shape_key).get("label")
                     or ""),
                )
                # Every rate the workbook prices with comes from the app's own catalog -
                # never from the numbers the source template happened to ship with. If a
                # SKU rate changes in app.py, the exported Rate Card changes with it.
                content = bom_template.build_full_bom_bytes(
                    pricing, rows, fields, payload.get("ramp"), bom_name,
                    shape_payload(shape_key), hours_per_month or HOURS_PER_MONTH,
                    block_rate=storage_rate("B91961"),
                    vpu_rate=storage_rate("B91962"),
                    default_vpus=BLOCK_PERFORMANCE_UNITS_PER_GB,
                    file_rate=storage_rate("B89057"),
                    windows_rate=WINDOWS_LICENSE_RATE,
                    windows_sku=WINDOWS_LICENSE_SKU,
                    # Services added from the "Add OCI services" panel.
                    extra_services=payload.get("extraServices") or [],
                    # Optimization mirrors the app: Rightsize already trimmed the OCPU/RAM
                    # quantities we export, so the template takes no extra OCPU discount.
                    optimization=0.0,
                    cloud_comparison=cloud_comparison,
                    diagram_options=full_diagram_options,
                    # Embed the app workflow so a Full BOM can be re-imported via "Load previous BOM".
                    workflow_json=workflow_json,
                )
                safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", bom_name).strip("_") if bom_name else ""
                download_name = f"{safe_name}_Full_BOM.xlsx" if safe_name else "OCI_Full_BOM.xlsx"
                self.send_bytes(
                    200,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    content,
                    filename=download_name,
                    extra_headers={
                        "X-Architecture-AI": full_architecture_plan["status"],
                        "X-Architecture-Model": full_architecture_plan["model"],
                    },
                )
                return

            # Cloud bill mode exports the AWS->OCI comparison workbook (reference style),
            # not the on-prem BOM-script workbook.
            if intake_mode == INTAKE_MODE_CLOUD_BILL:
                content = bom_export.build_cloud_comparison_bytes(pricing, payload.get("ramp"), bom_name, oci_discount, workflow_json, extra_services=payload.get("extraServices") or [], hours=hours_per_month or HOURS_PER_MONTH)
                safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", bom_name).strip("_") if bom_name else ""
                download_name = f"{safe_name}.xlsx" if safe_name else "OCI_Cloud_Bill_Comparison.xlsx"
                self.send_bytes(
                    200,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    content,
                    filename=download_name,
                )
                return

            eff_hours = hours_per_month if (hours_per_month and hours_per_month > 0) else HOURS_PER_MONTH
            servers = bom_export.servers_from_pricing(pricing, rows)
            shape = pricing.get("selectedShape") or {}
            shape_for_export = {
                "label": shape.get("shortLabel") or shape.get("label"),
                "shortLabel": shape.get("shortLabel") or shape.get("label"),
                "computeSku": shape.get("computeSku"),
                "memorySku": shape.get("memorySku"),
                "computeRate": shape.get("computeRate"),
                "memoryRate": shape.get("memoryRate"),
            }
            # If the source data is AWS/Azure, prefill the Overview's existing-cost cell
            # with that cloud's estimated annual spend and relabel it accordingly.
            existing_label = "Existing Infra Cost (enter):"
            source_cloud = _dominant_source_cloud(pricing, source_provider)
            if source_cloud:
                cc_best = (pricing.get("crossCloud") or {}).get("bestMatch") or {}
                annual = (cc_best.get(source_cloud) or {}).get("annualTotal")
                if annual:
                    existing_infra_cost = annual
                    existing_label = f"Existing {'AWS' if source_cloud == 'aws' else 'Azure'} Cost:"
            content = bom_export.build_workbook_bytes(servers, ramp, existing_infra_cost, shape_for_export, hide_windows_pricing, eff_hours, bom_name, auto, existing_label, oci_discount, extra_services=payload.get("extraServices") or [])
            safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", bom_name).strip("_") if bom_name else ""
            download_name = f"{safe_name}.xlsx" if safe_name else "OCI_BOM_Export.xlsx"
            self.send_bytes(
                200,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                content,
                filename=download_name,
            )
        except Exception as exc:
            # Print the traceback. A bare message here made export failures undebuggable -
            # the browser just saw "Could not export workbook" with no idea why.
            traceback.print_exc()
            self.send_error_json(500, f"Could not export workbook: {type(exc).__name__}: {exc}")

    def handle_table_edit(self):
        self.send_error_json(
            410,
            "The table assistant is disabled. AI is used only for upload scrubbing and architecture planning.",
        )

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")


APP_BUILD_TAG = "onprem-rulebased-preferred-2026-07-27"


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), IntakeHandler)
    print(f"OCI Intake app running at http://127.0.0.1:{PORT}")
    print(f">>> BUILD {APP_BUILD_TAG} <<<  (rule-based parser preferred for on-prem sizing)")
    server.serve_forever()


if __name__ == "__main__":
    main()
