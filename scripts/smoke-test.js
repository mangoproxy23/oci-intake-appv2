const path = require("node:path");
const fs = require("node:fs/promises");
const { execFileSync } = require("node:child_process");
const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "..");
const QA_DIR = path.join(ROOT, "qa");
const SAMPLE = process.env.SAMPLE_XLSX || "/Users/gus/Downloads/Current State Inventory (2).xlsx";
const APP_URL = process.env.APP_URL || "http://127.0.0.1:8787";
const UPLOAD_TIMEOUT_MS = Number(process.env.UPLOAD_TIMEOUT_MS || 100000);
const PRICING_TIMEOUT_MS = Number(process.env.PRICING_TIMEOUT_MS || 100000);

async function assertNoEmDashes(page, label) {
  const html = await page.locator("html").innerHTML();
  if (html.includes("\u2014")) {
    throw new Error(`Customer-facing em dash found in ${label}.`);
  }
}

async function assertStepLabelFits(page, step, context) {
  const metrics = await page.locator(`.step[data-step="${step}"]`).evaluate((tab) => {
    const rect = tab.getBoundingClientRect();
    const visibleLabel = Array.from(tab.children).find(
      (child) => getComputedStyle(child).display !== "none",
    );
    const style = getComputedStyle(tab);
    return {
      left: rect.left,
      right: rect.right,
      viewport: window.innerWidth,
      labelWidth: visibleLabel?.getBoundingClientRect().width || tab.scrollWidth,
      contentWidth: tab.clientWidth
        - parseFloat(style.paddingLeft)
        - parseFloat(style.paddingRight),
    };
  });
  if (metrics.labelWidth > metrics.contentWidth + 0.5) {
    throw new Error(`${context} label overflows its tab: ${JSON.stringify(metrics)}`);
  }
  return metrics;
}

async function main() {
  await fs.mkdir(QA_DIR, { recursive: true });

  const browser = await chromium.launch({
    headless: true,
    channel: "chrome",
  });
  const page = await browser.newPage({ viewport: { width: 1440, height: 960 } });
  const priceResponsePromise = page.waitForResponse(
    (response) => response.url().includes("/api/price") && response.request().method() === "POST",
    { timeout: PRICING_TIMEOUT_MS },
  );

  await page.goto(APP_URL, { waitUntil: "domcontentloaded" });
  await page.screenshot({ path: path.join(QA_DIR, "landing.png"), fullPage: false });
  await page.locator("#modeCloudBill").click();
  const cloudBillCasing = {
    heading: await page.locator("#uploadHeading").innerText(),
    eyebrow: await page.locator("#modeEyebrow").innerText(),
    chooser: await page.locator("#dropZone strong").innerText(),
  };
  if (
    cloudBillCasing.heading !== "Upload Cloud Bill"
    || cloudBillCasing.eyebrow !== "CLOUD BILL"
    || cloudBillCasing.chooser !== "Choose Bill Export"
  ) {
    throw new Error(`Cloud Bill mode did not use the expected title casing: ${JSON.stringify(cloudBillCasing)}`);
  }
  await page.locator("#modeOnPrem").click();
  if ((await page.locator("#uploadHeading").innerText()) !== "Upload Inventory") {
    throw new Error("On-Prem Inventory mode did not use the expected title casing.");
  }
  if (await page.locator("#uploadPanel #loadPrevBom").count()) {
    throw new Error("Load previous BOM is still rendered inside the upload panel.");
  }
  const settingsTriggerStyle = await page.locator("#settingsToggle").evaluate((button) => {
    const style = getComputedStyle(button);
    return {
      borderWidth: style.borderTopWidth,
      backgroundColor: style.backgroundColor,
    };
  });
  if (
    settingsTriggerStyle.borderWidth !== "0px"
    || settingsTriggerStyle.backgroundColor !== "rgba(0, 0, 0, 0)"
  ) {
    throw new Error(`Settings trigger is not borderless and transparent: ${JSON.stringify(settingsTriggerStyle)}`);
  }
  await page.getByRole("button", { name: "Settings" }).click();
  await page.getByRole("heading", { name: "Settings", exact: true }).waitFor();
  const workflowChooser = page.waitForEvent("filechooser");
  await page.getByRole("button", { name: "Load previous BOM" }).click();
  await workflowChooser;
  await page.locator("#darkModeToggle").check();
  if (await page.locator("html").getAttribute("data-theme") !== "dark") {
    throw new Error("Dark mode did not apply to the document.");
  }
  if (await page.evaluate(() => localStorage.getItem("oci-intake-theme")) !== "dark") {
    throw new Error("Dark mode preference was not saved.");
  }
  await page.waitForTimeout(200);
  await page.screenshot({ path: path.join(QA_DIR, "dark-settings.png"), fullPage: false });
  await page.reload({ waitUntil: "domcontentloaded" });
  if (await page.locator("html").getAttribute("data-theme") !== "dark") {
    throw new Error("Dark mode did not persist after reload.");
  }
  await page.getByRole("button", { name: "Settings" }).click();
  await page.locator("#darkModeToggle").uncheck();
  await page.keyboard.press("Escape");
  if (!(await page.locator("#settingsMenu").isHidden())) {
    throw new Error("Settings menu did not close with Escape.");
  }

  const navigationCheck = await browser.newPage({ viewport: { width: 1024, height: 768 } });
  await navigationCheck.goto(APP_URL, { waitUntil: "domcontentloaded" });
  if (await navigationCheck.locator("#pricingRail").isVisible()) {
    throw new Error("The SKU mapping rail appeared on a fresh Upload step.");
  }
  if ((await navigationCheck.locator(".step:disabled").count()) !== 7) {
    throw new Error("Fresh workflow did not start with steps 2 through 8 locked.");
  }
  if (!(await navigationCheck.getByRole("button", { name: "Continue to Review" }).isDisabled())) {
    throw new Error("Upload Continue action was enabled before a spreadsheet was chosen.");
  }
  await navigationCheck.setInputFiles("#fileInput", SAMPLE);
  await navigationCheck.locator("#continueToReviewFromUpload:not(:disabled)")
    .waitFor({ timeout: UPLOAD_TIMEOUT_MS });
  if (await navigationCheck.locator('[data-step="review"]').isDisabled()) {
    throw new Error("Review stayed locked after a successful upload.");
  }
  if (!(await navigationCheck.locator('[data-step="shape"]').isDisabled())) {
    throw new Error("Shape unlocked before Review was completed.");
  }
  if ((await navigationCheck.locator(".step.is-active").getAttribute("data-step")) !== "upload") {
    throw new Error("A successful upload advanced without the user choosing Continue.");
  }
  if (await navigationCheck.locator("#pricingRail").isVisible()) {
    throw new Error("The SKU mapping rail appeared on Upload after parsing.");
  }
  await navigationCheck.getByRole("button", { name: "Continue to Review" }).click();
  await navigationCheck.getByText("Adjust Your Table", { exact: true }).waitFor();
  if (await navigationCheck.locator("#dataCheckList .dc-label").getByText("Tier", { exact: true }).count()) {
    throw new Error("Tier is still shown in the Review data check.");
  }
  if (
    await navigationCheck.locator("#dataCheckList .dc-label")
      .getByText("Site / location", { exact: true }).count()
  ) {
    throw new Error("Site / location is still shown in the Review data check.");
  }
  if (await navigationCheck.locator("#pricingRail").isVisible()) {
    throw new Error("The SKU mapping rail appeared on Review.");
  }
  const reviewWidth = await navigationCheck.locator("#reviewPanel").evaluate((panel) => {
    const panelRect = panel.getBoundingClientRect();
    const pageRect = document.querySelector("#intakePage").getBoundingClientRect();
    return { panelWidth: panelRect.width, pageWidth: pageRect.width };
  });
  if (reviewWidth.panelWidth < reviewWidth.pageWidth - 40) {
    throw new Error(`Review did not expand to the available width: ${JSON.stringify(reviewWidth)}`);
  }
  const groupedPricingRows = await navigationCheck.evaluate(() => {
    renderPricing({
      intakeMode: "on_prem",
      selectedShape: { label: "E6 Standard Ax", shortLabel: "E6 Ax" },
      totals: { monthly: 225, annual: 2700, ocpus: 3 },
      rows: [
        { applicationName: "JDE", name: "JDE", environment: "DR", monthly: 100 },
        { applicationName: "JDE", name: "JDE", environment: "DR", monthly: 75 },
        { applicationName: "JDE", name: "JDE", environment: "Prod", monthly: 50 },
      ],
    });
    return Array.from(document.querySelectorAll("#pricingSummary .result-table tbody tr")).map((row) => ({
      name: row.querySelector(".pricing-summary-name")?.textContent.trim(),
      count: row.querySelector("small")?.textContent.trim() || "",
      environment: row.children[1]?.textContent.trim(),
      monthly: row.children[2]?.textContent.trim(),
    }));
  });
  if (
    groupedPricingRows.length !== 2
    || JSON.stringify(groupedPricingRows[0]) !== JSON.stringify({
      name: "JDE",
      count: "2 workload rows",
      environment: "DR",
      monthly: "$175.00",
    })
  ) {
    throw new Error(`Application and environment pricing rows were not grouped: ${JSON.stringify(groupedPricingRows)}`);
  }
  await navigationCheck.getByRole("button", { name: "Continue to Shape" }).click();
  await navigationCheck.getByText("Choose the OCI Shape for This Estimate", { exact: true }).waitFor();
  if (await navigationCheck.locator('[data-step="shape"]').isDisabled()) {
    throw new Error("Shape stayed locked after Review was completed.");
  }
  if (!(await navigationCheck.locator('[data-step="networking"]').isDisabled())) {
    throw new Error("Services unlocked before Shape was completed.");
  }
  await navigationCheck.locator('[data-step="upload"]').click();
  await navigationCheck.getByText("Upload Inventory", { exact: true }).waitFor();
  if (await navigationCheck.locator("#pricingRail").isVisible()) {
    throw new Error("The SKU mapping rail remained visible after returning to Upload.");
  }
  await navigationCheck.setViewportSize({ width: 850, height: 768 });
  await assertStepLabelFits(navigationCheck, "architecture", "Compact Architecture");
  await assertStepLabelFits(navigationCheck, "deliverables", "Compact Deliverables");
  await navigationCheck.close();

  await page.setInputFiles("#fileInput", SAMPLE);
  await page.locator("#continueToReviewFromUpload:not(:disabled)").waitFor({ timeout: UPLOAD_TIMEOUT_MS });
  if ((await page.locator(".step.is-active").getAttribute("data-step")) !== "upload") {
    throw new Error("Upload did not remain active after parsing.");
  }
  await page.getByRole("button", { name: "Continue to Review" }).click();
  await page.getByText("Adjust Your Table", { exact: true }).waitFor({ timeout: UPLOAD_TIMEOUT_MS });
  if (await page.locator(".table-assistant, #tableEditPrompt, #applyTableEdit").count()) {
    throw new Error("The removed table assistant is still present on Review.");
  }
  const activeAfterUpload = await page.locator(".step.is-active").getAttribute("data-step");
  if (activeAfterUpload !== "review") {
    throw new Error(`Review content loaded with the wrong active step: ${activeAfterUpload}`);
  }
  await page.getByRole("columnheader", { name: "Hours Running" }).waitFor();
  await page.getByRole("columnheader", { name: "Application Name" }).waitFor();
  await page.getByRole("columnheader", { name: "Machine Name" }).waitFor();
  const applicationValues = await page
    .locator('input[aria-label^="Application Name, row"]')
    .evaluateAll((inputs) => inputs.map((input) => input.value.trim()).filter(Boolean));
  const machineValues = await page
    .locator('input[aria-label^="Machine Name, row"]')
    .evaluateAll((inputs) => inputs.map((input) => input.value.trim()).filter(Boolean));
  if (!applicationValues.length || !machineValues.length) {
    throw new Error("Uploaded inventory did not preserve both application and machine names.");
  }
  const hoursRunningValues = await page.locator('input[aria-label^="Hours Running, row"]').evaluateAll(
    (inputs) => inputs.map((input) => input.value),
  );
  if (!hoursRunningValues.length || hoursRunningValues.some((value) => value !== "730")) {
    throw new Error(`Unexpected Hours Running defaults: ${JSON.stringify(hoursRunningValues.slice(0, 10))}`);
  }
  await page.screenshot({ path: path.join(QA_DIR, "review.png"), fullPage: false });

  const rowCount = await page.locator("#rowCount").textContent();
  const columnCount = await page.locator("#columnCount").textContent();
  const parsedRows = Number(rowCount);
  const parsedColumns = Number(columnCount);
  const reviewHeaders = await page.locator("#reviewTable th").evaluateAll(
    (headers) => headers.map((header) => header.textContent.trim()),
  );
  const expectedReviewHeaders = [
    "Approve",
    "Application Name",
    "Machine Name",
    "Environment",
    "OCPUs",
    "RAM (GB)",
    "Storage (GB)",
    "Hours Running",
  ];
  if (
    parsedRows < 1
    || parsedColumns !== 7
    || JSON.stringify(reviewHeaders) !== JSON.stringify(expectedReviewHeaders)
  ) {
    throw new Error(`Unexpected parsed dimensions: rows=${rowCount}, columns=${columnCount}`);
  }

  await page.getByRole("button", { name: "Continue to Shape" }).click();
  await page.getByText("Choose the OCI Shape for This Estimate", { exact: true }).waitFor({ timeout: 20000 });
  if (!(await page.locator('[data-step="networking"]').isDisabled())) {
    throw new Error("Services unlocked before the Shape action completed.");
  }
  if (await page.locator("#hoursPerMonth, .hours-control-box").count()) {
    throw new Error("The removed global hours control is still present on Shape.");
  }
  await page.locator("#shapeGrid button").first().waitFor({ timeout: 20000 });
  await page.screenshot({ path: path.join(QA_DIR, "shape.png"), fullPage: false });

  await page.getByRole("button", { name: "Continue to Services" }).click();
  await page.getByRole("heading", { name: "OCI Services" }).waitFor({ timeout: PRICING_TIMEOUT_MS });
  if (!(await page.locator('[data-step="price"]').isDisabled())) {
    throw new Error("Price unlocked before Services was completed.");
  }
  await page.locator("#serviceResults .service-group-head").first().waitFor({ timeout: 20000 });
  await page.locator('.service-chip[data-group="Storage"]').click();
  const storageCards = page.locator("#serviceResults .service-card");
  await storageCards.filter({ hasText: "Object Storage - Standard" }).waitFor();
  await storageCards.filter({ hasText: "Object Storage - Infrequent Access" }).waitFor();
  await storageCards.filter({ hasText: "Object Storage - Archive" }).waitFor();
  const storageChipText = await page.locator('.service-chip[data-group="Storage"]').textContent();
  if (!/Storage\s+5/.test(storageChipText || "")) {
    throw new Error(`Unexpected Storage catalog count: ${storageChipText}`);
  }
  const tierCosts = {
    standard: await storageCards.filter({ hasText: "Object Storage - Standard" })
      .locator(".service-card-cost").textContent(),
    infrequent: await storageCards.filter({ hasText: "Object Storage - Infrequent Access" })
      .locator(".service-card-cost").textContent(),
    archive: await storageCards.filter({ hasText: "Object Storage - Archive" })
      .locator(".service-card-cost").textContent(),
  };
  if (tierCosts.standard !== "$25.24/mo"
      || tierCosts.infrequent !== "$9.90/mo"
      || tierCosts.archive !== "$2.57/mo") {
    throw new Error(`Unexpected Object Storage tier prices: ${JSON.stringify(tierCosts)}`);
  }
  await page.locator('.service-chip[data-group="Networking"]').click();
  const fastConnectCard = page.locator("#serviceResults .service-card")
    .filter({ hasText: "FastConnect port" });
  await fastConnectCard.waitFor();
  const fastConnectExpected = {
    "1G": { cost: "$155.13/mo", sku: "B88325" },
    "10G": { cost: "$930.75/mo", sku: "B88326" },
    "100G": { cost: "$7,847.50/mo", sku: "B93126" },
    "400G": { cost: "$14,600.00/mo", sku: "B107975" },
  };
  for (const [speed, expected] of Object.entries(fastConnectExpected)) {
    await fastConnectCard.locator('select[data-key="speed"]').selectOption(speed);
    const cost = await fastConnectCard.locator(".service-card-cost").textContent();
    const meta = await fastConnectCard.locator(".service-card-meta").textContent();
    if (cost !== expected.cost || !meta.includes(expected.sku)) {
      throw new Error(`Unexpected FastConnect ${speed} price: ${cost}; ${meta}`);
    }
  }
  await fastConnectCard.screenshot({ path: path.join(QA_DIR, "fastconnect-ports.png") });
  await fastConnectCard.getByRole("button", { name: "Add to BOM" }).click();
  await page.locator("#serviceCartReview")
    .getByText("FastConnect port (400 Gbps)", { exact: true }).waitFor();

  await page.locator('.service-chip[data-group="Storage"]').click();
  const refreshedStorageCards = page.locator("#serviceResults .service-card");
  const standardStorageCard = refreshedStorageCards.filter({ hasText: "Object Storage - Standard" });
  await standardStorageCard.getByRole("button", { name: "Add to BOM" }).click();
  await page.locator("#serviceCartReview").getByText("Object Storage - Standard", { exact: true }).waitFor();
  const addedServiceCount = await page.locator("#serviceCartCount").textContent();
  if (addedServiceCount !== "2") {
    throw new Error(`Unexpected added service count: ${addedServiceCount}`);
  }
  const expectedAddedServicesMonthly = 14600 + 25.24;
  if (await page.locator("#addServicesToggle").count()) {
    throw new Error("The service catalog collapse control is still present.");
  }
  if (!(await page.locator("#addServicesBody").isVisible())) {
    throw new Error("The service catalog is not permanently visible.");
  }
  if (!(await page.locator("#serviceCartReview").isVisible())) {
    throw new Error("Added services review disappeared with the service catalog.");
  }
  await page.locator("#serviceCartReview").screenshot({ path: path.join(QA_DIR, "services-review.png") });
  await page.screenshot({ path: path.join(QA_DIR, "networking.png"), fullPage: false });
  await page.getByRole("button", { name: "Continue to Price" }).click();
  const priceResponse = await priceResponsePromise;
  const pricePayload = await priceResponse.json();
  await page.getByText("OCI Cost Breakdown", { exact: true }).waitFor({ timeout: PRICING_TIMEOUT_MS });
  if (!(await page.locator('[data-step="other-clouds"]').isDisabled())) {
    throw new Error("Compare unlocked before the Price action completed.");
  }
  await page.locator("#resultsShape").waitFor({ timeout: 20000 });
  await page.locator("#resultsPage").getByText("Total Contract Value", { exact: true }).waitFor({ timeout: 20000 });
  await page.locator("#resultsKpis").getByText("Pricing summary", { exact: true }).waitFor({ timeout: 20000 });
  await page.locator("#resultsKpis").getByText("Specs identified", { exact: true }).waitFor({ timeout: 20000 });
  const activeOnPrice = await page.locator(".step.is-active").getAttribute("data-step");
  if (activeOnPrice !== "price") {
    throw new Error(`Price content loaded with the wrong active step: ${activeOnPrice}`);
  }
  const priceBomButton = page.locator("#resultsPage #exportFullBom");
  if (
    (await priceBomButton.innerText()).trim() !== "Download BOM"
    || !(await priceBomButton.isVisible())
  ) {
    throw new Error("Price does not show the direct Download BOM action.");
  }
  if (
    await page.locator(
      "#resultsPage :is(#exportMenuToggle, #exportMenuList, #exportExcel, #exportJson)",
    ).count()
  ) {
    throw new Error("The removed BOM export dropdown is still present on Price.");
  }
  await page.screenshot({ path: path.join(QA_DIR, "pricing.png"), fullPage: false });
  await page.locator(".cost-mix-panel").screenshot({ path: path.join(QA_DIR, "cost-mix.png") });

  const pricingText = await page.locator("#resultsPage").textContent();
  if (!/\$[\d,]+\.\d{2}/.test(pricingText)) {
    throw new Error("No formatted pricing total was visible after pricing.");
  }
  const rampCeilingText = await page.locator("#rampCeilingLabel").textContent();
  const rampCeiling = Number(String(rampCeilingText || "").replace(/[^\d.]/g, ""));
  const expectedRampCeiling = Number(pricePayload.totals.monthly || 0) + expectedAddedServicesMonthly;
  if (Math.abs(rampCeiling - expectedRampCeiling) > 0.01) {
    throw new Error(
      `Ramp or headline double-counted a backend line item: expected=${expectedRampCeiling}, actual=${rampCeiling}`,
    );
  }
  const rampHandles = page.locator("#rampChart .ramp-handle");
  if ((await rampHandles.count()) < 1) {
    throw new Error("The consumption ramp rendered without adjustable points.");
  }
  await rampHandles.first().click();
  const firstRampMonth = Number(await page.locator("#rampPeakMonth").inputValue());
  const firstRampMonthly = Number(await page.locator("#rampPeakMonthly").inputValue());
  if (firstRampMonth !== 1 || !(firstRampMonthly > 0)) {
    throw new Error(
      `Default ramp did not start in month 1: month=${firstRampMonth}, monthly=${firstRampMonthly}`,
    );
  }
  await page.locator("#rampChart").screenshot({ path: path.join(QA_DIR, "ramp.png") });

  await page.getByRole("button", { name: "Estimate on Other Clouds", exact: true }).click();
  await page.getByRole("heading", { name: "Estimate on Other Clouds" }).waitFor();
  if (!(await page.locator('[data-step="architecture"]').isDisabled())) {
    throw new Error("Architecture unlocked before Compare was completed.");
  }
  await page.locator("#crossCloudResults .cross-cloud-card").first().waitFor();
  await page.screenshot({ path: path.join(QA_DIR, "other-clouds.png"), fullPage: false });
  await page.getByRole("button", { name: "Continue to Architecture" }).click();
  await page.getByRole("heading", { name: "Configure the OCI Architecture" }).waitFor({ timeout: PRICING_TIMEOUT_MS });
  if (!(await page.locator('[data-step="deliverables"]').isDisabled())) {
    throw new Error("Deliverables unlocked before Architecture was completed.");
  }
  await page.getByRole("button", { name: "Continue to Deliverables" }).waitFor({ timeout: 20000 });
  const regionMarkers = page.locator("#ociRegionMarkers .oci-region-marker");
  if ((await regionMarkers.count()) !== 45) {
    throw new Error(`Architecture map did not render all 45 commercial regions.`);
  }
  await page.locator('[data-map-view="europe"]').click();
  if ((await page.locator("#ociRegionMap").getAttribute("viewBox")) !== "462 66 125 95") {
    throw new Error("Architecture map did not zoom to the selected geography.");
  }
  await page.locator('[data-map-view="world"]').click();
  await page.locator("#clearRegionMap").click();
  await page.locator('.oci-region-marker[data-region="eu-jovanovac-1"]').click();
  if (
    (await page.locator('.oci-region-marker[data-region="us-ashburn-1"]').getAttribute(
      "aria-disabled",
    )) !== "true"
  ) {
    throw new Error("Architecture map did not disable a cross-realm DR region.");
  }
  await page.locator("#drRegion").evaluate((select) => {
    select.value = "us-ashburn-1";
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
  if (
    (await page.locator("#drRegion").inputValue()) !== ""
    || !(await page.locator("#regionMapLive").innerText()).includes("same OCI realm")
  ) {
    throw new Error("Architecture map allowed an invalid cross-realm DR selection.");
  }
  await page.locator("#clearRegionMap").click();
  await page.locator('.oci-region-marker[data-region="us-ashburn-1"]').click();
  if ((await page.locator("#primaryRegion").inputValue()) !== "us-ashburn-1") {
    throw new Error("The first map click did not set the Primary region.");
  }
  await page.locator('.oci-region-marker[data-region="us-phoenix-1"]').click();
  if (
    !(await page.locator("#enableDr").isChecked())
    || (await page.locator("#drRegion").inputValue()) !== "us-phoenix-1"
  ) {
    throw new Error("The second map click did not enable and set the DR region.");
  }
  if (
    !(await page.locator('.oci-region-marker[data-region="us-ashburn-1"]').evaluate(
      (marker) => marker.classList.contains("is-primary"),
    ))
    || !(await page.locator('.oci-region-marker[data-region="us-phoenix-1"]').evaluate(
      (marker) => marker.classList.contains("is-dr"),
    ))
  ) {
    throw new Error("Architecture map selection states were not rendered.");
  }
  if (await page.locator("#splitAcrossADs").isDisabled()) {
    throw new Error("Availability Domain split stayed disabled for a three-AD region.");
  }
  await page.locator("#splitAcrossADsRow").click();
  if (!(await page.locator("#splitAcrossADs").isChecked())) {
    throw new Error("Availability Domain split did not turn on from the visible switch.");
  }
  await page.screenshot({ path: path.join(QA_DIR, "architecture.png"), fullPage: false });

  await page.getByRole("button", { name: "Continue to Deliverables" }).click();
  await page.locator("#deliverablesPage").waitFor();
  if (await page.locator(".deliverables-hero").count()) {
    throw new Error("The removed Deliverables intro block is still present.");
  }
  const activeOnDeliverables = await page.locator(".step.is-active").getAttribute("data-step");
  if (activeOnDeliverables !== "deliverables") {
    throw new Error(`Deliverables content loaded with the wrong active step: ${activeOnDeliverables}`);
  }
  await page.waitForTimeout(250);
  await page.screenshot({ path: path.join(QA_DIR, "deliverables.png"), fullPage: false });

  const bomDownload = page.waitForEvent("download", { timeout: 180000 });
  await page.getByRole("button", { name: "Download BOM", exact: true }).click();
  const downloadedBom = await bomDownload;
  if (!/_BOM(?:_\d{4}-\d{2}-\d{2})?\.xlsx$/.test(downloadedBom.suggestedFilename())) {
    throw new Error(`Unexpected BOM download: ${downloadedBom.suggestedFilename()}`);
  }
  await downloadedBom.saveAs(path.join(QA_DIR, "quick-bom.xlsx"));
  await page.locator("#deliverablesBomStatus").getByText(/Downloaded .*_BOM/).waitFor();

  const architectureDownload = page.waitForEvent("download", { timeout: 120000 });
  await page.getByRole("button", { name: "Download Architecture" }).click();
  const downloadedArchitecture = await architectureDownload;
  if (!downloadedArchitecture.suggestedFilename().endsWith("_architecture.zip")) {
    throw new Error(`Unexpected architecture download: ${downloadedArchitecture.suggestedFilename()}`);
  }
  const architectureZipPath = path.join(QA_DIR, "architecture.zip");
  await downloadedArchitecture.saveAs(architectureZipPath);
  const architectureZipListing = execFileSync("unzip", ["-Z1", architectureZipPath], {
    encoding: "utf8",
  }).trim().split(/\r?\n/);
  if (
    architectureZipListing.length !== 2
    || !architectureZipListing.some((name) => name.endsWith("_architecture.drawio"))
    || !architectureZipListing.some((name) => name.endsWith("_architecture.png"))
  ) {
    throw new Error(
      `Architecture ZIP must contain only draw.io and PNG: ${JSON.stringify(architectureZipListing)}`,
    );
  }
  await page.locator("#deliverablesArchitectureStatus").getByText(/Downloaded .*_architecture\.zip/).waitFor({ timeout: 20000 });
  await page.getByRole("button", { name: "Back to Architecture" }).click();
  await page.getByRole("heading", { name: "Configure the OCI Architecture" }).waitFor();
  await assertNoEmDashes(page, "desktop workflow");

  await page.getByRole("button", { name: "Settings" }).click();
  await page.locator("#darkModeToggle").check();
  await page.keyboard.press("Escape");
  const darkViews = [
    ["review", "#reviewPanel", "dark-review.png"],
    ["shape", "#shapePage", "dark-shape.png"],
    ["networking", "#networkingPage", "dark-services.png"],
    ["price", "#resultsPage", "dark-pricing.png"],
    ["other-clouds", "#otherCloudsPage", "dark-compare.png"],
    ["architecture", "#architecturePage", "dark-architecture.png"],
    ["deliverables", "#deliverablesPage", "dark-deliverables.png"],
  ];
  for (const [step, locator, screenshot] of darkViews) {
    await page.locator(`.step[data-step="${step}"]`).click();
    await page.locator(locator).waitFor({ state: "visible" });
    if (step === "review") {
      const reviewHeaderStyle = await page.locator("#reviewTable th").first().evaluate((header) => {
        const style = getComputedStyle(header);
        return { background: style.backgroundColor, color: style.color };
      });
      if (reviewHeaderStyle.background !== "rgb(32, 59, 64)"
          || reviewHeaderStyle.color !== "rgb(255, 255, 255)") {
        throw new Error(`Dark Review header has invalid contrast: ${JSON.stringify(reviewHeaderStyle)}`);
      }
      const reviewFooterStyle = await page.locator(".table-footer").evaluate((footer) => {
        const style = getComputedStyle(footer);
        return { background: style.backgroundColor, border: style.borderTopColor };
      });
      if (
        reviewFooterStyle.background !== "rgb(36, 46, 47)"
        || reviewFooterStyle.border !== "rgb(60, 73, 75)"
      ) {
        throw new Error(`Dark Review footer palette is invalid: ${JSON.stringify(reviewFooterStyle)}`);
      }
    }
    if (step === "networking") {
      const serviceCostColor = await page.locator("#serviceResults .service-card-cost").first()
        .evaluate((cost) => getComputedStyle(cost).color);
      if (serviceCostColor !== "rgb(100, 213, 154)") {
        throw new Error(`Dark service price has invalid contrast: ${serviceCostColor}`);
      }
    }
    if (step === "price") {
      const priceStyles = await page.evaluate(() => {
        const donut = document.querySelector("#costDonut");
        const donutValue = donut?.querySelector("strong");
        const tableHeader = document.querySelector("#resultsTable th");
        const tableHeaderButton = tableHeader?.querySelector(".sort-header");
        const workloadTrack = document.querySelector("#topWorkloads .bar-track");
        const bulkBar = document.querySelector("#bulkActionBar");
        return {
          donutCenter: donut ? getComputedStyle(donut, "::after").backgroundColor : "",
          donutValue: donutValue ? getComputedStyle(donutValue).color : "",
          tableHeader: tableHeader ? getComputedStyle(tableHeader).backgroundColor : "",
          tableHeaderText: tableHeaderButton ? getComputedStyle(tableHeaderButton).color : "",
          workloadTrack: workloadTrack ? getComputedStyle(workloadTrack).backgroundColor : "",
          bulkBarHidden: bulkBar ? getComputedStyle(bulkBar).display === "none" : false,
        };
      });
      const expectedPriceStyles = {
        donutCenter: "rgb(21, 29, 30)",
        donutValue: "rgb(243, 241, 236)",
        tableHeader: "rgb(32, 59, 64)",
        tableHeaderText: "rgb(255, 255, 255)",
        workloadTrack: "rgb(52, 64, 65)",
        bulkBarHidden: true,
      };
      for (const [property, expected] of Object.entries(expectedPriceStyles)) {
        if (priceStyles[property] !== expected) {
          throw new Error(`Dark Price ${property} mismatch: expected ${expected}, got ${priceStyles[property]}`);
        }
      }
      await page.locator(".cost-mix-panel").screenshot({
        path: path.join(QA_DIR, "dark-cost-mix.png"),
      });
      await page.locator(".result-detail-panel").screenshot({
        path: path.join(QA_DIR, "dark-application-cost-details.png"),
      });
      await page.locator("#exportOverlay").evaluate((overlay) => {
        overlay.hidden = false;
      });
      const exportOverlayStyles = await page.locator("#exportOverlay").evaluate((overlay) => {
        const text = overlay.querySelector(".export-overlay-text");
        const spinner = overlay.querySelector(".export-spinner");
        return {
          card: getComputedStyle(overlay, "::before").backgroundColor,
          text: text ? getComputedStyle(text).color : "",
          spinner: spinner ? getComputedStyle(spinner).borderTopColor : "",
        };
      });
      const expectedExportOverlayStyles = {
        card: "rgb(36, 46, 47)",
        text: "rgb(243, 241, 236)",
        spinner: "rgb(100, 213, 154)",
      };
      for (const [property, expected] of Object.entries(expectedExportOverlayStyles)) {
        if (exportOverlayStyles[property] !== expected) {
          throw new Error(
            `Dark export overlay ${property} mismatch: expected ${expected}, got ${exportOverlayStyles[property]}`,
          );
        }
      }
      await page.screenshot({
        path: path.join(QA_DIR, "dark-export-overlay.png"),
        fullPage: false,
      });
      await page.locator("#exportOverlay").evaluate((overlay) => {
        overlay.hidden = true;
      });
    }
    await page.waitForTimeout(100);
    await page.screenshot({ path: path.join(QA_DIR, screenshot), fullPage: false });
  }
  await page.getByRole("button", { name: "Settings" }).click();
  await page.locator("#darkModeToggle").uncheck();
  await page.keyboard.press("Escape");

  const mobile = await browser.newPage({
    viewport: { width: 390, height: 844 },
    isMobile: true,
  });
  await mobile.goto(APP_URL, { waitUntil: "domcontentloaded" });
  await mobile.screenshot({ path: path.join(QA_DIR, "mobile-landing.png"), fullPage: false });
  await mobile.getByRole("button", { name: "Settings" }).click();
  const mobileSettingsBounds = await mobile.locator("#settingsMenu").evaluate((menu) => {
    const rect = menu.getBoundingClientRect();
    return { left: rect.left, right: rect.right, viewport: window.innerWidth };
  });
  if (mobileSettingsBounds.left < 0 || mobileSettingsBounds.right > mobileSettingsBounds.viewport) {
    throw new Error(`Settings menu is clipped on mobile: ${JSON.stringify(mobileSettingsBounds)}`);
  }
  await mobile.screenshot({ path: path.join(QA_DIR, "mobile-settings.png"), fullPage: false });
  await mobile.keyboard.press("Escape");
  await mobile.setInputFiles("#fileInput", SAMPLE);
  await mobile.locator("#continueToReviewFromUpload:not(:disabled)").waitFor({ timeout: UPLOAD_TIMEOUT_MS });
  await mobile.getByRole("button", { name: "Continue to Review" }).click();
  await mobile.getByText("Adjust Your Table", { exact: true }).waitFor({ timeout: UPLOAD_TIMEOUT_MS });
  await mobile.screenshot({ path: path.join(QA_DIR, "mobile-review.png"), fullPage: false });
  await mobile.getByRole("button", { name: "Continue to Shape" }).click();
  await mobile.getByText("Choose the OCI Shape for This Estimate", { exact: true }).waitFor({ timeout: 20000 });
  await mobile.getByRole("button", { name: "Continue to Services" }).click();
  await mobile.getByRole("heading", { name: "OCI Services" }).waitFor({ timeout: PRICING_TIMEOUT_MS });
  await mobile.locator("#serviceResults .service-group-head").first().waitFor({ timeout: 20000 });
  await mobile.screenshot({ path: path.join(QA_DIR, "mobile-networking.png"), fullPage: false });
  await mobile.getByRole("button", { name: "Continue to Price" }).click();
  await mobile.getByText("OCI Cost Breakdown", { exact: true }).waitFor({ timeout: PRICING_TIMEOUT_MS });
  await mobile.locator("#resultsPage").getByText("Total Contract Value", { exact: true }).waitFor({ timeout: 20000 });
  await mobile.locator("#resultsKpis").getByText("Pricing summary", { exact: true }).waitFor({ timeout: 20000 });
  await mobile.locator("#resultsKpis").getByText("Specs identified", { exact: true }).waitFor({ timeout: 20000 });
  await mobile.screenshot({ path: path.join(QA_DIR, "mobile-pricing.png"), fullPage: false });
  await mobile.locator(".cost-mix-panel").screenshot({ path: path.join(QA_DIR, "mobile-cost-mix.png") });
  await mobile.getByRole("button", { name: "Estimate on Other Clouds", exact: true }).click();
  await mobile.getByRole("heading", { name: "Estimate on Other Clouds" }).waitFor();
  await mobile.locator("#crossCloudResults .cross-cloud-card").first().waitFor();
  await mobile.screenshot({ path: path.join(QA_DIR, "mobile-other-clouds.png"), fullPage: false });
  await mobile.getByRole("button", { name: "Continue to Architecture" }).click();
  await mobile.getByRole("heading", { name: "Configure the OCI Architecture" }).waitFor({ timeout: PRICING_TIMEOUT_MS });
  await mobile.getByRole("button", { name: "Continue to Deliverables" }).waitFor({ timeout: 20000 });
  await mobile.screenshot({ path: path.join(QA_DIR, "mobile-architecture.png"), fullPage: false });
  await mobile.getByRole("button", { name: "Continue to Deliverables" }).click();
  await mobile.locator("#deliverablesPage").waitFor();
  await mobile.getByRole("button", { name: "Download BOM", exact: true }).waitFor();
  await mobile.getByRole("button", { name: "Download Architecture" }).waitFor();
  await mobile.waitForTimeout(250);
  const mobileActiveTab = await assertStepLabelFits(
    mobile,
    "deliverables",
    "Mobile Deliverables",
  );
  if (mobileActiveTab.left < 0 || mobileActiveTab.right > mobileActiveTab.viewport) {
    throw new Error(`Deliverables tab is clipped on mobile: ${JSON.stringify(mobileActiveTab)}`);
  }
  await assertNoEmDashes(mobile, "mobile workflow");
  await mobile.screenshot({ path: path.join(QA_DIR, "mobile-deliverables.png"), fullPage: false });

  await browser.close();
  console.log(
    JSON.stringify(
      {
        ok: true,
        url: APP_URL,
        rows: rowCount,
        columns: columnCount,
        screenshots: [
          "qa/landing.png",
          "qa/dark-settings.png",
          "qa/dark-review.png",
          "qa/dark-shape.png",
          "qa/dark-services.png",
          "qa/dark-pricing.png",
          "qa/dark-cost-mix.png",
          "qa/dark-application-cost-details.png",
          "qa/dark-export-overlay.png",
          "qa/dark-compare.png",
          "qa/dark-architecture.png",
          "qa/dark-deliverables.png",
          "qa/review.png",
          "qa/shape.png",
          "qa/networking.png",
          "qa/services-review.png",
          "qa/pricing.png",
          "qa/cost-mix.png",
          "qa/ramp.png",
          "qa/quick-bom.xlsx",
          "qa/other-clouds.png",
          "qa/architecture.png",
          "qa/deliverables.png",
          "qa/architecture.zip",
          "qa/mobile-landing.png",
          "qa/mobile-settings.png",
          "qa/mobile-review.png",
          "qa/mobile-networking.png",
          "qa/mobile-pricing.png",
          "qa/mobile-cost-mix.png",
          "qa/mobile-other-clouds.png",
          "qa/mobile-architecture.png",
          "qa/mobile-deliverables.png",
        ],
      },
      null,
      2,
    ),
  );
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
