#!/usr/bin/env sh
set -eu

BASE_URL='https://www.oracle.com/a/ocom/docs/cloudestimator2'
MANIFEST_URL="$BASE_URL/js/env.json"
OUTPUT_DIR="${1:-./oracle-cost-estimator-refresh}"
WORK_DIR="$OUTPUT_DIR/.source"

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Required command not found: $1" >&2
    exit 1
  }
}

require_command curl
require_command jq
require_command shasum

mkdir -p "$WORK_DIR"

curl --fail --silent --show-error --location "$MANIFEST_URL" \
  --output "$WORK_DIR/env.json"

DATASET_FILES='prodURL presetsURL categoriesURL servicesURL currenciesURL metricsURL signaturesURL estimatesURL shapeCriterionsURL shapesURL exadataURL'

for FIELD in $DATASET_FILES; do
  RELATIVE_PATH=$(jq -r ".datasets.default.$FIELD // empty" "$WORK_DIR/env.json")
  if [ -z "$RELATIVE_PATH" ]; then
    echo "Manifest is missing datasets.default.$FIELD" >&2
    exit 1
  fi
  FILE_NAME=$(printf '%s' "$RELATIVE_PATH" | sed 's#?.*$##; s#^.*/##')
  URL="$BASE_URL/${RELATIVE_PATH#./}"
  curl --fail --silent --show-error --location "$URL" \
    --output "$WORK_DIR/$FILE_NAME"
done

DATE_UTC=$(date -u '+%Y-%m-%d')
CATALOG_FILE="$OUTPUT_DIR/oracle-cost-estimator-catalog-$DATE_UTC.json"

jq -n \
  --slurpfile env "$WORK_DIR/env.json" \
  --slurpfile products "$WORK_DIR/products.json" \
  --slurpfile services "$WORK_DIR/services.json" \
  --slurpfile metrics "$WORK_DIR/metrics.json" \
  --slurpfile currencies "$WORK_DIR/currencies.json" \
  --slurpfile presets "$WORK_DIR/productPresets.json" \
  --slurpfile preset_categories "$WORK_DIR/productPresetCategories.json" \
  --slurpfile shapes "$WORK_DIR/shapes.json" \
  --slurpfile shape_criterions "$WORK_DIR/shapeCriterions.json" \
  --slurpfile exadata "$WORK_DIR/exadata.json" \
  --slurpfile estimates "$WORK_DIR/estimates.json" \
  --slurpfile signatures "$WORK_DIR/signatures.json" \
  --arg source_url "$MANIFEST_URL" \
  --arg extracted_at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
  '{
    schema_version: "1.0.0",
    catalog: "Oracle Cloud Cost Estimator",
    extracted_at_utc: $extracted_at,
    source: {
      estimator_url: "https://www.oracle.com/cloud/costestimator.html",
      manifest_url: $source_url,
      dataset_version: ($env[0].datasets.default.prodURL | capture("ver=(?<version>[0-9]+)").version),
      terms_note: "Use is subject to Oracle terms and pricing may change; refresh from the manifest before quoting or checkout."
    },
    counts: {
      skus: ($products[0].items | length),
      services: ($services[0].items | length),
      metrics: ($metrics[0].items | length),
      currencies: ($currencies[0].items | length)
    },
    field_guide: {
      product: {
        primary_key: "partNumber",
        service_join: "serviceid -> services.items[].id",
        metric_join: "metricId -> metrics.items[].id",
        price_path: "currencyCodeLocalizations[].prices[]",
        price_unit: "pricetype",
        estimator_input_constraints: ["allowDecimalQty", "eligibleSelections", "availability", "prorateEnabled"],
        note: "This catalog does not contain OCI tenancy/service quota limits. Obtain quotas separately from OCI limits/usage APIs or service documentation."
      },
      service: {
        primary_key: "id",
        estimator_input_constraints: ["fixedUtilization", "days", "hours", "hideSection", "hideInstances", "hideHours", "capacityReservation", "priceRatio"]
      },
      metric: {primary_key: "id", localized_name_path: "languageLocalizations[]", utilization_period_field: "utilization_period"}
    },
    datasets: {
      manifest: $env[0], products: $products[0], services: $services[0], metrics: $metrics[0], currencies: $currencies[0],
      product_presets: $presets[0], product_preset_categories: $preset_categories[0], shapes: $shapes[0],
      shape_criterions: $shape_criterions[0], exadata: $exadata[0], estimates: $estimates[0], signatures: $signatures[0]
    }
  }' > "$CATALOG_FILE"

jq empty "$CATALOG_FILE"
shasum -a 256 "$CATALOG_FILE" > "$CATALOG_FILE.sha256"
echo "Created $CATALOG_FILE"
