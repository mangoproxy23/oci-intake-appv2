"""Convert an *alternate* OCI BOM (a spreadsheet already expressed in OCI SKUs,
line items, quantities and pricing) into the app's pricing-result structure.

The converter recognizes every line item against the OCI SKU catalog
(data/oci_price_list.json - 617 SKUs with pay-as-you-go rates, units and product
names), re-prices recognized SKUs at the app's own known rate, recovers sizing
(OCPU / RAM / storage) from the recognized resource type, and emits the same
`pricing` dict shape that calculate_pricing produces so the frontend can load it
live into the results view.

Handles ALL SKUs / line items, not just VMs: compute, memory, block/file/object
storage, performance units, networking, licenses, databases, and anything else in
the catalog. Unrecognized lines are still carried (flagged for review) so nothing
is dropped.
"""

import json
import re
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent / "data"
HOURS_PER_MONTH = 730.0
SKU_RE = re.compile(r"\bB\d{4,6}\b", re.IGNORECASE)


def _clean(value):
    if value is None:
        return ""
    text = str(value).replace("\xa0", " ").strip()
    return "" if text.lower() in {"nan", "none", "nat"} else text


EXCEL_ERRORS = {"#value!", "#ref!", "#n/a", "#div/0!", "#name?", "#null!", "#num!", "n/a", "na"}


def _to_float(value, default=0.0):
    text = _clean(value)
    if not text:
        return default
    # Excel error text and "N/A" placeholders are NOT numbers. Without this the digit-scavenging
    # fallback below turns cells like "#DIV/0!" or a stray "M1" ramp label into a quantity.
    if text.lower() in EXCEL_ERRORS:
        return default
    text = text.replace(",", "").replace("$", "").replace("(", "-").replace(")", "").strip()
    try:
        return float(text)
    except ValueError:
        m = re.search(r"-?\d+(?:\.\d+)?", text)
        return float(m.group(0)) if m else default


# Rows that restate a total rather than adding a line. These carry a description and a number
# but no SKU, so they look exactly like a priced line - and adding them double-counts the whole
# sheet. Also catches the boilerplate the estimator appends under the table.
_SUMMARY_ROW_RE = re.compile(
    r"^\s*(?:\*\s*)?(?:"
    r"(?:estimated\s+)?(?:total|sub\s*-?\s*total|grand\s+total)\b"
    r"|(?:monthly|yearly|annual|weekly|daily|one\s+time|onetime|upfront)\s+total\b"
    r"|total\s+(?:cost|costs|monthly|annual|yearly|price|\$)"
    r"|(?:estimated\s+)?total\s+cost\s*-"
    r"|quote\s+is\s+for"
    r"|disclaimer\b"
    r"|note\s*:"
    r"|\*\s*total\s+was\s+calculated"
    r")",
    re.IGNORECASE,
)


def _is_summary_row(desc, values):
    """True when this row restates a total instead of adding a line item."""
    if _SUMMARY_ROW_RE.search(_clean(desc)):
        return True
    # The label sometimes sits in a neighbouring cell rather than the description column
    # (e.g. F52 'Yearly Total with Transition' with the figure in H52).
    for v in values:
        text = _clean(v)
        if text and _SUMMARY_ROW_RE.search(text):
            return True
    return False


def _is_blank_cell(value):
    """True when a cell carries no data. Oracle's estimator writes a single space into the
    columns of a section-header row, so ' ' has to count as blank."""
    return _clean(value) == ""


def _norm(value):
    return re.sub(r"[^a-z0-9]+", " ", _clean(value).lower()).strip()


def _load_sku_catalog():
    """SKU -> {sku, product, unit, rate} from the full OCI price list. Layered with
    the app's curated per-service rates (oci_service_prices.json) which win when a
    SKU appears in both, so recognized services use the exact rate the app prices at."""
    catalog = {}
    try:
        data = json.loads((DATA_DIR / "oci_price_list.json").read_text())
        items = data.get("items") if isinstance(data, dict) else data
    except Exception:
        items = []
    for it in items if isinstance(items, list) else []:
        sku = _clean(it.get("sku")).upper()
        if not sku:
            continue
        catalog[sku] = {
            "sku": sku,
            "product": _clean(it.get("desc")),
            "unit": _clean(it.get("metric")),
            "rate": _to_float(it.get("payg")),
        }
    # Curated rates override (Object Storage, Block Volume, Load Balancer, DBaaS, etc.).
    try:
        svc = json.loads((DATA_DIR / "oci_service_prices.json").read_text())
        for name, entry in (svc.get("services") or {}).items():
            sku = _clean(entry.get("sku")).upper()
            if not sku or not re.match(r"^B\d", sku):
                continue
            catalog.setdefault(sku, {"sku": sku, "product": name, "unit": entry.get("unit", "")})
            if entry.get("rate") is not None:
                catalog[sku]["rate"] = _to_float(entry.get("rate"))
                catalog[sku]["product"] = catalog[sku].get("product") or name
            if entry.get("unit"):
                catalog[sku]["unit"] = entry.get("unit")
            # Block Volume performance-units companion SKU.
            if entry.get("perfUnitsSku"):
                ps = _clean(entry["perfUnitsSku"]).upper()
                catalog.setdefault(ps, {"sku": ps, "product": f"{name} - Performance Units",
                                        "unit": "Performance units per gigabyte per month",
                                        "rate": _to_float(entry.get("perfUnitsRate"))})
    except Exception:
        pass
    return catalog


SKU_CATALOG = _load_sku_catalog()


# Anything whose text names a database engine is a database line, whatever it bills in.
_DATABASE_TERMS = (
    "database", "autonomous", "exadata", "mysql", "heatwave", "postgres", "nosql",
    "goldengate", "timesten", "berkeley db",
)


def classify_resource(product, unit):
    """Map a line (by its combined description + unit text) to a resource kind + OCI
    service category, used to recover sizing (OCPU/RAM/storage) and to label the row.
    Order matters: licenses, databases and memory are checked before the generic OCPU rule,
    so neither a 'Windows OS (OCPU Per Hour)' license line nor an 'Oracle Base Database
    Compute Infrastructure (ECPU Per Hour)' line is miscounted as re-mappable compute."""
    both = f"{_norm(product)} {_norm(unit)}"
    if "gpu" in both:
        return "gpu", "Compute"
    if ("windows" in both and "os" in both) or "licens" in both:
        return "license", "Licensing"
    # A database engine bills per OCPU/ECPU exactly like compute does, so it has to be claimed
    # before the processor and memory rules - but ONLY there. Leave the storage and networking
    # rules below alone: a "Database Storage (Gigabyte...)" line is still storage, and stealing
    # it from blockStorage would drop it out of totals.blockStorageGb and shrink the diagram.
    #
    # Getting this wrong is expensive and silent. A DB line typed "ocpu" is merged into a
    # re-mappable VM by _merge_server_compute, and then re-priced at plain compute rates the
    # instant a shape is chosen - which threw away the Enterprise Edition licence, 81% of the
    # line, on a real customer BOM. 115 of the 219 database SKUs in the price list reach here.
    is_db = any(k in both for k in _DATABASE_TERMS)
    is_processor = "ocpu" in both or "ecpu" in both
    # Memory: "...- Memory" OR Oracle's per-hour gigabyte metric (RAM), but NOT the
    # per-month gigabyte STORAGE metric.
    is_memory = ("memory" in both or "ram" in both
                 or ("gigabyte" in both and "per hour" in both and "storage" not in both))
    if is_db and (is_processor or is_memory):
        return "database", "Database"
    if is_processor:
        return "ocpu", "Compute"
    if is_memory:
        return "memory", "Compute"
    if "performance unit" in both or "vpu" in both:
        return "perf", "Storage"
    if "block volume" in both or ("block" in both and "storage" in both):
        return "blockStorage", "Storage"
    if "file storage" in both or "file system" in both:
        return "fileStorage", "Storage"
    if "object storage" in both or "archive storage" in both:
        return "objectStorage", "Storage"
    if "storage" in both and ("gigabyte" in both or "terabyte" in both):
        return "blockStorage", "Storage"
    if any(k in both for k in ["data transfer", "outbound", "fastconnect", "load balancer",
                               "vpn", "dns", "nat gateway", "networking"]):
        return "network", "Networking"
    # Backstop for database lines that are neither processor, memory, storage nor network -
    # infrastructure and per-instance charges, mostly. Same list as the diversion above, so a
    # service is labelled Database consistently whichever meter it happens to bill on.
    if is_db:
        return "database", "Database"
    return "other", "Other Services"


_APP_SHAPES_ADDED = False


def _augment_with_app_shapes():
    """Add the app's own flex-shape compute/memory SKUs (e.g. E6 Ax B112530/B112531)
    to the catalog so BOMs the app itself produced are fully recognized. Lazy-imported
    at call time to avoid a circular import with app.py."""
    global _APP_SHAPES_ADDED
    if _APP_SHAPES_ADDED:
        return
    _APP_SHAPES_ADDED = True
    try:
        import app
        for sh in getattr(app, "SHAPE_LOOKUP", {}).values():
            for sku_key, rate_key, kind_label, unit in (
                ("computeSku", "computeRate", "OCPU", "OCPU Per Hour"),
                ("memorySku", "memoryRate", "Memory", "Gigabyte Per Hour"),
            ):
                sku = _clean(sh.get(sku_key)).upper()
                if not sku:
                    continue
                # The app's shape table is the better source even when the raw price list
                # already has the SKU: the price list's `payg` for these rows is sometimes a
                # bundle factor (a literal 1.0) rather than a dollar rate, and its product text
                # is marketing copy ("AMD-based Available on both Bare Metal or Virtual
                # Machine."). Overwrite rather than setdefault so a converted BOM shows the
                # shape name and the rate the app would actually price at.
                SKU_CATALOG[sku] = {
                    "sku": sku,
                    "product": f"Compute {sh.get('label', '')} - {kind_label}".strip(),
                    "unit": unit,
                    "rate": _to_float(sh.get(rate_key)),
                }
    except Exception:
        pass
    # The curated add-on catalog (Autonomous DB, OIC, FastConnect port speeds, WAF, DR...) holds
    # SKUs the raw price list doesn't. Without these, a BOM full of ADB or OIC lines comes back
    # almost entirely "unrecognized" even though the app prices those services itself.
    try:
        import oci_catalog
        for entry in getattr(oci_catalog, "CURATED", []):
            for sku, product, unit, rate in _curated_sku_variants(entry):
                sku = _clean(sku).upper()
                if sku and sku not in SKU_CATALOG:
                    SKU_CATALOG[sku] = {"sku": sku, "product": product,
                                        "unit": unit, "rate": _to_float(rate)}
    except Exception:
        pass


def _curated_sku_variants(entry):
    """Every SKU a curated catalog entry can bill under - the headline SKU plus the per-option
    SKUs a card carries (FastConnect's four port speeds, for example)."""
    out = [(entry.get("sku"), entry.get("name") or "", entry.get("unit") or "", entry.get("rate"))]
    speed_skus = entry.get("speedSkus") or {}
    speed_rates = entry.get("speedRates") or {}
    speed_labels = entry.get("speedLabels") or {}
    for key, sku in speed_skus.items():
        out.append((sku,
                    f"{entry.get('name') or ''} ({speed_labels.get(key, key)})".strip(),
                    entry.get("unit") or "",
                    speed_rates.get(key)))
    return out


def recognize_sku(sku):
    return SKU_CATALOG.get(_clean(sku).upper())


# ---------------------------------------------------------------------------
# Workbook / CSV parsing
# ---------------------------------------------------------------------------
def _hidden_sheets(path):
    """Titles of hidden / very-hidden worksheets (ignored during conversion)."""
    if not str(path).lower().endswith((".xlsx", ".xlsm")):
        return set()
    try:
        import openpyxl
        wb = openpyxl.load_workbook(str(path), read_only=True)
        hidden = {ws.title for ws in wb.worksheets
                  if getattr(ws, "sheet_state", "visible") != "visible"}
        wb.close()
        return hidden
    except Exception:
        return set()


def _read_ragged_csv(path):
    """Read a CSV/TSV whose rows have DIFFERENT column counts.

    A CSV exported from Oracle's cost estimator is not a rectangle: the header block
    ("Oracle Investment Proposal...", "Currency: USD") is one field wide, the line-item table is
    seven or more, and the disclaimer at the bottom is one again. pandas.read_csv infers the
    column count from the first line and then raises "Expected 1 fields in line 6, saw 7", so the
    whole file was rejected. Parse with the csv module and pad every row to the widest instead.
    """
    import csv as _csv
    sep = "\t" if str(path).lower().endswith(".tsv") else ","
    with open(path, "r", newline="", encoding="utf-8-sig", errors="replace") as fh:
        # Sniff the delimiter so a comma-named .csv that is really semicolon-separated
        # (common from European Excel locales) still parses.
        sample = fh.read(8192)
        fh.seek(0)
        try:
            sep = _csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except Exception:
            pass
        rows = [list(r) for r in _csv.reader(fh, delimiter=sep)]
    width = max((len(r) for r in rows), default=0)
    padded = [r + [None] * (width - len(r)) for r in rows]
    return pd.DataFrame(padded, dtype=object)


def _read_sheets(path):
    path = str(path)
    if path.lower().endswith((".csv", ".tsv")):
        return {"Sheet1": _read_ragged_csv(path)}
    xl = pd.ExcelFile(path)
    hidden = _hidden_sheets(path)
    names = [n for n in xl.sheet_names if n not in hidden] or list(xl.sheet_names)
    return {name: pd.read_excel(xl, sheet_name=name, header=None, dtype=object)
            for name in names}


def _comparison_summary_columns(raw):
    """Find the paired source-cloud/OCI summary columns used by finished comparison files."""
    for row_index in range(min(18, len(raw.index))):
        cells = [_norm(value) for value in raw.iloc[row_index].tolist()]

        def find(*labels):
            return next((index for index, cell in enumerate(cells) if cell in labels), None)

        group_col = find("product group")
        offer_col = find("offer name")
        source_cost_col = find("invoice costs")
        if source_cost_col is None:
            source_cost_col = find("list costs")
        oci_cost_col = find("total discounted")
        if oci_cost_col is None:
            oci_cost_col = find("total list")
        source_service_col = next(
            (
                index
                for index, cell in enumerate(cells)
                if cell.endswith(" service") and cell != "cloud service"
            ),
            None,
        )
        if None not in (group_col, source_service_col, source_cost_col, offer_col, oci_cost_col):
            return row_index, {
                "group": group_col,
                "source_service": source_service_col,
                "source_cost": source_cost_col,
                "offer": offer_col,
                "oci_cost": oci_cost_col,
            }
    return None, {}


def _comparison_provider(raw, header_row):
    for row_index in range(max(0, header_row - 4), header_row + 1):
        for value in raw.iloc[row_index].tolist():
            normalized = _norm(value)
            if normalized == "aws":
                return "aws", "AWS"
            if normalized == "azure":
                return "azure", "Microsoft Azure"
    return "", "Source cloud"


def _convert_comparison_summary(sheets):
    """Import a finished AWS/Azure comparison as summary service rows.

    These files contain valid source-cloud and OCI totals but not the raw usage records
    required to re-price another cloud or generate workload topology. Import the numbers
    they actually contain and explicitly mark the missing architecture detail.
    """
    candidates = []
    for name, raw in sheets.items():
        header_row, columns = _comparison_summary_columns(raw)
        if not columns:
            continue
        normalized_name = _norm(name)
        if "new comparison" in normalized_name:
            priority = 4
        elif "product breakdown" in normalized_name and "ax compute" not in normalized_name:
            priority = 3
        elif "expansion commit" in normalized_name:
            priority = 2
        elif "comparison" in normalized_name:
            priority = 1
        else:
            priority = 0
        candidates.append((priority, name, raw, header_row, columns))
    if not candidates:
        return None

    _, sheet_name, raw, header_row, columns = max(candidates, key=lambda item: item[0])
    provider_key, provider_label = _comparison_provider(raw, header_row)
    rows = []
    source_monthly_total = 0.0
    oci_monthly_total = 0.0

    for row_index in range(header_row + 1, len(raw.index)):
        values = raw.iloc[row_index].tolist()

        def value(key):
            column = columns[key]
            return values[column] if column < len(values) else ""

        group = _clean(value("group"))
        source_service = _clean(value("source_service"))
        offer = _clean(value("offer"))
        summary_label = _norm(f"{source_service} {offer}")
        if "monthly" in summary_label and "cost" in summary_label:
            break
        if "annual costs" in summary_label:
            continue
        source_monthly = _to_float(value("source_cost"), 0.0)
        oci_monthly = _to_float(value("oci_cost"), 0.0)
        free_offer = _norm(value("oci_cost")) in {"free", "included", "no charge"}
        if not (group or source_service or offer):
            continue
        if not (source_monthly or oci_monthly or free_offer):
            continue

        category = group or classify_resource(offer or source_service, "")[1]
        name = offer or source_service or category
        row_id = f"comparison-{len(rows) + 1}"
        specs = {
            "applicationServers": 0.0,
            "databaseServers": 0.0,
            "vcpus": 0.0,
            "ocpus": 0.0,
            "memoryGb": 0.0,
            "blockStorageGb": 0.0,
            "fileStorageGb": 0.0,
        }
        rows.append({
            "rowId": row_id,
            "sourceRow": row_index + 1,
            "name": name[:120],
            "environment": "",
            "region": "",
            "sizeCheck": {"status": "ok"},
            "mappingFlag": "Imported comparison summary",
            "costAction": "price",
            "ociServiceCategory": category,
            "ociProduct": offer or name,
            "sourceService": source_service or provider_label,
            "sourceMonthlyCost": round(source_monthly, 4),
            "windowsLicenseMonthly": 0.0,
            "hoursPerMonth": HOURS_PER_MONTH,
            "shapeUsed": None,
            "specs": specs,
            "fullServiceMapping": {
                "sku": "",
                "ociProduct": offer or name,
                "sourceProvider": provider_label,
                "sourceService": source_service or provider_label,
                "sourceProduct": source_service,
                "sourceMonthlyCost": round(source_monthly, 4),
                "sourceCurrency": "USD",
                "quantity": 1.0,
                "unit": "monthly summary",
                "confidence": 0.8,
                "reviewRequired": False,
            },
            "lineItems": [{
                "sku": "",
                "description": offer or name,
                "quantity": 1.0,
                "unit": "monthly summary",
                "rate": round(oci_monthly, 4),
                "monthly": round(oci_monthly, 4),
                "mapping": "Imported from a finished cloud comparison workbook.",
                "ociServiceUsage": True,
            }],
            "monthly": round(oci_monthly, 4),
            "annual": round(oci_monthly * 12, 4),
            "assumptions": [
                "Imported from a finished cloud comparison workbook.",
                "Workload-level sizing was not present, so architecture generation is unavailable.",
            ],
        })
        source_monthly_total += source_monthly
        oci_monthly_total += oci_monthly

    if not rows or oci_monthly_total <= 0:
        return None

    source_monthly_total = round(source_monthly_total, 4)
    oci_monthly_total = round(oci_monthly_total, 4)
    source_card = {
        "label": provider_label,
        "monthlyTotal": source_monthly_total,
        "annualTotal": round(source_monthly_total * 12, 4),
        "priced": True,
        "basis": "imported comparison total",
    }
    cross_cloud = {
        "sourceCloud": provider_key,
        "importedComparison": True,
        "gcp": {
            "label": "Google Cloud",
            "priced": False,
            "note": "Not present in the imported comparison",
        },
    }
    if provider_key:
        cross_cloud[provider_key] = source_card

    totals = {
        "ocpus": 0.0,
        "memoryGb": 0.0,
        "blockStorageGb": 0.0,
        "fileStorageGb": 0.0,
        "cloudStorageGb": 0.0,
        "fullServiceMonthly": oci_monthly_total,
        "mappedServiceRows": len(rows),
        "unpricedServiceRows": 0,
        "oversizeRows": 0,
        "impossibleRows": 0,
        "sourceMonthlyCost": source_monthly_total,
        "mappedSourceMonthlyCost": source_monthly_total,
        "unmappedSourceMonthlyCost": 0.0,
        "monthly": oci_monthly_total,
        "annual": round(oci_monthly_total * 12, 4),
    }
    return {
        "converted": True,
        "comparisonSummary": True,
        "intakeMode": "on_prem",
        "fullServiceBeta": True,
        "auto": False,
        "engine": "imported comparison summary",
        "sheetName": sheet_name,
        "hoursPerMonth": HOURS_PER_MONTH,
        "cpuUnitResolved": "ocpu",
        "totals": totals,
        "rows": rows,
        "recognizedSkus": 0,
        "unrecognizedSkus": 0,
        "sourceCloud": provider_key,
        "crossCloud": cross_cloud,
        "diagramAvailable": False,
        "diagramUnavailableReason": (
            "This comparison workbook has pricing summaries but no workload-level "
            "CPU, memory, or storage detail for an architecture diagram."
        ),
    }


_PRICE_LIST_HINTS = ("price list", "rate card", "pricelist", "price catalog", "cpl",
                     "specs", "spec ", "shapes", "exchange", "discounts")


def _pick_bom_sheet(sheets):
    """Choose the actual BOM sheet. The sheet NAME is the most reliable signal: a sheet
    named like a BOM beats a 'price list' / 'rate card' even when the price list has far
    more SKU rows. Within a name tier, prefer the sheet with the most priced line items
    (SKU rows that also carry a quantity/cost - price lists have rates but no quantities
    of their own beyond the unit rate)."""
    best, best_key = None, None
    for name, raw in sheets.items():
        nm = _norm(name)
        priced_rows = sku_rows = 0
        for r in range(min(800, len(raw.index))):
            vals = raw.iloc[r].tolist()
            if not any(SKU_RE.search(_clean(v)) for v in vals):
                continue
            sku_rows += 1
            if any(_to_float(v, 0) > 0 for v in vals):
                priced_rows += 1
        if sku_rows <= 0:
            continue
        is_pricelist = any(h in nm for h in _PRICE_LIST_HINTS)
        is_bom = "bom" in nm and not is_pricelist
        name_rank = 2 if is_bom else (0 if is_pricelist else 1)
        key = (name_rank, priced_rows, sku_rows)
        if best_key is None or key > best_key:
            best, best_key = name, key
    return best, (best_key[2] if best_key else -1)


ALL_SHEETS = "__all__"


def _convert_all_sheets(path, sheets, sheet_options):
    """Convert every BOM sheet in the workbook and add them together.

    Used for estimator exports that split one estimate across a sheet per service type. Each
    sheet is converted by the ordinary single-sheet path (so all the per-line logic is identical),
    then the line items are concatenated with unique row ids and the totals summed."""
    parts = []
    for option in sheet_options:
        try:
            parts.append((option["sheet"], convert_oci_bom(path, sheet=option["sheet"])))
        except Exception:
            continue
    if not parts:
        raise ValueError("No OCI SKUs (e.g. B94277) were found in this file - it does not look like an OCI BOM.")
    if len(parts) == 1:
        return parts[0][1]

    base = dict(parts[0][1])
    rows, warnings = [], []
    numeric_totals = {}
    stated_total = 0.0
    recognized = unrecognized = 0
    for sheet_name, part in parts:
        for row in (part.get("rows") or []):
            row = dict(row)
            # Row ids must stay unique across sheets, and the sheet has to stay visible on the
            # line - "Compute OCPU" means little without knowing which proposal it came from.
            row["rowId"] = f"{sheet_name}::{row.get('rowId')}"
            row["sourceSheet"] = sheet_name
            if not _clean(row.get("environment")):
                row["environment"] = sheet_name
            rows.append(row)
        for key, value in (part.get("totals") or {}).items():
            if isinstance(value, (int, float)):
                numeric_totals[key] = numeric_totals.get(key, 0.0) + float(value)
        stated_total += float(part.get("statedMonthlyTotal") or 0)
        recognized += int(part.get("recognizedSkus") or 0)
        unrecognized += int(part.get("unrecognizedSkus") or 0)
        for warning in (part.get("conversionWarnings") or []):
            warnings.append(f"[{sheet_name}] {warning}")

    monthly = round(numeric_totals.get("monthly", 0.0), 4)
    numeric_totals["monthly"] = monthly
    numeric_totals["annual"] = round(monthly * 12, 4)
    base.update({
        "rows": rows,
        "totals": numeric_totals,
        "sheetName": f"All {len(parts)} BOM sheets combined",
        "sheetOptions": sheet_options,
        "combinedSheets": [name for name, _ in parts],
        "statedMonthlyTotal": round(stated_total, 4),
        "recognizedSkus": recognized,
        "unrecognizedSkus": unrecognized,
        "conversionWarnings": warnings,
    })
    return base


def _bom_sheet_options(sheets):
    """Every sheet that looks like a BOM, with enough detail for the user to choose between
    them. Real customer workbooks routinely hold several proposals side by side (an OCVS
    option, an IaaS option, a 1-year commit, a separate ERP BOM), and picking one silently
    hides the rest - so the caller gets the whole list and converts whichever is asked for."""
    options = []
    for name, raw in sheets.items():
        sku_rows = 0
        for r in range(min(800, len(raw.index))):
            vals = raw.iloc[r].tolist()
            if any(SKU_RE.search(_clean(v)) for v in vals):
                sku_rows += 1
        if sku_rows <= 0:
            continue
        options.append({
            "sheet": name,
            "skuRows": sku_rows,
            "label": _sheet_reference_label(raw),
            "statedMonthly": _stated_monthly_total(raw),
        })
    return options


def _sheet_reference_label(raw):
    """The estimator writes 'Reference label: <name>' in the first few rows - a far better
    description of what a sheet holds than the tab name someone typed."""
    for r in range(min(6, len(raw.index))):
        for v in raw.iloc[r].tolist():
            text = _clean(v)
            if text.lower().startswith("reference label:"):
                return text.split(":", 1)[1].strip()
    return ""


def _detect_currency(raw):
    """The estimator writes 'Currency: USD' (or CAD, EUR...) in the header block. A BOM priced
    in another currency must not be silently mixed with the app's USD rate card."""
    for r in range(min(8, len(raw.index))):
        for v in raw.iloc[r].tolist():
            text = _clean(v)
            if text.lower().startswith("currency:"):
                code = text.split(":", 1)[1].strip().upper()
                if re.fullmatch(r"[A-Z]{3}", code):
                    return code
    return ""


def _stated_monthly_total(raw):
    """The BOM's own 'Monthly Total' figure, so a converted sheet can be checked against what
    the source file claims rather than trusting the re-added lines blindly."""
    for r in range(len(raw.index)):
        vals = raw.iloc[r].tolist()
        labelled = any(_norm(v) in {"monthly total", "total monthly", "monthly total cost"}
                       for v in vals)
        if not labelled:
            continue
        nums = [_to_float(v, 0) for v in vals]
        best = max(nums) if nums else 0.0
        if best > 0:
            return round(best, 4)
    return 0.0


HEADER_KEYS = {
    "sku": ["prod #", "prod", "part number", "part", "sku", "product number"],
    "desc": ["description", "oracle cloud service", "cloud service", "product", "item", "service", "line item"],
    "metric": ["metric", "unit of measure", "uom"],
    "partqty": ["part qty", "quantity", "qty", "units"],
    "instqty": ["instance qty", "instances", "instance"],
    "usageqty": ["usage qty", "usage", "hrs", "hours"],
    "unitprice": ["unit cost", "unit price", "rate"],
    "monthly": ["monthly cost", "monthly", "list price", "disc price", "extended", "amount", "total cost"],
}
# Header cells that trigger the header-row detector.
_HEADER_TRIGGERS = ["part", "sku", "description", "prod", "metric", "product number"]


def _detect_columns(raw):
    """Find the header row and map logical fields to column indexes."""
    for r in range(min(25, len(raw.index))):
        cells = [_norm(v) for v in raw.iloc[r].tolist()]
        joined = " ".join(cells)
        if not (any(t in joined for t in _HEADER_TRIGGERS) and
                ("price" in joined or "cost" in joined or "qty" in joined or "quantity" in joined)):
            continue
        cols = {}
        for key, needles in HEADER_KEYS.items():
            for ci, cell in enumerate(cells):
                if cell and any(cell == n or n in cell for n in needles):
                    if key not in cols:
                        cols[key] = ci
        if ("desc" in cols or "sku" in cols) and (("unitprice" in cols) or ("monthly" in cols)):
            return r, cols
    return None, {}


def _detect_hours_per_month(raw):
    """Oracle BOMs carry an 'Hours per month' parameter cell; find it (defaults 730).

    This is only a fallback. A line that states its own Usage Qty is priced and reported on
    that figure instead - the sheet's per-row number always wins over any sheet-wide one.
    """
    for r in range(min(12, len(raw.index))):
        vals = raw.iloc[r].tolist()
        for ci, v in enumerate(vals):
            if "hours per month" in _norm(v) or "hours  per month" in _norm(v):
                for cand in (vals[ci - 1] if ci else None, vals[ci + 1] if ci + 1 < len(vals) else None):
                    n = _to_float(cand, 0)
                    if n > 0:
                        return n
    return HOURS_PER_MONTH


def _row_sku(values, sku_col):
    if sku_col is not None and sku_col < len(values):
        m = SKU_RE.search(_clean(values[sku_col]))
        if m:
            return m.group(0).upper()
    for v in values:
        m = SKU_RE.search(_clean(v))
        if m:
            return m.group(0).upper()
    return ""


def _detect_shape_for_rates(ocpu_rate, mem_rate):
    """Return the app shape_payload whose OCPU/memory rates best match the BOM's, so a
    converted compute VM shows (and re-prices against) a real OCI shape."""
    try:
        import app
        best_key, best_d = None, 1e9
        for k, sh in getattr(app, "SHAPE_LOOKUP", {}).items():
            cr = _to_float(sh.get("computeRate"))
            mr = _to_float(sh.get("memoryRate"))
            d = abs(cr - ocpu_rate) + abs(mr - mem_rate)
            if d < best_d:
                best_d, best_key = d, k
        if best_key:
            return app.shape_payload(best_key)
    except Exception:
        pass
    return None


def _make_compute_vm(comp, shape, hours):
    ocpu = round(comp["ocpu"], 4)
    mem = round(comp["mem"], 4)
    monthly = round(comp["monthly"], 4)
    label = (shape or {}).get("shortLabel") or (shape or {}).get("label") or "BOM shape"
    specs = {"applicationServers": 1.0, "databaseServers": 0.0, "vcpus": ocpu * 2,
             "ocpus": ocpu, "memoryGb": mem, "blockStorageGb": 0.0, "fileStorageGb": 0.0}
    prod = f"OCI Compute VM - {label} ({ocpu:g} OCPU / {mem:g} GB)"
    return {
        "rowId": "", "sourceRow": comp.get("sourceRow", 0), "_kind": "vm",
        "name": f"{comp['section']} - Compute VM" if comp["section"] else "Compute VM",
        "environment": comp["section"], "region": "",
        "sizeCheck": {"status": "ok"}, "mappingFlag": "", "costAction": "price",
        "ociServiceCategory": "Compute", "ociProduct": prod,
        "sourceService": comp["section"] or "OCI BOM",
        "sourceMonthlyCost": monthly, "windowsLicenseMonthly": 0.0,
        "hoursPerMonth": hours, "computeHours": hours,
        "isConvertedCompute": True,
        "originalOcpus": ocpu, "originalMemoryGb": mem,
        "shapeUsed": shape, "specs": specs,
        "fullServiceMapping": {
            "ociProduct": prod, "sourceProvider": "OCI BOM",
            "sourceService": comp["section"] or "OCI BOM",
            "sourceProduct": ", ".join(sorted({i.get("sku", "") for i in comp["items"] if i.get("sku")})),
            "sourceMonthlyCost": monthly, "sourceCurrency": "USD",
            "quantity": ocpu, "unit": "OCPU", "confidence": 0.9, "reviewRequired": False,
        },
        "lineItems": comp["items"],
        "monthly": monthly, "annual": round(monthly * 12, 4),
        "assumptions": ["Converted OCI BOM - this server's compute is grouped into a "
                        "re-mappable VM; pick a different OCI shape to re-price it."],
    }


def _merge_server_compute(rows, hours):
    """Merge each server section's OCPU + memory line rows into ONE compute VM row
    (keeps a detected shape + original sizing so the results page can re-map it).
    Non-compute rows are left untouched, in order."""
    out = []
    cur_section = None
    comp = None

    def flush():
        nonlocal comp
        if not comp:
            return
        # A re-mappable flex VM is an OCPU line AND a memory line. Anything else is not a
        # machine we can re-shape: applyShapeToVm() re-prices as ocpu*rate + memoryGb*rate,
        # so merging an OCPU-only section fabricates a "0 GB" VM whose memory half silently
        # prices at $0 the moment a shape is picked. Leave those rows exactly as they came in.
        if comp["ocpu"] > 0 and comp["mem"] > 0:
            shape = _detect_shape_for_rates(comp["ocpu_rate"], comp["mem_rate"])
            # Bill this VM on the hours its OWN lines were priced at, not the sheet-wide
            # default. Oracle's estimator prices a month as 744 hours (31 x 24) and writes
            # that in the Usage Qty column per line; falling back to the 730-hour default
            # re-priced every converted VM ~1.9% light the moment a shape was picked.
            out.append(_make_compute_vm(comp, shape,
                                        comp["ocpu_hours"] or comp["mem_hours"] or hours))
        else:
            out.extend(comp["rows"])
        comp = None

    for r in rows:
        sec = r.get("sourceService") or ""
        if sec != cur_section:
            flush()
            cur_section = sec
        if r.get("_kind") in ("ocpu", "memory"):
            if comp is None:
                comp = {"section": sec, "ocpu": 0.0, "mem": 0.0, "ocpu_rate": 0.0,
                        "mem_rate": 0.0, "ocpu_hours": 0.0, "mem_hours": 0.0,
                        "items": [], "monthly": 0.0, "rows": [],
                        "sourceRow": r.get("sourceRow", 0)}
            li = dict(r["lineItems"][0])
            line_hours = _to_float(r.get("hoursPerMonth"))
            if r["_kind"] == "ocpu":
                comp["ocpu"] += _to_float(r["specs"].get("ocpus"))
                comp["ocpu_rate"] = _to_float(li.get("rate")) or comp["ocpu_rate"]
                comp["ocpu_hours"] = line_hours or comp["ocpu_hours"]
            else:
                comp["mem"] += _to_float(r["specs"].get("memoryGb"))
                comp["mem_rate"] = _to_float(li.get("rate")) or comp["mem_rate"]
                comp["mem_hours"] = line_hours or comp["mem_hours"]
            comp["items"].append(li)
            comp["rows"].append(r)          # kept so flush() can put them back unmerged
            comp["monthly"] += _to_float(r.get("monthly"))
        else:
            out.append(r)
    flush()
    for i, r in enumerate(out, start=1):
        r["rowId"] = f"bom-{i}"
        r.pop("_kind", None)
    return out


def _looks_like_estimator_json(path):
    """True for the cost estimator's native .json export (its own save format)."""
    if not str(path).lower().endswith(".json"):
        return False
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            data = json.load(fh)
        return isinstance(data, dict) and isinstance(data.get("configs"), list)
    except Exception:
        return False


def convert_estimator_json(path):
    """Convert the cost estimator's native JSON export.

    This is cleaner than parsing the spreadsheet: the JSON carries the SKU, quantity, unit price
    and monthly cost per line as real numbers, with no merged cells, section headers or free-tier
    split rows to disentangle. Structure is configs[] -> services[] -> items[], where a config is
    a card in the estimator UI and a service is one metered product within it.

    Items with no quantity AND no cost are the preset's unused meters - the estimator lists every
    SKU a service *could* bill, not just the ones you configured - so they are skipped rather
    than emitted as a wall of $0 lines.
    """
    _augment_with_app_shapes()
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        data = json.load(fh)

    rows = []
    totals = {"ocpus": 0.0, "memoryGb": 0.0, "blockStorageGb": 0.0, "fileStorageGb": 0.0,
              "cloudStorageGb": 0.0, "monthly": 0.0, "recognized": 0, "unrecognized": 0,
              "review": 0}
    order = 0
    for config in (data.get("configs") or []):
        section = _clean(config.get("label"))
        for service in (config.get("services") or []):
            svc_label = _clean(service.get("label")) or section
            util = service.get("utilization") or {}
            hours = _to_float(util.get("hoursPerMonth"), HOURS_PER_MONTH) or HOURS_PER_MONTH
            instances = _to_float(util.get("instances"), 1) or 1.0
            for item in (service.get("items") or []):
                qty = _to_float(item.get("quantity"), 0)
                monthly = _to_float(item.get("monthlyCost"), 0)
                if not qty and not monthly:
                    continue          # a preset meter the user never configured
                sku = _clean(item.get("sku")).upper()
                rate = _to_float(item.get("unitPrice"), 0)
                rec = recognize_sku(sku) if sku else None
                product = (rec or {}).get("product") or svc_label or sku or "OCI line item"
                unit = (rec or {}).get("unit") or ""
                kind, category = classify_resource(f"{svc_label} {product}", unit)

                specs = {"applicationServers": 0.0, "databaseServers": 0.0, "vcpus": 0.0,
                         "ocpus": 0.0, "memoryGb": 0.0, "blockStorageGb": 0.0,
                         "fileStorageGb": 0.0}
                size = qty * instances
                for k, tk in (("ocpu", "ocpus"), ("memory", "memoryGb"),
                              ("blockStorage", "blockStorageGb"), ("fileStorage", "fileStorageGb"),
                              ("objectStorage", "cloudStorageGb")):
                    if kind == k:
                        if tk in specs:
                            specs[tk] = size
                        totals[tk] += size

                recognized = bool(rec)
                is_review = not recognized and monthly > 0
                totals["recognized" if recognized else "unrecognized"] += 1
                if is_review:
                    totals["review"] += 1
                totals["monthly"] += monthly
                order += 1
                name = (f"{section} · {product}" if section and section != product else product)[:120]
                rows.append({
                    "rowId": f"json-{order}", "sourceRow": order, "_kind": kind, "name": name,
                    "environment": section or "", "region": "",
                    "sizeCheck": {"status": "ok"},
                    "mappingFlag": "May not be an optimal mapping" if is_review else "",
                    "costAction": "price", "ociServiceCategory": category, "ociProduct": product,
                    "sourceService": svc_label or "OCI Cost Estimator",
                    "sourceMonthlyCost": round(monthly, 4), "windowsLicenseMonthly": 0.0,
                    "hoursPerMonth": hours, "shapeUsed": None, "specs": specs,
                    "fullServiceMapping": {
                        "sku": sku, "ociProduct": product, "sourceProvider": "OCI Cost Estimator",
                        "sourceService": svc_label, "sourceProduct": sku,
                        "sourceMonthlyCost": round(monthly, 4), "sourceCurrency":
                            _clean(data.get("currency")) or "USD",
                        "quantity": round(qty, 4), "unit": unit,
                        "confidence": 0.95 if recognized else 0.4,
                        "reviewRequired": is_review,
                    },
                    "lineItems": [{
                        "sku": sku, "description": product, "quantity": round(qty, 4),
                        "unit": unit, "rate": rate, "monthly": round(monthly, 4),
                        "mapping": (f"Recognized OCI SKU {sku}: {product}." if recognized else
                                    f"SKU {sku or '(none)'} not in the OCI catalog - carried at "
                                    f"the estimate's stated cost for review."),
                        "ociServiceUsage": recognized,
                    }],
                    "monthly": round(monthly, 4), "annual": round(monthly * 12, 4),
                    "assumptions": ["Converted from an OCI Cost Estimator JSON export."],
                })

    if not rows:
        raise ValueError("This estimator JSON has no configured line items.")

    monthly_total = round(totals["monthly"], 4)
    currency = _clean(data.get("currency")).upper() or "USD"
    warnings = []
    if currency != "USD":
        warnings.append(f"This estimate is priced in {currency}; figures are used as written.")
    if totals["unrecognized"]:
        share = totals["unrecognized"] / max(1, totals["unrecognized"] + totals["recognized"])
        if share >= 0.25:
            warnings.append(
                f"{totals['unrecognized']} of {totals['unrecognized'] + totals['recognized']} "
                f"line items use SKUs this app's catalog doesn't carry. They keep the cost the "
                f"estimate states and are flagged for review.")
    diagram_available = any(
        any(_to_float((r.get("specs") or {}).get(k), 0) > 0
            for k in ("ocpus", "memoryGb", "blockStorageGb")) for r in rows)
    return {
        "converted": True, "intakeMode": "on_prem", "fullServiceBeta": True, "auto": False,
        "engine": "local deterministic", "sheetName": _clean(data.get("label")) or "Cost Estimator",
        "sheetOptions": [], "statedMonthlyTotal": monthly_total, "sourceCurrency": currency,
        "conversionWarnings": warnings, "hoursPerMonth": HOURS_PER_MONTH,
        "cpuUnitResolved": "ocpu",
        "totals": {
            "ocpus": totals["ocpus"], "memoryGb": totals["memoryGb"],
            "blockStorageGb": totals["blockStorageGb"], "fileStorageGb": totals["fileStorageGb"],
            "cloudStorageGb": totals["cloudStorageGb"], "fullServiceMonthly": monthly_total,
            "mappedServiceRows": totals["recognized"], "unpricedServiceRows": totals["review"],
            "oversizeRows": 0, "impossibleRows": 0, "sourceMonthlyCost": monthly_total,
            "mappedSourceMonthlyCost": monthly_total, "unmappedSourceMonthlyCost": 0.0,
            "monthly": monthly_total, "annual": round(monthly_total * 12, 4),
        },
        "rows": rows, "recognizedSkus": totals["recognized"],
        "unrecognizedSkus": totals["unrecognized"], "diagramAvailable": diagram_available,
        "diagramUnavailableReason": ("" if diagram_available else
            "This estimate has pricing lines but no workload-level CPU, memory or storage "
            "detail for an architecture diagram."),
    }


def convert_oci_bom(path, sheet=None):
    if _looks_like_estimator_json(path):
        return convert_estimator_json(path)
    return _convert_oci_bom_sheets(path, sheet)


def _convert_oci_bom_sheets(path, sheet=None):
    """Parse an alternate OCI BOM and return a pricing-result dict for the app.

    `sheet` names which worksheet to convert. Workbooks holding several proposals side by side
    are normal, so when it's omitted the best-looking sheet is converted and every candidate is
    returned as `sheetOptions` for the caller to offer as a re-pick."""
    _augment_with_app_shapes()
    sheets = _read_sheets(path)
    comparison_summary = _convert_comparison_summary(sheets)
    if comparison_summary:
        return comparison_summary
    sheet_options = _bom_sheet_options(sheets)
    requested = _clean(sheet)
    # The estimator splits ONE estimate across a sheet per service type (IAAS + PAAS + SAAS), so
    # converting a single sheet silently drops half the deal. ALL_SHEETS converts every BOM sheet
    # and adds them together.
    if requested == ALL_SHEETS and len(sheet_options) > 1:
        return _convert_all_sheets(path, sheets, sheet_options)
    if requested and requested in sheets:
        sheet_name = requested
        sku_count = next((o["skuRows"] for o in sheet_options if o["sheet"] == requested), 1)
    else:
        sheet_name, sku_count = _pick_bom_sheet(sheets)
    if not sheet_name or sku_count <= 0:
        raise ValueError("No OCI SKUs (e.g. B94277) were found in this file - it does not look like an OCI BOM.")
    raw = sheets[sheet_name]
    header_row, cols = _detect_columns(raw)
    data_start = (header_row + 1) if header_row is not None else 0
    hours_per_month = _detect_hours_per_month(raw)

    rows = []
    totals = {"ocpus": 0.0, "memoryGb": 0.0, "blockStorageGb": 0.0, "fileStorageGb": 0.0,
              "cloudStorageGb": 0.0, "monthly": 0.0, "recognized": 0, "unrecognized": 0,
              "review": 0}
    section = ""
    order = 0
    # Previous line-item row, so a blank-Part tier continuation can inherit its SKU.
    prev_sku, prev_desc = "", ""

    for ri in range(data_start, len(raw.index)):
        values = raw.iloc[ri].tolist()
        if not any(_clean(v) for v in values):
            continue
        sku = _row_sku(values, cols.get("sku"))
        desc = _clean(values[cols["desc"]]) if "desc" in cols and cols["desc"] < len(values) else ""
        if not desc:
            # Fall back to the longest text cell as the description.
            texts = [_clean(v) for v in values if _clean(v) and not SKU_RE.fullmatch(_clean(v))]
            desc = max(texts, key=len) if texts else ""

        def cell(key):
            ci = cols.get(key)
            return values[ci] if ci is not None and ci < len(values) else ""

        metric = _clean(cell("metric"))
        partqty = _to_float(cell("partqty"), 0)
        instqty = _to_float(cell("instqty"), 0) or 1.0
        usageqty = _to_float(cell("usageqty"), 0)
        unitprice = _to_float(cell("unitprice"), 0)
        bom_monthly = _to_float(cell("monthly"), 0)
        free_text = _norm(cell("partqty")) in {"free", "included", "no charge"} or \
            _norm(desc) in {"free tier"} or _norm(cell("monthly")) in {"free", "included"}

        # Is there any actual data in the numeric columns? A section-header row carries a
        # description and nothing else (Oracle's estimator fills the rest with a single space).
        # This must look at the RAW cells: instqty/hours get defaulted above, so the parsed
        # values can't distinguish "blank" from "1".
        has_numbers = any(
            not _is_blank_cell(cell(k)) and _to_float(cell(k), 0) != 0
            for k in ("partqty", "usageqty", "unitprice", "monthly")
        )
        tier_continuation = False

        # A row with no recognizable SKU is a section header (e.g. a server or service-group
        # name, which becomes the grouping for the lines beneath it) UNLESS it carries real
        # numbers - hand-built BOMs routinely have priced lines whose Part cell is blank,
        # "N/A", or a service with no SKU at all. Treating those as headers silently dropped
        # their cost from the total and mislabeled every line that followed.
        if not sku:
            if _is_summary_row(desc, values):
                continue      # a restated total or boilerplate - never a line item
            if free_text and desc:
                pass          # fall through to emit a $0 free line item
            elif has_numbers:
                # The estimator splits a metered service across TIERS: the free allowance on the
                # SKU row, then the billable remainder on the next row with the Part cell blank
                # and the SAME description. That continuation row is not an unknown service - it
                # is the line above at its paid rate - so inherit the SKU instead of flagging a
                # perfectly well understood line as "not in the catalog".
                if desc and _norm(desc) == _norm(prev_desc) and prev_sku:
                    sku = prev_sku
                    tier_continuation = True
            elif desc:
                section = desc
                continue
            else:
                continue

        rec = recognize_sku(sku) if sku else None
        # Prefer the BOM's own description for the product name (it is per-line accurate
        # even when a SKU is reused for OCPU and Memory); fall back to the catalog name.
        product = desc or (rec or {}).get("product") or sku or "Unrecognized OCI line"
        cat_unit = (rec or {}).get("unit") or ""
        # Classify from the line's own metric + description first (authoritative for what
        # the line is), then the catalog product.
        kind, category = classify_resource(f"{desc} {product}", metric or cat_unit)
        unit = metric or cat_unit or ""

        # Pricing. The BOM's per-line unit price is authoritative OCI pricing. Hours
        # factor: an explicit usage-hours column wins (app BOM); otherwise a per-hour
        # metric bills x hours/month and a per-month metric x1 (Oracle BOM-tool format).
        #
        # A stated unit price of ZERO is a real price, not a missing one. Free-tier and
        # included lines (Load Balancer Base, the first 10 TB of egress, a bundled NLB) are
        # written as $0.00 by the estimator, and falling back to the catalog rate on a falsy
        # 0 billed them at list - inventing cost the source quote explicitly says is free.
        # So only reach for the catalog rate when the cell is genuinely absent.
        priced_in_source = not _is_blank_cell(cell("unitprice"))
        rate = unitprice if priced_in_source else ((rec or {}).get("rate") or 0.0)
        qty_units = (partqty * instqty) if partqty else (instqty if instqty != 1.0 else 0.0)
        metric_l = _norm(metric or cat_unit)
        if usageqty:
            hours_factor = usageqty
        elif "hour" in metric_l and "month" not in metric_l:
            # Any hourly metric ("OCPU Per Hour", "Gigabyte Per Hour", "Load Balancer
            # Hour", "Mbps Per Hour", ...) bills x hours/month; per-month metrics x1.
            hours_factor = hours_per_month
        else:
            hours_factor = 1.0
        if free_text:
            monthly = 0.0
        elif rate and partqty:
            monthly = round(partqty * instqty * hours_factor * rate, 4)
        elif bom_monthly:
            monthly = round(bom_monthly, 4)
        else:
            monthly = round(qty_units * hours_factor * rate, 4)

        # Recover sizing from the recognized resource kind.
        specs = {"applicationServers": 0.0, "databaseServers": 0.0, "vcpus": 0.0,
                 "ocpus": 0.0, "memoryGb": 0.0, "blockStorageGb": 0.0, "fileStorageGb": 0.0}
        size_units = partqty * instqty if partqty else qty_units
        if kind == "ocpu":
            specs["ocpus"] = size_units
            totals["ocpus"] += size_units
        elif kind == "memory":
            specs["memoryGb"] = size_units
            totals["memoryGb"] += size_units
        elif kind == "blockStorage":
            specs["blockStorageGb"] = size_units
            totals["blockStorageGb"] += size_units
        elif kind == "fileStorage":
            specs["fileStorageGb"] = size_units
            totals["fileStorageGb"] += size_units
        elif kind == "objectStorage":
            totals["cloudStorageGb"] += size_units

        recognized = bool(rec)
        # Free-tier / included lines are legitimate $0 items, not "needs review".
        is_review = not recognized and not free_text
        totals["recognized" if recognized else "unrecognized"] += 1
        if is_review:
            totals["review"] += 1
        totals["monthly"] += monthly
        order += 1

        line_item = {
            "sku": sku,
            "description": product,
            "quantity": round(qty_units or partqty, 4),
            "unit": unit,
            "rate": rate,
            "monthly": monthly,
            "mapping": (f"Recognized OCI SKU {sku}: {product}"
                        + (f" - re-priced at ${rate}/{unit or 'unit'}." if rate else ".")
                        if recognized else
                        f"SKU {sku or '(none)'} not found in the OCI catalog - carried at the BOM's stated cost for review."),
            "ociServiceUsage": recognized,
        }
        name = (f"{section} · {product}" if section else product)[:120]
        rows.append({
            "rowId": f"bom-{order}",
            "sourceRow": ri + 1,
            "_kind": kind,
            "name": name,
            "environment": section or "",
            "region": "",
            "sizeCheck": {"status": "ok"},
            "mappingFlag": "May not be an optimal mapping" if is_review else "",
            "costAction": "price",
            "ociServiceCategory": category,
            "ociProduct": product,
            "sourceService": section or "OCI BOM",
            "sourceMonthlyCost": round(bom_monthly, 4),
            "windowsLicenseMonthly": 0.0,
            "hoursPerMonth": usageqty or hours_per_month,
            "shapeUsed": None,
            "specs": specs,
            "fullServiceMapping": {
                "sku": sku,
                "ociProduct": product,
                "sourceProvider": "OCI BOM",
                "sourceService": section or sheet_name,
                "sourceProduct": (f"{sku} · {desc}" if sku and desc and desc != product else (sku or desc)),
                "sourceMonthlyCost": round(bom_monthly, 4),
                "sourceCurrency": "USD",
                "quantity": round(qty_units or partqty, 4),
                "unit": unit,
                "confidence": 0.95 if recognized else (0.9 if free_text else 0.4),
                "reviewRequired": is_review,
            },
            "lineItems": [line_item],
            "monthly": monthly,
            "annual": round(monthly * 12, 4),
            "assumptions": [
                "Converted from an alternate OCI BOM.",
                "Recognized SKUs are re-priced at the app's OCI catalog rate; unrecognized lines are carried at the BOM's stated cost and flagged for review.",
            ]
            + (["Billable tier of the line above (the estimator lists the free allowance and the "
                "paid remainder as separate rows sharing one SKU)."] if tier_continuation else []),
        })
        if sku and desc:
            prev_sku, prev_desc = sku, desc

    # Group each server section's compute (OCPU + memory) into ONE re-mappable VM row
    # so the results page can offer a per-server shape dropdown.
    rows = _merge_server_compute(rows, hours_per_month)

    monthly_total = round(sum(r["monthly"] for r in rows), 4)
    result_totals = {
        "ocpus": round(sum(r["specs"].get("ocpus", 0) for r in rows), 4),
        "memoryGb": round(sum(r["specs"].get("memoryGb", 0) for r in rows), 4),
        "blockStorageGb": round(sum(r["specs"].get("blockStorageGb", 0) for r in rows), 4),
        "fileStorageGb": round(sum(r["specs"].get("fileStorageGb", 0) for r in rows), 4),
        "cloudStorageGb": round(totals["cloudStorageGb"], 4),
        "fullServiceMonthly": monthly_total,
        "mappedServiceRows": totals["recognized"],
        "unpricedServiceRows": totals["review"],
        "oversizeRows": 0,
        "impossibleRows": 0,
        "sourceMonthlyCost": round(sum(r["sourceMonthlyCost"] for r in rows), 4),
        "mappedSourceMonthlyCost": round(sum(r["sourceMonthlyCost"] for r in rows), 4),
        "unmappedSourceMonthlyCost": 0.0,
        "monthly": monthly_total,
        "annual": round(monthly_total * 12, 4),
    }
    diagram_available = any(
        any(_to_float((row.get("specs") or {}).get(key), 0) > 0
            for key in ("ocpus", "memoryGb", "blockStorageGb"))
        for row in rows
    )

    # Say plainly what didn't line up. A foreign BOM is somebody else's document: it can be in
    # another currency, carry hand-added adjustment columns the converter can't know about, or
    # use SKUs this app has never seen. None of that is silently repaired - the deterministic
    # numbers stand and the discrepancy is reported so a human can judge it.
    currency = _detect_currency(raw)
    stated_total = _stated_monthly_total(raw)
    warnings = []
    if currency and currency != "USD":
        warnings.append(
            f"This BOM is priced in {currency}. The app's rate card is USD, so the converted "
            f"figures stay in {currency} as written - they are not converted."
        )
    if stated_total > 0:
        drift = monthly_total - stated_total
        if abs(drift) > max(1.0, stated_total * 0.005):
            warnings.append(
                f"This sheet states a monthly total of {stated_total:,.2f} but its line items "
                f"add up to {monthly_total:,.2f} ({drift:+,.2f}). That usually means the sheet "
                f"carries a hand-added adjustment column (a transition or ramp factor) that "
                f"isn't part of the standard Monthly Cost column. The line items are used as-is."
            )
    if totals["unrecognized"] > 0:
        share = totals["unrecognized"] / max(1, totals["unrecognized"] + totals["recognized"])
        if share >= 0.25:
            warnings.append(
                f"{totals['unrecognized']} of "
                f"{totals['unrecognized'] + totals['recognized']} line items use SKUs this app's "
                f"catalog doesn't carry. They are kept at the cost the BOM states and flagged "
                f"for review; their sizing (OCPU / RAM / storage) may be incomplete."
            )
    return {
        "converted": True,
        "intakeMode": "on_prem",
        "fullServiceBeta": True,
        "auto": False,
        "engine": "local deterministic",
        "sheetName": sheet_name,
        # Every BOM-looking sheet in the workbook, so the UI can offer a re-pick instead of
        # quietly converting one proposal out of six.
        "sheetOptions": sheet_options,
        # What the source sheet says its own monthly total is. Surfaced so a mismatch between
        # the file's figure and the sum of the lines we recognized is visible rather than hidden.
        "statedMonthlyTotal": stated_total,
        "sourceCurrency": currency,
        "conversionWarnings": warnings,
        "hoursPerMonth": HOURS_PER_MONTH,
        "cpuUnitResolved": "ocpu",
        "totals": result_totals,
        "rows": rows,
        "recognizedSkus": totals["recognized"],
        "unrecognizedSkus": totals["unrecognized"],
        "diagramAvailable": diagram_available,
        "diagramUnavailableReason": (
            "" if diagram_available else
            "This converted BOM has pricing lines but no workload-level CPU, memory, or "
            "storage detail for an architecture diagram."
        ),
    }
