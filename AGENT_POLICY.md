# Agent authority policy

**The deterministic engine owns the numbers. An AI agent is advisory everywhere except the
architecture diagram.**

This applies to the OpenAI calls already in the app and to any agent added later. It is enforced
in code (`AGENT_AUTHORITY` / `resolve_agent_result` in `app.py`), not by convention, so a new
integration inherits it by default instead of having to remember it.

## The rule

| Domain | Authority | What the agent may do |
|---|---|---|
| `architecture` | **override** | Change the generated diagram freely — rearrange, restyle, re-group |
| `inventory_scrub` | advisory | Suggest column mappings; fill a gap the parser left empty |
| `cloud_bill_mapping` | advisory | Propose an OCI target for a line the engine couldn't map |
| `pricing` | advisory | Comment on rates/sizing; never change a figure |
| `shape_selection` | advisory | Recommend a shape; never reassign one |
| `bom_export` | advisory | Annotate; never alter workbook totals |
| `table_edit` | advisory | Propose edits for the user to accept |

**Why the diagram is the exception:** it's a drawing. An agent rearranging it cannot move a
price, a mapping, or a BOM figure. Everything that feeds a customer-facing number stays
advisory, because an estimate has to be reproducible — the same upload must price the same way
every time, whether or not an agent ran.

## Major errors are heard, not obeyed

An agent can't overrule the engine, but it must be able to say *"this looks badly wrong."*

When an agent raises a major error on an advisory domain:

- the deterministic result **still stands** — the logic remains primary;
- the estimate is **flagged for human review** (`review: True`, surfaced as `agentReview` in
  metadata) so the objection is escalated rather than silently dropped.

Signals recognised (any one is enough, so an agent needn't match an exact schema):

```
{"majorError": true}      {"blocking": true}      {"isMajorError": true}
{"severity": "major"}     {"level": "critical"}   {"errorSeverity": "blocker"}
```
Accepted levels: `major`, `critical`, `blocker`, `severe`, `high`.

## How to integrate

Call the resolver at the boundary. Don't branch on authority by hand.

```python
decision = resolve_agent_result("pricing", deterministic_result, agent_result)
decision["result"]   # what to use
decision["source"]   # "deterministic" | "agent"
decision["review"]   # True -> flag for a human
decision["note"]     # human-readable reason, safe to show
```

To change what an agent is allowed to touch, edit `AGENT_AUTHORITY` — one dict, one place.
Flipping `architecture` to `advisory` immediately stops agent plans driving the diagram
(they're retained as `aiPlanAdvisory` for inspection), with no other code change.

## Guarantees to preserve

- Pricing must be reproducible: same input, same output, agent or no agent.
- An agent must never write a rate. Rates come from the rate card and the curated OCI catalog.
- An agent must never turn a blank into a number on a customer-facing figure. Empty stays empty
  and gets flagged — the app does not invent data.

## The Oracle SKU catalog refreshes itself

Prices and part numbers come from Oracle's Cost Estimator catalog, mirrored into
`data/oci_price_list.json`. The app checks the catalog's age at startup and refreshes in the
background when it is more than 7 days old (`bootstrap.refresh_catalog_if_stale`). A stale
catalog does not break pricing — it just misses SKUs Oracle published since the last pull, which
shows up as unrecognized lines on an Other OCI Bill import rather than as a wrong number.

**Agents must not hand-edit `data/oci_price_list.json`.** It is a mirror, and an edit is
overwritten by the next refresh. To change a rate, fix the source:

- a rate the app curates itself → `oci_catalog.py` or `data/oci_service_prices.json`
- a rate Oracle publishes → nothing to do; the refresh picks it up

To pull on demand, or on a machine that has been offline:

```sh
python3 scripts/refresh_oracle_catalog.py            # fetch, merge, report
python3 scripts/refresh_oracle_catalog.py --dry-run  # report without writing
```

`GET /api/catalog-status` returns the dataset version, the UTC refresh stamp, its age in days,
the SKU count, and whether it is considered stale. Check that before concluding a SKU is missing
from Oracle's catalog — it may just be that this copy hasn't been refreshed.

Set `OCI_APP_NO_CATALOG_REFRESH=1` to disable the automatic refresh entirely.
