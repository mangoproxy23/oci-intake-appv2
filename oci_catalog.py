"""Searchable OCI service catalog for the "Add OCI services" panel.

The results page lets a user look up OCI services (Networking, Storage, PaaS, ...), fill in
sizing, and add them to the BOM - the way Oracle's own Cost Estimator works. This module is
the catalog behind that search.

Two layers:
  1. CURATED services - the things a solutions engineer actually adds to a BOM, each with a
     verified rate and explicit sizing fields (GB, count, ports, OCPU, ...). Rates are the
     app's own price data (data/oci_price_list.json / oci_service_prices.json), NOT invented.
  2. RAW search fallback - full-text over all 629 price-list SKUs so nothing is unreachable;
     these add as a plain quantity x unit rate.

Every entry declares a `basis` so the monthly cost is computed one way everywhere:
    hour   -> rate * qty * HOURS_PER_MONTH      (per-OCPU-hour, per-port-hour, ...)
    month  -> rate * qty                        (per-GB-month, per-instance-month, ...)
    op     -> rate * qty                        (per-1M-calls etc.; qty is in the SKU's unit)
    once   -> rate * qty                        (one-off; shown but not multiplied by hours)
"""

import json
import math
import re
from pathlib import Path

HOURS_PER_MONTH = 730

# Autonomous AI Database (serverless) rates - customer-supplied OCI price-list values.
ADB_ECPU_RATE = 0.336       # per ECPU-hour (B95702 ATP / B95701 ADW; B95713/B95712 dedicated)
ADB_STORAGE_ATP = 0.1953    # ATP / AJD / APEX database storage per GB-month (B95706)
ADB_STORAGE_ADW = 0.0299    # ADW / Lakehouse database storage per GB-month (B95754)
ADB_BACKUP_RATE = 0.0299    # serverless Autonomous DB backup storage per GB-month (B95754)
# Dedicated (Exadata Cloud Infrastructure) - Hosted Environment per hour, X11M.
ADB_EXA_DB_SERVER = 6.3014      # Exadata Database Server per hour (B112666)
ADB_EXA_STORAGE_SERVER = 5.4795  # Exadata Storage Server per hour (B112667)
ADB_OBJ_BACKUP = 0.0255     # dedicated backup -> Object Storage per GB-month (B91628)
ADB_OBJ_BACKUP_FREE = 10    # first 10 GB of object-storage backup is free

# Flexible Load Balancer - two meters, both with a free allowance in unit-hours (estimator
# service 885). The base is per LB-hour; bandwidth is per Mbps-hour and is what actually scales.
LB_BASE_RATE = 0.0113        # per load-balancer-hour after the first 744 LB-hours (B93030)
LB_BANDWIDTH_RATE = 0.0001   # per Mbps-hour after the first 7,440 Mbps-hours (B93031)

# Oracle Integration Cloud (OIC) - per 5,000-messages/hour "message pack" per hour.
OIC_STD_RATE = 0.6452       # Standard edition, per message-pack-hour (B89639)
OIC_ENT_RATE = 1.2903       # Enterprise edition, per message-pack-hour (B109559)
OIC_MSG_PER_PACK_HR = 5000  # 1 pack = 5,000 messages/hour (payload <=50KB each)
# Pack sizing:
#   surge   -> ceil(peak daily volume / (24 hrs * 5,000))   [OCI estimator "surge" path]
#   monthly -> ceil(total messages per month / (hours * 5,000))   (e.g. 730*5,000 = 3.65M/pack)


# MySQL HeatWave Database Service - customer-supplied OCI price-list values.
MYSQL_ECPU_RATE = 0.0366     # MySQL Database ECPU per hour (B108030)
MYSQL_STORAGE_RATE = 0.04    # MySQL storage / backup / inter-region egress per GB-mo (B92426/B92483/B109169)
MYSQL_HW_RATE = 0.011        # HeatWave capacity per hour (B96626)
MYSQL_HW_STORAGE_RATE = 0.02  # HeatWave storage per GB-mo (B96625)

# OCI Database with PostgreSQL - customer-supplied OCI price-list values.
PG_MANAGED_OCPU_RATE = 0.098   # managed PostgreSQL OCPU per hour (B99060)
PG_STORAGE_RATE = 0.072        # database-optimized storage per GB-mo (B99062)
PG_COMPUTE_OCPU_RATE = 0.03    # underlying AMD E5 compute OCPU per hour (B97384)
PG_COMPUTE_MEM_RATE = 0.002    # underlying AMD E5 compute memory per GB-hr (B97385)
PG_COMPUTE_OCPU_RATE_INTEL = 0.04    # Intel X9 compute OCPU per hour
PG_COMPUTE_MEM_RATE_INTEL = 0.0015   # Intel X9 compute memory per GB-hr
PG_VPU_RATE = 0.0017           # block-volume performance units per GB-mo (B91962)

# Object Storage - tiered with free allowances.
OBJ_STORAGE_RATE = 0.0255          # Standard, per GB-month after free tier (B91628)
OBJ_STORAGE_FREE_GB = 10           # first 10 GB/month free
OBJ_IA_STORAGE_RATE = 0.0100       # Infrequent Access, per GB-month (B93000)
OBJ_IA_RETRIEVAL_RATE = 0.0100     # Infrequent Access, per GB retrieved (B93001)
OBJ_IA_RETRIEVAL_FREE_GB = 10      # first 10 GB retrieved/month free
ARCHIVE_STORAGE_RATE = 0.0026      # Archive, per GB-month after free tier (B91633)
OBJ_REQUEST_RATE = 0.0034          # per 10,000 requests after free tier (B91627)
OBJ_REQUEST_FREE_UNITS = 5         # first 50,000 requests (5 units of 10k) free

# Web Application Firewall - instance + request tiers with free allowances.
WAF_INSTANCE_RATE = 5.00       # per WAF instance per month after the first (B94579)
WAF_INSTANCE_FREE = 1          # first instance free
WAF_REQUEST_RATE = 0.60        # per 1,000,000 incoming requests after the free tier (B94277)
WAF_REQUEST_FREE = 10          # first 10,000,000 requests (10 units of 1M) free

# Key Management / Vault. Software key versions (B92092) are free; the paid options:
KMS_VAULT_RATE = 3.724     # Virtual Private Vault per hour (B90328)
KMS_EXTERNAL_RATE = 3.00   # External Key Management per key version-month (B98100)
KMS_HSM_RATE = 1.75        # Dedicated Key Management HSM partition per hour (B99597, min 3)

# OCI Full Stack Disaster Recovery, metered per member per hour, summed across the
# primary AND standby protection groups. Compute + Database member OCPUs bill at the OCPU
# rate; Database member ECPUs at the ECPU rate; OIC message packs at the 5K-msg rate.
FSDR_OCPU_RATE = 0.0128     # OCPU per hour (B95485)
FSDR_ECPU_RATE = 0.0032     # ECPU per hour (B110274)
FSDR_OIC_RATE = 0.192       # 5K messages per hour, per OIC message pack (B112110)

# Microsoft SQL Server license-included (OCI marketplace compute image), per OCPU-hour.
SQL_ENT_RATE = 1.47        # SQL Server Enterprise (B91372)
SQL_STD_RATE = 0.37        # SQL Server Standard (B91373)
# SQL Server Express is free ($0).

# Secure Desktops - desktop fee + underlying E6 compute + block volumes (boot + optional).
DESKTOP_UNIT_RATE = 20.00      # Secure Desktop per month (B95518)
DESKTOP_OCPU_RATE = 0.03       # E6 Standard compute OCPU per hour (B111129)
DESKTOP_MEM_RATE = 0.002       # E6 Standard compute memory per GB-hr (B111130)
DESKTOP_BLOCK_RATE = 0.0255    # Block Volume storage per GB-mo (B91961)
DESKTOP_VPU_RATE = 0.0017      # Block Volume performance units per GB-mo (B91962)
# Windows-BYOL-on-DVH mode: desktops run on Dedicated VM Host(s) (DVH.Standard.E4.128).
DESKTOP_E4_OCPU_RATE = 0.025   # E4 compute OCPU per hour (B93113)
DESKTOP_E4_MEM_RATE = 0.0015   # E4 compute memory per GB-hr (B93114)
DVH_HOST_OCPU = 128            # DVH.Standard.E4.128 total OCPUs (billed)
DVH_HOST_MEM = 2048            # DVH.Standard.E4.128 total memory GB (billed)
DVH_AVAIL_OCPU = 124           # OCPUs available for desktops per host (128 - 4 reserved)


def oic_packs(values, hours):
    """Approximate message packs from the sizing inputs (surge peak daily volume wins,
    else total monthly messages, else the directly-entered pack count)."""
    import math
    peak = float((values.get("peakday") if values else 0) or 0)
    monthly = float((values.get("monthlymsgs") if values else 0) or 0)
    if peak > 0:
        return math.ceil(peak / (24 * OIC_MSG_PER_PACK_HR))
    if monthly > 0:
        return math.ceil(monthly / (float(hours or HOURS_PER_MONTH) * OIC_MSG_PER_PACK_HR))
    return float((values.get("packs") if values else 0) or 0)
DATA = Path(__file__).resolve().parent / "data"


def _price_list():
    items = json.loads((DATA / "oci_price_list.json").read_text()).get("items", [])
    return {it["sku"]: it for it in items if it.get("sku")}


def _service_prices():
    return json.loads((DATA / "oci_service_prices.json").read_text()).get("services", {})


_PRICES = _price_list()
_SVC = _service_prices()

_FASTCONNECT = _SVC.get("OCI FastConnect") or {}
_FASTCONNECT_SOURCE_RATES = _FASTCONNECT.get("speedRates") or {}
_FASTCONNECT_SOURCE_SKUS = _FASTCONNECT.get("speedSkus") or {}
FASTCONNECT_SPEED_RATES = {
    "1G": float(_FASTCONNECT_SOURCE_RATES.get("1G", 0.2125)),
    "10G": float(_FASTCONNECT_SOURCE_RATES.get("10G", 1.275)),
    "100G": float(_FASTCONNECT_SOURCE_RATES.get("100G", 10.75)),
    "400G": float(_FASTCONNECT_SOURCE_RATES.get("400G", 20.00)),
}
FASTCONNECT_SPEED_SKUS = {
    "1G": str(_FASTCONNECT_SOURCE_SKUS.get("1G") or "B88325"),
    "10G": str(_FASTCONNECT_SOURCE_SKUS.get("10G") or "B88326"),
    "100G": str(_FASTCONNECT_SOURCE_SKUS.get("100G") or "B93126"),
    "400G": str(_FASTCONNECT_SOURCE_SKUS.get("400G") or "B107975"),
}
FASTCONNECT_SPEED_LABELS = {
    "1G": "1 Gbps",
    "10G": "10 Gbps",
    "100G": "100 Gbps",
    "400G": "400 Gbps",
}


def _rate(sku, fallback=None):
    """PAYG rate for a SKU straight from the app's price list.

    Careful: the raw price list's `payg` is sometimes a *bundle factor* (e.g. a Load
    Balancer row lists 13 for "13 Mbps", API Gateway lists 1,000,000 for "per 1M calls"),
    not a dollar rate. For anything with a verified per-unit rate, use `_svc_rate` instead.
    """
    it = _PRICES.get(sku)
    if it and isinstance(it.get("payg"), (int, float)):
        return float(it["payg"])
    return fallback


def _svc_rate(name, key="rate", fallback=None):
    """Verified per-unit rate from oci_service_prices.json (the clean, hand-checked file)."""
    svc = _SVC.get(name) or {}
    v = svc.get(key)
    return float(v) if isinstance(v, (int, float)) else fallback


def _norm(s):
    """Lowercase, punctuation-free text for matching (search, icon keywords, 3rd-party terms)."""
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).strip()


def _sf(key, label, unit, default=0, step=1, min_=0, show_when=None, hide_when=None):
    """One numeric sizing field the user fills in. `show_when` = (field_key, value) shows the
    field only when another (select) field has that value; `hide_when` hides it when so."""
    f = {"key": key, "label": label, "unit": unit, "default": default,
         "step": step, "min": min_}
    if show_when:
        f["showWhen"] = {"field": show_when[0], "value": _when_value(show_when[1])}
    if hide_when:
        f["hideWhen"] = {"field": hide_when[0], "value": _when_value(hide_when[1])}
    return f


def _when_value(value):
    """showWhen/hideWhen accept one value or many; many are sent as a "|"-joined string.

    The Compute VM card needs "show the Memory box for any of the 15 flexible shapes", which a
    single value cannot express. The client splits on "|", so a lone value is unchanged.
    """
    if isinstance(value, (list, tuple, set)):
        return "|".join(str(v) for v in value)
    return value


def _sel(key, label, options, default, show_when=None, hide_when=None):
    """A dropdown field. `options` is a list of (value, label) pairs; the selected value
    is a string used by the entry's cost function (not multiplied). show_when / hide_when
    work exactly as they do on _sf, so one card can swap whole sets of controls - the Base
    Database card shows a different edition list per billing metric."""
    f = {"key": key, "label": label, "unit": "", "default": default,
         "options": [{"value": v, "label": l} for v, l in options]}
    if show_when:
        f["showWhen"] = {"field": show_when[0], "value": _when_value(show_when[1])}
    if hide_when:
        f["hideWhen"] = {"field": hide_when[0], "value": _when_value(hide_when[1])}
    return f


# --- curated, fillable services -------------------------------------------------------------
# group order mirrors data/service_comp_list.json so the chips read like Oracle's console.
GROUPS = ["Compute", "Storage", "Networking", "Database", "Integration", "Security",
          "Observability", "Analytics", "AI & Machine Learning", "Licensing", "Other Services"]

# Every curated service has an explicit, bundled OCI icon contract. "fallback" means the
# selected icon is the closest honest visual available in the bundled Oracle library.
ARCHITECTURE_ICON_BY_ID = {
    "block": ("Storage - Block Storage", "direct"),
    "object": ("Storage - Object Storage", "direct"),
    "object_ia": ("Storage - Object Storage", "direct"),
    "file": ("Storage - File Storage", "direct"),
    "archive": ("Storage - Object Storage", "direct"),
    "lb": ("Networking - Flexible Load Balancer", "direct"),
    "egress": ("Networking - Service Gateway", "fallback"),
    "fastconnect": ("Networking - Dynamic Routing Gateway DRG", "fallback"),
    "dns": ("Networking - DNS", "direct"),
    "adb": ("Database - Autonomous DB", "direct"),
    "mysql": ("Database - MySQL", "direct"),
    "pg": ("Database - Database System", "fallback"),
    "dbbackup": ("Storage - Object Storage", "fallback"),
    "recovery": ("Storage - Object Storage", "fallback"),
    "oic": ("Developer Services - Integrations", "direct"),
    "waf": ("Identity and Security - WAF", "direct"),
    "kms": ("Identity and Security - Vault", "direct"),
    "fsdr": ("Governance and Administration - Cloud Advisor", "fallback"),
    "logging": ("Observability and Management - Logging", "direct"),
    "desktops": ("Compute - Virtual Machine VM", "fallback"),
    "winlic": ("Compute - Virtual Machine VM", "fallback"),
    "sqllic": ("Database - Database System", "fallback"),
    "genai": ("Analytics and AI", "fallback"),
    "genai_agents": ("Analytics and AI", "fallback"),
    "vision": ("Analytics and AI", "fallback"),
    "docunderstanding": ("Analytics and AI", "fallback"),
    "language": ("Analytics and AI", "fallback"),
    "speech": ("Analytics and AI", "fallback"),
    "queue": ("Developer Services - Integrations", "fallback"),
    "basedb": ("Database - Database System", "direct"),
}

# Base Database Service (VM). Every part number, rate and metric below was read directly off
# the cost estimator's own "Pricing Details" panel, so these are confirmed pairings - not
# inferred. An instance always bills THREE lines: the shared compute infrastructure
# (B112724), the edition licence, and database storage (B111584).
BASEDB_INFRA_SKU, BASEDB_INFRA_RATE = "B112724", 0.0251      # ECPU per hour
BASEDB_STORAGE_SKU, BASEDB_STORAGE_RATE = "B111584", 0.12    # GB per month
BASEDB_EDITIONS = [
    ("standard", "Standard - x86", 0.0538, "ECPU / hour", "B112725"),
    ("enterprise", "Enterprise - x86", 0.1075, "ECPU / hour", "B112726"),
    ("high_perf", "Enterprise High Performance - x86", 0.2218, "ECPU / hour", "B112727"),
    ("byol", "BYOL - x86", 0.0484, "ECPU / hour", "B112728"),
]
BASEDB_EDITIONS_BY_KEY = {k: r for k, _l, r, _u, _s in BASEDB_EDITIONS}

# Usable database storage is a FIXED tier list, not a free-form number, and the bill is raised
# on the TOTAL provisioned capacity - which includes the redo/reco overhead Oracle adds on top
# of the usable figure, and is not proportional to it (256 GB usable provisions 717 GB, but
# 512 GB usable provisions only 973). Charging the usable number would under-quote every tier,
# by 63% at the smallest. usable GB -> total capacity GB, read off the cost estimator.
BASEDB_STORAGE_TIERS = [
    (256, 717), (512, 973), (1024, 1741), (2048, 2765),
    (4096, 5325), (8192, 10445), (16384, 20685), (24576, 30925),
    (32768, 41165), (40960, 51405), (49152, 61645),
    # Intel/Enterprise continues in 8,192 GB steps to 81,920. Provisioned capacity for these is
    # NOT known - see BASEDB_UNPRICED_TIERS - so the ECPU figures here are placeholders only.
    (57344, 0), (65536, 0), (73728, 0), (81920, 0),
]
BASEDB_TOTAL_BY_USABLE = {u: t for u, t in BASEDB_STORAGE_TIERS}


# The OCPU-metered flavour of the SAME service. It is not just a different rate: there is no
# separate compute-infrastructure SKU, and storage is billed as ordinary BLOCK VOLUME (capacity
# + performance units) rather than the database-storage SKU the ECPU flavour uses. Only the
# Enterprise part number is confirmed from an estimator panel; the rates are the price list's.
BASEDB_OCPU_EDITIONS = [
    ("standard", "Standard", 0.215, "OCPU / hour", "B90569"),
    ("enterprise", "Enterprise", 0.4301, "OCPU / hour", "B90570"),
    ("high_perf", "Enterprise High Performance", 0.8871, "OCPU / hour", "B90571"),
    ("extreme", "Enterprise Extreme Performance", 1.3441, "OCPU / hour", "B90572"),
    ("byol", "BYOL", 0.1935, "OCPU / hour", "B90573"),
]
BASEDB_OCPU_BY_KEY = {k: r for k, _l, r, _u, _s in BASEDB_OCPU_EDITIONS}

# Arm (Ampere) is a DIFFERENT part number and a different rate from the x86 editions above -
# Enterprise is $0.2151/OCPU-hr on Arm vs $0.4301 on x86, so the processor choice halves the
# licence. (Shape within a processor family does NOT matter: VM.Standard.E4.Flex and E5.Flex
# price identically. It's the architecture that moves the rate, not the shape.)
BASEDB_ARM_EDITIONS = [
    ("enterprise", "Enterprise (Arm)", 0.2151, "OCPU / hour", "B97197"),
    ("high_perf", "Enterprise High Performance (Arm)", 0.4436, "OCPU / hour", "B97198"),
    ("extreme", "Enterprise Extreme Performance (Arm)", 0.6721, "OCPU / hour", "B97199"),
    ("byol", "BYOL (Arm)", 0.0968, "OCPU / hour", "B97200"),
    ("developer", "Developer - Ampere A1 (fixed 1 OCPU / 50 GB)", 0.022, "OCPU / hour", "B109635"),
]
BASEDB_ARM_BY_KEY = {k: r for k, _l, r, _u, _s in BASEDB_ARM_EDITIONS}
# Arm offers a SHORTER storage list than x86 - the estimator's dropdown stops at 8,192 GB usable
# (x86 runs to 40,960) - and only one shape, VM.Standard.A1.Flex, so there is no shape choice to
# make. Quoting an Arm database above this cap would describe a configuration you can't order.
BASEDB_ARM_MAX_USABLE_GB = 8192
BASEDB_ARM_SHAPE = "VM.Standard.A1.Flex"
# Developer is a FIXED configuration, not a sizing choice: the estimator locks it to 1 OCPU and
# 50 GB usable (300 GB provisioned) and drops the block volume to 10 VPU/GB (Balanced) rather
# than the 20 (High performance) every other edition defaults to. Honouring whatever the user
# typed in the OCPU or storage box would quote a shape Oracle won't provision.
BASEDB_DEVELOPER_FIXED = {"ocpu": 1, "usable_gb": 50, "provisioned_gb": 300, "vpus": 10}

# Intel offers FIXED shapes alongside the flexible one, and on a fixed shape the trailing number
# IS the OCPU count - VM.Standard2.4 is 4 OCPUs at 15 GB each, and the estimator greys the OCPU
# box out rather than letting you type. Quoting 8 OCPUs on a VM.Standard2.1 would price a
# machine that cannot be provisioned, so a fixed shape overrides whatever OCPU was entered.
# ocpu of None means the shape is flexible and the user's OCPU value stands.
BASEDB_SHAPES = {
    # OCPU = the trailing number. Memory is 15 GB x OCPU for 2.1 through 2.16, but 2.24 caps
    # at 320 GB rather than the 360 that rule predicts - so the rule is a guide, not a law, and
    # every row here is panel-confirmed rather than extrapolated.
    "VM.Standard2.1": {"ocpu": 1, "memory_gb": 15, "confirmed": True},
    "VM.Standard2.2": {"ocpu": 2, "memory_gb": 30, "confirmed": True},
    "VM.Standard2.4": {"ocpu": 4, "memory_gb": 60, "confirmed": True},
    "VM.Standard2.8": {"ocpu": 8, "memory_gb": 120, "confirmed": True},
    "VM.Standard2.16": {"ocpu": 16, "memory_gb": 240, "confirmed": True},
    # 2.24 breaks the 15 GB/OCPU rule - it is 320 GB, not 360. Panel-confirmed.
    "VM.Standard2.24": {"ocpu": 24, "memory_gb": 320, "confirmed": True},
    # VM.Standard3.Flex caps well below the other Flex shapes, and the ceiling depends on the
    # EDITION, not just the shape: Standard allows 8 OCPUs, Enterprise 32. So max_ocpu here is
    # only the floor of that range; max_ocpu_by_edition overrides it where we've seen a panel.
    "VM.Standard3.Flex": {"ocpu": None, "memory_gb": None, "max_ocpu": 8, "gb_per_ocpu": 16,
                          # Standard is the outlier at 8; Enterprise and High Performance both allow 32
                          # (both panel-confirmed). Extreme and BYOL are assumed 32 by that pattern
                          # and still want a panel each.
                          "max_ocpu_by_edition": {"standard": 8, "enterprise": 32,
                                                  "high_perf": 32, "extreme": 32, "byol": 32}},
    "VM.Standard.E4.Flex": {"ocpu": None, "memory_gb": None, "max_ocpu": 64, "gb_per_ocpu": 16,
                            # Same edition-dependent ceiling as Standard3.Flex, but AMD tops out
                            # at 64 rather than 32. Standard's max 8 is panel-confirmed; the
                            # 64 for the paid editions is confirmed on Enterprise / High
                            # Performance / BYOL, and Extreme is now confirmed at 64 too
                            # (8 OCPU / 128 GB panel = 16 GB per OCPU, as encoded).
                            "max_ocpu_by_edition": {"standard": 8, "enterprise": 64,
                                                    "high_perf": 64, "extreme": 64, "byol": 64}},
    "VM.Standard.E5.Flex": {"ocpu": None, "memory_gb": None, "max_ocpu": 64, "gb_per_ocpu": 16,
                            # Same edition-dependent ceiling as Standard3.Flex, but AMD tops out
                            # at 64 rather than 32. Standard's max 8 is panel-confirmed; the
                            # 64 for the paid editions is confirmed on Enterprise / High
                            # Performance / BYOL, and Extreme is now confirmed at 64 too
                            # (8 OCPU / 128 GB panel = 16 GB per OCPU, as encoded).
                            "max_ocpu_by_edition": {"standard": 8, "enterprise": 64,
                                                    "high_perf": 64, "extreme": 64, "byol": 64}},
    "VM.Standard.A1.Flex": {"ocpu": None, "memory_gb": None, "max_ocpu": 64, "gb_per_ocpu": 8},
}


# Provisioned capacity is SHAPE-dependent, not just tier-dependent: 2,048 GB usable provisions
# 2,760 GB on VM.Standard.E4.Flex but only 2,656 GB on VM.Standard2.1, while 256 GB usable gives
# 712 GB on both. The overhead evidently tracks the shape's memory/system storage, and one
# conflicting pair is not enough to derive the rule - so observed shape+tier pairs are recorded
# verbatim here and everything else falls back to the general table. Add a pair whenever a
# Pricing Details panel shows one; do NOT interpolate.
# FLEX shapes all share the general tier map - VM.Standard3.Flex confirmed at 256 -> 712 and
# 8,192 -> 10,440, matching E4.Flex and A1.Flex exactly. Only the FIXED VM.Standard2.x shapes
# deviate (2,048 -> 2,656 on 2.1 vs 2,760 on Flex), which fits: a fixed shape has fixed memory
# and system storage, so its overhead differs. Only fixed-shape exceptions belong in here.
# The fixed VM.Standard2.x family provisions LESS than Flex for the same usable tier, because
# it carries a smaller recovery allowance: ~1.2x usable + 200 against Flex's 1.25x + 200. At
# 32,768 GB usable that is 39,520 GB versus 41,160 - a 1,640 GB difference, about $60/month at
# 20 VPU. Confirmed across 2.1, 2.4 and 2.16, so it is a family rule, not a per-shape one.
#
# The 1.2 multiplier reproduces every observed pair to within 1.6 GB but never exactly, so the
# true rule has an integer rounding step this data can't pin down. Observed pairs are therefore
# used verbatim and the formula only fills tiers we have not seen - which is why the tier list
# for these shapes is the one to extend as more panels arrive.
BASEDB_FIXED_SHAPE_TIERS = {
    256: 712, 2048: 2656, 18432: 22320, 24576: 29692, 32768: 39520, 38912: 46896,
}


def basedb_fixed_shape_provisioned(usable):
    """Provisioned GB for a fixed (non-Flex) shape. Observed tiers are exact; anything else
    falls back to the family's ~1.2x + 200 rule and should be treated as an estimate."""
    want = int(float(usable or 0))
    if want in BASEDB_FIXED_SHAPE_TIERS:
        return float(BASEDB_FIXED_SHAPE_TIERS[want])
    return float(round(want * 1.2 + 200))
BASEDB_SHAPE_TIER_OVERRIDES = {
    ("VM.Standard2.1", 256): 712,
    ("VM.Standard2.1", 2048): 2656,
}


# Usable tiers the estimator offers but whose PROVISIONED capacity we have never seen. The
# overhead is not a fixed offset or ratio (confirmed diffs run 456, 456, 712, 712, 1224, 2248,
# 4296, 12488 - neither linear nor geometric), so there is nothing honest to interpolate from.
# A quote on one of these would be a fabricated number on a customer deliverable, which
# AGENT_POLICY.md forbids: empty stays empty and gets flagged.
BASEDB_UNPRICED_TIERS = ()   # solved - see basedb_provisioned_formula below


# Block volume performance levels, transcribed from the estimator's Storage - Block Volumes
# panel. Per-GB figures are constant; the Max IOPS / Max Throughput readouts are the per-GB
# rate times the volume, clamped to a per-level ceiling - which is why a 712 GB balanced volume
# reports 333 MBps but a 1,736 GB one reports 480 (the cap), both at 480 KBPS/GB.
BLOCK_PERFORMANCE_LEVELS = {
    10: {"label": "Balanced", "iops_per_gb": 60, "kbps_per_gb": 480,
         "max_iops": 25000, "max_mbps": 480},
    20: {"label": "High performance", "iops_per_gb": 75, "kbps_per_gb": 600,
         "max_iops": 50000, "max_mbps": 680},
}


def block_volume_stats(gb, vpus=10):
    """The IOPS / throughput figures the estimator shows for a block volume, so a quoted volume
    can be described the same way the customer saw it. Throughput is KBPS/GB converted at
    1024 KB per MB, then capped."""
    level = BLOCK_PERFORMANCE_LEVELS.get(int(vpus or 10)) or BLOCK_PERFORMANCE_LEVELS[10]
    gb = float(gb or 0)
    return {
        "performanceLevel": level["label"],
        "vpus": int(vpus or 10),
        "iopsPerGb": level["iops_per_gb"],
        "kbpsPerGb": level["kbps_per_gb"],
        "maxIops": int(min(level["iops_per_gb"] * gb, level["max_iops"])),
        "maxThroughputMbps": int(min(level["kbps_per_gb"] * gb / 1024.0, level["max_mbps"])),
    }


def basedb_shape_stats(shape, ocpu, processor=None):
    """Shape facts the estimator prints under the OCPU box: the vCPU ratio and derived memory.
    Arm runs 1 OCPU = 1 vCPU at 8 GB; x86 runs 1 OCPU = 2 vCPU at 16 GB."""
    spec = BASEDB_SHAPES.get(str(shape or "")) or {}
    arm = str(processor or "").lower() == "arm" or "A1.Flex" in str(shape or "")
    ocpu = spec.get("ocpu") or float(ocpu or 0)
    per = spec.get("gb_per_ocpu") or (8 if arm else 16)
    return {
        "ocpu": ocpu,
        "vcpu": ocpu * (1 if arm else 2),
        "vcpuNote": "1 OCPU equals %d vCPUs." % (1 if arm else 2),
        "memoryGb": spec.get("memory_gb") or ocpu * per,
        "fixed": bool(spec.get("ocpu")),
    }


# AMD and Intel share the same x86 rates AND the same 81,920 GB storage ceiling; they are kept
# as separate options only because their SHAPE lists differ - Intel offers the fixed
# VM.Standard2.x family plus VM.Standard3.Flex, AMD offers only E4/E5.Flex. Arm is the sole
# processor with a smaller storage ceiling (8,192 GB), so an Arm quote above that describes a
# configuration the estimator will not produce.
BASEDB_MAX_USABLE_GB = {"intel": 81920, "amd": 81920, "arm": BASEDB_ARM_MAX_USABLE_GB}
# Which EDITIONS a shape offers depends on the shape's OCPU count, not on the processor.
# Standard caps at 8 OCPUs everywhere, so a fixed shape above that (VM.Standard2.16 at 16,
# VM.Standard2.24 at 24) simply cannot run Standard and the estimator drops it from the list -
# confirmed on 2.24, whose edition dropdown shows only Enterprise / High Performance / Extreme /
# BYOL. Intel DOES offer Standard on its smaller shapes: panels for VM.Standard2.1 ($190.22) and
# VM.Standard3.Flex ($603.66) both priced Standard on Intel.
BASEDB_STANDARD_MAX_OCPU = 8
# Fixed (non-Flex) shapes cap storage at 40,960 GB where Flex reaches 81,920 - confirmed on
# VM.Standard2.24, whose dropdown ends at 40,960. They also step in 2,048 GB increments
# (...26,624 / 28,672 / 30,720 / 32,768 / 34,816 / 36,864 / 38,912 / 40,960) rather than the
# Flex 8,192. The full low end of that list is not captured yet, so the tier list below is the
# Flex one truncated at the cap: correct at both ends, but it omits the intermediate
# 2,048-step values a fixed shape actually offers.
BASEDB_FIXED_SHAPE_MAX_USABLE_GB = 40960


def basedb_editions_for(shape, processor=None):
    """Editions a shape can actually run. Arm adds Developer; Standard drops off any shape whose
    fixed OCPU count exceeds Standard's 8-OCPU ceiling."""
    arm = str(processor or "").lower() == "arm"
    keys = [k for k, _l, _r, _u, _s in (BASEDB_ARM_EDITIONS if arm else BASEDB_OCPU_EDITIONS)]
    fixed = (BASEDB_SHAPES.get(str(shape or "")) or {}).get("ocpu")
    if fixed and fixed > BASEDB_STANDARD_MAX_OCPU:
        keys = [k for k in keys if k != "standard"]
    return keys


BASEDB_SHAPES_BY_PROCESSOR = {
    # Confirmed identical on Standard, Enterprise, High Performance, Extreme and BYOL - the
    # fixed 2.x family is NOT restricted to the Standard edition.
    "intel": ["VM.Standard2.1", "VM.Standard2.2", "VM.Standard2.4", "VM.Standard2.8",
              "VM.Standard2.16", "VM.Standard2.24", "VM.Standard3.Flex"],
    "amd": ["VM.Standard.E4.Flex", "VM.Standard.E5.Flex"],
    "arm": ["VM.Standard.A1.Flex"],
}


def basedb_usable_tiers(processor, shape=None):
    """Storage tiers actually offered. Arm caps at 8,192 GB; a fixed (non-Flex) shape caps at
    40,960 even on Intel/AMD, which reach 81,920 on a Flex shape."""
    cap = BASEDB_MAX_USABLE_GB.get(str(processor or "intel").lower(), 81920)
    if (BASEDB_SHAPES.get(str(shape or "")) or {}).get("ocpu"):
        cap = min(cap, BASEDB_FIXED_SHAPE_MAX_USABLE_GB)
    return [(u, t) for u, t in BASEDB_STORAGE_TIERS if u <= cap]


def basedb_shapes_for(processor):
    """Shapes the estimator offers for a processor."""
    return BASEDB_SHAPES_BY_PROCESSOR.get(str(processor or "intel").lower(), [])


def basedb_provisioned_formula(usable):
    """Provisioned block-volume GB for a usable tier, from Oracle's own stated rule.

    The estimator's footnote says it charges for "data storage, recovery storage, and storage
    required for the system software" and recommends recovery at >=20% of TOTAL. 25% on top of
    usable is exactly 20% of the total, and the system software is a flat 200 GB - so:

        provisioned = 1.25 * usable + 200        (usable >= 2048)

    Confirmed exactly against 7 panels: 2,048/4,096/8,192/16,384/49,152/73,728/81,920. Small
    tiers carry proportionally more recovery (1.5x) and 256 GB is its own special case; both
    are covered by the explicit table rather than this formula.
    """
    return round(usable * 1.25 + 200)


def basedb_tier_is_priceable(usable):
    try:
        return int(float(usable or 0)) not in BASEDB_UNPRICED_TIERS
    except (TypeError, ValueError):
        return True


def basedb_provisioned_gb(shape, usable):
    """Provisioned block-volume GB for a shape + usable tier. Uses an observed shape-specific
    pair when we have one, else the general OCPU table."""
    try:
        want = int(float(usable or 0))
    except (TypeError, ValueError):
        return 0.0
    hit = BASEDB_SHAPE_TIER_OVERRIDES.get((str(shape or ""), want))
    if hit:
        return float(hit)
    # Fixed shapes provision less than Flex for the same usable tier - their own rule applies.
    if (BASEDB_SHAPES.get(str(shape or "")) or {}).get("ocpu"):
        return basedb_fixed_shape_provisioned(want)
    known = BASEDB_OCPU_CONFIRMED_TIERS.get(want)
    if known:
        return float(known)
    if want >= 2048:
        return float(basedb_provisioned_formula(want))
    return basedb_ocpu_total_capacity(want)


def basedb_effective_ocpu(shape, requested, edition=None):
    """OCPUs a shape will actually run. A fixed shape dictates the count; a Flex shape honours
    what the user asked for."""
    spec = BASEDB_SHAPES.get(str(shape or "")) or {}
    if spec.get("ocpu"):
        return float(spec["ocpu"])          # fixed shape dictates the count
    want = float(requested or 0)
    # Flex: clamp to the ceiling, which can differ per edition on the same shape.
    cap = (spec.get("max_ocpu_by_edition") or {}).get(str(edition or "")) or spec.get("max_ocpu")
    return min(want, float(cap)) if cap else want
# The OCPU flavour offers the SAME usable tiers as the ECPU one, but provisions exactly 5 GB
# less at every tier observed so far - 256 -> 712 vs 717, 1024 -> 1736 vs 1741, 4096 -> 5320 vs
# 5325. The ECPU table stays the source of truth for the tier list and the -5 is applied on top,
# so adding a tier in one place keeps both flavours in step. Tiers confirmed against an
# estimator panel are listed explicitly; the rest inherit the offset.
BASEDB_OCPU_STORAGE_OFFSET = 5
BASEDB_OCPU_CONFIRMED_TIERS = {256: 712, 512: 968, 1024: 1736, 2048: 2760,
                              4096: 5320, 8192: 10440, 16384: 20680,
                              49152: 61640, 73728: 92360, 81920: 102600}
BASEDB_OCPU_STORAGE_TIERS = [
    (u, BASEDB_OCPU_CONFIRMED_TIERS.get(u, t - BASEDB_OCPU_STORAGE_OFFSET))
    for u, t in BASEDB_STORAGE_TIERS
]


def basedb_ocpu_total_capacity(usable):
    """Provisioned block-volume GB for an OCPU-flavour storage tier. Falls back to Oracle's own
    guidance - total = usable plus recovery and system overhead - when the tier isn't known
    yet, erring high rather than under-quoting."""
    try:
        want = float(usable or 0)
    except (TypeError, ValueError):
        return 0.0
    for u, t in BASEDB_OCPU_STORAGE_TIERS:
        if abs(want - u) < 0.5:
            return float(t)
    for u, t in BASEDB_OCPU_STORAGE_TIERS:
        if want <= u:
            return float(t)
    # Unknown tier: scale by the one ratio Oracle has shown us (712/256) so the estimate
    # includes recovery + system storage instead of billing bare usable GB.
    return round(want * (712.0 / 256.0), 0)


def basedb_total_capacity(usable):
    """Total provisioned GB for a usable-storage tier. An off-tier value falls back to the
    nearest tier at or above it, so a quote can never come in under the real capacity."""
    try:
        want = float(usable or 0)
    except (TypeError, ValueError):
        return 0.0
    for u, t in BASEDB_STORAGE_TIERS:
        if want <= u:
            return float(t)
    return float(BASEDB_STORAGE_TIERS[-1][1])


# ---------------------------------------------------------------------------------------------
# OCI Generative AI
#
# Every rate and part number below is cross-checked against Oracle's own Cost Estimator catalog
# (oracle-cost-estimator-ai-integration/, dataset version 439, 674 SKUs with USD PAYG prices),
# which publishes partNumber + displayName + price together. That resolved the part numbers the
# public price list omits - it prints names and rates but no SKUs, and several GenAI lines share
# a rate, so price alone was never enough to match them.
#
# Each tuple is (key, label, rate, unit, sku).
# ---------------------------------------------------------------------------------------------
# ---------------------------------------------------------------------------------------------
# TIER-PRICED SERVICES
#
# Several OCI services bill "first N free, then $X per unit". Oracle's Cost Estimator catalog
# lists these at $0 - the free tier IS the SKU, and the paid tier is a billing rule rather than a
# separate part number - which is why searching the raw price list for "Queue" or "Document
# Understanding" turns up nothing usable. The rate has to come from the published price list and
# the allowance has to be modelled here.
#
# This is the same shape as the `free=` argument the storage cards already use (Object Storage's
# first 10 GB, egress's first 10 TB). The difference is that those have one free field, while
# these services have several meters each with its OWN allowance - so the allowance travels with
# the meter in the tier table instead of in a per-card `free` dict.
#
# Each tuple is (key, label, rate, unit, sku, free_units). free_units is expressed in the SAME
# unit as the quantity the user types: "first 5,000 transactions free" on a per-1,000 meter is
# 5 free units, not 5000. Getting that wrong under-bills by a factor of a thousand, so the
# comment on each block states which it is.
# ---------------------------------------------------------------------------------------------

# Quantities entered in units of 1,000 transactions; allowances are therefore in thousands too.
VISION_METERS = [
    ("image_analysis", "Image analysis", 0.25, "1K transactions", "B94973", 5),
    ("ocr", "OCR", 1.00, "1K transactions", "B94974", 5),
    ("custom_training", "Custom training", 1.50, "training hour", "B94977", 15),
    ("stored_video", "Stored video analysis", 0.10, "processed video minute", "B110617", 1000),
    ("stream_video", "Stream video analysis", 0.15, "processed video minute", "B111539", 0),
]
DOCU_METERS = [
    ("ocr", "OCR", 1.00, "1K transactions", "B96110", 5),
    ("doc_properties", "Document properties", 0.25, "1K transactions", "B96111", 5),
    ("doc_extraction", "Document extraction", 10.00, "1K transactions", "B96112", 5),
    ("custom_properties", "Custom document properties", 1.50, "1K transactions", "B97193", 5),
    ("custom_extraction", "Custom document extraction", 30.00, "1K transactions", "B97194", 5),
    ("custom_training", "Custom training", 1.50, "training hour", "B96113", 15),
]
# Language is the one service here whose per-meter part numbers the catalog doesn't separate -
# it ships several "Custom Inferencing" rows that carry free-tier thresholds rather than rates.
# Every meter therefore points at the custom-inferencing SKU B93423; the RATES are from the
# published price list and are correct, but treat the part number on a Language line as the
# service's, not the meter's, until an estimator panel shows otherwise.
LANGUAGE_METERS = [
    ("pretrained", "Pre-trained inferencing", 0.25, "1K transactions", "B93423", 5),
    ("custom_inference", "Custom inferencing", 3.50, "1K transactions", "B93423", 0),
    ("translation", "Text translation", 10.00, "1K transactions", "B93423", 1),
    ("custom_training", "Custom training", 1.50, "training hour", "B93423", 15),
    ("dedicated_custom", "Custom inferencing - dedicated", 1.50, "inferencing unit hour", "B95917", 15),
    ("dedicated_health", "Dedicated inferencing - healthcare", 20.00, "inferencing unit hour", "B95918", 0),
]
SPEECH_METERS = [("transcription", "Transcription", 0.35, "transcription hour", "B94896", 5)]
# Queue bills per 1,000,000 requests with the first million free - so one free unit.
QUEUE_METERS = [("requests", "Requests", 0.22, "1M requests", "B95697", 1)]


def _tier_variant_maps(rows):
    """rate / sku / label / unit / free lookups for a tier-priced meter table."""
    return ({r[0]: r[2] for r in rows}, {r[0]: r[4] for r in rows},
            {r[0]: r[1] for r in rows}, {r[0]: r[3] for r in rows},
            {r[0]: r[5] for r in rows})


def _tier_options(rows):
    """Dropdown labels that state the allowance, so the free tier is visible before you buy."""
    out = []
    for key, label, rate, unit, _sku, free in rows:
        allowance = f", first {free:,g} free" if free else ""
        out.append((key, f"{label} - ${rate:g}/{unit}{allowance}"))
    return out


GENAI_ONDEMAND = [
    # --- Cohere (per 10,000 transactions; a transaction is a character) ---
    ("cohere_large", "Cohere - Large", 0.0156, "10K transactions", "B108077"),
    ("cohere_small", "Cohere - Small", 0.0009, "10K transactions", "B108078"),
    ("cohere_embed", "Cohere - Embed", 0.001, "10K transactions", "B108079"),
    ("rerank4_pro", "Cohere - Rerank 4 Pro", 2.50, "1K search units", "B112423"),
    ("rerank4_fast", "Cohere - Rerank 4 Fast", 2.00, "1K search units", "B112424"),
    # --- Meta (per 10,000 transactions) ---
    ("llama4_scout", "Meta - Llama 4 Scout", 0.0018, "10K transactions", "B111035"),
    ("llama4_maverick", "Meta - Llama 4 Maverick", 0.0018, "10K transactions", "B111036"),
    ("meta_large", "Meta - Large", 0.0018, "10K transactions", "B108080"),
    ("llama31_405b", "Meta - Llama 3.1 405B", 0.0267, "10K transactions", "B110517"),
    ("llama32_90b", "Meta - Llama 3.2 90B Vision", 0.005, "10K transactions", "B110679"),
    # --- xAI (per 1,000,000 tokens) ---
    ("grok43_in", "xAI - Grok 4.3 Input (<200K)", 1.25, "1M tokens", "B112080"),
    ("grok43_in_cached", "xAI - Grok 4.3 Cached Input (<200K)", 0.20, "1M tokens", "B112081"),
    ("grok43_out", "xAI - Grok 4.3 Output (<200K)", 2.50, "1M tokens", "B112082"),
    ("grok43_in_big", "xAI - Grok 4.3 Input (>200K)", 2.50, "1M tokens", "B112083"),
    ("grok43_in_cached_big", "xAI - Grok 4.3 Cached Input (>200K)", 0.40, "1M tokens", "B112084"),
    ("grok43_out_big", "xAI - Grok 4.3 Output (>200K)", 5.00, "1M tokens", "B112085"),
    ("grok42_in", "xAI - Grok 4.2 Input (<200K)", 1.25, "1M tokens", "B111910"),
    ("grok42_in_cached", "xAI - Grok 4.2 Cached Input (<200K)", 0.20, "1M tokens", "B111911"),
    ("grok42_out", "xAI - Grok 4.2 Output (<200K)", 2.50, "1M tokens", "B112076"),
    ("grok42_in_big", "xAI - Grok 4.2 Input (>200K)", 2.50, "1M tokens", "B112077"),
    ("grok42_in_cached_big", "xAI - Grok 4.2 Cached Input (>200K)", 0.40, "1M tokens", "B112078"),
    ("grok42_out_big", "xAI - Grok 4.2 Output (>200K)", 5.00, "1M tokens", "B112079"),
    ("grok4code_in", "xAI - Grok 4 Code (Grok-Code-Fast-1) Input", 1.25, "1M tokens", "B111803"),
    ("grok4code_in_cached", "xAI - Grok 4 Code Cached Input", 0.25, "1M tokens", "B111804"),
    ("grok4code_out", "xAI - Grok 4 Code Output", 2.50, "1M tokens", "B111805"),
    ("grok4fast_in", "xAI - Grok 4 Fast Input (<128K)", 1.00, "1M tokens", "B111900"),
    ("grok4fast_in_big", "xAI - Grok 4 Fast Input (>128K)", 2.00, "1M tokens", "B111901"),
    ("grok4fast_in_cached", "xAI - Grok 4 Fast Cached Input (<128K)", 0.20, "1M tokens", "B111902"),
    ("grok4fast_in_cached_big", "xAI - Grok 4 Fast Cached Input (>128K)", 0.40, "1M tokens", "B111903"),
    ("grok4fast_out", "xAI - Grok 4 Fast Output (<128K)", 2.00, "1M tokens", "B111904"),
    ("grok4fast_out_big", "xAI - Grok 4 Fast Output (>128K)", 4.00, "1M tokens", "B111905"),
    ("grok34_in", "xAI - Grok 3 or Grok 4 Input", 5.00, "1M tokens", "B111438"),
    ("grok34_in_cached", "xAI - Grok 3 or Grok 4 Cached Input", 0.20, "1M tokens", "B111799"),
    ("grok34_out", "xAI - Grok 3 or Grok 4 Output", 25.00, "1M tokens", "B111439"),
    ("grok3mini_in", "xAI - Grok 3 Mini Input", 1.50, "1M tokens", "B111440"),
    ("grok3mini_in_cached", "xAI - Grok 3 Mini Cached Input", 0.25, "1M tokens", "B111800"),
    ("grok3mini_out", "xAI - Grok 3 Mini Output", 5.00, "1M tokens", "B111441"),
    ("grok3fast_in_cached", "xAI - Grok 3 Fast Cached Input", 1.50, "1M tokens", "B111801"),
    ("grok3minifast_in", "xAI - Grok 3 Mini Fast Input", 1.50, "1M tokens", "B111554"),
    ("grok3minifast_in_cached", "xAI - Grok 3 Mini Fast Cached Input", 0.25, "1M tokens", "B111802"),
    ("grok3minifast_out", "xAI - Grok 3 Mini Fast Output", 5.00, "1M tokens", "B111555"),
    ("grok_imagine", "xAI - Grok Imagine Image", 0.02, "image", "B112517"),
    ("grok_voice", "xAI - Grok Voice Agent API", 0.05, "connection minute", "B112518"),
    ("grok_tts", "xAI - Grok Text to Speech", 15.00, "1M characters", "B112608"),
    ("xai_x_search", "xAI - X Search", 5.00, "1K requests", "B112609"),
    ("xai_web_search", "xAI - Web Search", 5.00, "1K requests", "B112610"),
    ("xai_code_exec", "xAI - Code Execution", 5.00, "1K requests", "B112611"),
    # --- Google Gemini (per 1,000,000 tokens) ---
    ("gemini25pro_in", "Google - Gemini 2.5 Pro Input (<200K)", 1.25, "1M tokens", "B111847"),
    ("gemini25pro_in_big", "Google - Gemini 2.5 Pro Input (>200K)", 2.50, "1M tokens", "B111848"),
    ("gemini25pro_out", "Google - Gemini 2.5 Pro Output (<200K)", 10.00, "1M tokens", "B111849"),
    ("gemini25pro_out_big", "Google - Gemini 2.5 Pro Output (>200K)", 15.00, "1M tokens", "B111850"),
    ("gemini25flash_in", "Google - Gemini 2.5 Flash Input (text/image/video)", 0.30, "1M tokens", "B111851"),
    ("gemini25flash_in_audio", "Google - Gemini 2.5 Flash Input (audio)", 1.00, "1M tokens", "B111852"),
    ("gemini25flash_out", "Google - Gemini 2.5 Flash Output (text)", 2.50, "1M tokens", "B111853"),
    ("gemini25lite_in", "Google - Gemini 2.5 Flash Lite Input (text/image/video)", 0.10, "1M tokens", "B111854"),
    ("gemini25lite_in_audio", "Google - Gemini 2.5 Flash Lite Input (audio)", 0.30, "1M tokens", "B111855"),
    ("gemini25lite_out", "Google - Gemini 2.5 Flash Lite Output (text)", 0.40, "1M tokens", "B111856"),
    # --- OpenAI open-weight (per 1,000,000 tokens) ---
    ("gptoss120b_in", "OpenAI - gpt-oss-120b Input", 0.15, "1M tokens", "B112004"),
    ("gptoss120b_out", "OpenAI - gpt-oss-120b Output", 0.60, "1M tokens", "B112005"),
    ("gptoss20b_in", "OpenAI - gpt-oss-20b Input", 0.07, "1M tokens", "B112006"),
    ("gptoss20b_out", "OpenAI - gpt-oss-20b Output", 0.30, "1M tokens", "B112007"),
]

# Dedicated AI clusters bill per AI-unit-hour (Cohere Rerank bills per cluster hour). Oracle
# requires a minimum commitment of 1 unit-hour per cluster; fine-tuning clusters need 2 units.
GENAI_DEDICATED = [
    ("cohere", "Cohere - Dedicated", 1.00, "AI unit / hour", "B112425"),
    ("cohere_rerank", "Cohere Rerank - Dedicated", 10.00, "cluster hour", "B111015"),
    ("cohere_large", "Large Cohere - Dedicated", 24.00, "AI unit / hour", "B108082"),
    ("cohere_small", "Small Cohere - Dedicated", 6.50, "AI unit / hour", "B108083"),
    ("cohere_embed", "Embed Cohere - Dedicated", 10.90, "AI unit / hour", "B108084"),
    ("meta", "Meta - Dedicated", 1.00, "AI unit / hour", "B111882"),
    ("meta_large", "Large Meta - Dedicated", 12.00, "AI unit / hour", "B108085"),
    ("openai", "OpenAI - Dedicated", 1.00, "AI unit / hour", "B112008"),
    ("model_import", "Model Import (custom model)", 1.00, "AI unit / hour", "B111959"),
]

# Agentic / RAG building blocks. Storage lines bill per GB-HOUR, so they follow the card's
# hours-per-month setting; request and event lines are flat monthly counts.
GENAI_RAG = [
    ("web_search", "Web Search", 10.00, "1K requests", "B111973", False),
    ("file_search", "File Search Storage", 0.0042, "GB / hour", "B112414", True),
    ("vector_store", "Vector Store Storage", 0.0042, "GB / hour", "B112577", True),
    ("vector_retrieval", "Vector Store Retrieval", 0.20, "1K requests", "B112578", False),
    ("memory_ingest", "Memory Ingestion", 0.20, "1K events", "B112575", False),
    ("memory_retention", "Memory Retention", 0.0042, "GB / hour", "B112579", True),
]

# OCI Generative AI Agents (a separate service from the Generative AI models above).
GENAI_AGENTS = [
    ("agent_txn", "Agent transactions", 0.003, "10K transactions", "B110461", False),
    ("agent_kb", "Knowledge Base Storage", 0.0084, "GB / hour", "B110462", True),
    ("agent_ingest", "Agent Data Ingestion", 0.0003, "10K transactions", "B110463", False),
]

# ---------------------------------------------------------------------------------------------
# Combined "OCI Generative AI" card, laid out like Oracle's own Cost Estimator (one card with a
# Models section and a Search & Retrieval section, one combined total). Every rate/SKU/metric
# below is taken directly from the estimator catalog snapshot
# (oracle-cost-estimator-ai-integration/, services 2741 Models and 3562 Search and Retrieval).
#
# Models pricing (per selected model):
#   cost = requests * (prompt_len/divisor * inRate + response_len/divisor * outRate)
# The "unit basis" sets what prompt/response length is entered in and the divisor that turns a
# raw length into billable units - this is the ONLY place Cohere/Meta (characters, per 10,000)
# and xAI/Google/OpenAI (tokens, per 1,000,000) differ. Verified against the estimator:
#   Cohere Large, 1e6 req x 10k/10k prompt + 10k/10k response  = $31,200.00
#   OpenAI gpt-oss-120b, 1e3 req x 10k/1e6 x ($0.15 + $0.60)   = $7.50
GENAI_UNIT_BASES = {
    "char10k": {"divisor": 10000, "lengthUnit": "characters"},
    "token1m": {"divisor": 1000000, "lengthUnit": "tokens"},
}

# (provider, key, label, basis, inRate, inSku, outRate, outSku). outRate 0 => model has no
# separately-billed output meter (e.g. embeddings).
GENAI_MODELS = [
    ("Cohere", "cohere_large", "Command R+ (Large Cohere)", "char10k", 0.0156, "B108077", 0.0156, "B108077"),
    ("Cohere", "cohere_small", "Command R (Small Cohere)", "char10k", 0.0009, "B108078", 0.0009, "B108078"),
    ("Cohere", "cohere_embed", "Embed Cohere", "char10k", 0.001, "B108079", 0.0, ""),
    ("Meta", "llama4_scout", "Llama 4 Scout", "char10k", 0.0018, "B111035", 0.0018, "B111035"),
    ("Meta", "llama4_maverick", "Llama 4 Maverick", "char10k", 0.0018, "B111036", 0.0018, "B111036"),
    ("Meta", "meta_large", "Large Meta", "char10k", 0.0018, "B108080", 0.0018, "B108080"),
    ("Meta", "llama31_405b", "Llama 3.1 405B", "char10k", 0.0267, "B110517", 0.0267, "B110517"),
    ("Meta", "llama32_90b", "Llama 3.2 90B Vision", "char10k", 0.005, "B110679", 0.005, "B110679"),
    ("xAI", "grok43", "Grok 4.3 (<200K)", "token1m", 1.25, "B112080", 2.50, "B112082"),
    ("xAI", "grok42", "Grok 4.2 (<200K)", "token1m", 1.25, "B111910", 2.50, "B112076"),
    ("xAI", "grok4code", "Grok 4 Code (Grok-Code-Fast-1)", "token1m", 1.25, "B111803", 2.50, "B111805"),
    ("xAI", "grok4fast", "Grok 4 Fast (<128K)", "token1m", 1.00, "B111900", 2.00, "B111904"),
    ("xAI", "grok34", "Grok 3 or Grok 4", "token1m", 5.00, "B111438", 25.00, "B111439"),
    ("xAI", "grok3mini", "Grok 3 Mini", "token1m", 1.50, "B111440", 5.00, "B111441"),
    ("xAI", "grok3fast", "Grok 3 Fast", "token1m", 5.00, "B111552", 25.00, "B111553"),
    ("xAI", "grok3minifast", "Grok 3 Mini Fast", "token1m", 1.50, "B111554", 5.00, "B111555"),
    ("Google", "gemini25pro", "Gemini 2.5 Pro (<200K)", "token1m", 1.25, "B111847", 10.00, "B111849"),
    ("Google", "gemini25flash", "Gemini 2.5 Flash", "token1m", 0.30, "B111851", 2.50, "B111853"),
    ("Google", "gemini25lite", "Gemini 2.5 Flash Lite", "token1m", 0.10, "B111854", 0.40, "B111856"),
    ("OpenAI", "gptoss120b", "gpt-oss-120b", "token1m", 0.15, "B112004", 0.60, "B112005"),
    ("OpenAI", "gptoss20b", "gpt-oss-20b", "token1m", 0.07, "B112006", 0.30, "B112007"),
]
# Provider order for the dropdown mirrors the estimator / Oracle price list.
GENAI_PROVIDERS = ["Cohere", "Meta", "xAI", "Google", "OpenAI"]

# Search & Retrieval meters (estimator service 3562). Storage meters bill per GB-HOUR and follow
# the card's hours setting (estimator default 744); request/event meters are flat per-1,000.
# (key, label, section, rate, unit, sku, hourly, divisor). Verified: three storage meters at
# 100,000 GB x $0.0042 x 744 + web/retrieval/ingestion = $938,442.20.
GENAI_RETRIEVAL = [
    ("web_search", "Web Search", "Web Search", 10.00, "1K requests", "B111973", False, 1000),
    ("file_storage", "File Search Storage [GB]", "File Storage", 0.0042, "GB / hour", "B112414", True, 1),
    ("vector_storage", "Vector Store Storage [GB]", "Vector Storage", 0.0042, "GB / hour", "B112577", True, 1),
    ("vector_retrieval", "Vector Store Retrieval", "Vector Storage", 0.20, "1K requests", "B112578", False, 1000),
    ("memory_retention", "Memory Retention [GB]", "Memory", 0.0042, "GB / hour", "B112579", True, 1),
    ("memory_ingestion", "Memory Ingestion", "Memory", 0.20, "1K events", "B112575", False, 1000),
]
# The estimator's fixed utilization is 744 hrs/mo (24 x 31) and the card ships with that value;
# the meters themselves all start at 0, so nothing is pre-priced until the user enters quantities.
GENAI_RETRIEVAL_HOURS_DEFAULT = 744

# Lookup maps consumed by line_cost and the /api/catalog payload (the client mirrors them).
GENAI_MODELS_BY_KEY = {m[1]: m for m in GENAI_MODELS}
GENAI_MODEL_OPTIONS_BY_PROVIDER = {
    p: [(m[1], m[2]) for m in GENAI_MODELS if m[0] == p] for p in GENAI_PROVIDERS
}


def _variant_options(rows):
    """(value, label) pairs for a variant dropdown, labelled with the rate so the choice is
    self-explanatory in the picker."""
    return [(r[0], f"{r[1]} - ${r[2]:g}/{r[3]}") for r in rows]


def _variant_maps(rows):
    """rate / sku / label / unit lookups keyed by the dropdown value."""
    return ({r[0]: r[2] for r in rows}, {r[0]: r[4] for r in rows},
            {r[0]: r[1] for r in rows}, {r[0]: r[3] for r in rows})

ARCHITECTURE_GROUP_ICONS = {
    "Compute": "Compute - Virtual Machine VM",
    "Storage": "Storage - Object Storage",
    "Networking": "Networking - Service Gateway",
    "Database": "Database - Database System",
    "Integration": "Developer Services - Integrations",
    "Security": "Identity and Security - Vault",
    "Observability": "Observability and Management - Monitoring",
    "Obs. & Management": "Observability and Management - Monitoring",
    "AI & Machine Learning": "Analytics and AI",
    "Licensing": "Compute - Virtual Machine VM",
    "Other Services": "Compute - Functions",
}

# Ordered from most specific to broadest. This gives raw price-list SKUs the same
# deterministic product-to-icon contract as curated services instead of reducing every
# uncommon service to its broad catalog group.
ARCHITECTURE_NAME_ICONS = [
    ("autonomous data warehouse", "Database - Autonomous Data Warehouse ADW", "direct"),
    ("autonomous transaction processing", "Database - Autonomous Transaction Processing ATP", "direct"),
    ("autonomous recovery", "Storage - Object Storage", "fallback"),
    ("database backup", "Storage - Object Storage", "fallback"),
    ("autonomous", "Database - Autonomous DB", "direct"),
    ("container engine for kubernetes", "Developer Services - Container Engine for Kubernetes", "direct"),
    ("kubernetes engine", "Developer Services - Container Engine for Kubernetes", "direct"),
    ("container registry", "Developer Services - Container Registry", "direct"),
    ("api gateway", "Developer Services - API Gateway", "direct"),
    ("object storage", "Storage - Object Storage", "direct"),
    ("block volume", "Storage - Block Storage", "direct"),
    ("block storage", "Storage - Block Storage", "direct"),
    ("file storage", "Storage - File Storage", "direct"),
    ("load balancer", "Networking - Flexible Load Balancer", "direct"),
    ("fastconnect", "Networking - Dynamic Routing Gateway DRG", "fallback"),
    ("data transfer", "Networking - Service Gateway", "fallback"),
    ("service gateway", "Networking - Service Gateway", "direct"),
    ("web application firewall", "Identity and Security - WAF", "direct"),
    ("key management", "Identity and Security - Vault", "direct"),
    ("secure desktop", "Compute - Virtual Machine VM", "fallback"),
    ("windows server", "Compute - Virtual Machine VM", "fallback"),
    ("sql server", "Database - Database System", "fallback"),
    ("integration cloud", "Developer Services - Integrations", "direct"),
    ("application integration", "Developer Services - Integrations", "direct"),
    ("generative ai", "Analytics and AI", "fallback"),
    ("goldengate", "Database - GoldenGate", "direct"),
    ("postgres", "Database - Database System", "fallback"),
    ("mysql", "Database - MySQL", "direct"),
    ("heatwave", "Database - MySQL", "direct"),
    ("exadata", "Database - Exadata", "direct"),
    ("nosql", "Database - NoSQL", "direct"),
    ("function", "Compute - Functions", "direct"),
    ("logging", "Observability and Management - Logging", "direct"),
    ("monitoring", "Observability and Management - Monitoring", "direct"),
    ("vault", "Identity and Security - Vault", "direct"),
    ("waf", "Identity and Security - WAF", "direct"),
    ("dns", "Networking - DNS", "direct"),
]


def architecture_mapping(name="", group=""):
    """Return the bundled OCI icon title and the honesty level of the match."""
    normalized_name = _norm(name)
    for keyword, icon_title, resolution in ARCHITECTURE_NAME_ICONS:
        if keyword in normalized_name:
            return icon_title, resolution
    return (
        ARCHITECTURE_GROUP_ICONS.get(
            group,
            ARCHITECTURE_GROUP_ICONS["Other Services"],
        ),
        "category-fallback",
    )


def architecture_group(name="", group=""):
    """Place raw SKUs in the diagram zone implied by their resolved OCI icon."""
    icon_title, _resolution = architecture_mapping(name, group)
    prefix_groups = {
        "Storage -": "Storage",
        "Networking -": "Networking",
        "Database -": "Database",
        "Developer Services -": "Integration",
        "Identity and Security -": "Security",
        "Observability and Management -": "Observability",
        "Analytics and AI": "AI & Machine Learning",
    }
    for prefix, mapped_group in prefix_groups.items():
        if icon_title.startswith(prefix):
            return mapped_group
    return group or "Other Services"

# Names/keywords that mark a line as 3rd-party licensing (never OCI-discounted).
_THIRD_PARTY_TERMS = ("windows", "sql server", "license", "licence", "byol")


def _curated():
    """Curated, fillable services. Rates come from oci_service_prices.json (verified
    per-unit) where available, else the price list by SKU. Free tiers are declared so the
    cost math matches the app's own free-pool handling."""
    C = []

    def add(id, group, name, sku, rate, unit, basis, fields, note="", free=None,
            third_party=False):
        architecture_icon, architecture_resolution = ARCHITECTURE_ICON_BY_ID[id]
        C.append({"id": id, "group": group, "name": name, "sku": sku,
                  "rate": rate, "unit": unit, "basis": basis, "fields": fields,
                  "note": note, "free": free or {}, "source": "curated",
                  "architectureIcon": architecture_icon,
                  "architectureResolution": architecture_resolution,
                  # 3rd-party licensing (Windows, SQL Server, ...) is NOT eligible for the
                  # OCI discount; native OCI services are.
                  "thirdParty": third_party})

    # ---- Storage ----
    add("block", "Storage", "Block Volume (Balanced)", "B91961",
        _svc_rate("OCI Block Volumes", fallback=0.0255), "GB / month", "month",
        [_sf("gb", "Capacity", "GB", 1024, 128),
         _sf("vpus", "Performance (VPUs/GB)", "VPU", 10, 10)],
        "Balanced = 10 VPUs/GB. Storage + performance units both priced.")
    add("object", "Storage", "Object Storage - Standard", "B91628", OBJ_STORAGE_RATE,
        "GB + requests", "month",
        [_sf("gb", "Storage Capacity", "GB", 1000, 1, 0),
         _sf("requests", "Requests (10k units)", "10k req", 0, 1, 0)],
        "Storage $0.0255/GB-mo (first 10 GB free) + requests $0.0034 per 10,000 (first 50,000 "
        "free). Requests are entered in units of 10,000. SKUs B91628/B91627.")
    add("object_ia", "Storage", "Object Storage - Infrequent Access", "B93000",
        OBJ_IA_STORAGE_RATE, "GB + retrieval + requests", "month",
        [_sf("gb", "Storage Capacity", "GB", 1000, 1, 0),
         _sf("retrievalGb", "Data Retrieved / month", "GB", 0, 1, 0),
         _sf("requests", "Requests (10k units)", "10k req", 0, 1, 0)],
        "Storage $0.0100/GB-mo (first 10 GB free) + retrieval $0.0100/GB (first 10 GB free) "
        "+ requests $0.0034 per 10,000 (first 50,000 free). SKUs B93000/B93001/B91627.")
    add("file", "Storage", "File Storage (NFS)", "B89057",
        _svc_rate("OCI File Storage", fallback=0.30), "GB / month", "month",
        [_sf("gb", "Capacity", "GB", 1024, 128)])
    add("archive", "Storage", "Object Storage - Archive", "B91633",
        ARCHIVE_STORAGE_RATE, "GB + requests", "month",
        [_sf("gb", "Storage Capacity", "GB", 1000, 1, 0),
         _sf("requests", "Requests (10k units)", "10k req", 0, 1, 0)],
        "Storage $0.0026/GB-mo (first 10 GB free) + requests $0.0034 per 10,000 "
        "(first 50,000 free). SKUs B91633/B91627.")

    # ---- Networking ----
    # The Flexible Load Balancer bills TWO meters, and the card had them conflated: B93031 is
    # "Load Balancer Bandwidth" ($0.0001 per Mbps-hour), not the base LB. The base is B93030 at
    # $0.0113 per LB-hour. The old card carried the base RATE on the bandwidth SKU and called
    # bandwidth free, which is exactly backwards - bandwidth is the meter that scales.
    #
    # Both meters have a free allowance expressed in unit-HOURS: 744 LB-hours (one LB for a
    # month) and 7,440 Mbps-hours (the first 10 Mbps for a month), so the tiers apply to
    # quantity x hours, not to the quantity on its own.
    add("lb", "Networking", "Flexible Load Balancer", "B93030",
        LB_BASE_RATE, "LB + bandwidth / hour", "hour",
        [_sf("count", "Load balancers", "LB", 1, 1, 1),
         _sf("mbps", "Bandwidth per LB", "Mbps", 100, 10, 10)],
        "Two meters: the load balancer itself ($0.0113/LB-hour, first 744 LB-hours free) and "
        "its bandwidth ($0.0001/Mbps-hour, first 7,440 Mbps-hours free - i.e. 10 Mbps for a "
        "month). Minimum shape is 10 Mbps, maximum 8,000.")
    C[-1]["lbMeters"] = {
        "base": {"sku": "B93030", "label": "Load Balancer Base", "field": "count",
                 "tiers": [{"min": 0, "max": 744, "rate": 0.0},
                           {"min": 744, "max": None, "rate": LB_BASE_RATE}]},
        "bandwidth": {"sku": "B93031", "label": "Load Balancer Bandwidth", "field": "mbps",
                      "tiers": [{"min": 0, "max": 7440, "rate": 0.0},
                                {"min": 7440, "max": None, "rate": LB_BANDWIDTH_RATE}]},
    }
    add("egress", "Networking", "Outbound Data Transfer", "B87062",
        _svc_rate("OCI Outbound Data Transfer", fallback=0.0085), "GB / month", "month",
        [_sf("gb", "Egress", "GB", 0, 1024)],
        "First 10 TB/region/month is free.", free={"gb": 10240})
    add("fastconnect", "Networking", "FastConnect port", FASTCONNECT_SPEED_SKUS["10G"],
        FASTCONNECT_SPEED_RATES["10G"], "port / hour", "hour",
        [_sel("speed", "Port speed",
              [(key, FASTCONNECT_SPEED_LABELS[key])
               for key in ("1G", "10G", "100G", "400G")], "10G"),
         _sf("ports", "Ports", "port", 1, 1, 1)],
        "Choose a 1, 10, 100, or 400 Gbps provisioned port. Private virtual-circuit "
        "traffic has no separate inbound or outbound transfer charge.")
    C[-1]["speedRates"] = FASTCONNECT_SPEED_RATES
    C[-1]["speedSkus"] = FASTCONNECT_SPEED_SKUS
    C[-1]["speedLabels"] = FASTCONNECT_SPEED_LABELS
    # ---- Base Database Service (VM) ----
    # Three SKUs per instance: edition licence + compute infrastructure, both per ECPU-hour,
    # plus database storage per GB-month. Modelled on one card because you can't buy the
    # edition without the infrastructure - quoting them separately invites leaving one out.
    _bdb_rates, _bdb_skus, _bdb_labels, _bdb_units = _variant_maps(BASEDB_EDITIONS)
    add("basedb", "Database", "Base Database Service (VM)", "B112726", 0.1075,
        "ECPU-hr + storage", "hour",
        # Field order mirrors the cost estimator's own card so the two read the same way:
        # Billing Metric, edition, Processor, CPU count, then Total Available Storage.
        [_sel("metric", "Billing Metric", [("ecpu", "ECPU"), ("ocpu", "OCPU")], "ecpu"),
         _sel("edition", "Base Database Service - Virtual Machine",
              _variant_options(BASEDB_EDITIONS), "enterprise",
              hide_when=("metric", "ocpu")),
         _sel("edition_ocpu", "Base Database Service - Virtual Machine",
              _variant_options(BASEDB_OCPU_EDITIONS), "enterprise",
              show_when=("metric", "ocpu")),
         _sel("processor", "Processor",
              [("amd", "AMD (E4/E5.Flex)"),
               ("intel", "Intel (Standard2.x + Standard3.Flex)"),
               ("arm", "Ampere Arm (A1.Flex, storage capped at 8,192 GB)")], "amd",
              show_when=("metric", "ocpu")),
         _sel("shape", "Shape", [(k, k if BASEDB_SHAPES[k]["ocpu"] is None
                                  else f"{k}  ({BASEDB_SHAPES[k]['ocpu']} OCPU / "
                                       f"{BASEDB_SHAPES[k]['memory_gb']} GB, fixed)")
                                 for k in BASEDB_SHAPES], "VM.Standard.E4.Flex",
              show_when=("metric", "ocpu")),
         _sf("ecpu", "ECPU / OCPU", "CPU", 0, 1, 0),
         _sel("storagegb", "Total Available Storage [GB]",
              [(str(u), f"{u:,} usable  ->  {t:,} GB provisioned")
               for u, t in BASEDB_STORAGE_TIERS], "256"),
         _sf("vpus", "Block Volume VPU/GB", "VPU", 20, 10, 0,
             show_when=("metric", "ocpu"))],
        "Bills the edition licence and the shared compute infrastructure "
        f"(${BASEDB_INFRA_RATE}/ECPU-hr, {BASEDB_INFRA_SKU}) per ECPU-hour, plus database "
        f"storage at ${BASEDB_STORAGE_RATE}/GB-month ({BASEDB_STORAGE_SKU}). Usable storage "
        "comes in fixed tiers and is billed on the TOTAL provisioned capacity, which carries "
        "Oracle's redo/reco overhead - 256 GB usable provisions 717 GB.")
    C[-1].update({"editionRates": _bdb_rates, "editionSkus": _bdb_skus,
                  "editionLabels": _bdb_labels, "editionUnits": _bdb_units})

    # ---- AI & Machine Learning: tier-priced services ----
    # Layout follows Oracle's own estimator service flags (datasets.services in the catalog
    # snapshot): all five set hideInstances=yes, so there is no instance multiplier, and only
    # Speech leaves hideHours=no. Vision / Language / Document Understanding / Queue report
    # days=1 hours=1 - a flat monthly count, not an hourly rate - so their basis is "month".
    for _cid, _name, _meters, _basis, _note in (
        ("vision", "Vision", VISION_METERS, "month",
         "Image and video analysis. Transaction meters bill per 1,000 with the first 5,000 free; "
         "custom training gets 15 free hours and stored video the first 1,000 minutes."),
        ("docunderstanding", "Document Understanding", DOCU_METERS, "month",
         "OCR, property and extraction meters bill per 1,000 transactions with the first 5,000 "
         "free on each; custom training gets 15 free hours."),
        ("language", "Language", LANGUAGE_METERS, "month",
         "Pre-trained inferencing gets the first 5,000 transactions free and translation the "
         "first 1,000; custom inferencing and dedicated healthcare bill from the first unit."),
        ("speech", "Speech", SPEECH_METERS, "hour",
         "Transcription billed per hour of audio, with the first 5 transcription hours free."),
        ("queue", "Queue", QUEUE_METERS, "month",
         "Billed per 1,000,000 requests with the first million free. A request is 64 KB - a "
         "larger message counts as several."),
    ):
        _r, _sk, _lb, _un, _fr = _tier_variant_maps(_meters)
        add(_cid, "AI & Machine Learning" if _cid != "queue" else "Integration", _name,
            _meters[0][4], _meters[0][2], "per meter", _basis,
            [_sel("meter", "Meter", _tier_options(_meters), _meters[0][0]),
             _sf("units", "Quantity", "units", 0, 1, 0)],
            _note)
        C[-1].update({"variantKey": "meter", "variantField": "units", "variantRates": _r,
                      "variantSkus": _sk, "variantLabels": _lb, "variantUnits": _un,
                      "variantFree": _fr, "variantHourly": {k: (_basis == "hour") for k in _r}})

    # ---- AI & Machine Learning ----
    # Combined "OCI Generative AI" card, laid out like Oracle's Cost Estimator: one card with a
    # Models section and a Search & Retrieval section, one combined total. The Models section is
    # a Service Metric -> Model Provider -> Model cascade plus request/prompt/response inputs;
    # the Search & Retrieval section shows all six meters as simultaneous quantity boxes. The
    # frontend renders these two sections and the provider->model cascade specially (entry.id
    # == "genai"); line_cost / clientLineCost carry the authoritative math. See GENAI_MODELS /
    # GENAI_RETRIEVAL above for the source rates.
    _ded_rates, _ded_skus, _ded_labels, _ded_units = _variant_maps(GENAI_DEDICATED)
    _first_provider = GENAI_PROVIDERS[0]
    _first_model_opts = GENAI_MODEL_OPTIONS_BY_PROVIDER[_first_provider]
    _model_info = {
        m[1]: {"provider": m[0], "label": m[2], "basis": m[3],
               "inRate": m[4], "inSku": m[5], "outRate": m[6], "outSku": m[7],
               "lengthUnit": GENAI_UNIT_BASES[m[3]]["lengthUnit"],
               "divisor": GENAI_UNIT_BASES[m[3]]["divisor"]}
        for m in GENAI_MODELS}
    add("genai", "AI & Machine Learning", "OCI Generative AI",
        GENAI_MODELS[0][5], GENAI_MODELS[0][4], "combined", "hour",
        [_sel("metric", "Service Metric",
              [("on_demand", "On Demand"), ("dedicated", "Dedicated")], "on_demand"),
         _sel("provider", "Model Provider", [(p, p) for p in GENAI_PROVIDERS],
              _first_provider, hide_when=("metric", "dedicated")),
         _sel("model", "Model", _first_model_opts, _first_model_opts[0][0],
              hide_when=("metric", "dedicated")),
         _sf("requests", "Expected number of requests per month", "requests", 0, 1, 0,
             hide_when=("metric", "dedicated")),
         _sf("prompt_len", "Prompt length", "", 0, 1, 0, hide_when=("metric", "dedicated")),
         _sf("response_len", "Model Response length", "", 0, 1, 0, hide_when=("metric", "dedicated")),
         _sel("ded_cluster", "Cluster type", _variant_options(GENAI_DEDICATED), "cohere_large",
              show_when=("metric", "dedicated")),
         _sf("ded_units", "AI units", "unit", 0, 1, 0, show_when=("metric", "dedicated"))]
        + [_sf(k, l, u, 0, 1, 0) for k, l, _sec, _r, u, _s, _h, _d in GENAI_RETRIEVAL],
        "Priced like Oracle's Cost Estimator. Models: pick a provider and model, then enter "
        "requests/month and prompt & response length (characters for Cohere/Meta, tokens for "
        "xAI/Google/OpenAI). Search & Retrieval: storage meters bill per GB-hour at the card's "
        "Utilization (hrs/mo, 744 = the estimator's default); request/event meters are flat "
        "per-1,000 counts. All quantity inputs start at 0.")
    C[-1].update({
        "genaiCombined": True,
        "genaiProviders": GENAI_PROVIDERS,
        "genaiModelOptions": {p: [{"value": k, "label": l}
                                  for k, l in GENAI_MODEL_OPTIONS_BY_PROVIDER[p]]
                              for p in GENAI_PROVIDERS},
        "genaiModelInfo": _model_info,
        "genaiRetrieval": [{"key": k, "label": l, "section": sec, "rate": r, "unit": u,
                            "sku": s, "hourly": h, "divisor": d}
                           for k, l, sec, r, u, s, h, d in GENAI_RETRIEVAL],
        "genaiDedicated": {"rates": _ded_rates, "skus": _ded_skus,
                           "labels": _ded_labels, "units": _ded_units},
        "retrievalHoursDefault": GENAI_RETRIEVAL_HOURS_DEFAULT,
    })

    # OCI Generative AI Agents - a SEPARATE service (estimator service 3081). Two sections:
    # Retrieval-Augmented Generation (agent transactions, a transaction is a character) and an
    # optional Managed Knowledge Base (per-GB-hour storage + per-job ingestion characters).
    add("genai_agents", "AI & Machine Learning", "OCI Generative AI Agents",
        "B110461", 0.003, "combined", "hour",
        [_sf("rag_requests", "Requests per month", "requests", 0, 1, 0),
         _sf("rag_chars", "Average characters processed per request", "chars", 0, 1, 0),
         _sf("kb_storage", "Storage capacity", "GB", 0, 1, 0),
         _sf("kb_jobs", "Data ingestion jobs per month", "jobs", 0, 1, 0),
         _sf("kb_chars", "Average characters processed per job", "chars", 0, 1, 0)],
        "OCI Generative AI Agents - a separate service from the Generative AI models. RAG bills "
        "agent transactions ($0.003/10K characters); the optional Managed Knowledge Base bills "
        "storage per GB-hour ($0.0084) and ingestion per 10K characters processed ($0.0003).")
    C[-1].update({
        "genaiAgents": True,
        "genaiAgentMeters": {
            "txn": {"rate": 0.003, "sku": "B110461", "divisor": 10000},
            "kb": {"rate": 0.0084, "sku": "B110462"},
            "ingest": {"rate": 0.0003, "sku": "B110463", "divisor": 10000},
        },
        "retrievalHoursDefault": GENAI_RETRIEVAL_HOURS_DEFAULT,
    })

    # B88516 was NOT a DNS part number - it is "Compute - Virtual Machine Dense I/O - X7"
    # ($0.1275/OCPU-hr). The rate on the card was right, so the money was right, but every BOM
    # and every exported paper trail cited a compute SKU for a DNS line. DNS is B88525.
    # Traffic Management (B90327) is a second, separately-metered DNS product the card never
    # offered at all; its $4.00/1M rate is Oracle's estimator figure (service 886), which the
    # app's own price list has as 0.85 - a copy of the base DNS rate.
    add("dns", "Networking", "DNS (metered queries)", "B88525",
        _svc_rate("OCI DNS", fallback=0.85), "per 1M queries", "op",
        [_sf("millions", "Queries per month", "million", 1, 1),
         _sf("tm_millions", "Traffic Management queries per month", "million", 0, 1, 0)],
        "Hosted zones and intra-VCN queries are free. Traffic Management steering policies "
        "are metered separately at $4.00 per million queries.")
    C[-1]["skuMeters"] = [
        {"key": "millions", "sku": "B88525", "label": "DNS - Queries",
         "metric": "1,000,000 Queries", "billing": "MONTH", "hourly": False,
         "tiers": [{"min": 0, "max": None, "rate": _svc_rate("OCI DNS", fallback=0.85)}],
         "rate": _svc_rate("OCI DNS", fallback=0.85)},
        {"key": "tm_millions", "sku": "B90327", "label": "DNS - Traffic Management Queries",
         "metric": "1,000,000 DNS Traffic Management Queries", "billing": "MONTH",
         "hourly": False, "tiers": [{"min": 0, "max": None, "rate": 4.00}], "rate": 4.00},
    ]

    # ---- Database (PaaS) ----
    # Autonomous DB rates are the customer-supplied OCI price-list values (these SKUs aren't
    # in oci_price_list.json). ECPU is billed per ECPU-hour; storage per GB-month.
    # Comprehensive Autonomous AI Database (Single Database, serverless), mirroring the OCI
    # cost estimator: ECPU compute + database storage (rate depends on workload) + backup
    # storage, all on one card. Priced by the "adb" branch in line_cost.
    add("adb", "Database", "Autonomous AI Database", "B95702", ADB_ECPU_RATE,
        "ECPU-hr + storage", "hour",
        [_sel("deployment", "Deployment Type",
              [("serverless", "Serverless"), ("dedicated", "Dedicated (Exadata)")], "serverless"),
         _sel("workload", "Workload Type",
              [("atp", "Transaction Processing (ATP)"),
               ("adw", "Lakehouse / Data Warehouse (ADW)"),
               ("ajd", "JSON (AJD)"),
               ("apex", "APEX")], "atp"),
         _sf("ecpu", "ECPUs", "ECPU", 2, 1, 2),
         _sf("dbgb", "Database Storage", "GB", 20, 1, 1, show_when=("deployment", "serverless")),
         _sf("dbservers", "Exadata DB Servers (X11M)", "server", 2, 1, 1,
             show_when=("deployment", "dedicated")),
         _sf("storageservers", "Exadata Storage Servers (X11M)", "server", 3, 1, 1,
             show_when=("deployment", "dedicated")),
         _sf("bakgb", "Backup Storage", "GB", 60, 1, 0)],
        "Serverless: ECPU $0.336/hr + DB storage (ATP $0.1953, ADW $0.0299/GB-mo) + backup "
        "$0.0299/GB-mo. Dedicated: ECPU + Exadata DB $6.3014/hr + Exadata storage $5.4795/hr "
        "+ Object Storage backup $0.0255/GB (10 GB free). SKUs B95702/B95701/B112666/B112667.")
    add("mysql", "Database", "MySQL HeatWave Database", "B108030", MYSQL_ECPU_RATE,
        "ECPU-hr + storage", "hour",
        [_sel("ecpu", "Total ECPU",
              [(2, "2"), (4, "4"), (8, "8"), (16, "16"), (32, "32"), (48, "48"),
               (64, "64"), (96, "96"), (128, "128"), (256, "256"), (512, "512")], 8),
         _sf("storage", "MySQL Storage", "GB", 1000, 1, 0),
         _sf("backup", "Additional Backup Storage", "GB", 0, 1, 0),
         _sf("egress", "Inter-OCI Region Egress", "GB", 0, 100, 0),
         _sel("ha", "High Availability", [("no", "No"), ("yes", "Yes (3 instances)")], "no"),
         _sel("heatwave", "HeatWave Cluster", [("no", "No"), ("yes", "Yes")], "no"),
         _sf("hwcapacity", "HeatWave Capacity Units", "unit", 128, 1, 0,
             show_when=("heatwave", "yes")),
         _sf("hwstorage", "HeatWave Storage", "GB", 1000, 1, 0,
             show_when=("heatwave", "yes"))],
        "ECPU $0.0366/hr; storage/backup/inter-region egress $0.04/GB-mo. HA triples ECPU + "
        "storage. HeatWave adds $0.011/capacity-hr + $0.02/GB-mo storage. Total ECPU is a "
        "fixed shape (2/4/8/16/32/48/64/96/...); memory is derived at 8 GB per ECPU. "
        "SKUs B108030/B92426/B92483/B109169/B96626/B96625.")
    add("pg", "Database", "Database with PostgreSQL", "B99060", PG_MANAGED_OCPU_RATE,
        "OCPU-hr + storage", "hour",
        [_sel("processor", "Processor", [("amd", "AMD (E5)"), ("intel", "Intel (X9)")], "amd"),
         _sf("ocpu", "OCPU per node", "OCPU", 10, 1, 1),
         _sf("nodes", "Nodes per cluster", "node", 3, 1, 1),
         _sf("memory", "Memory per node", "GB", 100, 1, 16),
         _sf("storage", "DB-Optimized Storage", "GB", 1000, 1, 0),
         _sf("vpu", "Storage VPU", "VPU", 30, 5, 0)],
        "Managed PostgreSQL OCPU $0.098/hr (x nodes) + DB-optimized storage $0.072/GB-mo + "
        "underlying compute (AMD $0.03 OCPU/$0.002 mem, Intel $0.04/$0.0015, x nodes) + block "
        "performance $0.0017/(GB*VPU). Sizing limits: AMD 1-64 OCPU, 16-1024 GB; Intel 2-32 "
        "OCPU, 32-512 GB (max 64 GB/OCPU). SKUs B99060/B99062/B97384/B97385/B91962.")
    add("dbbackup", "Database", "Database Backup (to Object Storage)", "B90230",
        _svc_rate("OCI Database Backup", fallback=0.0051), "GB / month", "month",
        [_sf("gb", "Backup capacity", "GB", 500, 100)])
    add("recovery", "Database", "Autonomous Recovery Service", "B95240", 0.0306,
        "GB / month", "month", [_sf("gb", "Protected capacity", "GB", 100, 50)],
        "Oracle Database Autonomous Recovery Service - virtualized GB per month.")

    # ---- Integration ----
    add("oic", "Integration", "Application Integration (OIC)", "B89639", OIC_STD_RATE,
        "message pack-hr", "hour",
        [_sel("edition", "License Edition",
              [("standard", "Standard"), ("enterprise", "Enterprise")], "standard"),
         _sf("peakday", "Peak daily volume (surge)", "msg/day", 0, 1000, 0),
         _sf("monthlymsgs", "Total messages / month", "msg/mo", 0, 100000, 0),
         _sf("packs", "Message Packs (used if volumes = 0)", "pack", 1, 1, 0)],
        "Priced per message pack-hour: Standard $0.6452, Enterprise $1.2903. 1 pack = 5,000 "
        "msg/hr (payload <=50KB). Packs auto-size from peak daily volume /(24*5,000), else "
        "total monthly messages /(hours*5,000), else the entered pack count. SKU B89639.")

    # ---- Security ----
    add("waf", "Security", "Web Application Firewall", "B94579", WAF_INSTANCE_RATE,
        "instance + requests", "month",
        [_sf("instances", "WAF Instances", "instance", 1, 1, 0),
         _sf("requests", "Incoming Requests (1M units)", "1M req", 0, 1, 0)],
        "Instances $5.00/mo (first instance free) + incoming requests $0.60 per 1,000,000 "
        "(first 10,000,000 free). Requests are entered in units of 1,000,000. SKUs B94579/B94277.")
    add("kms", "Security", "Key Management (Vault)", "B90328", KMS_VAULT_RATE,
        "vault-hr + keys", "hour",
        [_sf("vaults", "Private Vaults", "vault", 0, 1, 0),
         _sf("keyversions", "Software Key Versions (free)", "key", 0, 1, 0),
         _sf("external", "External Key Management", "key", 0, 1, 0),
         _sf("hsm", "Dedicated HSM Partitions (min 3)", "partition", 0, 1, 0)],
        "Virtual Private Vault $3.724/hr (B90328) + External Key Management $3.00/key-mo "
        "(B98100) + Dedicated HSM partitions $1.75/hr (B99597, min 3). Software key versions "
        "are free (B92092).")

    # ---- Disaster Recovery ----
    add("fsdr", "Other Services", "Full Stack Disaster Recovery", "B95485", FSDR_OCPU_RATE,
        "member-hours (both regions)", "hour",
        [_sf("p_compute", "Primary: Compute Member OCPUs", "OCPU", 0, 1, 0),
         _sf("p_db_ocpu", "Primary: Database Member OCPUs", "OCPU", 0, 1, 0),
         _sf("p_db_ecpu", "Primary: Database Member ECPUs", "ECPU", 0, 1, 0),
         _sf("p_oic", "Primary: OIC Message Packs", "pack", 0, 1, 0),
         _sf("s_compute", "Standby: Compute Member OCPUs", "OCPU", 0, 1, 0),
         _sf("s_db_ocpu", "Standby: Database Member OCPUs", "OCPU", 0, 1, 0),
         _sf("s_db_ecpu", "Standby: Database Member ECPUs", "ECPU", 0, 1, 0),
         _sf("s_oic", "Standby: OIC Message Packs", "pack", 0, 1, 0)],
        "OCI Full Stack DR, metered per member-hour across BOTH protection groups: "
        "Compute + DB member OCPUs at $0.0128/OCPU-hr (B95485), DB member ECPUs at "
        "$0.0032/ECPU-hr (B110274), OIC message packs at $0.192/pack-hr (B112110).")

    # ---- Observability / Other ----
    # B92707 does not exist in Oracle's catalog or in the app's own price list. The Logging
    # storage part number is B92593; the rate and the 10 GB free tier were already correct.
    add("logging", "Observability", "Logging (ingest)", "B92593",
        _svc_rate("OCI Logging", fallback=0.05), "GB / month", "month",
        [_sf("gb", "Log data", "GB", 0, 10)],
        "First 10 GB/month is free.", free={"gb": 10})
    add("desktops", "Other Services", "Secure Desktops", "B95518", DESKTOP_UNIT_RATE,
        "desktop + compute + storage", "month",
        [_sf("desktops", "Secure Desktops Per Pool", "desktop", 20, 1, 1),
         _sel("os", "Desktop OS", [("linux", "Oracle Linux"),
              ("win_dvh", "Windows BYOL on DVH"), ("win_vm", "Windows BYOL on VM")], "linux"),
         _sf("ocpu", "Desktop OCPU", "OCPU", 2, 1, 1),
         _sf("memory", "Desktop Memory", "GB", 8, 1, 1),
         _sf("bootgb", "Boot Volume", "GB", 100, 1, 0),
         _sf("bootvpu", "Boot VPU", "VPU", 10, 5, 0),
         _sf("optgb", "Optional Block Storage / Desktop", "GB", 0, 1, 0),
         _sf("optvpu", "Optional Block VPU", "VPU", 10, 5, 0)],
        "Per desktop ($20/mo, B95518) x pool + underlying E6 compute ($0.03 OCPU/$0.002 mem, "
        "B111129/B111130) + boot & optional block volumes ($0.0255/GB + $0.0017/(GB*VPU), "
        "B91961/B91962). Windows options are BYOL (no added license). All x desktops.")

    # ---- 3rd-party licensing (NOT discounted) ----
    add("winlic", "Licensing", "Windows Server license", "B88318", _rate("B88318", 0.092),
        "OCPU / hour", "hour", [_sf("ocpu", "Licensed OCPUs", "OCPU", 2, 1, 1)],
        "3rd-party Microsoft licensing - excluded from the OCI discount.", third_party=True)
    add("sqllic", "Licensing", "SQL Server License", "B91372", SQL_ENT_RATE,
        "OCPU / hour", "hour",
        [_sel("edition", "Edition",
              [("enterprise", "Enterprise"), ("standard", "Standard"),
               ("express", "Express (free)")], "enterprise"),
         _sf("ocpu", "Licensed OCPUs", "OCPU", 1, 1, 1)],
        "License-included Microsoft SQL Server (OCI marketplace image): Enterprise $1.47/OCPU-hr "
        "(B91372), Standard $0.37/OCPU-hr (B91373), Express $0. 3rd-party licensing - excluded "
        "from the OCI discount.", third_party=True)

    return [c for c in C if isinstance(c["rate"], (int, float))]


# --- estimator-derived services ---------------------------------------------------------------
# Services generated from Oracle's own Cost Estimator snapshot (data/estimator_services.json,
# built by scripts/gen_estimator_services.py). The curated cards above are hand-tuned because
# their pricing has real logic in it - Base Database's provisioned-capacity tiers, OIC's message
# packs, the Generative AI model math. The services here are the ones where Oracle's estimator
# itself renders nothing more than a quantity box per SKU (component_type is null in its visual
# spec), so a generated card IS the estimator's card, not an approximation of it.
#
# Each SKU keeps its full graduated tier table rather than a single rate, because that is where
# OCI's free allowances live: Monitoring ingestion is $0 up to 500M datapoints and $0.0025 after,
# Notifications is $0 for the first million. Collapsing those to one rate would bill from the
# first unit.

ESTIMATOR_FILE = DATA / "estimator_services.json"


def _estimator_data():
    try:
        return json.loads(ESTIMATOR_FILE.read_text())
    except (FileNotFoundError, ValueError):
        # The catalog still works without the snapshot; only the generated cards disappear.
        return {"services": [], "counts": {}, "extractedAtUtc": None}


_ESTIMATOR = _estimator_data()
ESTIMATOR_SNAPSHOT_DATE = _ESTIMATOR.get("extractedAtUtc") or ""


def tiered_cost(qty, tiers):
    """Graduated (marginal) cost of `qty` against Oracle's rangeMin/rangeMax tier table.

    Graduated, not flat: OCI states "first 500 million datapoints free, $0.0025 after", so only
    the units ABOVE a tier's floor pay that tier's rate. A flat reading of the top tier would
    bill the free allowance too, and a flat reading of the bottom tier would bill nothing at all.
    """
    qty = float(qty or 0)
    if qty <= 0 or not tiers:
        return 0.0
    total = 0.0
    for t in tiers:
        lo = float(t.get("min") or 0)
        hi = t.get("max")
        if qty <= lo:
            break
        span = (qty - lo) if hi is None else (min(qty, float(hi)) - lo)
        if span > 0:
            total += span * float(t.get("rate") or 0)
    return total


def estimator_meter_cost(meter, qty, hours):
    """One SKU row: graduated tiers, multiplied by hours when the SKU bills per hour."""
    cost = tiered_cost(qty, meter.get("tiers") or [])
    return cost * float(hours or 0) if meter.get("hourly") else cost


# Bespoke estimator components whose card is nonetheless just a list of quantity rows, so the
# generated engine renders them correctly. OKE is two independent per-hour meters (virtual nodes,
# enhanced clusters) with no shape or dependency between them.
GENERIC_COMPONENT_TYPES = (None, "oke")


def _estimator_entries(include_component_types=GENERIC_COMPONENT_TYPES):
    """Catalog entries for the generated services.

    `include_component_types` gates which ones are wired up: (None,) is the set whose estimator
    card is genuinely just a list of quantity rows. Services with a bespoke estimator component
    (compute, exadata, vmware, ...) are deliberately left out until they get a hand-tuned card,
    because Oracle's visual spec explicitly does not describe their field dependencies.
    """
    out = []
    for svc in _ESTIMATOR.get("services") or []:
        if svc.get("componentType") not in include_component_types:
            continue
        meters = svc.get("skus") or []
        if not meters:
            continue
        form = svc.get("form") or {}
        # The estimator's utilisation for this service, e.g. 31 x 24 = 744 hrs/month.
        est_hours = float(form.get("days") or 31) * float(form.get("hoursPerDay") or 24)
        hourly = any(m.get("hourly") for m in meters)
        fields = [
            _sf(m["key"], m["label"], m.get("metric") or "unit", 0,
                # Oracle says whether a SKU takes fractional quantities; honour it so a card
                # asking for TB or ECPU-hours is not forced to whole numbers.
                0.01 if m.get("decimal") else (m.get("step") or 1), 0)
            for m in meters
        ]
        icon, resolution = architecture_mapping(svc["name"], svc["group"])
        headline = meters[0]
        note = svc.get("note") or ""
        note = (note + " " if note else "") + (
            "Priced from Oracle's Cost Estimator snapshot %s: one line per SKU, each at its own "
            "metric. Quantities start at 0." % (ESTIMATOR_SNAPSHOT_DATE[:10] or "")
        ).strip()
        out.append({
            "id": svc["id"],
            "group": svc["group"],
            "name": svc["name"],
            "sku": headline["sku"],
            "rate": headline["rate"],
            "unit": headline.get("metric") or "unit",
            "basis": "hour" if hourly else "month",
            "fields": fields,
            "note": note,
            "free": {},
            "source": "estimator",
            "architectureIcon": icon,
            "architectureResolution": resolution,
            "thirdParty": any(t in _norm(svc["name"]) for t in _THIRD_PARTY_TERMS),
            # The pricing contract the cost functions and the client mirror both read.
            "skuMeters": meters,
            "estimatorServiceId": svc.get("serviceId"),
            "estimatorHoursDefault": est_hours,
            "docUrl": svc.get("docUrl") or "",
        })
    out.sort(key=lambda e: (GROUPS.index(e["group"]) if e["group"] in GROUPS else 99, e["name"]))
    return out


# --- Compute cards ----------------------------------------------------------------------------
# Compute is the one estimator service the generated SKU-meter card cannot express. Oracle bills
# a flex VM as a PAIR of SKUs - OCPU-hours and GB-hours, at different rates per shape family -
# so a card with one quantity box per SKU would ask for "E6 OCPU" and "E6 Memory" as unrelated
# numbers and let you price 8 OCPUs of E6 against 64 GB of A1. These cards keep the shape as the
# unit of choice, the way the estimator does, and derive the SKUs from it.
#
# Rates still come from the snapshot (data/estimator_services.json), so a refresh moves them.

_COMPUTE_PREFIX = re.compile(r"^(oracle cloud infrastructure|oci)\s*-?\s*", re.I)
_COMPUTE_COMPONENT = re.compile(r"[\s-]*(ocpu|memory|nvme)\s*$", re.I)


def _estimator_service(service_id):
    for svc in _ESTIMATOR.get("services") or []:
        if svc.get("serviceId") == service_id:
            return svc
    return None


def _compute_families(service_id):
    """Group a compute service's SKUs into shape families keyed by their component.

    Oracle names them "OCI - Compute - Standard - E6 - OCPU" / "... - Memory" / "... - NVMe",
    so the component is the last word and the rest is the family. Legacy fixed shapes (X5, X7,
    B1, E2) have an OCPU SKU only - their memory is bundled into the core rate.
    """
    svc = _estimator_service(service_id)
    families = {}
    order = []
    for meter in (svc or {}).get("skus") or []:
        label = _COMPUTE_PREFIX.sub("", meter["label"]).strip()
        found = _COMPUTE_COMPONENT.search(label)
        component = found.group(1).lower() if found else "ocpu"
        name = _COMPUTE_COMPONENT.sub("", label).strip(" -")
        name = re.sub(r"^Compute\s*-\s*", "", name).strip()
        key = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        if key not in families:
            families[key] = {"key": key, "label": name}
            order.append(key)
        families[key][component] = {"sku": meter["sku"], "rate": float(meter["rate"])}
    return [families[k] for k in order]


def _compute_entry(entry_id, service_id, name, note, card, fields, extra):
    """Shared shell for the hand-tuned compute cards."""
    svc = _estimator_service(service_id) or {}
    form = svc.get("form") or {}
    icon, resolution = architecture_mapping(name, "Compute")
    payload = {
        "id": entry_id, "group": "Compute", "name": name,
        "sku": "", "rate": 0.0, "unit": "OCPU / hour", "basis": "hour",
        "fields": fields, "note": note, "free": {}, "source": "estimator-compute",
        "architectureIcon": icon, "architectureResolution": resolution,
        "thirdParty": False,
        "computeCard": card,
        "estimatorServiceId": service_id,
        "estimatorHoursDefault": float(form.get("days") or 31) * float(form.get("hoursPerDay") or 24),
    }
    payload.update(extra)
    return payload


_OCVS_TERMS = (("hourly", "Hourly"), ("1 month", "Monthly"), ("monthly", "Monthly"),
               ("1 year", "1 Year"), ("1 yr", "1 Year"),
               ("3 year", "3 Year"), ("3 yr", "3 Year"))


def _OCVS_TERM(label):
    """Commitment term from an OCVS SKU label; Oracle spells it several ways."""
    low = label.lower()
    for needle, name in _OCVS_TERMS:
        if needle in low:
            return name
    return "Hourly"


def _compute_entries():
    out = []

    # ---- Virtual Machine: shape -> OCPU + Memory (+ NVMe on Dense I/O) ----
    fams = _compute_families(822)
    if fams:
        shapes = {f["key"]: f for f in fams}
        flex = [f["key"] for f in fams if "memory" in f]
        nvme = [f["key"] for f in fams if "nvme" in f]
        default = "standard_e6" if "standard_e6" in shapes else fams[0]["key"]
        options = []
        for f in fams:
            bits = [f"${f['ocpu']['rate']:g}/OCPU-hr"]
            if "memory" in f:
                bits.append(f"${f['memory']['rate']:g}/GB-hr")
            options.append((f["key"], f"{f['label']} - " + ", ".join(bits)))
        out.append(_compute_entry(
            "compute_vm", 822, "Compute - Virtual Machine",
            "Flexible shapes bill OCPU-hours and GB-hours as two separate SKUs at different "
            "rates, so the shape is chosen first and the quantities follow it. Legacy fixed "
            "shapes (X5, X7, B1, E2) bill OCPU-hours only - their memory is bundled into the "
            "core rate, so the Memory box is hidden for them.",
            "vm",
            [_sel("shape", "Shape", options, default),
             _sf("ocpu", "OCPUs per instance", "OCPU", 0, 1, 0),
             _sf("memory", "Memory per instance", "GB", 0, 1, 0, show_when=("shape", flex)),
             _sf("nvme", "NVMe per instance", "TB", 0, 1, 0, show_when=("shape", nvme)),
             _sf("instances", "Instances", "instance", 1, 1, 0)],
            {"shapeOptions": shapes,
             "sku": shapes[default]["ocpu"]["sku"],
             "rate": shapes[default]["ocpu"]["rate"]},
        ))

    # ---- Bare Metal: one SKU per shape, billed per OCPU-hour ----
    bm = _compute_families(829)
    if bm:
        shapes = {f["key"]: f for f in bm}
        options = [(f["key"], f"{f['label']} - ${f['ocpu']['rate']:g}/OCPU-hr") for f in bm]
        out.append(_compute_entry(
            "compute_bm", 829, "Compute - Bare Metal",
            "A bare metal server is billed per OCPU-hour on all of its cores - there is no "
            "partial box, so enter the shape's full core count.",
            "bm",
            [_sel("shape", "Shape", options, bm[0]["key"]),
             _sf("ocpu", "OCPUs per server", "OCPU", 0, 1, 0),
             _sf("instances", "Servers", "server", 1, 1, 0)],
            {"shapeOptions": shapes,
             "sku": bm[0]["ocpu"]["sku"],
             "rate": bm[0]["ocpu"]["rate"]},
        ))

    # ---- VMware (OCVS): shape x commitment term, on one of two billing metrics ----
    # Oracle prices OCVS per SDDC shape AND per commitment term (Hourly / Monthly / 1 Year /
    # 3 Year), and the newer shapes bill per NODE-hour while the older ones bill per OCPU-hour.
    # Not every shape offers every term, so the plan itself is the selection - picking a shape
    # and a term separately would let a user choose a combination Oracle does not sell.
    vsvc = _estimator_service(1321)
    if vsvc:
        plans, expansion, hcx = {}, {}, {}
        for m in vsvc["skus"]:
            label = m["label"]
            shape = re.search(r"BM\.[A-Za-z0-9.]+", label)
            term = _OCVS_TERM(label)
            rec = {"sku": m["sku"], "rate": float(m["rate"]), "term": term,
                   "node": "node" in (m.get("metric") or "").lower()}
            if "hcx" in label.lower():
                hcx[term] = dict(rec, label=f"HCX Enterprise - {term}")
            elif shape:
                key = re.sub(r"[^a-z0-9]+", "_", f"{shape.group(0)} {term}".lower()).strip("_")
                plans[key] = dict(rec, label=f"{shape.group(0)} - {term}",
                                  shape=shape.group(0))
            elif "expansion" in label.lower():
                key = re.sub(r"[^a-z0-9]+", "_", term.lower())
                expansion[key] = dict(rec, label=f"Expansion - {term}")
        by_node = [k for k, r in plans.items() if r["node"]]
        by_ocpu = [k for k, r in plans.items() if not r["node"]]
        plan_opts = [(k, f"{r['label']} - ${r['rate']:g}/{'node' if r['node'] else 'OCPU'}-hr")
                     for k, r in plans.items()]
        exp_opts = [("none", "None")] + [
            (k, f"{r['term']} - ${r['rate']:g}/OCPU-hr") for k, r in expansion.items()]
        hcx_rec = hcx.get("Monthly") or (next(iter(hcx.values())) if hcx else None)
        fields = [_sel("plan", "SDDC shape and commitment", plan_opts,
                       next(iter(plans), "")),
                  _sf("nodes", "Nodes", "node", 3, 1, 0, show_when=("plan", by_node)),
                  _sf("ocpu", "OCPUs", "OCPU", 0, 1, 0, show_when=("plan", by_ocpu)),
                  _sel("expansion", "Expansion capacity", exp_opts, "none"),
                  _sf("expansion_ocpu", "Expansion OCPUs", "OCPU", 0, 1, 0,
                      hide_when=("expansion", "none"))]
        if hcx_rec:
            fields.append(_sf("hcx_ocpu", "HCX Enterprise OCPUs", "OCPU", 0, 1, 0))
        first = next(iter(plans.values()), {})
        out.append(_compute_entry(
            "compute_vmware", 1321, "Compute - VMware Solution (OCVS)",
            "Oracle Cloud VMware Solution is priced per SDDC shape AND commitment term, and "
            "not every shape offers every term - so the plan is one choice, not two. Newer "
            "shapes bill per node-hour, older ones per OCPU-hour, which is why only the "
            "matching quantity box is shown. A minimum 3-node SDDC is the supported "
            "configuration. Expansion capacity and HCX Enterprise are separate meters.",
            "ocvs", fields,
            {"ocvsPlans": plans, "ocvsExpansion": expansion,
             "ocvsHcx": hcx_rec, "unit": "node / hour",
             "sku": first.get("sku", ""), "rate": first.get("rate", 0.0)},
        ))

    # ---- GPU: the accelerator, plus the optional NVIDIA AI Enterprise licence ----
    gsvc = _estimator_service(801)
    if gsvc:
        gpus, aie = {}, {}
        for m in gsvc["skus"]:
            label = _COMPUTE_PREFIX.sub("", m["label"]).strip()
            key = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
            rec = {"key": key, "label": re.sub(r"^Compute\s*-\s*", "", label).strip(),
                   "sku": m["sku"], "rate": float(m["rate"])}
            # "NVIDIA AI Enterprise" rows are a per-GPU software licence layered on top of an
            # accelerator, not an accelerator you can buy on its own.
            (aie if "nvidia ai enterprise" in label.lower() else gpus)[key] = rec
        gpu_opts = [(k, f"{r['label']} - ${r['rate']:g}/GPU-hr") for k, r in gpus.items()]
        aie_opts = [("none", "None")] + [
            (k, f"{r['label'].replace('NVIDIA AI Enterprise - ', '')} - ${r['rate']:g}/GPU-hr")
            for k, r in aie.items()]
        out.append(_compute_entry(
            "compute_gpu", 801, "Compute - GPU",
            "GPU shapes bill per GPU-hour. NVIDIA AI Enterprise is a separate per-GPU-hour "
            "licence on top of the accelerator - leave it at None for a BYOL or bare stack.",
            "gpu",
            [_sel("gpu", "Accelerator", gpu_opts, next(iter(gpus), "")),
             _sf("gpus", "GPUs", "GPU", 0, 1, 0),
             _sel("aie", "NVIDIA AI Enterprise licence", aie_opts, "none"),
             _sf("aie_gpus", "Licensed GPUs", "GPU", 0, 1, 0, hide_when=("aie", "none"))],
            {"gpuOptions": gpus, "aieOptions": aie, "unit": "GPU / hour",
             "sku": next(iter(gpus.values()))["sku"],
             "rate": next(iter(gpus.values()))["rate"]},
        ))
    return out


CURATED = _curated() + _estimator_entries() + _compute_entries()


# --- monthly cost -----------------------------------------------------------------------------
def line_cost(entry, values, hours=HOURS_PER_MONTH):
    """Monthly USD for a filled-in catalog entry. Deterministic; mirrors the app's math,
    including free tiers (egress 10 TB, WAF 10M requests, Logging 10 GB).

    `hours` is the app's hours-per-month setting - anything billed per hour (ECPU, OCPU,
    load-balancer-hour, port-hour) multiplies by it, so the catalog follows the same hours
    the compute rows use rather than a static 730.
    """
    rate = float(entry.get("rate") or 0)
    basis = entry.get("basis", "month")
    free = entry.get("free") or {}
    # Per-hour add-ins default to 730 hours/month, editable per SKU via a "__hours" value.
    hours = float((values.get("__hours") if values else 0) or 0) or float(hours or HOURS_PER_MONTH)
    # Numeric sizing fields. A dropdown with numeric option values (e.g. MySQL Total ECPU)
    # parses to a number; a text dropdown (processor, workload) stays a string and is read
    # directly from `values` by the per-entry math below.
    v = {}
    for f in entry["fields"]:
        try:
            v[f["key"]] = float(values.get(f["key"], f.get("default", 0)) or 0)
        except (TypeError, ValueError):
            pass

    # Autonomous AI Database: ECPU compute + storage + backup. Serverless prices DB storage
    # per workload and backup at $0.0299/GB; Dedicated adds Exadata infra and backs up to
    # Object Storage ($0.0255/GB, first 10 GB free).
    if entry["id"] == "adb":
        deployment = str(values.get("deployment") or "serverless").lower()
        workload = str(values.get("workload") or "atp").lower()
        ecpu_cost = v.get("ecpu", 0) * ADB_ECPU_RATE * hours
        bakgb = v.get("bakgb", 0)
        if deployment == "dedicated":
            infra = (v.get("dbservers", 0) * ADB_EXA_DB_SERVER
                     + v.get("storageservers", 0) * ADB_EXA_STORAGE_SERVER) * hours
            backup = max(0.0, bakgb - ADB_OBJ_BACKUP_FREE) * ADB_OBJ_BACKUP
            return round(ecpu_cost + infra + backup, 2)
        store_rate = ADB_STORAGE_ADW if workload == "adw" else ADB_STORAGE_ATP
        return round(ecpu_cost + v.get("dbgb", 0) * store_rate + bakgb * ADB_BACKUP_RATE, 2)

    # Secure Desktops: per-desktop fee ($20) + compute + boot volume + optional block per
    # desktop. Two compute models depending on the desktop OS:
    #   VM (Oracle Linux / Windows-BYOL-on-VM): E6 compute + boot PER DESKTOP.
    #   DVH (Windows-BYOL-on-DVH): E4.128 Dedicated Host(s) + boot PER HOST; host count =
    #       ceil(desktops * OCPU / 124 available OCPUs).
    if entry["id"] == "desktops":
        n = v.get("desktops", 0)
        cost = n * DESKTOP_UNIT_RATE
        cost += (v.get("optgb", 0) * DESKTOP_BLOCK_RATE
                 + v.get("optgb", 0) * v.get("optvpu", 0) * DESKTOP_VPU_RATE) * n
        if str(values.get("os") or "linux").lower() == "win_dvh":
            hosts = max(1, math.ceil(n * v.get("ocpu", 0) / DVH_AVAIL_OCPU)) if v.get("ocpu", 0) else 1
            cost += hosts * DVH_HOST_OCPU * hours * DESKTOP_E4_OCPU_RATE
            cost += hosts * DVH_HOST_MEM * hours * DESKTOP_E4_MEM_RATE
            cost += (v.get("bootgb", 0) * DESKTOP_BLOCK_RATE
                     + v.get("bootgb", 0) * v.get("bootvpu", 0) * DESKTOP_VPU_RATE) * hosts
        else:
            cost += v.get("ocpu", 0) * n * hours * DESKTOP_OCPU_RATE
            cost += v.get("memory", 0) * n * hours * DESKTOP_MEM_RATE
            cost += (v.get("bootgb", 0) * DESKTOP_BLOCK_RATE
                     + v.get("bootgb", 0) * v.get("bootvpu", 0) * DESKTOP_VPU_RATE) * n
        return round(cost, 2)

    # SQL Server license (license-included): per-edition OCPU-hour rate (Express is free).
    if entry["id"] == "sqllic":
        edition = str(values.get("edition") or "enterprise").lower()
        rate = {"enterprise": SQL_ENT_RATE, "standard": SQL_STD_RATE}.get(edition, 0.0)
        return round(v.get("ocpu", 0) * rate * hours, 2)

    # Key Management: private vaults + external key mgmt + dedicated HSM partitions.
    # Software key versions are free.
    if entry["id"] == "kms":
        return round(v.get("vaults", 0) * hours * KMS_VAULT_RATE
                     + v.get("external", 0) * KMS_EXTERNAL_RATE
                     + v.get("hsm", 0) * hours * KMS_HSM_RATE, 2)

    # Web Application Firewall: instances (first free) + requests per 1M (first 10M free).
    if entry["id"] == "waf":
        return round(max(0.0, v.get("instances", 0) - WAF_INSTANCE_FREE) * WAF_INSTANCE_RATE
                     + max(0.0, v.get("requests", 0) - WAF_REQUEST_FREE) * WAF_REQUEST_RATE, 2)

    # Object Storage: GB storage (first 10 GB free) + requests per 10k (first 50k free).
    if entry["id"] == "object":
        return round(max(0.0, v.get("gb", 0) - OBJ_STORAGE_FREE_GB) * OBJ_STORAGE_RATE
                     + max(0.0, v.get("requests", 0) - OBJ_REQUEST_FREE_UNITS) * OBJ_REQUEST_RATE, 2)
    if entry["id"] == "object_ia":
        return round(max(0.0, v.get("gb", 0) - OBJ_STORAGE_FREE_GB) * OBJ_IA_STORAGE_RATE
                     + max(0.0, v.get("retrievalGb", 0) - OBJ_IA_RETRIEVAL_FREE_GB)
                     * OBJ_IA_RETRIEVAL_RATE
                     + max(0.0, v.get("requests", 0) - OBJ_REQUEST_FREE_UNITS)
                     * OBJ_REQUEST_RATE, 2)
    if entry["id"] == "archive":
        return round(max(0.0, v.get("gb", 0) - OBJ_STORAGE_FREE_GB) * ARCHIVE_STORAGE_RATE
                     + max(0.0, v.get("requests", 0) - OBJ_REQUEST_FREE_UNITS)
                     * OBJ_REQUEST_RATE, 2)

    # OCI Database with PostgreSQL: managed OCPU + DB-optimized storage + underlying compute
    # (per-processor OCPU/memory, x nodes) + block-volume performance units.
    if entry["id"] == "pg":
        ocpu = v.get("ocpu", 0)
        nodes = v.get("nodes", 1) or 1
        storage = v.get("storage", 0)
        if str(values.get("processor") or "amd").lower() == "intel":
            c_ocpu, c_mem = PG_COMPUTE_OCPU_RATE_INTEL, PG_COMPUTE_MEM_RATE_INTEL
        else:
            c_ocpu, c_mem = PG_COMPUTE_OCPU_RATE, PG_COMPUTE_MEM_RATE
        cost = (ocpu * nodes * hours * PG_MANAGED_OCPU_RATE     # managed PostgreSQL OCPU
                + storage * PG_STORAGE_RATE                     # DB-optimized storage
                + ocpu * nodes * hours * c_ocpu                 # underlying compute OCPU
                + v.get("memory", 0) * nodes * hours * c_mem    # underlying compute memory
                + storage * v.get("vpu", 0) * PG_VPU_RATE)      # block performance units
        return round(cost, 2)

    # MySQL HeatWave: ECPU + storage + backup + egress; HA triples ECPU + storage;
    # optional HeatWave cluster adds capacity + storage.
    if entry["id"] == "mysql":
        mult = 3 if str(values.get("ha") or "no").lower() == "yes" else 1
        cost = (v.get("ecpu", 0) * MYSQL_ECPU_RATE * hours * mult
                + v.get("storage", 0) * MYSQL_STORAGE_RATE * mult
                + v.get("backup", 0) * MYSQL_STORAGE_RATE
                + v.get("egress", 0) * MYSQL_STORAGE_RATE)
        if str(values.get("heatwave") or "no").lower() == "yes":
            cost += (v.get("hwcapacity", 0) * MYSQL_HW_RATE * hours
                     + v.get("hwstorage", 0) * MYSQL_HW_STORAGE_RATE)
        return round(cost, 2)

    # Oracle Integration Cloud: message packs (auto-sized) x hours x per-edition rate.
    if entry["id"] == "oic":
        edition = str(values.get("edition") or "standard").lower()
        rate = OIC_ENT_RATE if edition == "enterprise" else OIC_STD_RATE
        return round(oic_packs(values, hours) * rate * hours, 2)

    # Full Stack DR: OCPU + ECPU + OIC-pack members, summed across both protection groups.
    if entry["id"] == "fsdr":
        ocpu = v.get("p_compute", 0) + v.get("p_db_ocpu", 0) + v.get("s_compute", 0) + v.get("s_db_ocpu", 0)
        ecpu = v.get("p_db_ecpu", 0) + v.get("s_db_ecpu", 0)
        oic = v.get("p_oic", 0) + v.get("s_oic", 0)
        return round((ocpu * FSDR_OCPU_RATE + ecpu * FSDR_ECPU_RATE + oic * FSDR_OIC_RATE) * hours, 2)

    # Base Database Service: (edition + infrastructure) x ECPU x hours, plus storage per month.
    if entry["id"] == "basedb":
        ed = str(values.get("edition") or "enterprise")
        if str(values.get("metric") or "ecpu").lower() == "ocpu":
            # OCPU flavour: edition rate only (no infrastructure SKU), and storage is billed as
            # block volume - capacity plus performance units at the chosen VPU level.
            ed = str(values.get("edition_ocpu") or ed)
            # A fixed Intel shape sets the OCPU count; Flex shapes use the entered value.
            v["ecpu"] = basedb_effective_ocpu(values.get("shape"), v.get("ecpu", 0), ed)
            if str(values.get("processor") or "x86").lower() == "arm":
                rate = float(BASEDB_ARM_BY_KEY.get(ed, BASEDB_ARM_BY_KEY["enterprise"]))
                if ed == "developer":
                    f = BASEDB_DEVELOPER_FIXED
                    return round(f["ocpu"] * rate * hours
                                 + f["provisioned_gb"] * 0.0255
                                 + f["provisioned_gb"] * f["vpus"] * 0.0017, 2)
            else:
                # Developer is an ARM-ONLY edition. Falling through to .get(default) here would
                # have silently quoted it at Enterprise's $0.4301/OCPU-hr - 20x the real rate -
                # so an x86 + Developer combination is corrected to Arm rather than mispriced.
                if ed == "developer":
                    f = BASEDB_DEVELOPER_FIXED
                    return round(f["ocpu"] * BASEDB_ARM_BY_KEY["developer"] * hours
                                 + f["provisioned_gb"] * 0.0255
                                 + f["provisioned_gb"] * f["vpus"] * 0.0017, 2)
                rate = float(BASEDB_OCPU_BY_KEY.get(ed, BASEDB_OCPU_BY_KEY["enterprise"]))
            gb = basedb_provisioned_gb(values.get("shape"), values.get("storagegb"))
            vpus = v.get("vpus", 20)
            return round(v.get("ecpu", 0) * rate * hours
                         + gb * 0.0255 + gb * vpus * 0.0017, 2)
        ed_rate = float(BASEDB_EDITIONS_BY_KEY.get(ed, BASEDB_EDITIONS_BY_KEY["enterprise"]))
        ecpu = v.get("ecpu", 0)
        # Storage bills on TOTAL provisioned capacity, not the usable tier the user picks.
        total_gb = basedb_total_capacity(values.get("storagegb"))
        return round(ecpu * (ed_rate + BASEDB_INFRA_RATE) * hours
                     + total_gb * BASEDB_STORAGE_RATE, 2)

    # Flexible Load Balancer: both meters tier on quantity x HOURS, so the free allowance is a
    # monthly one (one LB, the first 10 Mbps) rather than a per-hour discount.
    if entry.get("lbMeters"):
        lb_hours = float((values.get("__hours") if values else 0) or 0) or float(hours or HOURS_PER_MONTH)
        total = 0.0
        for meter in entry["lbMeters"].values():
            qty = v.get(meter["field"], 0) * lb_hours
            if meter["field"] == "mbps":
                qty *= v.get("count", 1)      # bandwidth is per load balancer
            total += tiered_cost(qty, meter["tiers"])
        return round(total, 2)

    # Compute cards: the shape picks the SKUs, the quantities follow it. Everything is per
    # hour, on the estimator's own utilisation (744) unless the card's Hours box says otherwise.
    if entry.get("computeCard"):
        c_hours = (float((values.get("__hours") if values else 0) or 0)
                   or float(entry.get("estimatorHoursDefault") or 0)
                   or float(hours or HOURS_PER_MONTH))
        kind = entry["computeCard"]
        if kind in ("vm", "bm"):
            shape = (entry.get("shapeOptions") or {}).get(str(values.get("shape") or "")) or {}
            per_instance = (
                v.get("ocpu", 0) * float((shape.get("ocpu") or {}).get("rate") or 0)
                + v.get("memory", 0) * float((shape.get("memory") or {}).get("rate") or 0)
                + v.get("nvme", 0) * float((shape.get("nvme") or {}).get("rate") or 0))
            # Instances defaults to 1: a card left untouched should price one machine, not zero.
            count = v.get("instances", 1) if "instances" in v else 1
            return round(per_instance * c_hours * max(count, 0), 2)
        if kind == "gpu":
            gpu = (entry.get("gpuOptions") or {}).get(str(values.get("gpu") or "")) or {}
            lic = (entry.get("aieOptions") or {}).get(str(values.get("aie") or "")) or {}
            return round((v.get("gpus", 0) * float(gpu.get("rate") or 0)
                          + v.get("aie_gpus", 0) * float(lic.get("rate") or 0)) * c_hours, 2)
        if kind == "ocvs":
            plan = (entry.get("ocvsPlans") or {}).get(str(values.get("plan") or "")) or {}
            exp = (entry.get("ocvsExpansion") or {}).get(str(values.get("expansion") or "")) or {}
            hcx = entry.get("ocvsHcx") or {}
            # The plan's own metric decides which quantity counts - the other box is hidden.
            qty = v.get("nodes", 0) if plan.get("node") else v.get("ocpu", 0)
            return round((qty * float(plan.get("rate") or 0)
                          + v.get("expansion_ocpu", 0) * float(exp.get("rate") or 0)
                          + v.get("hcx_ocpu", 0) * float(hcx.get("rate") or 0)) * c_hours, 2)

    # Estimator-generated card: one graduated-tier meter per SKU. Hourly SKUs follow the
    # card's Hours/month box, defaulting to the estimator's own utilisation for that service
    # (31 x 24 = 744) rather than the app-wide 730, so the figure matches Oracle's estimator.
    if entry.get("skuMeters"):
        est_hours = (float((values.get("__hours") if values else 0) or 0)
                     or float(entry.get("estimatorHoursDefault") or 0)
                     or float(hours or HOURS_PER_MONTH))
        return round(sum(estimator_meter_cost(m, v.get(m["key"], 0), est_hours)
                         for m in entry["skuMeters"]), 2)

    # Combined "OCI Generative AI" card: Models section (per-request model math) + Search &
    # Retrieval section (six meters). GB-hour storage meters and dedicated clusters follow the
    # card's hours setting, which defaults to the estimator's 744 (not 730) when unset.
    if entry.get("genaiCombined"):
        # Utilization comes from the card, defaulting to the estimator's 744 hrs/mo when unset.
        g_hours = (float((values.get("__hours") if values else 0) or 0)
                   or float(GENAI_RETRIEVAL_HOURS_DEFAULT))
        total = 0.0
        if str(values.get("metric") or "on_demand").lower() == "dedicated":
            drates = (entry.get("genaiDedicated") or {}).get("rates") or {}
            drate = float(drates.get(str(values.get("ded_cluster") or ""), 0) or 0)
            total += v.get("ded_units", 0) * drate * g_hours
        else:
            mi = (entry.get("genaiModelInfo") or {}).get(str(values.get("model") or ""))
            if mi:
                div = float(mi.get("divisor") or 1) or 1
                total += v.get("requests", 0) * (
                    v.get("prompt_len", 0) / div * float(mi.get("inRate") or 0)
                    + v.get("response_len", 0) / div * float(mi.get("outRate") or 0))
        for r in entry.get("genaiRetrieval") or []:
            q = v.get(r["key"], 0)
            total += (q * float(r["rate"]) * g_hours if r.get("hourly")
                      else q / float(r.get("divisor") or 1) * float(r["rate"]))
        return round(total, 2)

    # OCI Generative AI Agents (separate card): RAG transactions + optional Managed KB.
    if entry.get("genaiAgents"):
        g_hours = (float((values.get("__hours") if values else 0) or 0)
                   or float(GENAI_RETRIEVAL_HOURS_DEFAULT))
        m = entry.get("genaiAgentMeters") or {}
        txn, kb, ing = m.get("txn", {}), m.get("kb", {}), m.get("ingest", {})
        total = (v.get("rag_requests", 0) * v.get("rag_chars", 0)
                 / float(txn.get("divisor") or 1) * float(txn.get("rate") or 0)
                 + v.get("kb_storage", 0) * float(kb.get("rate") or 0) * g_hours
                 + v.get("kb_jobs", 0) * v.get("kb_chars", 0)
                 / float(ing.get("divisor") or 1) * float(ing.get("rate") or 0))
        return round(total, 2)

    # Variant-priced entries (Generative AI): one card, a dropdown of published meters, each
    # with its own rate. The rate comes from the entry's own table - never from the client - and
    # a meter flagged hourly (GB-hour storage, AI-unit-hour clusters) multiplies by hours.
    if entry.get("variantRates"):
        key = str(values.get(entry.get("variantKey")) or "")
        rates = entry["variantRates"]
        rate = float(rates.get(key, entry.get("rate") or 0))
        qty = v.get(entry.get("variantField") or "units", 0)
        # Tier-priced meters carry a free allowance in the SAME unit as the quantity, so the
        # subtraction happens before the rate is applied - never after, which would refund the
        # allowance at the paid rate.
        qty = max(0.0, qty - float((entry.get("variantFree") or {}).get(key, 0) or 0))
        hourly = bool((entry.get("variantHourly") or {}).get(key))
        return round(qty * rate * (hours if hourly else 1), 2)

    if entry["id"] == "fastconnect":
        speed = str(values.get("speed") or "10G").upper()
        speed_rate = FASTCONNECT_SPEED_RATES.get(speed, FASTCONNECT_SPEED_RATES["10G"])
        return round(v.get("ports", 0) * speed_rate * hours, 2)

    # Block volume: capacity + performance units, two SKUs.
    if entry["id"] == "block":
        gb, vpus = v.get("gb", 0), v.get("vpus", 10)
        store = gb * _svc_rate("OCI Block Volumes", fallback=0.0255)
        perf = gb * vpus * (_SVC.get("OCI Block Volumes", {}).get("perfUnitsRate") or 0.0017)
        return round(store + perf, 2)

    # Billed quantity = the single sizing field (all remaining curated entries are single-field),
    # minus any free allowance on that field.
    fkey = entry["fields"][0]["key"] if entry["fields"] else None
    qty = v.get(fkey, 0) if fkey else 0
    if fkey in free:
        qty = max(0.0, qty - free[fkey])

    if basis == "hour":
        return round(rate * qty * hours, 2)
    return round(rate * qty, 2)          # month / op


# --- search -----------------------------------------------------------------------------------
def _raw_matches(q, limit=25):
    """Full-text fallback over the whole price list for anything not curated."""
    qn = _norm(q)
    if not qn:
        return []
    terms = _expand(qn)
    out = []
    for sku, it in _PRICES.items():
        rate = it.get("payg")
        if not isinstance(rate, (int, float)) or rate <= 0:
            continue
        # serviceCategory comes from Oracle's own catalog and is often how people search -
        # "Document Understanding" or "Queue" appears there when the desc says "OCI - ... - OCR".
        hay = _norm(f"{it.get('desc','')} {it.get('metric','')} {sku} {it.get('serviceCategory','')}")
        # Word-prefix, not raw substring: a search for "vision" must not match "provisioned".
        hay_words = hay.split()
        if all(any(w == t or w.startswith(t) for w in hay_words) for t in terms):
            metric = (it.get("metric") or "").strip()
            ml = metric.lower()
            # Rates quoted per OCPU/ECPU/GPU/vCPU-hour bill hourly; the rest per month.
            basis = "hour" if ("per hour" in ml or "ecpu" in ml or "ocpu" in ml
                               or "gpu per" in ml or "core per hour" in ml) else "month"
            unit = re.sub(r"\s+", " ", metric)[:40] or "unit"
            third = any(t in hay for t in _THIRD_PARTY_TERMS)
            out.append({
                "id": f"raw:{sku}", "group": "Licensing" if third else "Other Services",
                "name": re.sub(r"\s+", " ", it.get("desc", sku))[:70],
                "sku": sku, "rate": float(rate), "unit": unit, "basis": basis,
                "fields": [_sf("qty", "Quantity", unit, 1, 1)],
                "note": "Raw price-list SKU.", "source": "raw", "thirdParty": third,
            })
    out.sort(key=lambda e: len(e["name"]))
    return out[:limit]


def line_breakdown(entry, values, hours=HOURS_PER_MONTH):
    """Per-SKU line items for a filled-in catalog entry - the full paper trail (like the OCI
    estimator's 'Pricing Details'). Each item: {sku, desc, qty, rate, hours, monthly}. The
    sum of the items equals line_cost(entry, values, hours)."""
    hours = float((values.get("__hours") if values else 0) or 0) or float(hours or HOURS_PER_MONTH)
    v = {}
    for f in entry.get("fields", []):
        try:
            v[f["key"]] = float(values.get(f["key"], f.get("default", 0)) or 0)
        except (TypeError, ValueError):
            pass
    cid = entry["id"]
    out = []

    def li(sku, desc, qty, rate, hourly=False, monthly=None):
        m = monthly if monthly is not None else round(qty * rate * (hours if hourly else 1), 2)
        out.append({"sku": sku, "desc": desc, "qty": round(qty, 4), "rate": rate,
                    "hours": hours if hourly else "", "monthly": round(m, 2)})

    if cid == "mysql":
        mult = 3 if str(values.get("ha") or "no").lower() == "yes" else 1
        li("B108030", "MySQL Database - ECPU", v.get("ecpu", 0) * mult, MYSQL_ECPU_RATE, True)
        li("B92426", "MySQL Database - Storage", v.get("storage", 0) * mult, MYSQL_STORAGE_RATE)
        li("B92483", "MySQL Database - Backup Storage", v.get("backup", 0), MYSQL_STORAGE_RATE)
        li("B109169", "MySQL - Outbound Data Transfer (Inter-OCI)", v.get("egress", 0), MYSQL_STORAGE_RATE)
        if str(values.get("heatwave") or "no").lower() == "yes":
            li("B96626", "OCI HeatWave", v.get("hwcapacity", 0), MYSQL_HW_RATE, True)
            li("B96625", "OCI HeatWave - Storage", v.get("hwstorage", 0), MYSQL_HW_STORAGE_RATE)
    elif cid == "pg":
        ocpu, nodes = v.get("ocpu", 0), (v.get("nodes", 1) or 1)
        storage = v.get("storage", 0)
        intel = str(values.get("processor") or "amd").lower() == "intel"
        c_ocpu, c_mem = (PG_COMPUTE_OCPU_RATE_INTEL, PG_COMPUTE_MEM_RATE_INTEL) if intel else (PG_COMPUTE_OCPU_RATE, PG_COMPUTE_MEM_RATE)
        li("B99060", "Database with PostgreSQL - OCPU", ocpu * nodes, PG_MANAGED_OCPU_RATE, True)
        li("B99062", "Database Optimized Storage", storage, PG_STORAGE_RATE)
        li("B97384", "Compute - Standard - OCPU", ocpu * nodes, c_ocpu, True)
        li("B97385", "Compute - Standard - Memory", v.get("memory", 0) * nodes, c_mem, True)
        li("B91962", "Block Volume - Performance Units", storage * v.get("vpu", 0), PG_VPU_RATE)
    elif cid == "adb":
        workload = str(values.get("workload") or "atp").lower()
        packs_ecpu = v.get("ecpu", 0)
        if str(values.get("deployment") or "serverless").lower() == "dedicated":
            li("B95712" if workload == "adw" else "B95713", "Autonomous DB - Dedicated ECPU", packs_ecpu, ADB_ECPU_RATE, True)
            li("B112666", "Exadata Cloud Infrastructure - Database Server", v.get("dbservers", 0), ADB_EXA_DB_SERVER, True)
            li("B112667", "Exadata Cloud Infrastructure - Storage Server", v.get("storageservers", 0), ADB_EXA_STORAGE_SERVER, True)
            li("B91628", "Object Storage - Backup", max(0.0, v.get("bakgb", 0) - ADB_OBJ_BACKUP_FREE), ADB_OBJ_BACKUP)
        else:
            li("B95701" if workload == "adw" else "B95702", "Autonomous DB - ECPU", packs_ecpu, ADB_ECPU_RATE, True)
            li("B95754" if workload == "adw" else "B95706", "Autonomous DB - Storage", v.get("dbgb", 0), ADB_STORAGE_ADW if workload == "adw" else ADB_STORAGE_ATP)
            li("B95754", "Autonomous DB - Backup Storage", v.get("bakgb", 0), ADB_BACKUP_RATE)
    elif cid == "oic":
        edition = str(values.get("edition") or "standard").lower()
        rate = OIC_ENT_RATE if edition == "enterprise" else OIC_STD_RATE
        li("B89639", "Oracle Integration Cloud - " + ("Enterprise" if edition == "enterprise" else "Standard"), oic_packs(values, hours), rate, True)
    elif cid == "fsdr":
        ocpu = v.get("p_compute", 0) + v.get("p_db_ocpu", 0) + v.get("s_compute", 0) + v.get("s_db_ocpu", 0)
        ecpu = v.get("p_db_ecpu", 0) + v.get("s_db_ecpu", 0)
        oic = v.get("p_oic", 0) + v.get("s_oic", 0)
        li("B95485", "Full Stack DR - Compute + DB Member OCPUs", ocpu, FSDR_OCPU_RATE, True)
        li("B110274", "Full Stack DR - Database Member ECPUs", ecpu, FSDR_ECPU_RATE, True)
        li("B112110", "Full Stack DR - OIC Message Packs", oic, FSDR_OIC_RATE, True)
    elif cid == "fastconnect":
        speed = str(values.get("speed") or "10G").upper()
        speed = speed if speed in FASTCONNECT_SPEED_RATES else "10G"
        li(FASTCONNECT_SPEED_SKUS[speed],
           f"FastConnect {FASTCONNECT_SPEED_LABELS[speed]} port",
           v.get("ports", 0), FASTCONNECT_SPEED_RATES[speed], True)
    elif cid == "object":
        li("B91628", "Object Storage - Storage", max(0.0, v.get("gb", 0) - OBJ_STORAGE_FREE_GB), OBJ_STORAGE_RATE)
        li("B91627", "Object Storage - Requests", max(0.0, v.get("requests", 0) - OBJ_REQUEST_FREE_UNITS), OBJ_REQUEST_RATE)
    elif cid == "object_ia":
        li("B93000", "Object Storage - Infrequent Access Storage",
           max(0.0, v.get("gb", 0) - OBJ_STORAGE_FREE_GB), OBJ_IA_STORAGE_RATE)
        li("B93001", "Object Storage - Infrequent Access Retrieval",
           max(0.0, v.get("retrievalGb", 0) - OBJ_IA_RETRIEVAL_FREE_GB),
           OBJ_IA_RETRIEVAL_RATE)
        li("B91627", "Object Storage - Requests",
           max(0.0, v.get("requests", 0) - OBJ_REQUEST_FREE_UNITS), OBJ_REQUEST_RATE)
    elif cid == "archive":
        li("B91633", "Object Storage - Archive Storage",
           max(0.0, v.get("gb", 0) - OBJ_STORAGE_FREE_GB), ARCHIVE_STORAGE_RATE)
        li("B91627", "Object Storage - Requests",
           max(0.0, v.get("requests", 0) - OBJ_REQUEST_FREE_UNITS), OBJ_REQUEST_RATE)
    elif cid == "waf":
        li("B94579", "Web Application Firewall - Instance", max(0.0, v.get("instances", 0) - WAF_INSTANCE_FREE), WAF_INSTANCE_RATE)
        li("B94277", "Web Application Firewall - Requests", max(0.0, v.get("requests", 0) - WAF_REQUEST_FREE), WAF_REQUEST_RATE)
    elif cid == "kms":
        li("B90328", "Key Management - Private Vault", v.get("vaults", 0), KMS_VAULT_RATE, True)
        li("B92092", "Key Management - Key Versions (free)", v.get("keyversions", 0), 0.0)
        li("B98100", "External Key Management", v.get("external", 0), KMS_EXTERNAL_RATE)
        li("B99597", "Dedicated Key Management - HSM Partition", v.get("hsm", 0), KMS_HSM_RATE, True)
    elif cid == "desktops":
        n = v.get("desktops", 0)
        li("B95518", "Secure Desktop", n, DESKTOP_UNIT_RATE)
        if str(values.get("os") or "linux").lower() == "win_dvh":
            hosts = max(1, math.ceil(n * v.get("ocpu", 0) / DVH_AVAIL_OCPU)) if v.get("ocpu", 0) else 1
            li("B93113", "Compute E4 (DVH) - OCPU", DVH_HOST_OCPU * hosts, DESKTOP_E4_OCPU_RATE, True)
            li("B93114", "Compute E4 (DVH) - Memory", DVH_HOST_MEM * hosts, DESKTOP_E4_MEM_RATE, True)
            li("B91961", "Boot Volume - Storage", v.get("bootgb", 0) * hosts, DESKTOP_BLOCK_RATE)
            li("B91962", "Boot Volume - Performance Units", v.get("bootgb", 0) * v.get("bootvpu", 0) * hosts, DESKTOP_VPU_RATE)
        else:
            li("B111129", "Compute E6 - OCPU", v.get("ocpu", 0) * n, DESKTOP_OCPU_RATE, True)
            li("B111130", "Compute E6 - Memory", v.get("memory", 0) * n, DESKTOP_MEM_RATE, True)
            li("B91961", "Boot Volume - Storage", v.get("bootgb", 0) * n, DESKTOP_BLOCK_RATE)
            li("B91962", "Boot Volume - Performance Units", v.get("bootgb", 0) * v.get("bootvpu", 0) * n, DESKTOP_VPU_RATE)
        if v.get("optgb", 0):
            li("B91961", "Optional Block Storage - Storage", v.get("optgb", 0) * n, DESKTOP_BLOCK_RATE)
            li("B91962", "Optional Block Storage - Performance Units", v.get("optgb", 0) * v.get("optvpu", 0) * n, DESKTOP_VPU_RATE)
    elif cid == "sqllic":
        edition = str(values.get("edition") or "enterprise").lower()
        sku = {"enterprise": "B91372", "standard": "B91373", "express": "SQL-EXPRESS"}.get(edition, "B91372")
        rate = {"enterprise": SQL_ENT_RATE, "standard": SQL_STD_RATE}.get(edition, 0.0)
        li(sku, "Microsoft SQL " + edition.title() + " (license-included)", v.get("ocpu", 0), rate, True)
    elif cid == "block":
        li("B91961", "Block Volume - Storage", v.get("gb", 0), _svc_rate("OCI Block Volumes", fallback=0.0255))
        li("B91962", "Block Volume - Performance Units", v.get("gb", 0) * v.get("vpus", 10),
           _SVC.get("OCI Block Volumes", {}).get("perfUnitsRate") or 0.0017)
    elif entry.get("lbMeters"):
        for meter in entry["lbMeters"].values():
            qty = v.get(meter["field"], 0) * hours
            if meter["field"] == "mbps":
                qty *= v.get("count", 1)
            if not qty:
                continue
            gross = tiered_cost(qty, meter["tiers"])
            li(meter["sku"], meter["label"], qty, round(gross / qty, 8) if qty else 0,
               False, monthly=gross)

    elif entry.get("computeCard"):
        hours = (float((values.get("__hours") if values else 0) or 0)
                 or float(entry.get("estimatorHoursDefault") or 0)
                 or float(hours or HOURS_PER_MONTH))
        kind = entry["computeCard"]
        if kind in ("vm", "bm"):
            shape = (entry.get("shapeOptions") or {}).get(str(values.get("shape") or "")) or {}
            count = v.get("instances", 1) if "instances" in v else 1
            label = shape.get("label") or "Compute"
            # Quantities are per instance on the card; the BOM line is the whole fleet.
            for key, part, unit in (("ocpu", "ocpu", "OCPU"),
                                    ("memory", "memory", "Memory"),
                                    ("nvme", "nvme", "NVMe")):
                meter = shape.get(part)
                qty = v.get(key, 0) * max(count, 0)
                if meter and qty:
                    li(meter["sku"], f"Compute - {label} - {unit}", qty, float(meter["rate"]), True)
        elif kind == "ocvs":
            plan = (entry.get("ocvsPlans") or {}).get(str(values.get("plan") or "")) or {}
            exp = (entry.get("ocvsExpansion") or {}).get(str(values.get("expansion") or "")) or {}
            hcx = entry.get("ocvsHcx") or {}
            qty = v.get("nodes", 0) if plan.get("node") else v.get("ocpu", 0)
            if plan and qty:
                li(plan["sku"], "OCVS - " + plan["label"], qty, float(plan["rate"]), True)
            if exp and v.get("expansion_ocpu", 0):
                li(exp["sku"], "OCVS - " + exp["label"], v.get("expansion_ocpu", 0),
                   float(exp["rate"]), True)
            if hcx and v.get("hcx_ocpu", 0):
                li(hcx["sku"], "OCVS - " + hcx["label"], v.get("hcx_ocpu", 0),
                   float(hcx["rate"]), True)
        elif kind == "gpu":
            gpu = (entry.get("gpuOptions") or {}).get(str(values.get("gpu") or "")) or {}
            lic = (entry.get("aieOptions") or {}).get(str(values.get("aie") or "")) or {}
            if gpu and v.get("gpus", 0):
                li(gpu["sku"], f"Compute - {gpu['label']}", v.get("gpus", 0),
                   float(gpu["rate"]), True)
            if lic and v.get("aie_gpus", 0):
                li(lic["sku"], lic["label"], v.get("aie_gpus", 0), float(lic["rate"]), True)

    elif entry.get("skuMeters"):
        # One SKU line per filled meter, at the tier-blended effective rate so the paper trail
        # reconciles: qty x effective rate (x hours) == what line_cost charged.
        hours = (float((values.get("__hours") if values else 0) or 0)
                 or float(entry.get("estimatorHoursDefault") or 0)
                 or float(hours or HOURS_PER_MONTH))
        for m in entry["skuMeters"]:
            qty = v.get(m["key"], 0)
            if not qty:
                continue
            gross = tiered_cost(qty, m.get("tiers") or [])
            # A free allowance makes the effective rate lower than the headline rate. Show the
            # blended rate for readability, but pass the exact monthly so the breakdown always
            # sums to line_cost - rounding the rate alone drifts on large quantities.
            eff = (gross / qty) if qty else 0.0
            monthly = gross * hours if m.get("hourly") else gross
            li(m["sku"], m["label"], qty, round(eff, 6), bool(m.get("hourly")),
               monthly=monthly)

    elif cid == "genai":
        # Combined GenAI card -> one SKU line per meter. Models: input + output token/char
        # lines (billable units = requests x length / divisor). Search & Retrieval: each of the
        # six meters. Reassign `hours` so the hourly (GB-hour) lines use the card's setting
        # (744 default) that li() reads via closure.
        hours = (float((values.get("__hours") if values else 0) or 0)
                 or float(GENAI_RETRIEVAL_HOURS_DEFAULT))
        if str(values.get("metric") or "on_demand").lower() == "dedicated":
            dk = str(values.get("ded_cluster") or "")
            ded = entry.get("genaiDedicated") or {}
            li((ded.get("skus") or {}).get(dk) or "",
               "Generative AI - " + ((ded.get("labels") or {}).get(dk) or "Dedicated cluster"),
               v.get("ded_units", 0), float((ded.get("rates") or {}).get(dk, 0) or 0), True)
        else:
            mi = (entry.get("genaiModelInfo") or {}).get(str(values.get("model") or ""))
            if mi:
                div = float(mi.get("divisor") or 1) or 1
                req = v.get("requests", 0)
                li(mi.get("inSku") or "", f"Generative AI - {mi['label']} - Input",
                   req * v.get("prompt_len", 0) / div, float(mi.get("inRate") or 0))
                if float(mi.get("outRate") or 0) > 0:
                    li(mi.get("outSku") or "", f"Generative AI - {mi['label']} - Output",
                       req * v.get("response_len", 0) / div, float(mi.get("outRate") or 0))
        for r in entry.get("genaiRetrieval") or []:
            q = v.get(r["key"], 0)
            if r.get("hourly"):
                li(r["sku"], "Generative AI - " + r["label"], q, float(r["rate"]), True)
            else:
                li(r["sku"], "Generative AI - " + r["label"],
                   q / float(r.get("divisor") or 1), float(r["rate"]))
    elif cid == "genai_agents":
        hours = (float((values.get("__hours") if values else 0) or 0)
                 or float(GENAI_RETRIEVAL_HOURS_DEFAULT))
        m = entry.get("genaiAgentMeters") or {}
        txn, kb, ing = m.get("txn", {}), m.get("kb", {}), m.get("ingest", {})
        li(txn.get("sku") or "", "Generative AI Agents - RAG transactions",
           v.get("rag_requests", 0) * v.get("rag_chars", 0) / float(txn.get("divisor") or 1),
           float(txn.get("rate") or 0))
        li(kb.get("sku") or "", "Generative AI Agents - Knowledge Base Storage",
           v.get("kb_storage", 0), float(kb.get("rate") or 0), True)
        li(ing.get("sku") or "", "Generative AI Agents - Data Ingestion",
           v.get("kb_jobs", 0) * v.get("kb_chars", 0) / float(ing.get("divisor") or 1),
           float(ing.get("rate") or 0))
    else:
        # Single-SKU entry: one line at its own rate.
        fkey = next((f["key"] for f in entry.get("fields", []) if not f.get("options")), None)
        qty = v.get(fkey, 0) if fkey else 0
        free = (entry.get("free") or {}).get(fkey, 0) if fkey else 0
        li(entry["sku"], entry["name"], max(0.0, qty - free), float(entry.get("rate") or 0),
           entry.get("basis") == "hour")
    return [it for it in out if it["monthly"] or it["rate"] == 0]


# Shorthand people actually type -> the words that appear in the catalog. Applied token by
# token before ranking, so "genai" finds "OCI Generative AI" and "oke" finds Kubernetes Engine.
SEARCH_ALIASES = {
    "genai": "generative ai", "genais": "generative ai", "llm": "generative ai",
    "ai": "ai", "oke": "kubernetes engine", "k8s": "kubernetes engine",
    "adb": "autonomous ai database", "atp": "autonomous ai database",
    "adw": "autonomous ai database", "autonomous": "autonomous",
    "oss": "object storage", "fss": "file storage", "bv": "block volume",
    "lb": "load balancer", "nlb": "network load balancer", "flb": "flexible load balancer",
    "vcn": "virtual cloud network", "fc": "fastconnect", "dt": "data transfer",
    "egress": "outbound data transfer", "waf": "web application firewall",
    "kms": "key management vault", "vault": "key management vault",
    "oic": "application integration", "odi": "data integrator",
    "pg": "postgresql", "postgres": "postgresql", "mds": "mysql",
    "dbcs": "base database service", "exa": "exadata", "exacs": "exadata",
    "vm": "virtual machine", "bm": "bare metal", "ocr": "document understanding",
    "dr": "disaster recovery", "fsdr": "full stack disaster recovery",
    "iam": "identity access management", "rag": "generative ai agents",
}


def _expand(q):
    """Rewrite shorthand tokens in a normalised query into catalog vocabulary."""
    out = []
    for t in q.split():
        out.extend(SEARCH_ALIASES.get(t, t).split())
    return out


# Relevance tiers for search(). Lower sorts first: the service NAME is what people type, so
# every name-based tier outranks a SKU, group, or note hit. Note text is matched on word
# prefixes rather than raw substrings - otherwise "vision" matches FastConnect's "provisioned".
def _match_rank(e, terms, qn):
    """Relevance tier for one catalog entry, or None when it does not match at all."""
    name = _norm(e.get("name"))
    name_words = name.split()
    sku = _norm(e.get("sku"))
    group_words = _norm(e.get("group")).split()
    note_words = _norm(e.get("note")).split()

    def words_hit(words, exact=False):
        return all(any(w == t or (not exact and w.startswith(t)) for w in words) for t in terms)

    if name == qn:
        return 0                                   # exact service name
    if name.startswith(qn):
        return 1                                   # name starts with the query
    if words_hit(name_words, exact=True):
        return 2                                   # every term is a whole word in the name
    if words_hit(name_words):
        return 3                                   # every term prefixes a word in the name
    if all(t in name for t in terms):
        return 4                                   # substring anywhere in the name
    if all(t in sku for t in terms):
        return 5                                   # SKU / part number
    if words_hit(group_words):
        return 6                                   # category name
    if words_hit(note_words):
        return 7                                   # description text (word-prefix only)
    return None


def search(query="", group=""):
    """Return catalog entries matching a text query and/or a category group, ranked so that
    service-name matches come first and description-text matches last."""
    q, g = _norm(query), (group or "").strip()
    pool = [e for e in CURATED if not g or e["group"] == g]
    if not q:
        return list(pool)
    terms = _expand(q)
    scored = []
    for e in pool:
        rank = _match_rank(e, terms, q)
        if rank is not None:
            scored.append((rank, len(e["name"]), e["name"], e))
    scored.sort(key=lambda t: t[:3])
    results = [t[3] for t in scored]
    # Only reach into the raw list on an explicit text search, so browsing a category stays
    # clean. Raw SKUs always rank below curated services.
    if not g:
        seen = {e["sku"] for e in results}
        results += [r for r in _raw_matches(query) if r["sku"] not in seen]
    return results


def _entry_by_id(cid):
    for e in CURATED:
        if e["id"] == cid:
            return e
    return None


def price_extras(extra_services, hours=HOURS_PER_MONTH):
    """Re-price the services the user added, authoritatively, from the catalog - never
    trusting the client's number. `hours` is the app's hours-per-month setting so per-hour
    services follow it. Returns a clean list the exporter can consume:
        [{name, group, sku, unit, monthly, sizing}]  plus a total.
    """
    # Add-ins default to 730 hours/month regardless of the app-wide hours setting; each SKU
    # can override its own hours via a "__hours" value on the client record.
    default_hours = float(HOURS_PER_MONTH)
    out, total = [], 0.0
    for s in (extra_services or []):
        cid = s.get("catalogId") or s.get("id")
        entry = _entry_by_id(cid)
        values = s.get("values") or {}
        svc_hours = float((values.get("__hours") if values else 0) or 0) or default_hours
        if entry:
            monthly = line_cost(entry, values, default_hours)
            name, group, sku, unit = entry["name"], entry["group"], entry["sku"], entry["unit"]
            fields = entry["fields"]
            third = bool(entry.get("thirdParty"))
            rate = float(entry.get("rate") or 0)
            basis = entry.get("basis", "month")
            architecture_icon = entry["architectureIcon"]
            architecture_resolution = entry["architectureResolution"]
            architecture_service_group = group
            if cid == "fastconnect":
                speed = str(values.get("speed") or "10G").upper()
                speed = speed if speed in FASTCONNECT_SPEED_RATES else "10G"
                name = f"FastConnect port ({FASTCONNECT_SPEED_LABELS[speed]})"
                sku = FASTCONNECT_SPEED_SKUS[speed]
                rate = FASTCONNECT_SPEED_RATES[speed]
            elif entry.get("variantRates"):
                # Name the BOM line after the meter the user actually chose - "Generative AI -
                # On-Demand Inference" on its own says nothing on a customer deliverable.
                vkey = str(values.get(entry.get("variantKey")) or "")
                if vkey in entry["variantRates"]:
                    name = f"{entry['name'].split(' - ')[0]} - {entry['variantLabels'][vkey]}"
                    sku = entry["variantSkus"].get(vkey) or ""
                    rate = float(entry["variantRates"][vkey])
                    unit = entry["variantUnits"].get(vkey) or unit
        else:
            # A raw price-list SKU (raw:<sku>): basis carried on the client record.
            rate = float(s.get("rate") or 0)
            basis = s.get("basis", "month")
            qraw = float((values.get("qty") if values else 0) or 0)
            monthly = round(rate * qraw * (svc_hours if basis == "hour" else 1), 2)
            name = s.get("name", cid or "Service")
            group = s.get("group", "Other Services")
            sku = s.get("sku", "")
            unit = s.get("unit", "unit")
            fields = s.get("fields") or []
            third = bool(s.get("thirdParty")) or group == "Licensing"
            architecture_icon, architecture_resolution = architecture_mapping(name, group)
            architecture_service_group = architecture_group(name, group)
        # Primary billed quantity for display. OIC shows the auto-sized message packs.
        if cid == "oic":
            qty = oic_packs(values, svc_hours)
        else:
            num_fields = [f for f in fields if not f.get("options")]
            fkey = num_fields[0]["key"] if num_fields else None
            qty = (float(values.get(fkey, num_fields[0].get("default", 0)) or 0)
                   if fkey else 0)
        hours_used = svc_hours if basis == "hour" else ""
        # Keep the editable hours out of the sizing string (it has its own column). A card with
        # many meters - the generated estimator cards, Generative AI - is mostly zeros on any
        # real BOM, so list only what the user actually filled in; an all-zero card still shows
        # its first field rather than an empty cell.
        def _sizing_bits(only_filled):
            bits = []
            for f in fields:
                if f.get("key") == "__hours":
                    continue
                raw = values.get(f["key"], f.get("default", 0))
                try:
                    filled = float(raw or 0) != 0
                except (TypeError, ValueError):
                    filled = bool(raw)          # dropdowns carry a string value
                if only_filled and not filled:
                    continue
                label = f.get("unit") or f.get("label") or ""
                bits.append(f"{raw} {label}".strip())
            return bits
        sizing = " · ".join(_sizing_bits(True) or _sizing_bits(False)[:1])
        # Full per-SKU paper trail (estimator "Pricing Details"). Curated entries expand
        # into all their constituent SKUs; a raw price-list SKU stays a single line.
        if entry:
            skus = line_breakdown(entry, values, svc_hours)
        else:
            skus = [{"sku": sku, "desc": name, "qty": round(qty, 4), "rate": rate,
                     "hours": hours_used, "monthly": round(monthly, 2)}]
        if sku and not any(line.get("sku") == sku for line in skus):
            skus.insert(
                0,
                {"sku": sku, "desc": name, "qty": round(qty, 4), "rate": rate,
                 "hours": hours_used, "monthly": 0.0},
            )
        if not skus:
            skus = [{"sku": sku or "N/A", "desc": name, "qty": round(qty, 4),
                     "rate": rate, "hours": hours_used, "monthly": round(monthly, 2)}]
        out.append({"name": name, "group": group, "sku": sku, "unit": unit,
                    "monthly": round(monthly, 2), "sizing": sizing, "thirdParty": third,
                    "rate": rate, "qty": round(qty, 4), "basis": basis, "hours": hours_used,
                    "skus": skus, "architectureIcon": architecture_icon,
                    "architectureResolution": architecture_resolution,
                    "architectureGroup": architecture_service_group})
        total += monthly
    return out, round(total, 2)


# Which Pricing Overview line each catalog group rolls into (all sit inside SUM(B13:B20)).
GROUP_TO_OVERVIEW_ROW = {
    "Storage": 16, "Database": 16, "Observability": 16,
    "AI & Machine Learning": 16, "Other Services": 16, "Compute": 16, "Analytics": 16,
    "Networking": 18,
    "Security": 19,
    "Licensing": 21,        # Pricing Overview row 21 = "3rd Party Licensing"
}


def groups_with_counts():
    counts = {}
    for e in CURATED:
        counts[e["group"]] = counts.get(e["group"], 0) + 1
    return [{"group": g, "count": counts.get(g, 0)} for g in GROUPS if counts.get(g)]
