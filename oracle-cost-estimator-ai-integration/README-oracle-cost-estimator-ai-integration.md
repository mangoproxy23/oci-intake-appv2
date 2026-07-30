# Oracle Cost Estimator catalog — AI integration package

## Contents

- `oracle-cost-estimator-catalog-2026-07-30.json` — complete snapshot exported from Oracle's Cost Estimator.
- `refresh-oracle-cost-estimator.sh` — downloads and packages a fresh snapshot.
- This guide — source contract, schema, validation, and AI-agent instructions.

## Authoritative source contract

Oracle's estimator is a client-side application. It first reads this manifest:

`https://www.oracle.com/a/ocom/docs/cloudestimator2/js/env.json`

The manifest's `datasets.default` object names the current data files and their version query string. Resolve each relative value against:

`https://www.oracle.com/a/ocom/docs/cloudestimator2/`

For example, a manifest value of `./data/products.json?ver=439` becomes:

`https://www.oracle.com/a/ocom/docs/cloudestimator2/data/products.json?ver=439`

Never hard-code `ver=439`: fetch the manifest on each refresh and use its current values. These estimator files are a public source snapshot, but pricing can change; respect Oracle's terms and do not represent a snapshot as a binding quote.

## Refresh procedure

From this directory, run:

```sh
./refresh-oracle-cost-estimator.sh
```

Optional output directory:

```sh
./refresh-oracle-cost-estimator.sh /absolute/path/to/new-output
```

Requirements: `curl`, `jq`, and `shasum`. The script:

1. Downloads the manifest.
2. Resolves all published default-dataset file paths from the manifest.
3. Downloads products, services, metrics, currencies, presets, shapes, Exadata data, estimates, and signatures.
4. Produces one validated `oracle-cost-estimator-catalog-<UTC date>.json` and a SHA-256 sidecar.

Do not scrape the rendered UI or infer prices from the estimator display. Use the manifest and static JSON datasets above.

## Snapshot schema

Top-level fields:

```text
source                 origin URLs and dataset version
counts                 record counts for a quick freshness check
field_guide            join and constraint guidance
datasets.products      SKU records and currency-specific prices
datasets.services      service configuration and estimator UI constraints
datasets.metrics       billing metric names and translations
datasets.currencies    currency metadata
datasets.*             presets, shapes, signatures, and related estimator data
```

### Required joins

```text
products.items[].serviceid  -> services.items[].id
products.items[].metricId   -> metrics.items[].id
```

Use `products.items[].partNumber` as the SKU key. It is unique in this snapshot.

### Price lookup

For a SKU and requested ISO currency:

```text
products.items[]
  .currencyCodeLocalizations[]   where currencyCode == requested currency
  .prices[]                      one or more price tiers/models
```

The billing unit is `product.pricetype` (for example `MONTH`, `DAY`, `HOUR`, or `HOUR_UTILIZED`). Preserve all entries in `prices[]`; range/tier values must not be collapsed to a single number.

### Constraints and limits

The snapshot contains estimator input constraints, not OCI tenancy quotas:

- Product-level: `allowDecimalQty`, `eligibleSelections`, `availability`, `prorateEnabled`.
- Service-level: `fixedUtilization`, `days`, `hours`, `hideSection`, `hideInstances`, `hideHours`, `capacityReservation`, `priceRatio`.

It does **not** contain live compartment/tenancy service limits or usage quotas. Obtain those from OCI's authenticated Limits/Usage APIs or the relevant OCI service documentation; do not fabricate them from this file.

## Validation after refresh

Before adopting a refreshed snapshot:

1. Parse it as JSON.
2. Confirm `datasets.products.hasMore` is `false`.
3. Confirm each product's `serviceid` and `metricId` resolves to a service and metric.
4. Record `counts` and the generated SHA-256 file.
5. Mark the source date/version in your app and require refresh before price-sensitive use.

The 2026-07-30 snapshot has 674 SKUs, 125 services, 234 metrics, and 57 currencies.

## AI-agent operating prompt

```text
Use the Oracle Cost Estimator catalog JSON as the source of truth for this task.

1. Identify products by `datasets.products.items[].partNumber`.
2. Join `serviceid` to `datasets.services.items[].id` and `metricId` to
   `datasets.metrics.items[].id` before describing a SKU.
3. For pricing, select the requested currency from
   `currencyCodeLocalizations`, retain every `prices[]` entry, and state
   `pricetype` as the billing unit. Never assume USD or a monthly unit.
4. Treat `field_guide` constraints as estimator input metadata only. Do not
   call them OCI quota limits.
5. If the snapshot is old or a price will be presented externally, run the
   refresh script first, read the current manifest, validate the output, and
   cite the snapshot date and dataset version.
6. Do not scrape the estimator UI; refresh only through the manifest and its
   referenced static JSON files.
```
