"""The export must always equal what the app shows - including the user's customizations.

Three separate things have to hold, and each has broken in production at least once:

  1. STRUCTURAL   /api/price, /api/export and /api/diagram all re-price from scratch on the
                  server. Any pricing input one call sends and another omits makes the workbook
                  disagree with the screen, silently, in the money only. (`rightsize` was
                  missing from export and diagram: $117,329 on screen, $129,125 in the workbook.)

  2. ROUND-TRIP   A saved BOM is re-imported from the workflow embedded in the workbook. Every
                  pricing input therefore has to be saved by collectWorkflowState() AND restored
                  by applyWorkflowState(), or reopening a BOM re-prices it differently from the
                  file the customer already has. (hoursPerMonth/hoursOverride were saved and
                  then thrown away by a hard reset.)

  3. NUMERIC      With customizations applied, the app's headline must equal the workbook's
                  Pricing Overview total to the cent.

The structural checks read static/app.js as text on purpose: they are what stops the next
pricing input from being added to one call site and forgotten in the other two.

Usage:  python3 scripts/test_export_parity.py
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

APP_JS = (ROOT / "static" / "app.js").read_text()

# Keys applyWorkflowState() restores by hand rather than through its `assign` list. Each one is
# reconstructed explicitly (nested defaults merged, or validated), so they are legitimately
# absent from `assign` - but they must stay accounted for here so nothing slips through.
HANDLED_OUTSIDE_ASSIGN = {
    "savedAt",            # provenance, not state
    "version",            # workflow schema version
    "__workflow",         # marker the exporter writes into the hidden sheet
    "diagramOptions",     # merged against defaults, nested objects included
    "ramp",               # months/ceiling/points restored individually
    "uploadReady",        # derived from rows.length
    "convertedPricing",   # hand-restored (validated, rows re-shared) before the assign list
}

# Export-only inputs: real user customizations, but not part of calculate_pricing(), so they
# belong in the export payload rather than pricingInputs().
PRICING_STATE_KEYS = {}

EXPORT_ONLY_INPUTS = {"bomName", "ociDiscount", "extraServices", "diagramOptions",
                      "existingInfraCost", "ramp", "template", "workflowState",
                      "converted", "convertedPricing"}


def _strip_line_comments(text):
    """Drop whole-line // comments so brace counting is not thrown off by prose."""
    return "\n".join(l for l in text.splitlines() if not l.strip().startswith("//"))


def _object_after(text, anchor):
    """Inner text of the first {...} object literal at or after `anchor`."""
    i = text.index(anchor)
    j = text.index("{", i)
    depth = 0
    for k in range(j, len(text)):
        if text[k] == "{":
            depth += 1
        elif text[k] == "}":
            depth -= 1
            if depth == 0:
                return text[j + 1:k]
    raise AssertionError(f"unbalanced braces after {anchor!r}")


def _state_sources(body):
    """{payload key -> the state.<key> it reads}, for keys that read state directly."""
    body = _strip_line_comments(body)
    return {m.group(1): m.group(2) for m in
            re.finditer(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*!{0,2}state\.([A-Za-z_][A-Za-z0-9_]*)",
                        body, re.M)}


def _keys(body):
    """Top-level `key:` names of an object-literal body (nested objects ignored)."""
    body = _strip_line_comments(body)
    out, depth = [], 0
    for line in body.splitlines():
        stripped = line.strip()
        if depth == 0:
            m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*:", stripped)
            if m:
                out.append(m.group(1))
        depth += (line.count("{") + line.count("[")
                  - line.count("}") - line.count("]"))
    return out


def check_structural():
    """Every re-pricing call site sends the same inputs, from one shared builder."""
    failures = []

    pricing_body = _object_after(
        _object_after(APP_JS, "function pricingInputs() {"), "return {")
    pricing_inputs = set(_keys(pricing_body))
    # The state key each input reads - `shape: state.selectedShape` is saved as selectedShape.
    global PRICING_STATE_KEYS
    PRICING_STATE_KEYS = _state_sources(pricing_body)
    assert pricing_inputs, "could not parse pricingInputs()"

    # All three server calls that re-price must spread the shared object, not restate flags.
    for call, marker in (("/api/price", 'jsonRequestOptions(pricingInputs())'),
                         ("/api/export", "const exportPayload = {"),
                         ("/api/diagram", "const diagramPayload = {")):
        if marker.startswith("const"):
            body = _object_after(APP_JS, marker)
            if "...pricingInputs()" not in body:
                failures.append(
                    f"{call} payload does not spread pricingInputs() - it will drift. "
                    f"Add `...pricingInputs(),` instead of restating flags.")
        elif marker not in APP_JS:
            failures.append(f"{call} no longer calls pricingInputs()")

    # No call site may hand-restate a pricing input alongside the spread: a stale duplicate
    # after the spread silently wins.
    for call, marker in (("/api/export", "const exportPayload = {"),
                         ("/api/diagram", "const diagramPayload = {")):
        body = _object_after(APP_JS, marker)
        after_spread = body.split("...pricingInputs()", 1)[-1]
        for key in _keys(after_spread):
            if key in pricing_inputs:
                failures.append(
                    f"{call} re-states pricing input '{key}' after ...pricingInputs(); "
                    f"remove it so the shared builder is the only source.")
    return pricing_inputs, failures


def check_round_trip(pricing_inputs):
    """Everything that changes the money survives save -> re-import."""
    failures = []

    saved = set(_keys(_object_after(
        _object_after(APP_JS, "function collectWorkflowState()"), "return {")))
    assert saved, "could not parse collectWorkflowState()"

    assign_src = APP_JS[APP_JS.index("  const assign = ["):]
    assign_src = assign_src[:assign_src.index("];")]
    restored = set(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"', assign_src))

    # 1. Every pricing input is saved into the workflow (compared on the state key it reads).
    for key in sorted(pricing_inputs - {"fields", "rows"}):
        state_key = PRICING_STATE_KEYS.get(key, key)
        if state_key not in saved and key not in saved:
            failures.append(
                f"pricing input '{key}' is not saved by collectWorkflowState(); "
                f"a re-imported BOM would re-price without it.")

    # 2. Every saved key is restored (or explicitly handled).
    for key in sorted(saved - restored - HANDLED_OUTSIDE_ASSIGN):
        failures.append(
            f"'{key}' is saved by collectWorkflowState() but never restored by "
            f"applyWorkflowState(); the customization is silently dropped on re-import.")

    # 3. Nothing is restored that was never saved (would restore stale/undefined state).
    for key in sorted(restored - saved):
        failures.append(f"'{key}' is restored but never saved - it can never be present.")

    return failures


def check_numeric(only=None):
    """App headline == workbook Pricing Overview total, with customizations applied."""
    import app
    import bom_template
    import openpyxl

    bill = ROOT / "ecsv_5_2026 aws bill.csv"
    if not bill.exists():
        print("  numeric: SKIPPED (no sample bill in the repo)")
        return []

    # Parsing a 7,490-line bill is slow; cache it so a per-case run stays quick.
    cache = Path("/tmp/_parity_parsed.json")
    if cache.exists():
        parsed = json.loads(cache.read_text())
    else:
        parsed = app.parse_workbook(str(bill), True, app.INTAKE_MODE_CLOUD_BILL, "aws")
        cache.write_text(json.dumps({"fields": parsed["fields"], "rows": parsed["rows"]}))
    fields, rows = parsed["fields"], parsed["rows"]
    failures = []

    # Each case is a customization a user actually makes on the results page.
    cases = [
        ("defaults", dict()),
        ("rightsize", dict(rightsize=True)),
        ("rightsize + SQL hidden", dict(rightsize=True, hide_sql_pricing=True)),
        ("Windows hidden", dict(hide_windows_pricing=True)),
        ("GPU hidden", dict(hide_gpu_pricing=True)),
    ]
    if only is not None:
        cases = [cases[only]]
    for label, opts in cases:
        pricing = app.calculate_pricing(
            fields, rows, "e6-standard-ax", True, app.INTAKE_MODE_CLOUD_BILL,
            False, opts.get("hide_gpu_pricing", False),
            opts.get("hide_windows_pricing", False), opts.get("rightsize", False),
            False, None, "aws", "best", {}, {}, "auto",
            hide_sql_pricing=opts.get("hide_sql_pricing", False))

        # Mirror of ociMonthlyTotal() in app.js: Windows sits OUTSIDE totals.monthly in
        # cloud-bill mode, so the headline adds it back; SQL is already inside.
        windows = sum(float(r.get("windowsLicenseMonthly") or 0) for r in pricing["rows"])
        headline = round(float(pricing["totals"]["monthly"]) + windows, 2)

        content = bom_template.build_full_bom_bytes(
            pricing, rows, fields, None, "Parity Test",
            app.shape_payload("e6-standard-ax"), app.HOURS_PER_MONTH,
            block_rate=app.storage_rate("B91961"), vpu_rate=app.storage_rate("B91962"),
            default_vpus=app.BLOCK_PERFORMANCE_UNITS_PER_GB,
            file_rate=app.storage_rate("B89057"),
            windows_rate=app.WINDOWS_LICENSE_RATE, windows_sku=app.WINDOWS_LICENSE_SKU,
            extra_services=[], optimization=0.0,
            cloud_comparison={"pricing": pricing, "ramp": None, "ociDiscount": 0.0,
                              "extraServices": [], "hours": app.HOURS_PER_MONTH},
            diagram_options={}, workflow_json=None)
        out = Path("/tmp/_parity_test.xlsx")   # scratch, never inside the repo
        out.write_bytes(content)
        ws = openpyxl.load_workbook(out)["Pricing Overview"]
        # B18 is a formula; sum the baseline cells it adds up (discount is 0 in these cases).
        book = round(sum(v for v in (ws.cell(r, 2).value for r in range(9, 18))
                         if isinstance(v, (int, float))), 2)

        if abs(book - headline) > 0.01:
            failures.append(f"[{label}] app headline ${headline:,.2f} != "
                            f"workbook ${book:,.2f} (drift ${book - headline:,.2f})")
        else:
            print(f"  numeric: {label:<24} ${headline:>14,.2f}  app == workbook")
    return failures


def main():
    argv = sys.argv[1:]
    # One numeric case per run keeps each invocation short on a big bill.
    if argv and argv[0] == "--case":
        return 1 if check_numeric(int(argv[1])) else 0
    print("Export parity")
    pricing_inputs, failures = check_structural()
    print(f"  structural: {len(pricing_inputs)} shared pricing inputs")
    failures += check_round_trip(pricing_inputs)
    print("  round-trip: save -> re-import checked")
    failures += check_numeric()

    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK: the export matches the app, and customizations survive re-import.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
