# Session handoff — catalog expansion, export parity, compute cards

Branch: **`catalog-and-parity`** (19 commits off `Chris'-Branch`). Working tree clean, full test
suite green, everything committed. Read this before picking the work up.

---

## What this branch does

Three separate efforts, plus four commits of pre-existing work that was sitting uncommitted.

**1. Export parity.** `/api/export` and `/api/diagram` re-price the whole estate server-side from
the payload the browser sends — they do NOT reuse the estimate on screen. Those payloads were
hand-copied flag lists that had drifted: `rightsize` was missing from both, so an export priced
every workload at full size while the screen showed the trimmed number. **$117,329 on screen vs
$129,125 in the workbook** on a 7,490-line AWS bill. Separately, `applyWorkflowState()` saved
`hoursPerMonth`/`hoursOverride` and then hard-reset them, so re-importing a saved BOM re-priced
it at 730 hours regardless.

All three call sites now spread one `pricingInputs()`. `scripts/test_export_parity.py` enforces:

    pricingInputs() ⊆ collectWorkflowState() ⊆ applyWorkflowState()

structurally, by reading `static/app.js` as text — that is what catches the next pricing input
added to one call site and forgotten in the other two. **If you add a pricing input, add it to
`pricingInputs()` and nowhere else.**

**2. Catalog 30 → 56 services.** Oracle's Cost Estimator snapshot is vendored (raw 42 MB file is
gitignored, regenerable via `scripts/refresh_oracle_catalog.py`); `scripts/gen_estimator_services.py`
distils it into `data/estimator_services.json`. Two kinds of card:

- **Generated** (21) — services whose estimator card is genuinely a list of quantity rows. Each
  SKU keeps its full graduated tier table, which is where OCI's free allowances live.
- **Hand-tuned** (Compute VM / Bare Metal / GPU / VMware) — Oracle bills a flex VM as a PAIR of
  SKUs at different rates, so a per-SKU card can't express it.

**3. Bill-mapping and card fixes** — see "Bugs found" below.

---

## Commits

```
8d7d723  GPU card: the shape is the machine, and the add-ons follow it
c6b7078  Cluster long dropdowns by family, and finish the Other OCI Bill rename
04cd3e8  Rename the intake mode key to other_oci_bill, and migrate the old one
eb72e75  Rename the "Foreign OCI BOM" intake mode to "Other OCI Bill"
eab54f8  Price the top four Base Database storage tiers instead of dropping them
71b8f94  Base Database: make Processor, Shape, OCPU and Storage agree
39e9db0  Make service rows easier to tell apart at a glance
346715c  Keep unusable rows out of the raw price-list search fallback
1042870  Park "Other Services" at the end of the category list
8917ecf  Sort the service categories and cards alphabetically when browsing
4d14351  Ignore the raw snapshot's checksum sidecar too
9680b97  Import the official AWS/Azure/GCP shape tables      ← pre-existing work
8d8a892  Refresh the Oracle SKU catalog automatically         ← pre-existing work
9c65ac6  Import a Foreign OCI BOM: multi-sheet, JSON, CSV     ← pre-existing work
d8d7f58  Style the reworked Generative AI cards               ← pre-existing work
bebdb5f  Grow the service catalog from 30 to 56, fix 3 SKUs
3466e5e  Make the export always match the app
594f34c  Map S3 lines to the storage tier they actually bill at
3ca2166  Vendor the Oracle Cost Estimator catalog
```

The four marked pre-existing were grouped and message-written by me from reading the diffs —
worth a skim in case anything is mischaracterised.

---

## Bugs found and fixed

| what | impact |
|---|---|
| `rightsize` missing from export + diagram payloads | exports ~10% high whenever Rightsize was on |
| `hoursPerMonth` saved but discarded on re-import | re-opened BOMs re-priced at 730 h |
| S3 lines mapped on service name only | IA/Glacier priced as Standard ($0.0255 vs $0.0100/$0.0026) |
| DNS card cited `B88516` | that SKU is "Compute VM Dense I/O – X7". Correct: `B88525` |
| Logging card cited `B92707` | exists in neither Oracle's catalog nor ours. Correct: `B92593` |
| LB card cited `B93031` at a flat rate | that's the *bandwidth* meter. Base is `B93030`; bandwidth is the meter that scales, so a 1 Gbps LB was $8.25/mo instead of **$72.26** |
| Base Database had **no** `line_breakdown` branch | fixed-shape totals charged 24 OCPU while the paper trail printed 1; infra + storage SKUs never appeared in a BOM |
| Base DB Processor / Shape / OCPU / Storage unconstrained | AMD + Ampere shape selectable; Arm's storage cap offered to x86 |
| 4 Base DB storage tiers had provisioned = 0 | selecting one priced storage at $0 |
| `bom_diagram` skipped all Compute rows | added GPU/BM/OKE cards vanished from the architecture diagram |
| GPU card took free-typed GPU counts | could quote 3 × H200, a machine Oracle doesn't sell |

---

## Open decisions — need a human

1. **Load Balancer free tier.** Both meters have allowances (744 LB-hours, 7,440 Mbps-hours)
   that look exactly like OCI's Always Free tier. I apply them **per card**, but Always Free is
   **per tenancy** — two LB cards would get it twice. And you may not want $0.00 on the first LB
   in an enterprise quote at all. Decide, then either keep or drop the tier-0 rows.
2. **GPU Windows support conflict.** `data/oci_gpu_shapes.json` says only BM.GPU2.2 supports
   Windows; Oracle's estimator `windowsSupported` says BM.GPU.A10.4, VM.GPU.A10.x, VM.GPU3.x and
   BM.GPU3.8 do too. I used the local file. Oracle's own data is probably better.
3. **Base DB storage tiers 57,344 and 65,536** rest on the documented formula
   (`1.25 × usable + 200`, ECPU +5) alone. It reproduces all ten checkable tiers exactly, and
   73,728/81,920 are corroborated by panel figures — but those two are inference. Oracle's
   estimator prints the provisioned figure when you select a tier; 30 seconds to confirm.

---

## Next up

1. **`Compute Shapes.pdf`** (Oracle's compute-shapes doc page) was uploaded at the very end of
   the session and never parsed. It would verify the GPU OCPU counts and settle decision #2.
2. **Mapping audit.** The S3 tier bug is one instance of a general problem: a bill's *service
   name* can't carry the attribute that picks the rate. The same shape of bug may sit in database
   editions, compute generations, anything where AWS puts it in the usage type. `map_service_comparison`
   now refines only along `STORAGE_TIER_REFINEMENTS` — deliberately narrow, because a general
   "most specific wins" rule silently reshuffled 20 unrelated rows (it flipped CloudFront off CDN
   onto plain egress). Worth a systematic pass over the 7,490-line bill.
3. **26 bespoke services still unbuilt** — Media Flow (81 SKUs), IAM (12), Cloudflare (16),
   Redis, APEX, Functions, OpenSearch, Lustre, Exadata variants. The engine exists; these are
   mostly 1–4 SKUs each and should go fast.
4. **103 malformed rows in `data/oci_price_list.json`** (descriptions like `- 0.0113`,
   `0.3000 0.3000 0.1600`). Filtered out of search, but the data is still wrong.

---

## Gotchas

- **Restart `app.py` for Python changes; hard-refresh (`Cmd+Shift+R`) for JS/CSS.** Several
  rounds of "that didn't work" were just a stale server or cached `app.js`.
- **Two tests fail for environmental reasons, not code**: `test_ai_assists` needs an LLM key,
  `test_cross_cloud` wants a fixture `ecsv_5_2026(1).csv` that isn't in the repo.
- **`uploads/_to_delete/`** holds ~500 files (~15 MB) of git junk I couldn't remove — the cloud
  session reaches the disk through a bridge that can't delete. `rm -rf` it, then `git gc`.
- **Running Cowork in the cloud makes git painful here** — every commit fails to unlink its own
  lock and temp files. Starting the task on your computer avoids it entirely.
- `scripts/test_export_parity.py --case 0..4` runs the numeric cases one at a time; the full
  numeric pass is slow on a 7,490-line bill.

---

## Verification status

- All 121 SKU lines across all 56 cards exist in Oracle's catalog with matching rates
- `line_cost` == sum of `line_breakdown` == client mirror, for every card
- App headline == workbook Pricing Overview total, to the cent, across 5 customisation states
- BM.GPU.L40S.4 × 1 @ 744 h = **$10,416.00**, matching Oracle's own estimator panel
- GenAI: Cohere Command R+ $31,200.00, gpt-oss-120b $7.50 — unchanged from the earlier rework
