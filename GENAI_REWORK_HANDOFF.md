# GenAI catalog rework — handoff

Status: **core feature built and verified in-browser.** This documents what changed, what's
proven, and what's left, so it can be picked up on Cowork.

## What was asked
Rework the "Add OCI services" catalog to match Oracle's Cost Estimator:
1. Collapse the four separate GenAI cards (On-Demand / Dedicated / Search+Vector+Memory / Agents)
   into **one combined "OCI Generative AI" card** with a **Models** section and a **Search and
   Retrieval** section, one combined total — exactly like the estimator screenshots.
2. Keep **"OCI Generative AI Agents"** as a **separate** card (RAG + Managed Knowledge Base).
3. Every meter visible as its own quantity box at once (no dropdown-per-meter).
4. Fix the **Base Database** card showing two identical "Base Database Service - Virtual Machine"
   dropdowns in OCPU mode.
5. Data source of truth: `oracle-cost-estimator-ai-integration/` (services 2741 Models, 3562
   Search & Retrieval, 3081 Agents) — all SKUs/rates/units taken from there, not guessed.

## What was built
Files changed:
- **oci_catalog.py**
  - New data: `GENAI_MODELS` (provider→model with input/output rate+SKU+unit basis),
    `GENAI_PROVIDERS`, `GENAI_RETRIEVAL` (6 meters), `GENAI_UNIT_BASES`, plus lookup maps.
  - Replaced the 4 GenAI `add()` blocks with one combined `add("genai", ...)` entry (carrying
    `genaiCombined`, `genaiModelInfo`, `genaiModelOptions`, `genaiRetrieval`, `genaiDedicated`,
    `retrievalHoursDefault=744`) and a restructured separate `add("genai_agents", ...)`.
  - `line_cost`: new `genaiCombined` and `genaiAgents` branches.
  - `line_breakdown`: new `genai` / `genai_agents` branches → **one SKU line per filled meter**
    (Models input + output lines; each retrieval meter; agents RAG/KB/ingestion).
  - Quantity inputs all default to 0 ("make gen ai stuff default to 0"): Base Database `ecpu`
    4→0, GenAI `ded_units` 0, every Models and Search & Retrieval meter 0. The **Utilization
    (hrs/mo)** field is NOT a quantity input and stays at the estimator's
    `GENAI_RETRIEVAL_HOURS_DEFAULT = 744`; client and server both fall back to 744 when unset.
- **static/app.js**
  - `clientLineCost`: `genaiCombined` / `genaiAgents` branches mirroring the server.
  - New render helpers: `genaiModelsHtml`, `genaiRetrievalHtml`, `genaiAgentsHtml`,
    `genaiServiceCardShell`, plus `refreshGenaiModelOptions` / `refreshGenaiLenLabels`.
  - `serviceCardHtml` branches on `e.id === "genai"` / `"genai_agents"`.
  - Input listener: provider change repopulates the Model list; provider/model change flips the
    length-unit labels ("in characters" for Cohere/Meta, "in tokens" for xAI/Google/OpenAI).
- **static/styles.css**: `.genai-section` / `.genai-row` / `.genai-subhead` etc. — sectioned,
  stacked-box layout.

## Pricing model (all verified to the cent)
Models: `cost = requests × (prompt_len/divisor × inRate + response_len/divisor × outRate)`
- Cohere/Meta: length in **characters**, divisor 10,000. xAI/Google/OpenAI: **tokens**, 1,000,000.
Search & Retrieval (Utilization hrs default **744** = 24 × 31, the estimator's value): storage
meters `GB × rate × hours`; request/event meters `qty/1000 × rate`. All meters start at 0, so the
card prices at $0.00 until quantities are entered.
Dedicated metric: `AI units × cluster rate × hours`.

Verified in the running app:
- Cohere Command R+ (Large Cohere), 1e6 req × 10k/10k chars → **$31,200.00**
- OpenAI gpt-oss-120b, 1e3 req × 10k/10k tokens → **$7.50**
- Retrieval: 3×100,000 GB storage + web 100k + retrieval 10k + ingest 1k @744h → **$938,442.20**
- Combined (gpt-oss-120b + full retrieval) → **$938,449.70**; client mirror == server.
- Dedicated Cohere 1 unit + retrieval → $956,298.20; Agents sample → $3,624.96.
- Base Database: ECPU mode shows only the ECPU edition picker; OCPU mode only the OCPU one.

## How to run
```bash
cd "/Users/cwegenek/Documents/HPC Lab/oci-intake-app"
source .venv/bin/activate
python3 app.py        # http://localhost:8787  (PORT=9000 to change)
```
The combined card is under the **4 Services** tab → **AI & Machine Learning** category (the tab
unlocks after an upload on tab 1). During dev it can be rendered directly:
`fetch('/api/catalog?group=AI & Machine Learning')` then `serviceCardHtml(entry, idx)`.

## Not yet done / follow-ups
- End-to-end through the real upload→Services→Price→export flow (verified so far by injecting the
  card and via unit checks; the server reprice path `price_extras`/`line_breakdown` is covered by
  the branches but should be exercised through an actual "Add to BOM" + Excel export).
- Cart line sizing string for the combined card is verbose (dumps every value key) — could be a
  friendly summary.
- xAI models use base tier only (<200K / <128K); >200K/>128K tier auto-selection by length not
  modeled. Cohere Rerank / Model Import from service 2741 are not in the Models dropdown yet.
- Agents card visual matches the estimator sections but hasn't had a pixel pass.

## Spec / working notes
Full extracted spec: see the session scratchpad `genai-rework-spec.md` (data tables + formulas).
