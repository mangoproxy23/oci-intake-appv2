const state = {
  fields: [],
  rows: [],
  rateCard: [],
  rateCards: [],
  fullServiceCatalog: [],
  selectedShape: "e6-standard-ax",
  selectedVendor: "amd",
  lastShapeByVendor: {
    amd: "e6-standard-ax",
  },
  pricing: null,
  uploadReady: false,
  workflowMaxUnlockedStep: 0,
  intakeMode: "on_prem",
  providerHint: "auto",
  uploadMetadata: {},
  fullServiceBeta: false,
  hideGpuPricing: false,
  hideWindowsPricing: false,
  hideSqlPricing: false,
  cpuUnit: "auto",
  // Legacy payload fields remain fixed so server-side fallbacks use the standard month.
  hoursPerMonth: 730,
  hoursOverride: false,
  bomName: "",
  ociDiscount: 0,
  rightsize: false,
  oicMessagePacks: 1,
  // Services the user added from the "Add OCI services" panel. Each: {id, catalogId, name,
  // group, sku, unit, basis, values, monthly}. Included in totals and both BOM exports.
  extraServices: [],
  // Diagram & DR options (region names + availability-domain split for the architecture diagram).
  diagramOptions: {
    primaryRegion: "", drRegion: "", splitADs: false, primaryAds: 1,
    adSplitResources: { vms: true, dbs: true },
    enableDr: false, drReplicate: { vms: true, dbs: true, object: true },
  drSplitADs: false, drAds: 1, drAdSplitResources: { vms: true, dbs: true },
  },
  catalog: { groups: [], results: [], group: "", query: "", groupsOpen: {} },
  shapeOverrides: {},
  costOverrides: {},
  approvedFlags: {},
  flagMenuRow: null,
  hiddenSources: {},
  selectedRows: {},
  crossCloudTopTier: false,
  columnPrefs: {},
  existingInfraCost: 0,
  showMissingOnly: false,
  openaiApiEnabled: false,
  openaiApiConfigured: false,
  openaiApiConnected: false,
  openaiModel: "",
  resultSort: {
    key: "document",
    direction: "asc",
  },
  ramp: {
    months: 12,
    ceiling: 0,
    nextPointId: 1,
    selectedPointId: null,
    points: [],
    restorePending: false,
  },
};

// Restore per-session column visibility choices.
try {
  const savedPrefs = sessionStorage.getItem("ociColumnPrefs");
  if (savedPrefs) state.columnPrefs = JSON.parse(savedPrefs) || {};
} catch (err) {
  state.columnPrefs = {};
}

// Columns that auto-hide when the input has no data for them.
const AUTO_HIDE_COLUMNS = { region: "region", environment: "environment", hours: "hours" };

function autoHiddenColumnSet() {
  const flags = state.pricing?.dataFlags || {};
  const set = new Set();
  for (const [colKey, flagKey] of Object.entries(AUTO_HIDE_COLUMNS)) {
    if (!flags[flagKey]) set.add(colKey);
  }
  return set;
}

function isColumnHidden(key) {
  const pref = state.columnPrefs?.[key];
  if (pref === "show") return false;
  if (pref === "hide") return true;
  return autoHiddenColumnSet().has(key);
}

function saveColumnPrefs() {
  try {
    sessionStorage.setItem("ociColumnPrefs", JSON.stringify(state.columnPrefs || {}));
  } catch (err) {
    /* sessionStorage unavailable */
  }
}

const PROCESSOR_VENDORS = [
  {
    key: "amd",
    label: "AMD",
    description: "AMD-based E-series flexible shapes.",
  },
  {
    key: "intel",
    label: "Intel",
    description: "Intel-based X-series standard and Ax shapes.",
  },
  {
    key: "arm",
    label: "Arm (Ampere)",
    description: "Ampere Arm-based A-series flexible shapes.",
  },
];

let activeFill = null;

const PREVIEW_FIELD_RULES = [
  { key: "application_name", label: "Application Name", contains: ["application name"], required: true },
  { key: "machine_name", label: "Machine Name", containsAny: [["machine name"], ["server name"], ["host name"], ["hostname"], ["vm name"], ["instance name"]], required: true },
  { label: "Environment", contains: ["environment"], required: true },
  { label: "OCPUs", containsAny: [["ocpus per server"], ["ocpu"], ["number of cpu cores per server"], ["number of cpus"], ["vcpu"], ["cpu cores"], ["cores"]], required: true },
  { label: "RAM (GB)", containsAny: [["memory per server"], ["memory"], ["ram"]], required: true },
  { label: "Storage (GB)", containsAny: [["local storage"], ["shared storage"], ["total allocated storage"], ["database size"], ["total storage"], ["storage gb"], ["disk gb"]], required: true },
  { label: "Hours Running", containsAny: [["hours running"], ["hours per month"], ["monthly hours"], ["running hours"], ["uptime hours"], ["hours"]], required: true },
  // Optional: surface the OS column when the inventory has one (shows only if populated). Placed
  // last so it doesn't shift the index alignment between PREVIEW_FIELD_RULES and MANUAL_REVIEW_FIELDS.
  { label: "OS", containsAny: [["operating system"], ["os name"], ["os family"], ["os type"], ["os version"], ["guest os"], ["platform os"]] },
];

const FULL_SERVICE_PREVIEW_FIELD_RULES = [
  { label: "Provider", containsAny: [["source provider"], ["provider"], ["cloud provider"], ["vendor"]] },
  { label: "Source Service", containsAny: [["source service"], ["service name"], ["meter category"], ["product code"]] },
  { label: "Source Product", containsAny: [["source product"], ["usage type"], ["meter name"], ["sku name"], ["item description"]] },
  { label: "Usage Qty", containsAny: [["usage quantity"], ["usage amount"], ["consumed quantity"], ["quantity"]] },
  { label: "Usage Unit", containsAny: [["usage unit"], ["unit of measure"], ["pricing unit"], ["meter unit"]] },
  { label: "Source Cost", containsAny: [["source monthly cost"], ["monthly cost"], ["amortized cost"], ["unblended cost"]] },
];

const CLOUD_BILL_PREVIEW_FIELD_RULES = [
  { label: "Provider", containsAny: [["provider"], ["source provider"], ["cloud provider"]] },
  { label: "Account / Project", containsAny: [["account"], ["project"], ["subscription"]] },
  { label: "Source Service", containsAny: [["source service"], ["service"], ["meter category"], ["product code"]] },
  { label: "SKU / Meter", containsAny: [["sku"], ["meter"], ["usage type"], ["source product"], ["line item description"]] },
  { label: "Region", containsAny: [["region"], ["resource location"], ["location"]] },
  { label: "Usage Qty", containsAny: [["usage quantity"], ["usage amount"], ["quantity"], ["consumed quantity"]] },
  { label: "Usage Unit", containsAny: [["usage unit"], ["unit of measure"], ["pricing unit"], ["unit"]] },
  { label: "OCPUs", containsAny: [["ocpu"], ["vcpu"], ["cpu"], ["core count"], ["cores"]] },
  { label: "RAM (GB)", containsAny: [["ram"], ["memory"], ["memory gb"], ["ram gb"]] },
  { label: "Source Cost", containsAny: [["source cost"], ["source monthly cost"], ["cost"], ["unblended cost"]] },
  { label: "Currency", containsAny: [["currency"], ["billing currency"]] },
  { label: "OCI Service", containsAny: [["oci service"], ["oci service category"], ["target service"]] },
  { label: "OCI Product", containsAny: [["oci product"], ["target product"], ["mapped sku"]] },
  { label: "Confidence", containsAny: [["mapping confidence"], ["confidence"], ["review status"]] },
];

const HOURS_RUNNING_FIELD = { key: "hours_running", label: "Hours Running" };
const REVIEW_IDENTITY_FIELDS = [
  { key: "application_name", label: "Application Name" },
  { key: "machine_name", label: "Machine Name" },
];

const MANUAL_REVIEW_FIELDS = [
  ...REVIEW_IDENTITY_FIELDS,
  { key: "environment", label: "Environment" },
  {
    key: "application_details_number_of_cpu_cores_per_server",
    label: "Application Details: OCPUs",
  },
  { key: "application_details_memory_per_server_gb", label: "Application Details: Memory per server (GB)" },
  { key: "application_details_local_storage_gb", label: "Application Details: Local Storage (GB)" },
  HOURS_RUNNING_FIELD,
];

const els = {
  headerSettings: document.querySelector("#headerSettings"),
  settingsToggle: document.querySelector("#settingsToggle"),
  settingsMenu: document.querySelector("#settingsMenu"),
  darkModeToggle: document.querySelector("#darkModeToggle"),
  fileInput: document.querySelector("#fileInput"),
  dropZone: document.querySelector("#dropZone"),
  modeOnPrem: document.querySelector("#modeOnPrem"),
  modeCloudBill: document.querySelector("#modeCloudBill"),
  modeOtherOciBill: document.querySelector("#modeOtherOciBill"),
  modeEyebrow: document.querySelector("#modeEyebrow"),
  uploadHeading: document.querySelector("#uploadHeading"),
  uploadDescription: document.querySelector("#uploadDescription"),
  dropZoneHint: document.querySelector("#dropZoneHint"),
  providerControl: document.querySelector("#providerControl"),
  providerHint: document.querySelector("#providerHint"),
  uploadStatus: document.querySelector("#uploadStatus"),
  uploadProgress: document.querySelector("#uploadProgress"),
  uploadProgressDetail: document.querySelector("#uploadProgressDetail"),
  uploadPanel: document.querySelector("#uploadPanel"),
  intakePage: document.querySelector("#intakePage"),
  pricingRail: document.querySelector("#pricingRail"),
  shapePage: document.querySelector("#shapePage"),
  networkingPage: document.querySelector("#networkingPage"),
  architecturePage: document.querySelector("#architecturePage"),
  deliverablesPage: document.querySelector("#deliverablesPage"),
  reviewPanel: document.querySelector("#reviewPanel"),
  resultsPage: document.querySelector("#resultsPage"),
  otherCloudsPage: document.querySelector("#otherCloudsPage"),
  reviewTable: document.querySelector("#reviewTable"),
  sheetMeta: document.querySelector("#sheetMeta"),
  rowCount: document.querySelector("#rowCount"),
  columnCount: document.querySelector("#columnCount"),
  approvedCount: document.querySelector("#approvedCount"),
  sheetName: document.querySelector("#sheetName"),
  addRow: document.querySelector("#addRow"),
  addColumn: document.querySelector("#addColumn"),
  addColumnForm: document.querySelector("#addColumnForm"),
  newColumnName: document.querySelector("#newColumnName"),
  cancelAddColumn: document.querySelector("#cancelAddColumn"),
  missingOnlyToggle: document.querySelector("#missingOnlyToggle"),
  missingOnlySummary: document.querySelector("#missingOnlySummary"),
  priceButton: document.querySelector("#priceButton"),
  priceShapeButton: document.querySelector("#priceShapeButton"),
  existingInfraCost: document.querySelector("#existingInfraCost"),
  existingInfraControl: document.querySelector("#existingInfraControl"),
  existingInfraHint: document.querySelector("#existingInfraHint"),
  hideGpuToggle: document.querySelector("#hideGpuToggle"),
  hideWindowsToggle: document.querySelector("#hideWindowsToggle"),
  hideSqlToggle: document.querySelector("#hideSqlToggle"),
  cpuUnitSwitches: document.querySelectorAll(".cpuunit-switch"),
  cpuUnitDetected: document.getElementById("cpuUnitDetected"),
  cpuUnitRow: document.getElementById("cpuUnitRow"),
  exportFullBom: document.querySelector("#exportFullBom"),
  downloadDiagram: document.querySelector("#downloadDiagram"),
  deliverablesFullBom: document.querySelector("#deliverablesFullBom"),
  deliverablesDiagram: document.querySelector("#deliverablesDiagram"),
  loadWorkflow: document.querySelector("#loadWorkflow"),
  loadWorkflowFile: document.querySelector("#loadWorkflowFile"),
  loadPrevBom: document.querySelector("#loadPrevBom"),
  loadWorkflowStatus: document.querySelector("#loadWorkflowStatus"),
  convertBomBtn: document.querySelector("#convertBomBtn"),
  convertBomFile: document.querySelector("#convertBomFile"),
  convertBomStatus: document.querySelector("#convertBomStatus"),
  otherBillSheets: document.querySelector("#otherBillSheets"),
  otherBillSheetSelect: document.querySelector("#otherBillSheetSelect"),
  otherBillWarnings: document.querySelector("#otherBillWarnings"),
  bomName: document.querySelector("#bomName"),
  ociDiscount: document.querySelector("#ociDiscount"),
  sizingSwitch: document.querySelector(".sizing-switch"),
  sizingModeHint: document.querySelector("#sizingModeHint"),
  oicMessagePacks: document.querySelector("#oicMessagePacks"),
  oicMessagePacksControl: document.querySelector("#oicMessagePacksControl"),
  serviceSearch: document.querySelector("#serviceSearch"),
  serviceChips: document.querySelector("#serviceChips"),
  serviceResults: document.querySelector("#serviceResults"),
  serviceCartReview: document.querySelector("#serviceCartReview"),
  serviceCartList: document.querySelector("#serviceCartList"),
  serviceCartCount: document.querySelector("#serviceCartCount"),
  serviceCartTotal: document.querySelector("#serviceCartTotal"),
  crossCloudResults: document.querySelector("#crossCloudResults"),
  selectedDocTile: document.querySelector("#selectedDocTile"),
  selectedDocName: document.querySelector("#selectedDocName"),
  selectedDocSub: document.querySelector("#selectedDocSub"),
  selectedDocClear: document.querySelector("#selectedDocClear"),
  inventoryNotice: document.querySelector("#inventoryNotice"),
  switchToOnPrem: document.querySelector("#switchToOnPrem"),
  continueToReviewFromUpload: document.querySelector("#continueToReviewFromUpload"),
  backToUploadFromReview: document.querySelector("#backToUploadFromReview"),
  backToReviewFromShape: document.querySelector("#backToReviewFromShape"),
  backToShapeFromNetworking: document.querySelector("#backToShapeFromNetworking"),
  continueToPriceFromServices: document.querySelector("#continueToPriceFromServices"),
  backToCompareFromArchitecture: document.querySelector("#backToCompareFromArchitecture"),
  continueToDeliverables: document.querySelector("#continueToDeliverables"),
  backToArchitectureFromDeliverables: document.querySelector("#backToArchitectureFromDeliverables"),
  backToServicesFromPrice: document.querySelector("#backToServicesFromPrice"),
  continueToOtherClouds: document.querySelector("#continueToOtherClouds"),
  backToPriceFromOtherClouds: document.querySelector("#backToPriceFromOtherClouds"),
  continueToArchitectureFromOtherClouds: document.querySelector("#continueToArchitectureFromOtherClouds"),
  networkingPageStatus: document.querySelector("#networkingPageStatus"),
  pricePageStatus: document.querySelector("#pricePageStatus"),
  networkingShape: document.querySelector("#networkingShape"),
  architectureShape: document.querySelector("#architectureShape"),
  otherCloudsShape: document.querySelector("#otherCloudsShape"),
  architectureExportStatus: document.querySelector("#architectureExportStatus"),
  deliverablesBomStatus: document.querySelector("#deliverablesBomStatus"),
  deliverablesArchitectureStatus: document.querySelector("#deliverablesArchitectureStatus"),
  deliverablesBomFilename: document.querySelector("#deliverablesBomFilename"),
  deliverablesArchitectureFilename: document.querySelector("#deliverablesArchitectureFilename"),
  processorPicker: document.querySelector("#processorPicker"),
  shapeDropdown: document.querySelector("#shapeDropdown"),
  shapeVendorTitle: document.querySelector("#shapeVendorTitle"),
  shapeVendorDescription: document.querySelector("#shapeVendorDescription"),
  shapeVendorCount: document.querySelector("#shapeVendorCount"),
  shapeGrid: document.querySelector("#shapeGrid"),
  shapeFamily: document.querySelector("#shapeFamily"),
  shapeDetailTitle: document.querySelector("#shapeDetailTitle"),
  shapeDetailSummary: document.querySelector("#shapeDetailSummary"),
  shapeDetailRates: document.querySelector("#shapeDetailRates"),
  rateCard: document.querySelector("#rateCard"),
  rateCardShape: document.querySelector("#rateCardShape"),
  pricingSummary: document.querySelector("#pricingSummary"),
  engineStatus: document.querySelector("#engineStatus"),
  backToReview: document.querySelector("#backToReview"),
  rerunPricing: document.querySelector("#rerunPricing"),
  resultsShape: document.querySelector("#resultsShape"),
  resultsSubtitle: document.querySelector("#resultsSubtitle"),
  resultsKpis: document.querySelector("#resultsKpis"),
  rampCeilingLabel: document.querySelector("#rampCeilingLabel"),
  rampChart: document.querySelector("#rampChart"),
  rampPeakMonth: document.querySelector("#rampPeakMonth"),
  rampPeakMonthly: document.querySelector("#rampPeakMonthly"),
  addRampPoint: document.querySelector("#addRampPoint"),
  removeRampPoint: document.querySelector("#removeRampPoint"),
  rampThreeYearTotal: document.querySelector("#rampThreeYearTotal"),
  rampAvgMonthly: document.querySelector("#rampAvgMonthly"),
  rampYearOneTotal: document.querySelector("#rampYearOneTotal"),
  rampYearTwoTotal: document.querySelector("#rampYearTwoTotal"),
  rampYearThreeTotal: document.querySelector("#rampYearThreeTotal"),
  rampYearFourTotal: document.querySelector("#rampYearFourTotal"),
  rampYearFiveTotal: document.querySelector("#rampYearFiveTotal"),
  rampYearFourBox: document.querySelector("#rampYearFourBox"),
  rampYearFiveBox: document.querySelector("#rampYearFiveBox"),
  rampContractNote: document.querySelector("#rampContractNote"),
  rampHeading: document.querySelector("#rampHeading"),
  costDonut: document.querySelector("#costDonut"),
  costLegend: document.querySelector("#costLegend"),
  topListHeading: document.querySelector("#topListHeading"),
  topWorkloads: document.querySelector("#topWorkloads"),
  detailHeading: document.querySelector("#detailHeading"),
  resultRowCount: document.querySelector("#resultRowCount"),
  resultsTable: document.querySelector("#resultsTable"),
  priceSpinner: document.querySelector("#priceSpinner"),
  sourceFilterPanel: document.querySelector("#sourceFilterPanel"),
  sourceFilterList: document.querySelector("#sourceFilterList"),
  sourceFilterAll: document.querySelector("#sourceFilterAll"),
  sourceFilterNone: document.querySelector("#sourceFilterNone"),
  bulkActionBar: document.querySelector("#bulkActionBar"),
  bulkSelCount: document.querySelector("#bulkSelCount"),
  bulkCostAction: document.querySelector("#bulkCostAction"),
  bulkApply: document.querySelector("#bulkApply"),
  bulkClear: document.querySelector("#bulkClear"),
  steps: document.querySelectorAll(".step"),
};

const THEME_STORAGE_KEY = "oci-intake-theme";

function applyTheme(theme, persist = true) {
  const nextTheme = theme === "dark" ? "dark" : "light";
  document.documentElement.dataset.theme = nextTheme;
  if (els.darkModeToggle) els.darkModeToggle.checked = nextTheme === "dark";
  if (persist) {
    try {
      localStorage.setItem(THEME_STORAGE_KEY, nextTheme);
    } catch (error) {
      // The theme still applies for this session when storage is unavailable.
    }
  }
}

function setSettingsOpen(open) {
  if (!els.settingsMenu || !els.settingsToggle) return;
  els.settingsMenu.hidden = !open;
  els.settingsToggle.setAttribute("aria-expanded", String(open));
}

(function wireSettingsMenu() {
  if (!els.settingsToggle || !els.settingsMenu) return;
  applyTheme(document.documentElement.dataset.theme, false);

  els.settingsToggle.addEventListener("click", (event) => {
    event.stopPropagation();
    setSettingsOpen(els.settingsMenu.hidden);
  });
  els.darkModeToggle?.addEventListener("change", (event) => {
    applyTheme(event.target.checked ? "dark" : "light");
  });
  document.addEventListener("click", (event) => {
    if (!els.headerSettings?.contains(event.target)) setSettingsOpen(false);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || els.settingsMenu.hidden) return;
    setSettingsOpen(false);
    els.settingsToggle.focus();
  });
})();

// Diagram & DR options: region picks + AD split. The primary region's AD count enables
// the "split across ADs" toggle; a 1-AD region can't split.
const state_diagramOptions_default = {
  primaryRegion: "", drRegion: "", splitADs: false, primaryAds: 1,
  adSplitResources: { vms: true, dbs: true },
  enableDr: false, drReplicate: { vms: true, dbs: true, object: true },
  drSplitADs: false, drAds: 1, drAdSplitResources: { vms: true, dbs: true },
};
function _syncAdSplitResources() {
  // Show the "which resources to split" chips only when AD split is on.
  const on = !!document.querySelector("#splitAcrossADs")?.checked
             && !document.querySelector("#splitAcrossADs")?.disabled;
  const sub = document.querySelector("#adSplitResources");
  if (sub) sub.hidden = !on;
}
function _syncAdSplitControl() {
  const sel = document.querySelector("#primaryRegion");
  const chk = document.querySelector("#splitAcrossADs");
  const hint = document.querySelector("#adSplitHint");
  if (!sel || !chk) return;
  const ads = Number(sel.selectedOptions[0]?.dataset.ads || 1);
  state.diagramOptions.primaryRegion = sel.value;
  state.diagramOptions.primaryAds = ads;
  chk.disabled = ads < 2;
  if (ads < 2) { chk.checked = false; state.diagramOptions.splitADs = false; }
  if (hint) hint.textContent = ads >= 2 ? `${ads} availability domains available` : "Pick a multi-AD region to enable";
  _syncAdSplitResources();
}
document.querySelector("#primaryRegion")?.addEventListener("change", _syncAdSplitControl);
document.querySelector("#splitAcrossADs")?.addEventListener("change", (e) => {
  state.diagramOptions.splitADs = !!e.target.checked;
  _syncAdSplitResources();
});
function _syncAdSplitResourceState() {
  state.diagramOptions.adSplitResources = {
    vms: !!document.querySelector("#adSplitVms")?.checked,
    dbs: !!document.querySelector("#adSplitDbs")?.checked,
  };
}
["#adSplitVms", "#adSplitDbs"].forEach((s) =>
  document.querySelector(s)?.addEventListener("change", _syncAdSplitResourceState));
// The DR region gets the same AD-split control as the primary, gated the same way: a region
// has to actually have multiple availability domains before you can spread standbys across
// them. Single-AD DR regions leave the toggle off and disabled.
function _syncDrAdSplitResources() {
  const chk = document.querySelector("#drSplitAcrossADs");
  const sub = document.querySelector("#drAdSplitResources");
  if (sub) sub.hidden = !(chk && chk.checked && !chk.disabled);
}
function _syncDrAdSplitControl() {
  const sel = document.querySelector("#drRegion");
  const chk = document.querySelector("#drSplitAcrossADs");
  const hint = document.querySelector("#drAdSplitHint");
  if (!sel || !chk) return;
  const ads = Number(sel.selectedOptions[0]?.dataset.ads || 1);
  state.diagramOptions.drRegion = sel.value;
  state.diagramOptions.drAds = ads;
  chk.disabled = ads < 2;
  if (ads < 2) { chk.checked = false; state.diagramOptions.drSplitADs = false; }
  if (hint) {
    hint.textContent = ads >= 2
      ? `${ads} availability domains available`
      : "Pick a multi-AD DR region to enable";
  }
  _syncDrAdSplitResources();
}
document.querySelector("#drRegion")?.addEventListener("change", _syncDrAdSplitControl);
document.querySelector("#drSplitAcrossADs")?.addEventListener("change", (e) => {
  state.diagramOptions.drSplitADs = !!e.target.checked;
  _syncDrAdSplitResources();
});
function _syncDrAdSplitResourceState() {
  state.diagramOptions.drAdSplitResources = {
    vms: !!document.querySelector("#drAdSplitVms")?.checked,
    dbs: !!document.querySelector("#drAdSplitDbs")?.checked,
  };
}
["#drAdSplitVms", "#drAdSplitDbs"].forEach((s) =>
  document.querySelector(s)?.addEventListener("change", _syncDrAdSplitResourceState));
// Enable-DR toggle reveals the DR sub-options (region + which resources to replicate).
document.querySelector("#enableDr")?.addEventListener("change", (e) => {
  const on = !!e.target.checked;
  state.diagramOptions.enableDr = on;
  const sub = document.querySelector("#drSubOptions");
  if (sub) sub.hidden = !on;
});
function _syncDrReplicate() {
  state.diagramOptions.drReplicate = {
    vms: !!document.querySelector("#drRepVms")?.checked,
    dbs: !!document.querySelector("#drRepDbs")?.checked,
    object: !!document.querySelector("#drRepObj")?.checked,
  };
}
["#drRepVms", "#drRepDbs", "#drRepObj"].forEach((sel) =>
  document.querySelector(sel)?.addEventListener("change", _syncDrReplicate));

// Turn a native region <select> into a searchable combobox. The hidden <select> stays the
// source of truth (and keeps firing 'change'), so all the AD-split / DR wiring above is
// untouched. Ordering: Auto pinned first, multi-AD regions next, then the rest A–Z.
function enhanceSearchableSelect(select) {
  if (!select || select.dataset.enhanced) return;
  select.dataset.enhanced = "1";
  const wrap = document.createElement("div");
  wrap.className = "diagram-combo";
  select.parentNode.insertBefore(wrap, select);
  wrap.appendChild(select);
  const input = document.createElement("input");
  input.type = "text";
  input.className = "diagram-combo-input";
  input.setAttribute("role", "combobox");
  input.setAttribute("autocomplete", "off");
  input.setAttribute("aria-expanded", "false");
  input.placeholder = "Search regions…";
  const caret = document.createElement("span");
  caret.className = "diagram-combo-caret";
  caret.textContent = "▾";
  const panel = document.createElement("div");
  panel.className = "diagram-combo-panel";
  panel.setAttribute("role", "listbox");
  wrap.append(input, caret, panel);

  const opts = Array.from(select.options).map((o) => ({
    value: o.value,
    label: o.textContent.trim(),
    ads: Number(o.dataset.ads || 0),
    pin: o.dataset.pin || "",
  }));
  const labelFor = (v) => (opts.find((o) => o.value === v) || opts[0] || { label: "" }).label;
  const syncInputText = () => { input.value = labelFor(select.value); };

  let rendered = [];
  let activeIdx = -1;

  function groups(filter) {
    const f = filter.trim().toLowerCase();
    const match = (o) => !f || o.label.toLowerCase().includes(f);
    const auto = opts.filter((o) => o.pin === "auto" && match(o));
    const body = opts.filter((o) => !o.pin && match(o));
    const three = body.filter((o) => o.ads >= 3);   // keep launch order (Ashburn/Phoenix/London/Frankfurt)
    const rest = body.filter((o) => o.ads < 3).sort((a, b) => a.label.localeCompare(b.label));
    return { auto, three, rest, filtering: f.length > 0 };
  }
  function render(filter) {
    const { auto, three, rest, filtering } = groups(filter);
    panel.innerHTML = "";
    rendered = [];
    const addOpt = (o) => {
      const el = document.createElement("div");
      el.className = "diagram-combo-opt";
      el.setAttribute("role", "option");
      el.setAttribute("aria-selected", String(o.value === select.value));
      const txt = document.createElement("span");
      txt.textContent = o.label;
      el.appendChild(txt);
      if (o.ads >= 3) {
        const b = document.createElement("span");
        b.className = "diagram-combo-badge";
        b.textContent = o.ads + " ADs";
        el.appendChild(b);
      }
      const idx = rendered.length;
      el.addEventListener("mousedown", (e) => { e.preventDefault(); choose(o.value); });
      el.addEventListener("mousemove", () => { activeIdx = idx; paintActive(); });
      panel.appendChild(el);
      rendered.push({ el, value: o.value });
    };
    const addSep = (t) => {
      const s = document.createElement("div");
      s.className = "diagram-combo-sep";
      s.textContent = t;
      panel.appendChild(s);
    };
    auto.forEach(addOpt);
    if (three.length && !filtering) addSep("Multi-AD regions");
    three.forEach(addOpt);
    if (rest.length && !filtering && three.length) addSep("Other regions (A–Z)");
    rest.forEach(addOpt);
    if (!rendered.length) {
      const e = document.createElement("div");
      e.className = "diagram-combo-empty";
      e.textContent = "No matching region";
      panel.appendChild(e);
    }
    activeIdx = rendered.findIndex((r) => r.value === select.value);
    paintActive();
  }
  function paintActive() {
    rendered.forEach((r, i) => r.el.classList.toggle("is-active", i === activeIdx));
    if (activeIdx >= 0) rendered[activeIdx].el.scrollIntoView({ block: "nearest" });
  }
  function open() {
    wrap.classList.add("is-open");
    input.setAttribute("aria-expanded", "true");
    render("");
    input.select();
  }
  function close() {
    wrap.classList.remove("is-open");
    input.setAttribute("aria-expanded", "false");
    syncInputText();
  }
  function choose(value) {
    if (select.value !== value) {
      select.value = value;
      select.dispatchEvent(new Event("change", { bubbles: true }));
    }
    close();
    input.blur();
  }
  input.addEventListener("focus", open);
  input.addEventListener("click", () => { if (!wrap.classList.contains("is-open")) open(); });
  input.addEventListener("input", () => { wrap.classList.add("is-open"); render(input.value); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!wrap.classList.contains("is-open")) return open();
      activeIdx = Math.min(activeIdx + 1, rendered.length - 1); paintActive();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      activeIdx = Math.max(activeIdx - 1, 0); paintActive();
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (activeIdx >= 0 && rendered[activeIdx]) choose(rendered[activeIdx].value);
    } else if (e.key === "Escape") {
      close(); input.blur();
    }
  });
  input.addEventListener("blur", () => { setTimeout(close, 120); });
  select.addEventListener("change", syncInputText);
  syncInputText();
}
document.querySelectorAll('select[data-searchable="region"]').forEach(enhanceSearchableSelect);
_syncAdSplitControl();
_syncDrAdSplitControl();

function rowSourceName(row) {
  return row.fullServiceMapping?.sourceService || row.sourceService || fallbackEntityName(row, "Source line");
}

const WORKFLOW_STEP_ORDER = [
  "upload",
  "review",
  "shape",
  "networking",
  "price",
  "other-clouds",
  "architecture",
  "deliverables",
];

const WORKFLOW_STEP_LABELS = {
  upload: "Upload",
  review: "Review",
  shape: "Shape",
  networking: "Services",
  price: "Price",
  "other-clouds": "Compare",
  architecture: "Architecture",
  deliverables: "Deliverables",
};

function workflowStepIndex(step) {
  return WORKFLOW_STEP_ORDER.indexOf(step);
}

function isWorkflowStepUnlocked(step) {
  const index = workflowStepIndex(step);
  return index >= 0 && index <= Number(state.workflowMaxUnlockedStep || 0);
}

function setWorkflowButtonLock(button, locked, message = "") {
  if (!button) return;
  button.disabled = Boolean(locked);
  if (locked && message) {
    button.dataset.workflowLock = "true";
    button.title = message;
  } else if (button.dataset.workflowLock === "true") {
    delete button.dataset.workflowLock;
    button.removeAttribute("title");
  }
}

function syncWorkflowAvailability() {
  const highest = Math.max(
    0,
    Math.min(WORKFLOW_STEP_ORDER.length - 1, Number(state.workflowMaxUnlockedStep) || 0),
  );
  state.workflowMaxUnlockedStep = highest;

  els.steps.forEach((item) => {
    const step = item.dataset.step;
    const idx = workflowStepIndex(step);
    const locked = idx > highest;
    // Keep the IMMEDIATELY-next tab clickable so it can act as the page's forward button
    // (a disabled <button> swallows click events); tabs further ahead stay disabled.
    const isNext = idx === highest + 1;
    item.disabled = locked && !isNext;
    item.classList.toggle("is-locked", locked);
    item.classList.toggle("is-next", isNext);
    if (locked) {
      const previous = WORKFLOW_STEP_ORDER[idx - 1];
      item.title = isNext
        ? `Continue to ${WORKFLOW_STEP_LABELS[step]}`
        : `Complete ${WORKFLOW_STEP_LABELS[previous]} to unlock ${WORKFLOW_STEP_LABELS[step]}.`;
    } else {
      item.removeAttribute("title");
    }
  });

  const hasUploadedRows = state.uploadReady && state.rows.length > 0;
  const hasApprovedRows = state.rows.some((row) => row.__approved !== false);
  setWorkflowButtonLock(
    els.continueToReviewFromUpload,
    !hasUploadedRows,
    "Choose and finish parsing a spreadsheet first.",
  );
  setWorkflowButtonLock(
    els.priceButton,
    !isWorkflowStepUnlocked("review") || !hasApprovedRows,
    "Keep at least one reviewed row approved to continue.",
  );
  setWorkflowButtonLock(
    els.priceShapeButton,
    !isWorkflowStepUnlocked("shape") || !state.selectedShape || !hasApprovedRows,
    "Choose an OCI shape to continue.",
  );
  setWorkflowButtonLock(
    els.continueToPriceFromServices,
    !isWorkflowStepUnlocked("networking") || !state.pricing,
    "Prepare workload pricing from Shape first.",
  );
  setWorkflowButtonLock(
    els.continueToOtherClouds,
    !isWorkflowStepUnlocked("price") || !state.pricing,
    "Prepare the OCI estimate first.",
  );
  setWorkflowButtonLock(
    els.continueToArchitectureFromOtherClouds,
    !isWorkflowStepUnlocked("other-clouds") || !state.pricing,
    "View the cloud comparison first.",
  );
  setWorkflowButtonLock(
    els.continueToDeliverables,
    !isWorkflowStepUnlocked("architecture") || !state.pricing,
    "Configure the architecture first.",
  );
}

function unlockWorkflowStep(step) {
  const index = workflowStepIndex(step);
  if (index < 0) return;
  state.workflowMaxUnlockedStep = Math.max(Number(state.workflowMaxUnlockedStep) || 0, index);
  syncWorkflowAvailability();
}

function resetWorkflowProgress() {
  state.uploadReady = false;
  state.workflowMaxUnlockedStep = 0;
  syncWorkflowAvailability();
}

function setStep(step) {
  els.steps.forEach((item) => {
    const isActive = item.dataset.step === step;
    item.classList.toggle("is-active", isActive);
    if (isActive) {
      item.setAttribute("aria-current", "step");
      const nav = item.parentElement;
      if (nav && nav.scrollWidth > nav.clientWidth) {
        const left = Math.max(0, item.offsetLeft + item.offsetWidth - nav.clientWidth);
        nav.scrollTo({ left, behavior: "smooth" });
      }
    } else {
      item.removeAttribute("aria-current");
    }
  });
  syncWorkflowAvailability();
}

function setPricePageStatus(message = "", tone = "") {
  if (!els.pricePageStatus) return;
  els.pricePageStatus.textContent = message;
  els.pricePageStatus.hidden = !message;
  if (tone) {
    els.pricePageStatus.dataset.tone = tone;
  } else {
    delete els.pricePageStatus.dataset.tone;
  }
}

function setNetworkingPageStatus(message = "", tone = "") {
  if (!els.networkingPageStatus) return;
  els.networkingPageStatus.textContent = message;
  els.networkingPageStatus.hidden = !message;
  if (tone) {
    els.networkingPageStatus.dataset.tone = tone;
  } else {
    delete els.networkingPageStatus.dataset.tone;
  }
}

function formatCurrency(value) {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(Number(value || 0));
}

function formatCompactCurrency(value) {
  const amount = Number(value || 0);
  if (Math.abs(amount) < 10000) return formatCurrency(amount);
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    notation: "compact",
    maximumFractionDigits: Math.abs(amount) >= 1000000 ? 2 : 1,
  }).format(amount);
}

function formatCompactNumber(value) {
  const amount = Number(value || 0);
  if (Math.abs(amount) < 10000) return formatNumber(amount);
  return new Intl.NumberFormat("en-US", {
    notation: "compact",
    maximumFractionDigits: Math.abs(amount) >= 1000000 ? 2 : 1,
  }).format(amount);
}

function formatKpiQuantity(value, unit) {
  return `${formatCompactNumber(value)} ${unit}`;
}

function formatNumber(value) {
  return new Intl.NumberFormat("en-US", {
    maximumFractionDigits: 2,
  }).format(Number(value || 0));
}

function clamp(value, min, max) {
  return Math.min(Math.max(Number(value) || 0, min), max);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function percent(value, total) {
  if (!total) return 0;
  return Math.max(0, Math.min(100, (Number(value || 0) / total) * 100));
}

function normalizeText(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

function fieldKeyFromLabel(label) {
  const slug = normalizeText(label).replace(/\s+/g, "_") || "column";
  return `custom_${slug}`;
}

function uniqueFieldKey(label) {
  const existing = new Set(state.fields.map((field) => field.key));
  const base = fieldKeyFromLabel(label);
  let key = base;
  let index = 2;
  while (existing.has(key)) {
    key = `${base}_${index}`;
    index += 1;
  }
  return key;
}

function isHoursRunningField(field) {
  const label = normalizeText(field?.label || field?.sourceColumn || "");
  return label === "hours"
    || label.includes("hours running")
    || label.includes("hours per month")
    || label.includes("monthly hours")
    || label.includes("running hours")
    || label.includes("uptime hours");
}

function ensureHoursRunningReviewField() {
  if (isCloudBillMode()) return;
  let field = state.fields.find(isHoursRunningField);
  if (!field) {
    const key = state.fields.some((item) => item.key === HOURS_RUNNING_FIELD.key)
      ? uniqueFieldKey(HOURS_RUNNING_FIELD.label)
      : HOURS_RUNNING_FIELD.key;
    field = {
      ...HOURS_RUNNING_FIELD,
      key,
      sourceColumn: null,
      important: true,
      manual: true,
    };
    state.fields.push(field);
  }
  state.rows.forEach((row) => {
    if (!hasCellContent(row[field.key])) row[field.key] = 730;
  });
}

function isMachineIdentityLabel(value) {
  const label = normalizeText(value);
  return [
    "machine name",
    "machine id",
    "server name",
    "hostname",
    "host name",
    "vm name",
    "virtual machine name",
    "instance name",
    "asset name",
  ].some((term) => label.includes(term));
}

function isApplicationIdentityLabel(value) {
  const label = normalizeText(value);
  return ["application name", "application", "app name", "app"].includes(label);
}

function ensureIdentityReviewFields() {
  if (isCloudBillMode()) return;
  let applicationField = state.fields.find((field) => field?.key === "application_name");
  let machineField = state.fields.find((field) => field?.key === "machine_name");
  const applicationSourceFields = state.fields.filter(
    (field) => field?.key !== "application_name"
      && isApplicationIdentityLabel(field?.sourceHeader || field?.label),
  );
  const machineSourceFields = state.fields.filter(
    (field) => field?.key !== "machine_name" && isMachineIdentityLabel(field?.sourceHeader || field?.label),
  );

  // Older saved workflows mapped server/host columns into application_name.
  // Move those values into the new machine column when the source header is unambiguous.
  if (applicationField && isMachineIdentityLabel(applicationField.sourceHeader || applicationField.label)) {
    if (!machineField) {
      machineField = {
        ...REVIEW_IDENTITY_FIELDS[1],
        sourceColumn: applicationField.sourceColumn || null,
        sourceHeader: applicationField.sourceHeader || null,
        important: true,
        manual: true,
      };
      state.fields.push(machineField);
    }
    state.rows.forEach((row) => {
      if (!hasCellContent(row[machineField.key]) && hasCellContent(row[applicationField.key])) {
        row[machineField.key] = row[applicationField.key];
        row[applicationField.key] = "";
      }
    });
    applicationField.sourceColumn = null;
    applicationField.sourceHeader = null;
  }

  REVIEW_IDENTITY_FIELDS.forEach((definition) => {
    let field = state.fields.find((item) => item?.key === definition.key);
    if (!field) {
      const sourceFields = definition.key === "machine_name"
        ? machineSourceFields
        : applicationSourceFields;
      const sourceField = sourceFields[0] || null;
      field = {
        ...definition,
        sourceColumn: sourceField?.sourceColumn || null,
        sourceHeader: sourceField?.sourceHeader || sourceField?.label || null,
        important: true,
        manual: true,
      };
      state.fields.push(field);
    }
    field.requiredIdentity = true;
    state.rows.forEach((row) => {
      if (!(field.key in row)) row[field.key] = "";
    });
  });

  applicationField = state.fields.find((field) => field?.key === "application_name");
  if (applicationField && applicationSourceFields.length) {
    state.rows.forEach((row) => {
      if (hasCellContent(row[applicationField.key])) return;
      const sourceField = applicationSourceFields.find((field) => hasCellContent(row[field.key]));
      if (sourceField) row[applicationField.key] = row[sourceField.key];
    });
  }

  machineField = state.fields.find((field) => field?.key === "machine_name");
  if (machineField && machineSourceFields.length) {
    state.rows.forEach((row) => {
      if (hasCellContent(row[machineField.key])) return;
      const sourceField = machineSourceFields.find((field) => hasCellContent(row[field.key]));
      if (sourceField) row[machineField.key] = row[sourceField.key];
    });
  }
}

function ensureFixedReviewContractFields() {
  if (isCloudBillMode()) return;
  ensureIdentityReviewFields();
  ensureHoursRunningReviewField();
  PREVIEW_FIELD_RULES.forEach((rule, index) => {
    if (findField(rule)) return;
    const fallback = MANUAL_REVIEW_FIELDS[index];
    if (!fallback) return;
    const key = state.fields.some((field) => field?.key === fallback.key)
      ? uniqueFieldKey(fallback.label)
      : fallback.key;
    const field = {
      ...fallback,
      key,
      sourceColumn: null,
      sourceHeader: null,
      important: true,
      manual: true,
    };
    state.fields.push(field);
    state.rows.forEach((row) => {
      row[key] = isHoursRunningField(field) ? 730 : "";
    });
  });
}

function isManualField(field) {
  return Boolean(field?.manual || field?.userAdded);
}

function hasCellContent(value) {
  return value !== null && value !== undefined && String(value).trim() !== "";
}

// OCPU / RAM only count as "missing" for compute rows. In cloud-bill mode a storage,
// network, or other non-compute line legitimately has no OCPU/RAM, so leave it blank.
function cellIsMissing(row, fieldKey, valueOverride) {
  const value = valueOverride !== undefined ? valueOverride : row[fieldKey];
  if (hasCellContent(value)) return false;
  if (state.intakeMode === "cloud_bill" && (fieldKey === "resource_ocpus" || fieldKey === "resource_memory_gb")) {
    const prod = String(row.oci_product || "").toLowerCase();
    const isCompute = prod.includes("virtual machine") || prod.includes("compute") || prod.includes("container instance");
    return isCompute; // blank OCPU/RAM is only "missing" when the row is compute
  }
  return true;
}

function fieldHasContent(field) {
  if (!field?.key) return false;
  return state.rows.some((row) => hasCellContent(row[field.key]));
}

function shouldShowField(field) {
  return isManualField(field) || fieldHasContent(field);
}

function rowHasMissingData(row, fields = previewFields()) {
  return fields.some((field) => cellIsMissing(row, field.key));
}

function reviewRowEntries(fields = previewFields()) {
  const entries = state.rows.map((row, rowIndex) => ({ row, rowIndex }));
  if (!state.showMissingOnly) return entries;
  return entries.filter(({ row }) => rowHasMissingData(row, fields));
}

function missingDataRowCount(fields = previewFields()) {
  return state.rows.filter((row) => rowHasMissingData(row, fields)).length;
}

function syncMissingFilterUi(fields = previewFields()) {
  const missingCount = missingDataRowCount(fields);
  if (els.missingOnlyToggle) {
    els.missingOnlyToggle.checked = state.showMissingOnly;
    els.missingOnlyToggle.disabled = !state.rows.length || !fields.length;
  }
  if (els.missingOnlySummary) {
    const rowLabel = missingCount === 1 ? "row" : "rows";
    els.missingOnlySummary.textContent = state.showMissingOnly
      ? `${formatNumber(missingCount)} ${rowLabel} with blanks shown`
      : `${formatNumber(missingCount)} ${rowLabel} with blanks`;
  }
}

function selectedShape() {
  return (
    state.rateCards.find((shape) => shape.key === state.selectedShape) ||
    state.rateCards[0] || {
      key: state.selectedShape,
      label: "Selected shape",
      family: "OCI flex shape",
      summary: "Selected shape rates will be applied to approved rows.",
      computeRate: 0,
      memoryRate: 0,
      rateCard: state.rateCard,
    }
  );
}

function normalizeVendorKey(value) {
  const vendor = String(value || "").toLowerCase();
  if (vendor.includes("intel")) return "intel";
  if (vendor.includes("amd")) return "amd";
  if (vendor.includes("arm") || vendor.includes("ampere")) return "arm";
  return "";
}

function shapeVendor(shape = {}) {
  const explicit = normalizeVendorKey(shape.processorVendor || shape.vendor || shape.processor);
  if (explicit) return explicit;
  const text = normalizeText([shape.key, shape.label, shape.family].filter(Boolean).join(" "));
  if (text.includes("intel") || text.includes("x9")) return "intel";
  return "amd";
}

function vendorDefinition(vendorKey = state.selectedVendor) {
  return PROCESSOR_VENDORS.find((vendor) => vendor.key === vendorKey) || PROCESSOR_VENDORS[0];
}

function shapesForVendor(vendorKey = state.selectedVendor) {
  return state.rateCards.filter((shape) => shapeVendor(shape) === vendorKey);
}

function syncVendorForSelectedShape() {
  const shape = selectedShape();
  state.selectedVendor = shapeVendor(shape);
  if (shape?.key) {
    state.lastShapeByVendor[state.selectedVendor] = shape.key;
  }
}

function displayRateCard(rateCard = state.rateCard) {
  if (!state.fullServiceBeta) return rateCard || [];
  const items = [...(rateCard || [])];
  const seen = new Set(items.map((item) => item.sku));
  state.fullServiceCatalog.forEach((item) => {
    if (seen.has(item.sku)) return;
    items.push({
      sku: item.sku,
      description: item.description,
      unit: item.unit,
      rate: item.rate,
      notes: item.notes || item.category || item.unit,
    });
    seen.add(item.sku);
  });
  return items;
}

function isCloudBillMode() {
  return state.intakeMode === "cloud_bill";
}

// Other OCI Bill: a finished OCI BOM from somewhere else (Oracle's cost estimator, a partner, an
// older export of this app). It skips inventory parsing entirely - the file already carries OCI
// SKUs, quantities and prices, so it goes straight to the converter and lands on the Shape page.
//
// Workbooks exported before this mode was renamed carry intakeMode "foreign_bom" in their hidden
// workflow sheet. New files write "other_oci_bill"; the legacy value is migrated on the way in,
// so a BOM saved by any build still reloads into the right mode.
const LEGACY_INTAKE_MODES = { foreign_bom: "other_oci_bill" };

function normalizeIntakeMode(mode) {
  const m = LEGACY_INTAKE_MODES[mode] || mode;
  return m === "cloud_bill" || m === "other_oci_bill" ? m : "on_prem";
}

function isOtherOciBillMode() {
  return state.intakeMode === "other_oci_bill";
}

function providerLabel(value = state.providerHint) {
  const labels = {
    auto: "Auto-Detect",
    aws: "AWS",
    azure: "Azure",
    gcp: "GCP",
  };
  return labels[value] || "Auto-Detect";
}

// Display name for the bill's source cloud ("AWS"/"Azure"/"GCP", else "Source").
function cloudDisplayName(key = state.pricing?.sourceCloud) {
  const k = String(key || "").toLowerCase();
  return k === "aws" ? "AWS" : k === "azure" ? "Azure" : k === "gcp" ? "GCP" : "Source";
}

// True when the current pricing filled the source cost from usage (bill had no pricing).
function sourceCostIsEstimated() {
  return Boolean(state.pricing?.sourceCostEstimated);
}

// Label for any "<cloud> cost" / "current cost" spot: append "(App Estimate)" when the figure
// was reconstructed by the app because the uploaded bill carried no pricing.
function sourceCostLabel(base = "Source Cost") {
  return sourceCostIsEstimated() ? `${cloudDisplayName()} Cost (App Estimate)` : base;
}

function pricingActionLabel(action = "price") {
  return action === "rerun" ? "Reprice Estimate" : "Price Estimate";
}

function syncApiUi() {
  if (els.priceShapeButton && !els.priceShapeButton.disabled) {
    els.priceShapeButton.textContent = "Continue to Services";
  }
  if (els.rerunPricing && !els.rerunPricing.disabled) {
    els.rerunPricing.textContent = pricingActionLabel("rerun");
  }
  syncModeUi();
  if (!state.rows.length && els.engineStatus) {
    els.engineStatus.textContent = state.openaiApiConnected
      ? `OpenAI ready for uploads, unresolved bill mappings, and architecture: ${state.openaiModel || "configured model"}`
      : state.openaiApiEnabled
        ? "OpenAI API key missing"
        : "OpenAI temporarily disconnected";
  }
}

function processorLogo(key) {
  if (key === "amd") return `<span class="processor-logo amd-logo"><span>AMD</span><i aria-hidden="true"></i></span>`;
  if (key === "intel") return `<span class="processor-logo intel-logo"><span>intel</span></span>`;
  if (key === "arm") {
    return `<span class="processor-logo arm-logo"><span>Ampere</span></span>`;
  }
  return "";
}

function renderProcessorPicker() {
  if (!els.processorPicker) return;
  const vendorTiles = PROCESSOR_VENDORS.map((vendor) => {
    const shapeCount = shapesForVendor(vendor.key).length;
    const countLabel = `${formatNumber(shapeCount)} ${shapeCount === 1 ? "shape" : "shapes"}`;
    const isSelected = vendor.key === state.selectedVendor;
    return `
      <button
        class="processor-button ${isSelected ? "is-selected" : ""}"
        type="button"
        data-processor-vendor="${escapeHtml(vendor.key)}"
        aria-expanded="${isSelected ? "true" : "false"}"
        aria-controls="shapeDropdown"
      >
        ${processorLogo(vendor.key)}
        <em>${escapeHtml(countLabel)}</em>
      </button>
    `;
  }).join("");

  els.processorPicker.innerHTML = vendorTiles;

  els.processorPicker.querySelectorAll("[data-processor-vendor]").forEach((button) => {
    button.addEventListener("click", () => {
      setProcessorVendor(button.dataset.processorVendor);
      renderProcessorPicker();
    });
  });
}

function renderShapeVendorMeta() {
  const vendor = vendorDefinition();
  const shapes = shapesForVendor(vendor.key);
  const shapeCount = shapes.length;
  if (els.shapeVendorTitle) {
    els.shapeVendorTitle.textContent = `${vendor.label} Shapes`;
  }
  if (els.shapeVendorDescription) {
    els.shapeVendorDescription.textContent = vendor.description;
  }
  if (els.shapeVendorCount) {
    els.shapeVendorCount.textContent = `${formatNumber(shapeCount)} ${shapeCount === 1 ? "shape" : "shapes"}`;
  }
  if (els.shapeDropdown) {
    els.shapeDropdown.dataset.vendor = vendor.key;
  }
}

function setProcessorVendor(vendorKey) {
  const vendor = vendorDefinition(vendorKey).key;
  state.selectedVendor = vendor;
  const shapes = shapesForVendor(vendor);
  const rememberedShape = shapes.find((shape) => shape.key === state.lastShapeByVendor[vendor]);
  const currentShapeInVendor = shapes.some((shape) => shape.key === state.selectedShape);
  const targetShape = currentShapeInVendor ? selectedShape() : rememberedShape || shapes[0];
  if (targetShape && targetShape.key !== state.selectedShape) {
    setShape(targetShape.key);
    return;
  }
  renderProcessorPicker();
  renderShapeChoices();
  renderShapeDetail();
}

function setShape(shapeKey) {
  const shape = state.rateCards.find((item) => item.key === shapeKey);
  if (!shape) return;
  state.selectedShape = shape.key;
  state.selectedVendor = shapeVendor(shape);
  state.lastShapeByVendor[state.selectedVendor] = shape.key;
  state.rateCard = shape.rateCard || [];
  state.pricing = null;
  syncWorkflowAvailability();
  renderRateCard();
  renderProcessorPicker();
  renderShapeChoices();
  renderShapeDetail();
  els.engineStatus.textContent = `${shape.label} selected`;
}

async function fetchJson(url, options = {}, timeoutMs = 60000) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    const responseText = await response.text();
    let payload;
    try {
      payload = responseText ? JSON.parse(responseText) : {};
    } catch {
      const tooLarge =
        response.status === 413
        || /request entity too large|payload too large/i.test(responseText);
      if (tooLarge) {
        throw new Error(
          "This file is too large for the upload gateway. Large CSV and TSV bills are compressed automatically; refresh the app and try again.",
        );
      }
      throw new Error(
        response.ok
          ? "The server returned an unreadable response."
          : `The server could not complete the request (${response.status}).`,
      );
    }
    return { response, payload };
  } catch (error) {
    if (error.name === "AbortError") {
      throw new Error("The request took too long. Please refresh and try again.");
    }
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
}

const SIZING_MARKER_BY_LABEL = {
  "OCPUs": "cpuSourceLabel",
  "RAM (GB)": "memorySourceLabel",
  "Storage (GB)": "storageSourceLabel",
};

function findField(rule) {
  if (rule.key) {
    const keyedMatch = state.fields.find((field) => field?.key === rule.key);
    if (keyedMatch) return { ...keyedMatch, label: rule.label };
  }
  // Prefer the column the server tagged as the actual sizing source, so the review table
  // shows the same CPU / memory / storage column the parser sized and priced on (e.g. a
  // "Rationalized Cores" column, or "Disk in GB" storage) rather than a raw-vCPU / ratio
  // column that merely shares a keyword, or an empty fallback when the header wording differs.
  const marker = SIZING_MARKER_BY_LABEL[rule.label];
  if (marker) {
    const tagged = state.fields.find(
      (field) => field && field[marker] && state.rows.some((row) => hasCellContent(row[field.key]))
    );
    if (tagged) return { ...tagged, label: rule.label };
  }
  const matchGroups = (rule.containsAny || [rule.contains || []]).map((group) => group.map(normalizeText));
  const section = normalizeText(rule.section);
  const match = state.fields.find((field) => {
    const label = ` ${normalizeText(field.label)} `;
    if (section && !label.trim().startsWith(section)) return false;
    return matchGroups.some((terms) => terms.every((term) => label.includes(` ${term} `) || label.includes(term)));
  });
  return match ? { ...match, label: rule.label } : null;
}

function previewFields() {
  ensureFixedReviewContractFields();
  const rules = isCloudBillMode()
    ? CLOUD_BILL_PREVIEW_FIELD_RULES
    : state.fullServiceBeta
    ? [PREVIEW_FIELD_RULES[0], ...FULL_SERVICE_PREVIEW_FIELD_RULES, ...PREVIEW_FIELD_RULES.slice(1)]
    : PREVIEW_FIELD_RULES;
  const fields = [];
  const seen = new Set();
  rules.forEach((rule) => {
    const field = findField(rule);
    if (field && !seen.has(field.key) && (rule.required || shouldShowField(field))) {
      fields.push(field);
      seen.add(field.key);
    }
  });
  state.fields.forEach((field) => {
    if (!field?.key || seen.has(field.key) || !isManualField(field)) return;
    fields.push(field);
    seen.add(field.key);
  });
  return fields;
}

function syncModeUi() {
  const cloudBill = isCloudBillMode();
  const otherBill = isOtherOciBillMode();
  if (typeof syncExistingInfraUi === "function") syncExistingInfraUi();
  state.fullServiceBeta = cloudBill;
  els.modeOnPrem?.classList.toggle("is-selected", !cloudBill && !otherBill);
  els.modeCloudBill?.classList.toggle("is-selected", cloudBill);
  els.modeOtherOciBill?.classList.toggle("is-selected", otherBill);
  els.providerControl?.classList.toggle("is-hidden", !cloudBill);
  // OIC message packs only apply in cloud-bill mode (SQS/SNS/Transfer Family mapping).
  els.oicMessagePacksControl?.classList.toggle("is-hidden", !cloudBill);
  if (els.providerHint) {
    els.providerHint.value = state.providerHint;
  }
  // Cloud bill accepts several formats; Chrome can grey out CSV/TSV even when listed,
  // so don't filter at all here - the backend validates the file type on upload.
  els.fileInput.accept = cloudBill ? "" : otherBill ? ".xlsx,.xls,.csv,.tsv,.json" : ".xlsx,.xls";
  els.modeEyebrow.textContent = cloudBill ? "Cloud Bill" : otherBill ? "Other OCI Bill" : "On-Prem Inventory";
  els.uploadHeading.textContent = cloudBill
    ? "Upload Cloud Bill"
    : otherBill ? "Import Other OCI Bill" : "Upload Inventory";
  els.uploadDescription.textContent = cloudBill
    ? "Upload an AWS, Azure, or GCP bill export. PDF invoices and CSV, TSV, or Excel exports are mapped to OCI-equivalent services and meters."
    : otherBill
    ? "Upload an existing BOM from the cost estimator tool, or other formats. Every OCI SKU and line item is recognized against the app's catalog, re-priced, and loaded live into your results - a BOM this app exported is restored in full instead."
    : state.openaiApiConnected
    ? "Drop an Excel workbook here. OpenAI can inspect the workbook, choose the inventory table, and normalize server/application fields for review."
    : "Drop an Excel workbook here. The local parser will choose the inventory table and normalize CPU, RAM, storage, environment, and application fields for review.";
  els.dropZone.querySelector("strong").textContent = cloudBill
    ? "Choose Bill Export"
    : otherBill ? "Choose BOM" : "Choose Spreadsheet";
  els.dropZoneHint.textContent = cloudBill
    ? "or drag a PDF, CSV, TSV, or Excel bill export onto this upload area"
    : otherBill
    ? "Cost estimator proposal, a partner BOM, or an export from this app (.xlsx / .csv)"
    : "or drag the workbook onto this upload area";
}

function setIntakeMode(mode) {
  state.intakeMode = normalizeIntakeMode(mode);
  state.providerHint = state.intakeMode === "cloud_bill" ? state.providerHint : "auto";
  clearIntakeStatuses();   // switching intake path - drop stale load/convert banners
  syncModeUi();
  state.pricing = null;
  syncWorkflowAvailability();
  if (state.rows.length) {
    renderTable();
    renderShapeDetail();
    els.engineStatus.textContent = isCloudBillMode() ? "Cloud bill mode" : "On-prem inventory mode";
    els.pricingSummary.className = "empty-state";
    els.pricingSummary.textContent = isCloudBillMode()
      ? "Review source bill lines and OCI target mappings, then choose the OCI flex shape to price mapped usage."
      : "Review the rows, make adjustments, then choose the OCI flex shape to price.";
  }
}

function renderRateCard() {
  if (!els.rateCard || !els.rateCardShape) return;
  els.rateCard.innerHTML = "";
  const shape = selectedShape();
  els.rateCardShape.textContent = shape.label || "Selected shape";
  displayRateCard(state.rateCard).forEach((item) => {
    const row = document.createElement("div");
    row.className = "rate-row";
    row.innerHTML = `
      <strong>${escapeHtml(item.sku)}</strong>
      <span>${escapeHtml(item.description)}</span>
      <em>$${Number(item.rate).toFixed(4)}</em>
    `;
    els.rateCard.append(row);
  });
}

function syncIntakeLayout() {
  // Upload and Review are editing surfaces. SKU and pricing results belong on the
  // later workflow pages, so keep the legacy rail hidden and give the table full width.
  els.intakePage.classList.remove("has-review");
  els.pricingRail.classList.add("is-hidden");
  els.pricingRail.hidden = true;
  els.pricingRail.setAttribute("aria-hidden", "true");
}

function setUploadLoading(isLoading, fileName = "") {
  els.uploadPanel.classList.toggle("is-uploading", isLoading);
  els.uploadProgress.classList.toggle("is-hidden", !isLoading);
  els.fileInput.disabled = isLoading;
  if (isLoading) {
    els.uploadStatus.textContent = "";
    els.uploadStatus.style.color = "var(--muted)";
    els.uploadProgressDetail.textContent = fileName
      ? isCloudBillMode()
        ? `Parsing ${fileName}, detecting ${providerLabel().toLowerCase()} provider signals, and mapping bill-line meters to OCI services.`
        : `Parsing ${fileName}, finding the inventory table, and cleaning CPU, RAM, storage, OS, and environment fields.`
      : "Reading workbook sheets and normalizing server inventory fields.";
  }
  syncWorkflowAvailability();
}

function resetPricingAfterTableChange(statusText = "Table updated") {
  state.pricing = null;
  state.ramp.signature = "";
  syncWorkflowAvailability();
  els.engineStatus.textContent = statusText;
  els.pricingSummary.className = "empty-state";
  els.pricingSummary.textContent = state.fullServiceBeta
    ? "Review the updated source service rows, then choose the OCI flex shape to price."
    : "Review the updated rows, then choose the OCI flex shape to price.";
}

function renderShapeChoices() {
  if (!els.shapeGrid) return;
  const shapes = shapesForVendor();
  renderShapeVendorMeta();
  if (!shapes.length) {
    els.shapeGrid.innerHTML = `<div class="shape-empty-state">No ${escapeHtml(vendorDefinition().label)} shapes are currently available.</div>`;
    return;
  }
  els.shapeGrid.innerHTML = shapes
    .map((shape) => {
      const isSelected = shape.key === state.selectedShape;
      return `
        <button
          id="shape-tab-${escapeHtml(shape.key)}"
          class="shape-tab ${isSelected ? "is-selected" : ""}"
          type="button"
          role="tab"
          aria-selected="${isSelected ? "true" : "false"}"
          aria-controls="shapeRatePanel"
          data-shape="${escapeHtml(shape.key)}"
          style="--shape-accent:${escapeHtml(shape.accent || "#c74634")}"
        >
          <span class="shape-tab-name">${escapeHtml(shape.shortLabel || shape.label)}</span>
          <span class="shape-tab-meta">${escapeHtml(
            shape.bareMetal && shape.bmOcpu
              ? `Bare metal · ${formatNumber(shape.bmOcpu)} OCPU · ${formatNumber(shape.bmMemoryGb)} GB`
              : (shape.family || "OCI shape"),
          )}</span>
        </button>
      `;
    })
    .join("");

  els.shapeGrid.querySelectorAll("[data-shape]").forEach((button) => {
    button.addEventListener("click", () => setShape(button.dataset.shape));
  });
  renderBareMetalShapes();
}

function renderBareMetalShapes() {
  const section = document.getElementById("bareMetalSection");
  const grid = document.getElementById("bareMetalGrid");
  if (!section || !grid) return;
  // Bare-metal shapes are now selectable cards in the main shape grid (they carry real SKUs
  // and rates), so this reference panel would just duplicate them.
  section.hidden = true;
  return;
  // eslint-disable-next-line no-unreachable
  const list = (state.bareMetalShapes && state.bareMetalShapes[state.selectedVendor]) || [];
  if (!list.length) {
    section.hidden = true;
    return;
  }
  grid.innerHTML = list
    .map(
      (bm) => `
      <div class="bare-metal-card">
        <span class="bare-metal-name">${escapeHtml(bm.shape)}</span>
        <span class="bare-metal-spec">${formatNumber(bm.maxOcpu)} OCPU &middot; ${formatNumber(bm.maxMem)} GB RAM</span>
      </div>`,
    )
    .join("");
  section.hidden = false;
}

function renderShapeDetail() {
  const shape = selectedShape();
  if (els.shapeFamily) els.shapeFamily.textContent = shape.family || "OCI flex shape";
  if (els.shapeDetailTitle) els.shapeDetailTitle.textContent = shape.label || "Selected shape";
  if (els.shapeDetailSummary) els.shapeDetailSummary.textContent = shape.summary || "Selected shape rates will be applied to approved rows.";
  if (els.shapeDetailRates) {
    els.shapeDetailRates.innerHTML = `
      <table class="shape-rate-card-table">
        <thead>
          <tr>
            <th>Item</th>
            <th>SKU</th>
            <th>Value</th>
            <th>Unit</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>Compute</td>
            <td>${escapeHtml(shape.computeSku || "Compute")}</td>
            <td>$${Number(shape.computeRate || 0).toFixed(4)}</td>
            <td>OCPU/hr</td>
          </tr>
          <tr>
            <td>Memory</td>
            <td>${escapeHtml(shape.memorySku || "Memory")}</td>
            <td>$${Number(shape.memoryRate || 0).toFixed(4)}</td>
            <td>GB/hr</td>
          </tr>
          <tr>
            <td>Billing month</td>
            <td>-</td>
            <td>${formatNumber(shape.hoursPerMonth || 730)}</td>
            <td>hrs/mo</td>
          </tr>
        </tbody>
      </table>
    `;
  }
}

function renderStats(meta = {}) {
  const fields = previewFields();
  const visibleRows = reviewRowEntries(fields).length;
  const approved = state.rows.filter((row) => row.__approved !== false).length;
  els.rowCount.textContent = state.showMissingOnly
    ? `${formatNumber(visibleRows)} / ${formatNumber(state.rows.length)}`
    : meta.rowCount ?? state.rows.length;
  els.columnCount.textContent = fields.length;
  els.approvedCount.textContent = approved;
  els.sheetName.textContent = meta.sheetName || els.sheetName.textContent || "-";
  syncMissingFilterUi(fields);
  syncWorkflowAvailability();
}

// CPU-unit interpretation. The parser stores the CPU column already halved
// (raw vCPU / 2 = OCPU under the default vCPU assumption). "Already OCPUs" means the
// source count is the OCPU count, so the review/pricing value is doubled back to the
// raw count. "Auto" detects vCPU vs OCPU from the original header, defaulting to vCPU.
function resolvedCpuUnit() {
  if (state.cpuUnit === "vcpu" || state.cpuUnit === "ocpu") return state.cpuUnit;
  const f = (state.fields || []).find((x) => x && x.cpuSourceLabel);
  const src = String(f?.cpuSourceLabel || "").toLowerCase();
  if (src.includes("ocpu")) return "ocpu";
  if (src.includes("vcpu") || src.includes("virtual cpu")) return "vcpu";
  // Physical cores are OCPUs 1:1 (CPU cores per server, physical cores, rationalized cores),
  // so treat a "core" column as OCPUs, not halved like vCPUs. Mirror server detect_cpu_unit.
  if (src.includes("core")) return "ocpu";
  return "vcpu";
}
function cpuDisplayMult() {
  return resolvedCpuUnit() === "ocpu" ? 2 : 1;
}
function isCpuField(field) {
  return !!(field && field.cpuSourceLabel);
}
function formatCpuDisplay(n) {
  const r = Math.round(n * 1000) / 1000;
  return Number.isInteger(r) ? String(r) : String(r);
}
function updateCpuUnitHint() {
  if (!els.cpuUnitDetected) return;
  if (state.cpuUnit === "auto" && (state.fields || []).some((f) => f && f.cpuSourceLabel)) {
    const r = resolvedCpuUnit();
    els.cpuUnitDetected.textContent =
      "Auto-detected: " + (r === "ocpu" ? "already OCPUs" : "vCPUs (halved for OCI)");
    els.cpuUnitDetected.hidden = false;
  } else {
    els.cpuUnitDetected.hidden = true;
  }
}

// Pre-flight data check. Runs on every upload: says exactly which inputs the file carries
// and, for the ones it doesn't, what the app will therefore leave blank. Nothing downstream
// (BOM sheets, architecture diagram, topology) is allowed to invent what isn't here.
const DATA_CHECK_CONSEQUENCE = {
  cpu: "no compute can be priced",
  memory: "no compute can be priced",
  storage: "block storage is left out of the BOM",
  os: "no OS split and no Windows licensing line",
  server: "Compute sheet rows will be unnamed",
  environment: "the Environment column stays blank",
  application: "the Applications sheet and Master Application column stay empty",
};

function renderDataCheck(check) {
  const panel = document.getElementById("dataCheck");
  const list = document.getElementById("dataCheckList");
  const note = document.getElementById("dataCheckNote");
  if (!panel || !list) return;
  if (!check || !Array.isArray(check.signals) || !check.signals.length) {
    panel.hidden = true;
    return;
  }
  list.innerHTML = "";
  check.signals.forEach((s) => {
    const li = document.createElement("li");
    li.className = s.present ? "dc-ok" : "dc-missing";
    const detail = s.present
      ? `${s.column} - ${formatNumber(s.populated)} of ${formatNumber(s.total)} rows`
      : `not in this file - ${DATA_CHECK_CONSEQUENCE[s.key] || "left blank"}`;
    li.innerHTML =
      `<span class="dc-mark" aria-hidden="true">${s.present ? "✓" : "-"}</span>` +
      `<span class="dc-label">${escapeHtml(s.label)}</span>` +
      `<span class="dc-detail">${escapeHtml(detail)}</span>`;
    list.appendChild(li);
  });
  const missing = check.signals.filter((s) => !s.present).map((s) => s.label);
  const caps = check.capabilities || {};
  const bits = [];
  if (!caps.priceCompute) bits.push("Compute can't be priced without both a CPU and a memory column.");
  if (caps.segmentBy) {
    const by = { environment: "Environment", os: "OS family", application: "Application" }[caps.segmentBy];
    bits.push(`Architecture spokes will be split by ${by}.`);
  }
  if (missing.length) bits.push(`Missing: ${missing.join(", ")}. Those stay blank.`);
  note.textContent = bits.join(" ");
  note.hidden = !bits.length;
  panel.hidden = false;
}

function removeReviewRow(row) {
  const id = row && row.__id;
  const idx = state.rows.findIndex((r) => r === row || (id && r.__id === id));
  if (idx === -1) return;
  state.rows.splice(idx, 1);
  // Drop any per-row state keyed by this row's id so it doesn't linger.
  if (id) {
    [state.selectedRows, state.approvedFlags, state.shapeOverrides, state.costOverrides]
      .forEach((map) => { if (map && typeof map === "object") delete map[id]; });
  }
  // Sizing changed - invalidate the estimate so Price re-runs on the reduced set.
  state.pricing = null;
  renderTable();
  syncWorkflowAvailability();
}

// Review-table virtualization. A large inventory (or a cloud bill with thousands of line
// items) builds one <input> plus a fill handle per cell - 7,500 rows x 12 columns is ~270k
// DOM nodes, which makes scrolling crawl. Above this many rows we render only the visible
// window plus a small overscan, and pad the scroll height with spacer rows so the scrollbar
// still represents the whole table.
const VIRTUAL_ROW_THRESHOLD = 200;
const VIRTUAL_OVERSCAN = 10;
const VIRTUAL_DEFAULT_ROW_H = 44;
let reviewVirtual = null;

function teardownReviewVirtual() {
  if (reviewVirtual?.wrap && reviewVirtual.onScroll) {
    reviewVirtual.wrap.removeEventListener("scroll", reviewVirtual.onScroll);
  }
  reviewVirtual = null;
}

function spacerRow(colCount) {
  const tr = document.createElement("tr");
  tr.className = "vrow-spacer";
  tr.setAttribute("aria-hidden", "true");
  const td = document.createElement("td");
  td.colSpan = colCount;
  td.style.padding = "0";
  td.style.border = "0";
  td.style.height = "0px";
  tr.append(td);
  return tr;
}

function renderVirtualWindow() {
  const v = reviewVirtual;
  if (!v) return;
  const total = v.rowEntries.length;
  const scrollTop = v.wrap ? v.wrap.scrollTop : 0;
  const viewH = v.wrap ? v.wrap.clientHeight : 800;
  let start = Math.max(0, Math.floor(scrollTop / v.rowH) - VIRTUAL_OVERSCAN);
  let end = Math.min(total, Math.ceil((scrollTop + viewH) / v.rowH) + VIRTUAL_OVERSCAN);
  if (end <= start) end = Math.min(total, start + 1);
  if (v.start === start && v.end === end) return;
  v.start = start;
  v.end = end;
  const frag = document.createDocumentFragment();
  for (let i = start; i < end; i += 1) {
    const { row, rowIndex } = v.rowEntries[i];
    frag.append(buildReviewRowEl(row, rowIndex, v.fields));
  }
  v.topSpacer.firstChild.style.height = `${start * v.rowH}px`;
  v.botSpacer.firstChild.style.height = `${Math.max(0, total - end) * v.rowH}px`;
  // Replace just the rendered window, keeping the two spacers in place.
  while (v.topSpacer.nextSibling && v.topSpacer.nextSibling !== v.botSpacer) {
    v.topSpacer.nextSibling.remove();
  }
  v.tbody.insertBefore(frag, v.botSpacer);
  // Measure a real row once so the spacers match the true row height.
  if (!v.measured) {
    const sample = v.topSpacer.nextSibling;
    const h = sample && sample !== v.botSpacer ? sample.offsetHeight : 0;
    if (h && Math.abs(h - v.rowH) > 1) {
      v.rowH = h;
      v.measured = true;
      v.start = -1;
      v.end = -1;
      renderVirtualWindow();
      return;
    }
    v.measured = true;
  }
}

// ---- Per-row OS on the Review step (on-prem inventory only) -----------------------------
// Windows licensing is ~$0.09/OCPU-hr, so getting the OS wrong on a big server is worth real
// money. Detection just looks for the words "windows"/"linux" anywhere in the row, which a
// stray comment can flip, so on-prem inventories get an explicit toggle and the choice is
// stored on the row as __os. The server prefers __os over its own scan.
let osRepriceTimer = null;
function osColumnVisible() {
  return state.intakeMode === "on_prem";
}
function detectRowOs(row) {
  for (const [key, value] of Object.entries(row || {})) {
    if (key.startsWith("__")) continue;
    const text = String(value ?? "").toLowerCase();
    if (text.includes("windows")) return "windows";
  }
  for (const [key, value] of Object.entries(row || {})) {
    if (key.startsWith("__")) continue;
    if (String(value ?? "").toLowerCase().includes("linux")) return "linux";
  }
  return "";
}
function rowOs(row) {
  const explicit = String(row?.__os || "").toLowerCase();
  return explicit === "windows" || explicit === "linux" ? explicit : detectRowOs(row);
}
function buildOsCell(row, rowIndex) {
  const td = document.createElement("td");
  td.className = "os-cell";
  const select = document.createElement("select");
  select.className = `os-select is-${rowOs(row) || "unknown"}`;
  select.setAttribute("aria-label", `Operating system, row ${rowIndex + 1}`);
  select.title = "Windows adds OCI Windows OS licensing per OCPU. Linux adds none.";
  [["linux", "Linux"], ["windows", "Windows"]].forEach(([value, label]) => {
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = label;
    select.append(opt);
  });
  select.value = rowOs(row) === "windows" ? "windows" : "linux";
  select.addEventListener("change", () => {
    row.__os = select.value;
    select.className = `os-select is-${select.value}`;
    // Any estimate on screen was calculated with the old OS, so it's stale. Debounced
    // because setting the OS on a run of servers fires this once per dropdown.
    clearTimeout(osRepriceTimer);
    osRepriceTimer = setTimeout(() => {
      if (typeof repriceIfEstimated === "function") repriceIfEstimated();
    }, 400);
  });
  td.append(select);
  return td;
}

function buildReviewRowEl(row, rowIndex, fields) {
  {
    const tr = document.createElement("tr");
    tr.dataset.rowIndex = String(rowIndex);
    const approveCell = document.createElement("td");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = row.__approved !== false;
    checkbox.addEventListener("change", () => {
      row.__approved = checkbox.checked;
      renderStats();
    });
    approveCell.append(checkbox);
    // Remove this VM from the BOM entirely (unlike unchecking Approve, which keeps the row).
    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "row-remove-btn";
    removeBtn.textContent = "×";
    removeBtn.title = "Remove this VM from the BOM";
    removeBtn.setAttribute("aria-label", `Remove row ${rowIndex + 1}`);
    removeBtn.addEventListener("click", () => removeReviewRow(row));
    approveCell.append(removeBtn);
    tr.append(approveCell);

    fields.forEach((field) => {
      const td = document.createElement("td");
      td.dataset.rowIndex = String(rowIndex);
      td.dataset.fieldKey = field.key;
      td.classList.toggle("is-missing-data", cellIsMissing(row, field.key));
      const cellEditor = document.createElement("div");
      cellEditor.className = "cell-editor";
      const input = document.createElement("input");
      input.type = "text";
      if (isHoursRunningField(field)) {
        input.type = "number";
        input.min = "1";
        input.step = "1";
        input.inputMode = "numeric";
      }
      // The OCPUs column reflects the CPU-unit toggle: stored value is the parsed
      // (halved) OCPU; display it x2 when the source column is "Already OCPUs".
      const cpuField = isCpuField(field);
      const cpuMult = cpuField ? cpuDisplayMult() : 1;
      const storedVal = row[field.key];
      if (cpuField && storedVal !== "" && storedVal != null && !isNaN(Number(storedVal))) {
        input.value = formatCpuDisplay(Number(storedVal) * cpuMult);
      } else {
        input.value = storedVal ?? "";
      }
      input.placeholder = cellIsMissing(row, field.key) ? "Missing data" : "";
      input.dataset.rowIndex = String(rowIndex);
      input.dataset.fieldKey = field.key;
      input.setAttribute("aria-label", `${field.label}, row ${rowIndex + 1}`);
      input.addEventListener("input", () => {
        if (cpuField && input.value !== "" && !isNaN(Number(input.value))) {
          // Store back in the parsed (halved) OCPU space so pricing stays consistent.
          row[field.key] = Number(input.value) / cpuMult;
        } else {
          row[field.key] = input.value;
        }
        const isMissing = cellIsMissing(row, field.key, input.value);
        td.classList.toggle("is-missing-data", isMissing);
        input.placeholder = isMissing ? "Missing data" : "";
        syncMissingFilterUi(fields);
      });
      input.addEventListener("blur", () => {
        if (state.showMissingOnly && !rowHasMissingData(row, fields)) {
          renderTable();
        }
      });
      const fillHandle = document.createElement("button");
      fillHandle.className = "fill-handle";
      fillHandle.type = "button";
      fillHandle.dataset.rowIndex = String(rowIndex);
      fillHandle.dataset.fieldKey = field.key;
      fillHandle.setAttribute("aria-label", `Drag to fill ${field.label} down from row ${rowIndex + 1}`);
      fillHandle.addEventListener("pointerdown", startCellFill);
      cellEditor.append(input, fillHandle);
      td.append(cellEditor);
      tr.append(td);
    });
    if (osColumnVisible()) tr.append(buildOsCell(row, rowIndex));
    return tr;
  }
}

function renderTable() {
  const fields = previewFields();
  const rowEntries = reviewRowEntries(fields);
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  headRow.append(headerCell("Approve"));
  fields.forEach((field) => headRow.append(headerCell(field.label)));
  if (osColumnVisible()) headRow.append(headerCell("OS"));
  thead.append(headRow);

  const tbody = document.createElement("tbody");
  teardownReviewVirtual();

  if (rowEntries.length > VIRTUAL_ROW_THRESHOLD) {
    // Windowed rendering: only the visible slice lives in the DOM.
    const colCount = fields.length + 1 + (osColumnVisible() ? 1 : 0);
    const topSpacer = spacerRow(colCount);
    const botSpacer = spacerRow(colCount);
    tbody.append(topSpacer, botSpacer);
    els.reviewTable.replaceChildren(thead, tbody);
    const wrap = els.reviewTable.closest(".table-wrap");
    reviewVirtual = {
      fields, rowEntries, tbody, topSpacer, botSpacer, wrap,
      rowH: VIRTUAL_DEFAULT_ROW_H, start: -1, end: -1, measured: false, ticking: false,
    };
    if (wrap) {
      reviewVirtual.onScroll = () => {
        if (reviewVirtual?.ticking) return;
        if (reviewVirtual) reviewVirtual.ticking = true;
        window.requestAnimationFrame(() => {
          if (reviewVirtual) reviewVirtual.ticking = false;
          renderVirtualWindow();
        });
      };
      wrap.addEventListener("scroll", reviewVirtual.onScroll, { passive: true });
    }
    renderVirtualWindow();
  } else {
    rowEntries.forEach(({ row, rowIndex }) => {
      tbody.append(buildReviewRowEl(row, rowIndex, fields));
    });
    if (!rowEntries.length) {
      const emptyRow = document.createElement("tr");
      emptyRow.className = "table-empty-row";
      const emptyCell = document.createElement("td");
      emptyCell.colSpan = fields.length + 1 + (osColumnVisible() ? 1 : 0);
      emptyCell.textContent = fields.length
        ? "No rows are missing data in the visible fields."
        : "No visible fields to check for missing data.";
      emptyRow.append(emptyCell);
      tbody.append(emptyRow);
    }
    els.reviewTable.replaceChildren(thead, tbody);
  }

  renderStats();
  // CPU-unit override only applies to on-prem inventory (cloud bills carry OCPU/vCPU
  // in the usage rows), so hide the control in cloud-bill mode.
  if (els.cpuUnitRow) els.cpuUnitRow.hidden = isCloudBillMode();
  updateCpuUnitHint();
}

function focusReviewField(fieldKey) {
  window.requestAnimationFrame(() => {
    const wrap = els.reviewTable.closest(".table-wrap");
    const input = [...els.reviewTable.querySelectorAll("input[data-field-key]")].find(
      (item) => item.dataset.fieldKey === fieldKey,
    );
    const cell = input?.closest("td");
    if (!wrap || !input || !cell) return;
    wrap.scrollLeft = Math.max(0, cell.offsetLeft - 92);
    input.focus();
  });
}

function headerCell(label) {
  const th = document.createElement("th");
  th.scope = "col";
  th.textContent = label;
  if (label.toLowerCase().includes("ocpu")) {
    th.title = "Uploaded spreadsheet CPU values are assumed to be vCPUs and converted using 2 vCPUs = 1 OCPU.";
  }
  return th;
}

function clearFillPreview() {
  els.reviewTable.querySelectorAll(".is-fill-preview").forEach((cell) => {
    cell.classList.remove("is-fill-preview");
  });
}

function fillTargetRowIndex(event) {
  const target = document.elementFromPoint(event.clientX, event.clientY);
  const row = target?.closest?.("tr[data-row-index]");
  if (!row) return activeFill?.endRowIndex ?? activeFill?.startRowIndex ?? 0;
  const rowIndex = Number(row.dataset.rowIndex);
  return Number.isFinite(rowIndex) ? rowIndex : activeFill?.startRowIndex ?? 0;
}

function scrollTableDuringFill(event) {
  const wrap = els.reviewTable.closest(".table-wrap");
  if (!wrap) return;
  const rect = wrap.getBoundingClientRect();
  if (event.clientY > rect.bottom - 28) {
    wrap.scrollTop += 18;
  } else if (event.clientY < rect.top + 28) {
    wrap.scrollTop -= 18;
  }
}

function updateFillPreview(rowIndex) {
  if (!activeFill) return;
  clearFillPreview();
  const endRowIndex = Math.max(activeFill.startRowIndex, Math.min(rowIndex, state.rows.length - 1));
  activeFill.endRowIndex = endRowIndex;
  els.reviewTable.querySelectorAll("td[data-row-index][data-field-key]").forEach((cell) => {
    const cellRow = Number(cell.dataset.rowIndex);
    if (cell.dataset.fieldKey === activeFill.fieldKey && cellRow > activeFill.startRowIndex && cellRow <= endRowIndex) {
      cell.classList.add("is-fill-preview");
    }
  });
}

function moveCellFill(event) {
  if (!activeFill) return;
  event.preventDefault();
  scrollTableDuringFill(event);
  updateFillPreview(fillTargetRowIndex(event));
}

function endCellFill(event) {
  if (!activeFill) return;
  event.preventDefault();
  updateFillPreview(fillTargetRowIndex(event));
  document.removeEventListener("pointermove", moveCellFill);
  document.removeEventListener("pointerup", endCellFill);
  document.removeEventListener("pointercancel", cancelCellFill);
  document.body.classList.remove("is-fill-dragging");

  const { startRowIndex, endRowIndex, fieldKey, value, handle, pointerId } = activeFill;
  const rowsToFill = Math.max(0, endRowIndex - startRowIndex);
  if (handle?.releasePointerCapture && pointerId !== undefined) {
    try {
      handle.releasePointerCapture(pointerId);
    } catch {
      // Pointer capture may already be released by the browser.
    }
  }
  activeFill = null;
  clearFillPreview();

  if (!rowsToFill) return;
  if (!hasCellContent(value)) {
    return;
  }

  for (let rowIndex = startRowIndex + 1; rowIndex <= endRowIndex; rowIndex += 1) {
    state.rows[rowIndex][fieldKey] = value;
  }
  renderTable();
  resetPricingAfterTableChange("Table filled down");
}

function cancelCellFill() {
  const { handle, pointerId } = activeFill || {};
  document.removeEventListener("pointermove", moveCellFill);
  document.removeEventListener("pointerup", endCellFill);
  document.removeEventListener("pointercancel", cancelCellFill);
  document.body.classList.remove("is-fill-dragging");
  if (handle?.releasePointerCapture && pointerId !== undefined) {
    try {
      handle.releasePointerCapture(pointerId);
    } catch {
      // Pointer capture may already be released by the browser.
    }
  }
  activeFill = null;
  clearFillPreview();
}

function startCellFill(event) {
  event.preventDefault();
  event.stopPropagation();
  const handle = event.currentTarget;
  const startRowIndex = Number(handle.dataset.rowIndex);
  const fieldKey = handle.dataset.fieldKey;
  const input = handle.parentElement?.querySelector("input");
  if (!fieldKey || !Number.isFinite(startRowIndex) || !input) return;

  activeFill = {
    fieldKey,
    startRowIndex,
    endRowIndex: startRowIndex,
    value: input.value,
    handle,
    pointerId: event.pointerId,
  };
  if (handle.setPointerCapture) {
    try {
      handle.setPointerCapture(event.pointerId);
    } catch {
      // Pointer capture is an enhancement; drag-fill still works without it.
    }
  }
  document.body.classList.add("is-fill-dragging");
  document.addEventListener("pointermove", moveCellFill);
  document.addEventListener("pointerup", endCellFill);
  document.addEventListener("pointercancel", cancelCellFill);
}

const PROVIDER_NAME_TO_VALUE = { aws: "aws", azure: "azure", gcp: "gcp" };
const COMPRESSED_UPLOAD_THRESHOLD = 3.5 * 1024 * 1024;
const MAX_COMPRESSED_UPLOAD_BYTES = 4 * 1000 * 1000;

async function jsonRequestOptions(payload) {
  const serialized = JSON.stringify(payload);
  const body = new Blob([serialized], { type: "application/json" });
  if (body.size < COMPRESSED_UPLOAD_THRESHOLD) {
    return {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: serialized,
    };
  }
  if (typeof CompressionStream === "undefined") {
    throw new Error(
      "This workflow is too large for an uncompressed request. Open it in a browser with gzip upload support and try again.",
    );
  }
  const compressedStream = body.stream().pipeThrough(new CompressionStream("gzip"));
  const compressedBody = await new Response(compressedStream).blob();
  if (compressedBody.size > MAX_COMPRESSED_UPLOAD_BYTES) {
    throw new Error(
      "This workflow remains too large after compression. Reduce the bill date range and try again.",
    );
  }
  return {
    method: "POST",
    headers: { "Content-Type": "application/json+gzip" },
    body: compressedBody,
  };
}

async function compressedUploadRequest(file) {
  if (
    file.size < COMPRESSED_UPLOAD_THRESHOLD
    || typeof CompressionStream === "undefined"
  ) {
    return null;
  }
  const compressedStream = file.stream().pipeThrough(new CompressionStream("gzip"));
  const compressedBody = await new Response(compressedStream).blob();
  if (compressedBody.size > MAX_COMPRESSED_UPLOAD_BYTES) {
    throw new Error(
      "This file remains too large after compression. Reduce the export to the billing columns and date range you need, then upload it again.",
    );
  }
  return {
    url: "/api/upload",
    options: {
      method: "POST",
      headers: {
        "Content-Type": "application/gzip",
        "X-Upload-Filename": encodeURIComponent(file.name),
        "X-Intake-Mode": state.intakeMode,
        "X-Provider-Hint": state.providerHint,
        "X-Full-Service-Beta": state.fullServiceBeta ? "true" : "false",
      },
      body: compressedBody,
    },
  };
}

function showSelectedDoc(name, sub) {
  if (!els.selectedDocTile) return;
  if (!name) {
    els.selectedDocTile.hidden = true;
    return;
  }
  els.selectedDocTile.hidden = false;
  if (els.selectedDocName) els.selectedDocName.textContent = name;
  if (els.selectedDocSub) els.selectedDocSub.textContent = sub || "";
}

async function uploadFile(file) {
  if (!file) return;
  // Other OCI Bill mode: the file is already a priced OCI BOM, not a raw inventory or bill,
  // so the inventory parser would make nonsense of it. Route the whole drop zone (and the file
  // picker, which shares this entry point) to the converter instead.
  if (isOtherOciBillMode()) return convertBomFromFile(file);
  clearIntakeStatuses();   // starting a fresh upload - drop any load/convert banners
  resetWorkflowProgress();
  state.lastUploadFile = file;
  showSelectedDoc(file.name, "Reading file…");
  setUploadLoading(true, file.name);

  try {
    const compressedRequest = await compressedUploadRequest(file);
    const body = new FormData();
    body.append("file", file);
    body.append("intakeMode", state.intakeMode);
    body.append("providerHint", state.providerHint);
    body.append("fullServiceBeta", state.fullServiceBeta ? "true" : "false");
    const request = compressedRequest || {
      url: "/api/upload",
      options: {
        method: "POST",
        body,
      },
    };
    const { response, payload } = await fetchJson(
      request.url,
      request.options,
      100000,
    );
    if (!response.ok) {
      throw new Error(payload.error || "Upload failed.");
    }

    // A Full BOM export dropped here instead of on "Load previous BOM". It carries the whole
    // saved workflow, so restore it rather than making the user find the other drop zone.
    if (payload.workflowRestore && payload.workflow) {
      await applyWorkflowState(payload.workflow);
      showSelectedDoc(
        payload.fileName,
        `${formatNumber((payload.workflow.rows || []).length)} rows · restored from a saved BOM`,
      );
      els.uploadStatus.textContent =
        "That file is a saved BOM, so the whole estimate was restored - no re-upload needed.";
      els.uploadStatus.style.color = "var(--success)";
      return;
    }

    state.fields = payload.fields;
    state.rows = payload.rows;
    state.rateCards = payload.rateCards || [];
    state.fullServiceCatalog = payload.fullServiceCatalog || state.fullServiceCatalog;
    state.selectedShape = payload.selectedShape?.key || state.selectedShape;
    state.rateCard = selectedShape().rateCard || payload.rateCard || [];
    syncVendorForSelectedShape();
    state.uploadMetadata = payload.metadata || {};
    state.intakeMode = payload.metadata?.intakeMode || state.intakeMode;
    state.fullServiceBeta = state.intakeMode === "cloud_bill";
    ensureIdentityReviewFields();
    ensureHoursRunningReviewField();
    const docModeLabel = state.intakeMode === "cloud_bill"
      ? `${payload.metadata?.detectedProvider || "Cloud"} bill`
      : "On-prem inventory";
    showSelectedDoc(payload.fileName, `${formatNumber(payload.rows.length)} rows · ${docModeLabel}`);
    if (els.inventoryNotice) els.inventoryNotice.hidden = !payload.metadata?.inventorySuspected;
    // Warn if a finished comparison/BOM workbook was dropped into cloud-bill mode.
    const cmpNotice = document.getElementById("comparisonBomNotice");
    if (cmpNotice) {
      const msg = payload.comparisonBomWarning;
      const txt = document.getElementById("comparisonBomNoticeText");
      if (msg && txt) txt.textContent = msg;
      cmpNotice.hidden = !msg;
    }
    // Reflect the detected/guessed provider in the toggle so the user sees the guess
    // and can override it. (Only when they hadn't already forced a provider.)
    if (state.intakeMode === "cloud_bill" && state.providerHint === "auto") {
      const guessed = PROVIDER_NAME_TO_VALUE[(payload.metadata?.detectedProvider || "").toLowerCase()];
      if (guessed) {
        state.providerHint = guessed;
        if (els.providerHint) els.providerHint.value = guessed;
      }
    }
    state.showMissingOnly = false;
    state.pricing = null;
    syncModeUi();

    const parserLabel =
      payload.metadata?.parser === "llm-assisted"
        ? "OpenAI scrubbed"
        : payload.metadata?.parser === "cloud-bill-adapter"
          ? "Cloud bill parser"
          : payload.metadata?.parser === "cloud-bill-pdf"
            ? "PDF bill parser"
          : "Rule-based parse";
    const detectedProvider = payload.metadata?.detectedProvider;
    const modeLabel = isCloudBillMode()
      ? ` • ${detectedProvider || providerLabel()} cloud bill`
      : "";
    const grain = payload.metadata?.serverGrain && payload.metadata.serverGrain !== "unknown" ? ` • ${payload.metadata.serverGrain} grain` : "";
    els.sheetMeta.textContent = payload.fileName;
    els.sheetMeta.title = `Sheet "${payload.sheetName}" • ${parserLabel}${modeLabel}${grain} • data begins on row ${payload.metadata.dataStartRow}`;
    els.sheetName.textContent = payload.sheetName;
    renderDataCheck(payload.dataCheck);
    renderRateCard();
    renderProcessorPicker();
    renderShapeChoices();
    renderShapeDetail();
    renderTable();
    state.uploadReady = state.rows.length > 0;
    if (!state.uploadReady) {
      throw new Error("No usable rows were found in that spreadsheet.");
    }
    unlockWorkflowStep("review");
    showUploadPage();
    els.uploadStatus.textContent = "Spreadsheet ready. Continue to review.";
    els.uploadStatus.style.color = "var(--success)";
    syncIntakeLayout();
    els.engineStatus.textContent = isCloudBillMode()
      ? `${detectedProvider || providerLabel()} bill upload`
      : payload.metadata?.parser === "llm-assisted"
        ? "OpenAI scrubbed upload"
        : "Ready for approval";
    els.pricingSummary.className = "empty-state";
    const notes = payload.metadata?.extractionNotes || [];
    const warning = payload.llmWarning ? `${payload.llmWarning} ` : "";
    els.pricingSummary.textContent =
      warning ||
      (isCloudBillMode()
        ? `${formatNumber(payload.metadata?.mappedCount || 0)} bill lines mapped to OCI products; ${formatNumber(payload.metadata?.unmappedCount || 0)} lines need review before they affect the OCI total.`
        : notes.length
        ? `Review the normalized rows. ${notes.slice(0, 2).join(" ")}`
        : "Review the rows, make adjustments, then choose the OCI flex shape to price.");
  } finally {
    setUploadLoading(false);
  }
}

function setUploadingError(error) {
  state.uploadReady = false;
  state.workflowMaxUnlockedStep = 0;
  syncWorkflowAvailability();
  els.uploadStatus.textContent = error.message;
  els.uploadStatus.style.color = "var(--danger)";
}

function makeBlankRow(prefix = "manual") {
  const row = {
    __id: `${prefix}-${Date.now()}-${state.rows.length + 1}`,
    __sourceRow: "manual",
    __approved: true,
  };
  state.fields.forEach((field) => {
    row[field.key] = isHoursRunningField(field) ? 730 : "";
  });
  return row;
}

function initializeManualReviewTable() {
  state.intakeMode = "on_prem";
  state.providerHint = "auto";
  state.fullServiceBeta = false;
  state.fields = MANUAL_REVIEW_FIELDS.map((field) => ({
    ...field,
    sourceColumn: null,
    important: true,
    manual: true,
  }));
  state.rows = [makeBlankRow("manual-entry")];
  state.uploadMetadata = {
    parser: "manual-entry",
    rowCount: state.rows.length,
    sheetName: "Manual entry",
  };
  state.showMissingOnly = false;
  state.pricing = null;
  state.ramp.signature = "";
  syncModeUi();
  els.fileInput.value = "";
  els.uploadStatus.textContent = "";
  els.sheetMeta.textContent = "Manual entry";
  els.sheetMeta.title = "Blank table for manual entry.";
  els.sheetName.textContent = "Manual entry";
  renderDataCheck(null);
  renderRateCard();
  renderProcessorPicker();
  renderShapeChoices();
  renderShapeDetail();
  renderTable();
  syncIntakeLayout();
  els.engineStatus.textContent = "Manual entry";
  els.pricingSummary.className = "empty-state";
  els.pricingSummary.textContent = "Fill in one or more rows, then choose the OCI flex shape to price.";
}

function ensureReviewRows() {
  if (!state.rows.length) {
    initializeManualReviewTable();
  }
}

function addBlankRow() {
  if (!state.fields.length) {
    initializeManualReviewTable();
    return;
  }
  state.rows.push(makeBlankRow());
  renderTable();
  resetPricingAfterTableChange("Manual row added");
}

function showAddColumnForm() {
  if (!els.addColumnForm || !els.newColumnName) return;
  els.addColumnForm.classList.remove("is-hidden");
  els.newColumnName.focus();
}

function hideAddColumnForm() {
  if (!els.addColumnForm || !els.newColumnName) return;
  els.addColumnForm.classList.add("is-hidden");
  els.newColumnName.value = "";
}

function addManualColumn(label) {
  const cleanLabel = String(label || "").trim();
  if (!state.rows.length) {
    ensureReviewRows();
  }
  if (!cleanLabel) {
    els.newColumnName?.focus();
    return;
  }

  let field = state.fields.find((item) => normalizeText(item.label) === normalizeText(cleanLabel));
  const wasExisting = Boolean(field);
  if (!field) {
    field = {
      key: uniqueFieldKey(cleanLabel),
      label: cleanLabel,
      sourceColumn: null,
      manual: true,
      userAdded: true,
    };
    state.fields.push(field);
  } else {
    field.manual = true;
    field.userAdded = true;
  }

  state.rows.forEach((row) => {
    if (!(field.key in row)) {
      row[field.key] = "";
    }
  });

  renderTable();
  resetPricingAfterTableChange(wasExisting ? "Column revealed" : "Manual column added");
  hideAddColumnForm();
  focusReviewField(field.key);
}

function submitAddColumn(event) {
  event.preventDefault();
  addManualColumn(els.newColumnName?.value);
}

async function priceRows({ keepView = false, destination = "price" } = {}) {
  if (destination === "price" && !keepView) {
    setPricePageStatus("Preparing your OCI estimate...");
  }
  // Non-fading floating spinner so in-place edits (e.g. removing a large
  // selection from the BOM) show progress without blanking the screen.
  if (els.priceSpinner) els.priceSpinner.hidden = false;
  // Let the spinner paint before any heavy synchronous render that follows.
  await new Promise((r) => requestAnimationFrame(() => r()));
  els.priceButton.disabled = true;
  els.priceButton.textContent = "Pricing...";
  els.priceShapeButton.disabled = true;
  els.priceShapeButton.textContent = destination === "networking" ? "Preparing services..." : "Pricing...";
  if (els.rerunPricing) { els.rerunPricing.disabled = true; els.rerunPricing.textContent = "Pricing..."; }
  els.engineStatus.textContent = isCloudBillMode()
    ? `Mapping cloud bill lines to OCI equivalents for ${selectedShape().label}`
    : `Mapping SKUs for ${selectedShape().label}`;

  try {
    const requestOptions = await jsonRequestOptions(pricingInputs());
    const { response, payload } = await fetchJson(
      "/api/price",
      requestOptions,
      70000,
    );
    if (!response.ok) {
      throw new Error(payload.error || "Pricing failed.");
    }
    state.pricing = payload;
    updateCpuUnitHint();
    renderPricing(payload);
    renderResults(payload);
    setPricePageStatus();
    // When re-pricing in place (e.g. editing a shape dropdown), don't jump the page.
    if (keepView) {
      const y = window.scrollY;
      requestAnimationFrame(() => window.scrollTo({ top: y }));
    } else if (destination === "networking") {
      unlockWorkflowStep("networking");
      showNetworkingPage();
    } else if (destination === "architecture") {
      unlockWorkflowStep("architecture");
      showArchitecturePage();
    } else {
      unlockWorkflowStep("price");
      showResultsPage();
    }
    return payload;
  } catch (error) {
    els.engineStatus.textContent = "Pricing error";
    els.pricingSummary.className = "empty-state";
    els.pricingSummary.textContent = error.message;
    if (destination === "price") {
      setPricePageStatus(`Pricing could not be completed: ${error.message}`, "error");
    } else if (destination === "networking") {
      setNetworkingPageStatus(`Pricing could not be completed: ${error.message}`, "error");
    } else if (destination === "architecture") {
      setArchitectureExportStatus(`Pricing could not be completed: ${error.message}`, "error");
    }
    return null;
  } finally {
    els.priceButton.textContent = "Continue to Shape";
    els.priceShapeButton.textContent = "Continue to Services";
    if (els.rerunPricing) { els.rerunPricing.disabled = false; els.rerunPricing.textContent = pricingActionLabel("rerun"); }
    if (els.priceSpinner) els.priceSpinner.hidden = true;
    syncWorkflowAvailability();
  }
}

function setDeliverableStatus(element, message, tone = "") {
  if (!element) return;
  element.textContent = message;
  if (tone) element.dataset.tone = tone;
  else delete element.dataset.tone;
}

async function exportToExcel(triggerButton = null) {
  // The Full BOM is the deliverable: the multi-sheet workbook (Pricing Overview, Compute,
  // Storage, Rate Card, Consumption Ramp...) that ties out to the app. "quick" is the compact
  // comparison workbook, so it must not be the default.
  const template = "full";
  const button = triggerButton || els.exportFullBom || els.deliverablesFullBom;
  const isDeliverablesBom = button === els.deliverablesFullBom;
  const exportLabel = "BOM";
  const original = button ? button.innerHTML : "";
  const overlay = document.querySelector("#exportOverlay");
  const overlayText = overlay ? overlay.querySelector(".export-overlay-text") : null;

  // Don't fail silently. Without pricing there is nothing to export, and a dead-looking
  // button is worse than a message saying why.
  if (!state.pricing) {
    els.engineStatus.textContent =
      "Nothing to export yet - run \"Reprice estimate\" first, then try the BOM again.";
    setDeliverableStatus(
      els.deliverablesBomStatus,
      "Prepare the estimate before downloading the BOM.",
      "error",
    );
    return;
  }

  let stageTimer = null;
  let failed = false;
  if (button) {
    button.disabled = true;
    button.textContent = "Exporting BOM...";
  }
  if (isDeliverablesBom) {
    setDeliverableStatus(els.deliverablesBomStatus, "Building the BOM...");
  }
  if (overlayText) {
    overlayText.textContent = "Generating your Excel workbook…";
  }
  if (overlay) overlay.hidden = false;
  try {
    const rampVals = (typeof rampMonthlyValues === "function" && state.ramp.points.length)
      ? rampMonthlyValues()
      : [];
    // Short ramps (12/24 mo) still print a full 3-year contract: months past the ramp
    // run at the full BOM maximum. Longer ramps (36/60) already fill the contract.
    const monthly = rampVals.slice();
    if (rampVals.length) {
      const rampYears = Math.max(1, Math.round((state.ramp.months || 12) / 12));
      const contractMonths = Math.max(rampYears, 3) * 12;
      for (let m = monthly.length; m < contractMonths; m += 1) monthly.push(state.ramp.ceiling);
    }
    const ramp = { ceiling: state.ramp.ceiling, monthly };
    const exportPayload = {
      // The export RE-PRICES server-side, so it MUST send every flag /api/price sends -
      // see pricingInputs(). This used to be a hand-copied list that had drifted: rightsize
      // was missing, so an export re-priced the whole estate at full size while the screen
      // showed the trimmed number ($117,329 on screen vs $129,125 in the workbook on a
      // 7,490-line AWS bill). Spreading the shared object is what keeps them honest.
      ...pricingInputs(),
      bomName: state.bomName || "",
      // Universal Credits / negotiated discount. Sent as a fraction; the server also accepts a
      // percent. Third-party licensing (Windows, SQL) is excluded from the discount downstream.
      ociDiscount: Number(state.ociDiscount || 0) / 100,
      ramp,
      existingInfraCost: state.existingInfraCost || 0,
      workflowState: collectWorkflowState(),
      // A converted OCI BOM is already priced; export it in the AWS cloud-compare
      // workbook format straight from the converted pricing (no re-pricing).
      converted: !!(state.pricing && state.pricing.converted),
      convertedPricing: (state.pricing && state.pricing.converted)
        ? { rows: state.pricing.rows, totals: state.pricing.totals }
        : null,
      // Services added from the "Add OCI services" panel - priced server-side and folded
      // into the export totals and the matching Pricing Overview lines.
      extraServices: state.extraServices || [],
      // Region / AD-split / DR-region choices for the architecture diagram.
      diagramOptions: state.diagramOptions || {},
      template, // "quick" = compact BOM+Overview, "full" = 12-sheet deliverable
    };
    const response = await fetch("/api/export", {
      ...await jsonRequestOptions(exportPayload),
    });
    if (!response.ok) {
      let message = "Export failed.";
      try {
        message = (await response.json()).error || message;
      } catch (err) {
        /* non-JSON error body */
      }
      throw new Error(message);
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    // Filename = the BOM name the user typed at the top + today's date, so repeat exports
    // never silently overwrite each other in Downloads.
    const safeName = (state.bomName || "").trim().replace(/[\\/:*?"<>|]+/g, "_").replace(/\s+/g, "_");
    const suffix = "_BOM";
    const today = new Date();
    const stamp = [
      today.getFullYear(),
      String(today.getMonth() + 1).padStart(2, "0"),
      String(today.getDate()).padStart(2, "0"),
    ].join("-");
    link.download = `${safeName || "OCI"}${suffix}_${stamp}.xlsx`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    // Confirm it landed - the browser saves silently, which reads as "nothing happened".
    els.engineStatus.textContent = `${exportLabel} downloaded: ${link.download}`;
    if (isDeliverablesBom) {
      setDeliverableStatus(els.deliverablesBomStatus, `Downloaded ${link.download}`, "success");
    }
  } catch (error) {
    // A failed export used to just blink the overlay and write to a status line nobody
    // was looking at, so it read as "nothing happened". Say it out loud, and don't
    // dismiss until the user acknowledges it.
    failed = true;
    const msg = `${exportLabel} export failed - ${error.message}`;
    els.engineStatus.textContent = msg;
    if (isDeliverablesBom) {
      setDeliverableStatus(els.deliverablesBomStatus, msg, "error");
    }
    console.error("BOM export failed", error);
    if (stageTimer) clearInterval(stageTimer);
    stageTimer = null;
    if (overlayText) {
      overlayText.innerHTML = "";
      const p = document.createElement("p");
      p.className = "export-error-msg";
      p.textContent = msg;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "ghost-button";
      btn.textContent = "Dismiss";
      btn.addEventListener("click", () => {
        if (overlay) overlay.hidden = true;
        overlayText.textContent = "";
      });
      overlayText.append(p, btn);
    }
    const spinner = overlay ? overlay.querySelector(".export-spinner") : null;
    if (spinner) spinner.hidden = true;
  } finally {
    if (stageTimer) clearInterval(stageTimer);
    if (button) {
      button.disabled = false;
      button.innerHTML = original;
    }
    const spinner = overlay ? overlay.querySelector(".export-spinner") : null;
    if (!failed) {
      if (spinner) spinner.hidden = false;
      if (overlay) overlay.hidden = true;
    }
  }
}

// Capture the COMPLETE app state so a saved workflow restores the window exactly:
// the table data plus every user modification (shape/cost overrides, approvals,
// filters, selections, ramp, column choices, mode, etc.).
function collectWorkflowState() {
  return {
    __workflow: "oci-bom-app",
    version: 1,
    savedAt: new Date().toISOString(),
    intakeMode: state.intakeMode,
    providerHint: state.providerHint,
    fullServiceBeta: state.fullServiceBeta,
    hideGpuPricing: state.hideGpuPricing,
    hideWindowsPricing: state.hideWindowsPricing,
    hideSqlPricing: state.hideSqlPricing,
    cpuUnit: state.cpuUnit,
    hoursPerMonth: state.hoursPerMonth,
    hoursOverride: state.hoursOverride,
    bomName: state.bomName,
    ociDiscount: state.ociDiscount,
    rightsize: state.rightsize,
    oicMessagePacks: state.oicMessagePacks,
    selectedShape: state.selectedShape,
    selectedVendor: state.selectedVendor,
    lastShapeByVendor: state.lastShapeByVendor || {},
    existingInfraCost: state.existingInfraCost,
    crossCloudTopTier: state.crossCloudTopTier,
    workflowMaxUnlockedStep: state.workflowMaxUnlockedStep,
    uploadMetadata: state.uploadMetadata || {},
    showMissingOnly: state.showMissingOnly,
    // Add-in OCI services ("Add OCI services" panel) and the full diagram layout selections
    // (region, AD split + which resources, DR region + replication) so a reload restores the
    // exact BOM and architecture the user built.
    extraServices: state.extraServices || [],
    diagramOptions: state.diagramOptions || {},
    fields: state.fields,
    rows: state.rows,
    shapeOverrides: state.shapeOverrides,
    costOverrides: state.costOverrides,
    approvedFlags: state.approvedFlags,
    hiddenSources: state.hiddenSources,
    selectedRows: state.selectedRows,
    columnPrefs: state.columnPrefs,
    resultSort: state.resultSort,
    ramp: {
      months: state.ramp.months,
      ceiling: state.ramp.ceiling,
      points: state.ramp.points,
    },
  };
}

// Restore a saved workflow object into state, then re-price to rebuild the window.
async function applyWorkflowState(wf) {
  if (!wf || !Array.isArray(wf.rows)) {
    throw new Error("That file has no saved workflow data.");
  }
  const defaultDiagramOptions = {
    ...state_diagramOptions_default,
    adSplitResources: { ...state_diagramOptions_default.adSplitResources },
    drReplicate: { ...state_diagramOptions_default.drReplicate },
    drAdSplitResources: { ...state_diagramOptions_default.drAdSplitResources },
  };
  Object.assign(state, {
    intakeMode: "on_prem",
    providerHint: "auto",
    fullServiceBeta: false,
    hideGpuPricing: false,
    hideWindowsPricing: false,
    hideSqlPricing: false,
    cpuUnit: "auto",
    hoursPerMonth: 730,
    hoursOverride: false,
    bomName: "",
    ociDiscount: 0,
    rightsize: false,
    oicMessagePacks: 1,
    selectedShape: "e6-standard-ax",
    selectedVendor: "amd",
    lastShapeByVendor: { amd: "e6-standard-ax" },
    existingInfraCost: 0,
    crossCloudTopTier: false,
    uploadReady: false,
    workflowMaxUnlockedStep: 0,
    uploadMetadata: {},
    showMissingOnly: false,
    extraServices: [],
    diagramOptions: defaultDiagramOptions,
    fields: [],
    rows: [],
    shapeOverrides: {},
    costOverrides: {},
    approvedFlags: {},
    hiddenSources: {},
    selectedRows: {},
    columnPrefs: {},
    resultSort: { key: "document", direction: "asc" },
  });
  const assign = [
    "intakeMode", "providerHint", "fullServiceBeta", "hideGpuPricing",
    "hideWindowsPricing", "hideSqlPricing",
    "cpuUnit",
    "bomName", "ociDiscount", "rightsize", "oicMessagePacks", "selectedShape", "existingInfraCost",
    // Pricing inputs: these MUST be restored, or a re-imported BOM re-prices differently from
    // the one that was exported. hoursPerMonth/hoursOverride used to be saved and then thrown
    // away by a hard reset below.
    "hoursPerMonth", "hoursOverride",
    "selectedVendor", "lastShapeByVendor", "crossCloudTopTier", "uploadMetadata",
    "workflowMaxUnlockedStep",
    "showMissingOnly", "extraServices", "fields", "rows",
    "shapeOverrides", "costOverrides",
    "approvedFlags", "hiddenSources", "selectedRows", "columnPrefs", "resultSort",
  ];
  assign.forEach((k) => { if (wf[k] !== undefined) state[k] = wf[k]; });
  state.intakeMode = normalizeIntakeMode(state.intakeMode);
  const savedDiagramOptions =
    wf.diagramOptions && typeof wf.diagramOptions === "object" ? wf.diagramOptions : {};
  state.diagramOptions = {
    ...defaultDiagramOptions,
    ...savedDiagramOptions,
    adSplitResources: {
      ...defaultDiagramOptions.adSplitResources,
      ...(savedDiagramOptions.adSplitResources || {}),
    },
    drAdSplitResources: {
      ...defaultDiagramOptions.drAdSplitResources,
      ...(savedDiagramOptions.drAdSplitResources || {}),
    },
    drReplicate: {
      ...defaultDiagramOptions.drReplicate,
      ...(savedDiagramOptions.drReplicate || {}),
    },
  };
  state.extraServices = Array.isArray(state.extraServices) ? state.extraServices : [];
  state.fields = Array.isArray(state.fields) ? state.fields : [];
  ensureIdentityReviewFields();
  ensureHoursRunningReviewField();
  state.lastShapeByVendor =
    state.lastShapeByVendor && typeof state.lastShapeByVendor === "object"
      ? state.lastShapeByVendor
      : {};
  [
    "shapeOverrides", "costOverrides", "approvedFlags", "hiddenSources",
    "selectedRows", "columnPrefs",
  ].forEach((key) => {
    if (!state[key] || typeof state[key] !== "object" || Array.isArray(state[key])) {
      state[key] = {};
    }
  });
  if (!state.resultSort || typeof state.resultSort !== "object") {
    state.resultSort = { key: "document", direction: "asc" };
  }
  state.cpuUnit = ["auto", "vcpu", "ocpu"].includes(state.cpuUnit) ? state.cpuUnit : "auto";
  // Validate the restored hours rather than discarding them: a saved BOM that was priced on a
  // custom hours/month must re-import at that same figure, or the reopened workbook disagrees
  // with the one the customer already has.
  const savedHours = Number(state.hoursPerMonth);
  state.hoursPerMonth = savedHours > 0 && savedHours <= 744 ? savedHours : 730;
  state.hoursOverride = !!state.hoursOverride;
  state.uploadReady = state.rows.length > 0;
  state.workflowMaxUnlockedStep = state.uploadReady
    ? Math.max(
        workflowStepIndex("price"),
        Math.min(
          WORKFLOW_STEP_ORDER.length - 1,
          Number(state.workflowMaxUnlockedStep) || 0,
        ),
      )
    : 0;
  syncWorkflowAvailability();
  const restoredShape = state.rateCards.find((shape) => shape.key === state.selectedShape);
  if (restoredShape) {
    state.selectedVendor = shapeVendor(restoredShape);
    state.lastShapeByVendor[state.selectedVendor] = restoredShape.key;
    state.rateCard = restoredShape.rateCard || [];
  } else {
    state.selectedVendor = normalizeVendorKey(state.selectedVendor) || "amd";
  }
  if (wf.ramp) {
    state.ramp.months = Math.max(1, Math.min(60, Number(wf.ramp.months) || 12));
    state.ramp.ceiling = Number(wf.ramp.ceiling) || 0;
    state.ramp.points = Array.isArray(wf.ramp.points) ? wf.ramp.points : [];
    state.ramp.signature = null;
    state.ramp.restorePending = state.ramp.points.length > 0;
  } else {
    state.ramp.months = 12;
    state.ramp.ceiling = 0;
    state.ramp.points = [];
    state.ramp.signature = null;
    state.ramp.restorePending = false;
  }
  // Reflect restored simple inputs back into their controls if present.
  if (els.bomName) els.bomName.value = state.bomName || "";
  if (els.ociDiscount) els.ociDiscount.value = String(Number(state.ociDiscount || 0));
  if (typeof syncExistingInfraUi === "function") syncExistingInfraUi();
  if (typeof syncSizingModeUi === "function") syncSizingModeUi();
  if (typeof syncOptChips === "function") syncOptChips();
  if (els.oicMessagePacks) els.oicMessagePacks.value = state.oicMessagePacks || 1;
  syncModeUi();
  setCpuUnit(state.cpuUnit);
  syncVendorForSelectedShape();
  renderProcessorPicker();
  renderShapeChoices();
  renderShapeDetail();
  // Reflect the restored licensing/GPU toggles into their checkboxes.
  if (els.hideGpuToggle) els.hideGpuToggle.checked = !!state.hideGpuPricing;
  if (els.hideWindowsToggle) els.hideWindowsToggle.checked = !!state.hideWindowsPricing;
  if (els.hideSqlToggle) els.hideSqlToggle.checked = !!state.hideSqlPricing;
  // Reflect restored diagram-layout selections into their controls (region combobox,
  // AD-split toggle + chips, DR toggle + region + replication chips).
  if (state.diagramOptions) {
    const d = state.diagramOptions;
    const chk = (id, v) => { const el = document.querySelector(id); if (el) el.checked = !!v; };
    const sel = (id, v) => {
      const s = document.querySelector(id);
      if (!s) return;
      if (v != null) s.value = v;
      const input = s.closest(".diagram-combo")?.querySelector(".diagram-combo-input");
      if (input) input.value = s.selectedOptions[0]?.textContent.trim() || "";
    };
    sel("#primaryRegion", d.primaryRegion);
    sel("#drRegion", d.drRegion);
    chk("#splitAcrossADs", d.splitADs);
    chk("#adSplitVms", d.adSplitResources ? d.adSplitResources.vms : true);
    chk("#adSplitDbs", d.adSplitResources ? d.adSplitResources.dbs : true);
    chk("#enableDr", d.enableDr);
    chk("#drSplitAcrossADs", !!d.drSplitADs);
    chk("#drAdSplitVms", d.drAdSplitResources ? d.drAdSplitResources.vms : true);
    chk("#drAdSplitDbs", d.drAdSplitResources ? d.drAdSplitResources.dbs : true);
    chk("#drRepVms", d.drReplicate ? d.drReplicate.vms : true);
    chk("#drRepDbs", d.drReplicate ? d.drReplicate.dbs : true);
    chk("#drRepObj", d.drReplicate ? d.drReplicate.object : true);
    if (typeof _syncAdSplitControl === "function") _syncAdSplitControl();
    if (typeof _syncDrAdSplitControl === "function") _syncDrAdSplitControl();
    const drSub = document.querySelector("#drSubOptions"); if (drSub) drSub.hidden = !d.enableDr;
  }
  if (typeof renderTable === "function") renderTable();
  await priceRows();
  // priceRows() doesn't touch the "Add OCI services" panel, so re-render the cart and fold the
  // restored add-in services back into the results totals - otherwise a reloaded BOM shows an
  // empty cart and drops the extras the user selected last time.
  if (typeof renderServiceCart === "function") renderServiceCart();
  if (typeof refreshResultsTotals === "function") refreshResultsTotals();
  // Opening a previous BOM jumps straight to the results page (page 5).
  if (state.pricing) showResultsPage();
}

// Show the dropped/selected workflow file name + whether it was accepted.
function clearIntakeStatuses() {
  // Hide the "Load previous BOM" and "Convert an alternate BOM" result banners so a stale
  // success/error doesn't linger when the user switches to a different intake path.
  if (els.loadWorkflowStatus) els.loadWorkflowStatus.hidden = true;
  if (els.convertBomStatus) els.convertBomStatus.hidden = true;
  if (els.otherBillSheets) els.otherBillSheets.hidden = true;
  if (els.otherBillWarnings) els.otherBillWarnings.hidden = true;
}

function setWorkflowStatus(name, message, state) {
  const el = els.loadWorkflowStatus;
  if (!el) return;
  el.hidden = false;
  el.className = `load-workflow-status lws-${state}`;
  const icon = state === "ok" ? "✓" : state === "error" ? "✕" : "⏳";
  el.querySelector(".lws-icon").textContent = icon;
  el.querySelector(".lws-name").textContent = name || "";
  el.querySelector(".lws-state").textContent = message || "";
}

async function loadWorkflowFromFile(file) {
  if (!file) return;
  clearIntakeStatuses();   // switching to load - clear the convert banner
  const nm = file.name || "file";
  const okExt = /\.(json|xlsx)$/i.test(nm);
  setWorkflowStatus(nm, okExt ? "loaded - checking…" : "not a .json or .xlsx file", okExt ? "loading" : "error");
  if (!okExt) return;
  if (els.priceSpinner) {
    els.priceSpinner.querySelector(".price-spinner-text").textContent = "Loading workflow…";
    els.priceSpinner.hidden = false;
  }
  try {
    let wf;
    if (nm.toLowerCase().endsWith(".json")) {
      wf = JSON.parse(await file.text());
    } else {
      const fd = new FormData();
      fd.append("file", file);
      const resp = await fetch("/api/load-workflow", { method: "POST", body: fd });
      const payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || "Could not read workflow.");
      wf = payload.workflow;
    }
    await applyWorkflowState(wf);
    setWorkflowStatus(nm, "✓ Accepted - BOM restored", "ok");
  } catch (error) {
    els.engineStatus.textContent = `Workflow load failed: ${error.message}`;
    if (els.priceSpinner) els.priceSpinner.hidden = true;
    setWorkflowStatus(nm, `✕ Not accepted - ${error.message}`, "error");
  } finally {
    if (els.priceSpinner) els.priceSpinner.querySelector(".price-spinner-text").textContent = "Updating…";
  }
}

// "Foreign" BOMs include this app's OWN exports - people re-upload the workbook they just
// downloaded. Every export embeds its full app state on a hidden _workflow sheet, so restoring
// that reproduces the estimate exactly (ramp, discount, added services, diagram options)
// instead of re-deriving an approximation from the numbers printed on the sheets.
// Returns true when the file carried state and the workflow was restored.
async function tryRestoreOwnExport(file) {
  try {
    const fd = new FormData();
    fd.append("file", file);
    const resp = await fetch("/api/load-workflow", { method: "POST", body: fd });
    if (!resp.ok) return false;               // no embedded state - the BOM came from elsewhere
    const payload = await resp.json();
    if (!payload || !payload.workflow) return false;
    await applyWorkflowState(payload.workflow);
    setConvertStatus(file.name || "bom", "✓ this app's own export - BOM restored in full", "ok");
    return true;
  } catch (err) {
    return false;                             // fall through to SKU conversion
  }
}

function summarizePricingRows(pricingRows, cloudBill) {
  const sortedRows = pricingRows
    .slice()
    .sort((a, b) => Number(b.monthly || 0) - Number(a.monthly || 0));

  if (cloudBill) {
    return sortedRows.slice(0, 10).map((row) => ({
      name: fallbackEntityName(row),
      environment: row.environment || "-",
      monthly: Number(row.monthly || 0),
      workloadCount: 1,
    }));
  }

  const groups = new Map();
  sortedRows.forEach((row) => {
    const name = String(row.applicationName || fallbackEntityName(row)).trim() || "Workload";
    const environment = String(row.environment || "-").trim() || "-";
    const key = `${name.toLocaleLowerCase()}\u0000${environment.toLocaleLowerCase()}`;
    const group = groups.get(key) || {
      name,
      environment,
      monthly: 0,
      workloadCount: 0,
    };
    group.monthly += Number(row.monthly || 0);
    group.workloadCount += 1;
    groups.set(key, group);
  });

  return Array.from(groups.values())
    .sort((a, b) => b.monthly - a.monthly)
    .slice(0, 10);
}

function renderPricing(pricing) {
  const shape = pricing.selectedShape || selectedShape();
  const cloudBill = pricing.intakeMode === "cloud_bill" || pricing.cloudBillMode;
  const modeLabel = cloudBill ? "Cloud bill" : pricing.fullServiceBeta ? "Full service" : shape.label;
  els.engineStatus.textContent = `${modeLabel}: deterministic pricing`;
  els.pricingSummary.className = "pricing-result";

  const warning = pricing.llmWarning ? `<p class="warning">${pricing.llmWarning}</p>` : "";
  const rows = summarizePricingRows(pricing.rows, cloudBill)
    .map(
      (row) => `
        <tr>
          <td>
            <span class="pricing-summary-name">${escapeHtml(row.name)}</span>
            ${
              !cloudBill && row.workloadCount > 1
                ? `<small>${formatNumber(row.workloadCount)} workload rows</small>`
                : ""
            }
          </td>
          <td>${escapeHtml(row.environment || "-")}</td>
          <td>${formatCurrency(row.monthly)}</td>
        </tr>
      `,
    )
    .join("");

  els.pricingSummary.innerHTML = `
    ${warning}
    <div class="kpis">
      <div class="kpi"><span>Monthly</span><strong>${formatCurrency(pricing.totals.monthly)}</strong></div>
      <div class="kpi"><span>Annual</span><strong>${formatCurrency(pricing.totals.annual)}</strong></div>
      ${
        cloudBill
          ? `<div class="kpi"><span>Mapped lines</span><strong>${formatNumber(pricing.totals.mappedServiceRows)}</strong></div>
             <div class="kpi"><span>Needs review</span><strong>${formatNumber(pricing.totals.unpricedServiceRows)}</strong></div>`
          : pricing.fullServiceBeta
          ? `<div class="kpi"><span>Mapped services</span><strong>${formatNumber(pricing.totals.mappedServiceRows)}</strong></div>
             <div class="kpi"><span>Review rows</span><strong>${formatNumber(pricing.totals.unpricedServiceRows)}</strong></div>`
          : `<div class="kpi"><span>OCPUs</span><strong>${formatNumber(pricing.totals.ocpus)}</strong></div>
             <div class="kpi"><span>Shape</span><strong>${escapeHtml(shape.shortLabel || shape.label)}</strong></div>`
      }
    </div>
    <table class="result-table">
      <thead>
        <tr><th>${cloudBill ? "Source line" : "Application"}</th><th>${cloudBill ? "Context" : "Env"}</th><th>Monthly</th></tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

function showIntakePage() {
  els.intakePage.classList.remove("is-hidden");
  els.shapePage.classList.add("is-hidden");
  els.networkingPage.classList.add("is-hidden");
  els.architecturePage.classList.add("is-hidden");
  els.deliverablesPage.classList.add("is-hidden");
  els.resultsPage.classList.add("is-hidden");
  els.otherCloudsPage.classList.add("is-hidden");
  syncIntakeLayout();
  if (state.rows.length && isWorkflowStepUnlocked("review")) {
    els.uploadPanel.classList.add("is-hidden");
    els.reviewPanel.classList.remove("is-hidden");
    setStep("review");
  } else {
    els.uploadPanel.classList.remove("is-hidden");
    els.reviewPanel.classList.add("is-hidden");
    setStep("upload");
  }
}

function showUploadPage() {
  els.intakePage.classList.remove("is-hidden");
  els.shapePage.classList.add("is-hidden");
  els.networkingPage.classList.add("is-hidden");
  els.architecturePage.classList.add("is-hidden");
  els.deliverablesPage.classList.add("is-hidden");
  els.resultsPage.classList.add("is-hidden");
  els.otherCloudsPage.classList.add("is-hidden");
  els.uploadPanel.classList.remove("is-hidden");
  els.reviewPanel.classList.add("is-hidden");
  syncIntakeLayout();
  setStep("upload");
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function showReviewPage() {
  if (!state.rows.length || !isWorkflowStepUnlocked("review")) {
    showUploadPage();
    return;
  }
  els.intakePage.classList.remove("is-hidden");
  els.shapePage.classList.add("is-hidden");
  els.networkingPage.classList.add("is-hidden");
  els.architecturePage.classList.add("is-hidden");
  els.deliverablesPage.classList.add("is-hidden");
  els.resultsPage.classList.add("is-hidden");
  els.otherCloudsPage.classList.add("is-hidden");
  els.uploadPanel.classList.add("is-hidden");
  els.reviewPanel.classList.remove("is-hidden");
  renderTable();
  syncIntakeLayout();
  setStep("review");
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function showShapePage() {
  if (!state.rows.length || !isWorkflowStepUnlocked("shape")) {
    showReviewPage();
    return;
  }
  els.intakePage.classList.add("is-hidden");
  els.shapePage.classList.remove("is-hidden");
  els.networkingPage.classList.add("is-hidden");
  els.architecturePage.classList.add("is-hidden");
  els.deliverablesPage.classList.add("is-hidden");
  els.resultsPage.classList.add("is-hidden");
  els.otherCloudsPage.classList.add("is-hidden");
  renderProcessorPicker();
  renderShapeChoices();
  renderShapeDetail();
  setStep("shape");
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function showNetworkingPage() {
  if (!isWorkflowStepUnlocked("networking")) {
    showShapePage();
    return;
  }
  els.intakePage.classList.add("is-hidden");
  els.shapePage.classList.add("is-hidden");
  els.networkingPage.classList.remove("is-hidden");
  els.architecturePage.classList.add("is-hidden");
  els.deliverablesPage.classList.add("is-hidden");
  els.resultsPage.classList.add("is-hidden");
  els.otherCloudsPage.classList.add("is-hidden");
  if (els.networkingShape) {
    const shape = selectedShape();
    els.networkingShape.textContent = shape.shortLabel || shape.label;
  }
  if (state.pricing) {
    setNetworkingPageStatus();
  } else if (state.rows.length) {
    setNetworkingPageStatus();
  } else {
    setNetworkingPageStatus("Upload inventory before building the complete estimate.");
  }
  renderServiceCart();
  if (!(state.catalog?.groups?.length || state.catalog?.results?.length)) {
    fetchCatalog();
  }
  setStep("networking");
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function showArchitecturePage() {
  if (!isWorkflowStepUnlocked("architecture")) {
    showOtherCloudsPage();
    return;
  }
  els.intakePage.classList.add("is-hidden");
  els.shapePage.classList.add("is-hidden");
  els.networkingPage.classList.add("is-hidden");
  els.architecturePage.classList.remove("is-hidden");
  els.deliverablesPage.classList.add("is-hidden");
  els.resultsPage.classList.add("is-hidden");
  els.otherCloudsPage.classList.add("is-hidden");
  if (els.architectureShape) {
    const shape = selectedShape();
    els.architectureShape.textContent = shape.shortLabel || shape.label;
  }
  const diagramUnavailable = state.pricing?.diagramAvailable === false;
  if (els.downloadDiagram) {
    els.downloadDiagram.disabled = !state.pricing || diagramUnavailable;
  }
  if (diagramUnavailable) {
    setArchitectureExportStatus(
      state.pricing.diagramUnavailableReason ||
        "This converted BOM does not contain workload-level sizing for an architecture diagram.",
      "error",
    );
  } else if (state.pricing) {
    setArchitectureExportStatus("Ready to generate.");
  } else if (state.rows.length) {
    setArchitectureExportStatus("Choose a shape and prepare pricing before exporting.");
  } else {
    setArchitectureExportStatus("Upload inventory before generating a diagram.");
  }
  setStep("architecture");
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function showResultsPage() {
  if (!isWorkflowStepUnlocked("price")) {
    showNetworkingPage();
    return;
  }
  els.intakePage.classList.add("is-hidden");
  els.shapePage.classList.add("is-hidden");
  els.networkingPage.classList.add("is-hidden");
  els.architecturePage.classList.add("is-hidden");
  els.deliverablesPage.classList.add("is-hidden");
  els.resultsPage.classList.remove("is-hidden");
  els.otherCloudsPage.classList.add("is-hidden");
  setStep("price");
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function showOtherCloudsPage() {
  if (!isWorkflowStepUnlocked("other-clouds")) {
    showResultsPage();
    return;
  }
  els.intakePage.classList.add("is-hidden");
  els.shapePage.classList.add("is-hidden");
  els.networkingPage.classList.add("is-hidden");
  els.architecturePage.classList.add("is-hidden");
  els.deliverablesPage.classList.add("is-hidden");
  els.resultsPage.classList.add("is-hidden");
  els.otherCloudsPage.classList.remove("is-hidden");
  if (els.otherCloudsShape) {
    const shape = selectedShape();
    els.otherCloudsShape.textContent = shape.shortLabel || shape.label;
  }
  renderCrossCloud();
  setStep("other-clouds");
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function deliverableBaseName() {
  return (state.bomName || "OCI")
    .trim()
    .replace(/[\\/:*?"<>|]+/g, "_")
    .replace(/\s+/g, "_") || "OCI";
}

function showDeliverablesPage() {
  if (!isWorkflowStepUnlocked("deliverables")) {
    showArchitecturePage();
    return;
  }
  els.intakePage.classList.add("is-hidden");
  els.shapePage.classList.add("is-hidden");
  els.networkingPage.classList.add("is-hidden");
  els.architecturePage.classList.add("is-hidden");
  els.resultsPage.classList.add("is-hidden");
  els.otherCloudsPage.classList.add("is-hidden");
  els.deliverablesPage.classList.remove("is-hidden");

  const base = deliverableBaseName();
  const today = new Date();
  const stamp = [
    today.getFullYear(),
    String(today.getMonth() + 1).padStart(2, "0"),
    String(today.getDate()).padStart(2, "0"),
  ].join("-");
  if (els.deliverablesBomFilename) {
    els.deliverablesBomFilename.textContent = `${base}_BOM_${stamp}.xlsx`;
  }
  if (els.deliverablesArchitectureFilename) {
    els.deliverablesArchitectureFilename.textContent = `${base}_architecture.zip`;
  }

  const diagramUnavailable = state.pricing?.diagramAvailable === false;
  if (els.deliverablesFullBom) {
    els.deliverablesFullBom.disabled = !state.pricing;
  }
  if (els.deliverablesDiagram) {
    els.deliverablesDiagram.disabled = !state.pricing || diagramUnavailable;
  }
  setDeliverableStatus(
    els.deliverablesBomStatus,
    state.pricing ? "Ready to generate." : "Prepare pricing before downloading.",
    state.pricing ? "" : "error",
  );
  setArchitectureExportStatus(
    diagramUnavailable
      ? state.pricing.diagramUnavailableReason ||
        "This converted BOM does not contain workload-level sizing for an architecture diagram."
      : state.pricing
      ? "Ready to generate."
      : "Prepare pricing before downloading.",
    diagramUnavailable || !state.pricing ? "error" : "",
  );
  setStep("deliverables");
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function openPriceStep() {
  if (state.pricing) {
    setPricePageStatus();
    showResultsPage();
    return;
  }
  if (state.rows.length) {
    showResultsPage();
    priceRows({ destination: "price" });
    return;
  }
  showUploadPage();
  els.uploadStatus.textContent = "Upload inventory before viewing price.";
  els.uploadStatus.style.color = "var(--danger)";
}

async function openOtherCloudsStep() {
  if (state.pricing) {
    showOtherCloudsPage();
    return;
  }
  if (state.rows.length) {
    showResultsPage();
    const pricing = await priceRows({ destination: "price" });
    if (pricing) showOtherCloudsPage();
    return;
  }
  showUploadPage();
  els.uploadStatus.textContent = "Upload inventory before estimating other clouds.";
  els.uploadStatus.style.color = "var(--danger)";
}

async function openDeliverablesStep() {
  if (state.pricing) {
    showDeliverablesPage();
    return;
  }
  if (state.rows.length) {
    showResultsPage();
    const pricing = await priceRows({ destination: "price" });
    if (pricing) showDeliverablesPage();
    return;
  }
  showUploadPage();
  els.uploadStatus.textContent = "Upload inventory before downloading deliverables.";
  els.uploadStatus.style.color = "var(--danger)";
}

function navigateStep(step) {
  if (!isWorkflowStepUnlocked(step)) return;
  if (step === "upload") {
    showUploadPage();
    return;
  }
  if (step === "review") {
    showReviewPage();
    return;
  }
  if (step === "shape") {
    showShapePage();
    return;
  }
  if (step === "networking") {
    showNetworkingPage();
    return;
  }
  if (step === "architecture") {
    showArchitecturePage();
    return;
  }
  if (step === "price") {
    openPriceStep();
    return;
  }
  if (step === "other-clouds") {
    openOtherCloudsStep();
    return;
  }
  if (step === "deliverables") {
    openDeliverablesStep();
  }
}

function aggregateSkuCosts(pricing) {
  const bySku = new Map();
  pricing.rows.forEach((row) => {
    (row.lineItems || []).forEach((item) => {
      // Group by SKU. Line items with NO sku (SQL/ADW license-included, carried-over AWS
      // cost, etc.) are grouped by their description instead of all being lumped under
      // whichever empty-SKU line came first - otherwise SQL licenses + Autonomous DW get
      // mislabeled as "Carried over from source AWS cost". Trailing size hints like
      // "(4 OCPU)" / "(GB-mo)" are stripped so same-kind lines merge into one bucket.
      const label = item.sku
        ? item.description
        : (item.description || "Unlabeled").replace(/\s*\([^)]*\)\s*$/, "");
      const key = item.sku || label;
      const current = bySku.get(key) || { sku: item.sku, description: label, monthly: 0 };
      current.monthly += item.monthly;
      bySku.set(key, current);
    });
  });
  // Services added on the Services step aren't part of pricing.rows, so fold them in here -
  // otherwise they're counted in the headline total but missing from the Cost Mix chart.
  (state.extraServices || []).forEach((s) => {
    const monthly = Number(s.monthly || 0);
    if (!monthly) return;
    const label = String(s.name || "").trim() || "Added service";
    const key = s.sku || label;
    const current = bySku.get(key) || { sku: s.sku || "", description: label, monthly: 0 };
    current.monthly += monthly;
    bySku.set(key, current);
  });
  return [...bySku.values()].sort((a, b) => b.monthly - a.monthly);
}

const RAMP_CHART = {
  width: 760,
  height: 330,
  pad: { top: 30, right: 34, bottom: 48, left: 78 },
};

const RAMP_CHART_COMPACT = {
  width: 430,
  height: 300,
  pad: { top: 28, right: 22, bottom: 42, left: 58 },
  compact: true,
};

let rampDragPointerId = null;
let rampDragPointId = null;

function currentRampChartConfig() {
  const width = els.rampChart?.getBoundingClientRect().width || RAMP_CHART.width;
  return width < 560 ? RAMP_CHART_COMPACT : RAMP_CHART;
}

function formatCompactCurrency(value) {
  const number = Number(value || 0);
  const abs = Math.abs(number);
  if (abs >= 1000000) {
    const amount = number / 1000000;
    return `$${amount >= 10 ? amount.toFixed(0) : amount.toFixed(1)}M`;
  }
  if (abs >= 1000) {
    const amount = number / 1000;
    return `$${amount >= 10 ? amount.toFixed(0) : amount.toFixed(1)}K`;
  }
  return `$${Math.round(number)}`;
}

function newRampPoint(month, monthly) {
  const pointMonth = Math.round(clamp(month, 1, state.ramp.months));
  const point = {
    id: `ramp-point-${state.ramp.nextPointId}`,
    month: pointMonth,
    monthly: clampRampMonthly("", pointMonth, monthly),
  };
  state.ramp.nextPointId += 1;
  return point;
}

function rampPointNeighbors(pointId, month) {
  const targetMonth = Math.round(clamp(month, 1, state.ramp.months));
  const others = state.ramp.points
    .filter((point) => point.id !== pointId)
    .map((point) => ({
      ...point,
      month: Math.round(clamp(point.month, 1, state.ramp.months)),
      monthly: clamp(point.monthly, 0, state.ramp.ceiling),
    }))
    .sort((a, b) => a.month - b.month || a.id.localeCompare(b.id));
  let previous = { id: "ramp-origin", month: 0, monthly: 0 };
  let next = null;
  for (const point of others) {
    if (point.month <= targetMonth) {
      previous = point;
    } else {
      next = point;
      break;
    }
  }
  return { previous, next };
}

function clampRampMonthly(pointId, month, monthly) {
  const { previous, next } = rampPointNeighbors(pointId, month);
  return clamp(monthly, previous.monthly, next?.monthly ?? state.ramp.ceiling);
}

function sortedRampPoints(includeOrigin = true) {
  const points = state.ramp.points
    .map((point) => ({
      ...point,
      month: Math.round(clamp(point.month, 1, state.ramp.months)),
      monthly: clamp(point.monthly, 0, state.ramp.ceiling),
    }))
    .sort((a, b) => a.month - b.month || a.id.localeCompare(b.id));
  return includeOrigin ? [{ id: "ramp-origin", month: 0, monthly: 0, fixed: true }, ...points] : points;
}

function selectedRampPoint() {
  return (
    state.ramp.points.find((point) => point.id === state.ramp.selectedPointId) ||
    sortedRampPoints(false)[state.ramp.points.length - 1] ||
    null
  );
}

function selectRampPoint(pointId) {
  if (state.ramp.points.some((point) => point.id === pointId)) {
    state.ramp.selectedPointId = pointId;
  }
}

function setRampPoint(pointId, month, monthly) {
  const point = state.ramp.points.find((item) => item.id === pointId);
  if (!point) return;
  const pointMonth = Math.round(clamp(month, 1, state.ramp.months));
  point.month = pointMonth;
  point.monthly = clampRampMonthly(point.id, pointMonth, monthly);
  state.ramp.selectedPointId = point.id;
  renderConsumptionRamp();
}

function rampValueAtMonth(month) {
  const points = sortedRampPoints(true);
  const targetMonth = clamp(month, 0, state.ramp.months);
  for (let index = 1; index < points.length; index += 1) {
    const prev = points[index - 1];
    const next = points[index];
    if (targetMonth <= next.month) {
      const span = Math.max(1, next.month - prev.month);
      const progress = (targetMonth - prev.month) / span;
      return prev.monthly + (next.monthly - prev.monthly) * progress;
    }
  }
  return points[points.length - 1]?.monthly || 0;
}

function addRampPoint(month, monthly) {
  if (state.ramp.points.length >= 12) return selectedRampPoint();
  const point = newRampPoint(month, monthly);
  state.ramp.points.push(point);
  state.ramp.selectedPointId = point.id;
  renderConsumptionRamp();
  return point;
}

function addRampPointInLargestGap() {
  const points = sortedRampPoints(true);
  let bestStart = points[0];
  let bestEnd = points[1] || { month: state.ramp.months, monthly: state.ramp.ceiling };
  for (let index = 1; index < points.length; index += 1) {
    const start = points[index - 1];
    const end = points[index];
    if (end.month - start.month > bestEnd.month - bestStart.month) {
      bestStart = start;
      bestEnd = end;
    }
  }
  const month = Math.max(1, Math.round((bestStart.month + bestEnd.month) / 2));
  addRampPoint(month, rampValueAtMonth(month));
}

function removeSelectedRampPoint() {
  if (state.ramp.points.length <= 1) return;
  const selected = selectedRampPoint();
  if (!selected) return;
  state.ramp.points = state.ramp.points.filter((point) => point.id !== selected.id);
  state.ramp.selectedPointId = sortedRampPoints(false).at(-1)?.id || null;
  renderConsumptionRamp();
}

function rampMonthlyValues() {
  const values = [];
  for (let month = 1; month <= state.ramp.months; month += 1) {
    values.push(rampValueAtMonth(month));
  }
  return values;
}

// Every input the SERVER re-prices from, in one place.
//
// /api/price and /api/export both run calculate_pricing() from scratch on whatever they are
// sent - the export does NOT reuse the pricing already on screen. So any flag one call sends
// and the other omits makes the workbook disagree with the app, silently and only in the
// money. Both call sites spread this object; add new pricing inputs here, never inline.
function pricingInputs() {
  return {
    fields: state.fields,
    rows: state.rows,
    shape: state.selectedShape,
    intakeMode: state.intakeMode,
    providerHint: state.providerHint,
    fullServiceBeta: state.fullServiceBeta,
    hideGpuPricing: state.hideGpuPricing,
    hideWindowsPricing: state.hideWindowsPricing,
    hideSqlPricing: state.hideSqlPricing,
    cpuUnit: state.cpuUnit,
    shapeOverrides: state.shapeOverrides,
    costOverrides: state.costOverrides,
    hoursPerMonth: state.hoursPerMonth,
    hoursOverride: state.hoursOverride,
    oicMessagePacks: state.oicMessagePacks,
    // Gen-gap sizing trim. Off = map every workload 1:1 with the source.
    rightsize: !!state.rightsize,
  };
}

function windowsLicensingMonthly(pricing) {
  return (pricing?.rows || []).reduce((t, r) => t + Number(r.windowsLicenseMonthly || 0), 0);
}

// Windows licensing is a LINE ITEM inside totals.monthly for on-prem inventory, but in
// cloud-bill mode the backend deliberately keeps it OUT of totals.monthly and reports it only
// per row (windowsLicenseMonthly). Treating it as always-included understated the cloud-bill
// headline by the full Windows amount while the exported BOM included it - so the app and the
// workbook disagreed (e.g. $126,162.85 on screen vs $146,043.08 in the export).
function windowsInTotals(pricing) {
  return !(pricing?.intakeMode === "cloud_bill" || pricing?.cloudBillMode);
}

function sqlLicensingMonthly(pricing) {
  return (pricing?.rows || []).reduce((t, r) => t + Number(r.sqlLicenseMonthly || 0), 0);
}

function ociMonthlyTotal(pricing) {
  // Added services are client-side selections, so they're added here in both modes.
  const base = Number(pricing?.totals?.monthly || 0);
  const win = windowsLicensingMonthly(pricing);
  const sql = sqlLicensingMonthly(pricing);
  // Windows sits inside totals.monthly for on-prem but outside it for a cloud bill; SQL is a
  // line item inside totals.monthly in both.
  const winInside = windowsInTotals(pricing);
  const total = base + (winInside ? 0 : win) + extraServicesMonthly();
  const d = Math.min(0.99, Math.max(0, Number(state.ociDiscount || 0) / 100));
  if (!d) return Math.max(0, total);
  // The discount applies to OCI services only - third-party licensing (Windows, SQL Server) is
  // never discounted, which is exactly how the exported workbook computes it. Added services
  // flagged thirdParty (the Licensing group) are excluded too.
  const extrasThirdParty = (state.extraServices || [])
    .reduce((t, s) => t + (s.thirdParty ? Number(s.monthly || 0) : 0), 0);
  const licensing = win + sql + extrasThirdParty;
  const discountable = Math.max(0, total - licensing);
  return Math.max(0, discountable * (1 - d) + licensing);
}

function initializeConsumptionRamp(pricing) {
  const ceiling = ociMonthlyTotal(pricing);
  const shapeKey = pricing.selectedShape?.key || state.selectedShape;
  const signature = `${shapeKey}:${ceiling}:${pricing.rows.length}`;
  if (state.ramp.signature !== signature) {
    const restoredCeiling = Number(state.ramp.ceiling || 0);
    const restoredPoints = state.ramp.restorePending
      ? (state.ramp.points || []).map((point) => ({ ...point }))
      : [];
    state.ramp.signature = signature;
    state.ramp.ceiling = ceiling;
    state.ramp.nextPointId = 1;
    if (restoredPoints.length) {
      const ratio = restoredCeiling > 0 ? ceiling / restoredCeiling : 1;
      state.ramp.points = restoredPoints.map((point) => ({
        ...point,
        month: Math.round(clamp(point.month, 1, state.ramp.months)),
        monthly: clamp(Number(point.monthly || 0) * ratio, 0, ceiling),
      }));
      const maxId = state.ramp.points.reduce((max, point) => {
        const match = String(point.id || "").match(/(\d+)$/);
        return Math.max(max, match ? Number(match[1]) : 0);
      }, 0);
      state.ramp.nextPointId = maxId + 1;
    } else {
      // Start every new estimate in month 1 and grow continuously to the selected
      // horizon. Clear the prior curve before creating points so repricing cannot clamp
      // the new defaults against stale zero-value handles.
      state.ramp.points = [];
      const seedMonths = state.ramp.months || 12;
      const seedDots = Math.min(4, seedMonths);
      for (let i = 0; i < seedDots; i += 1) {
        const month = seedDots === 1
          ? 1
          : Math.floor(1 + ((seedMonths - 1) * i) / (seedDots - 1));
        state.ramp.points.push(newRampPoint(month, ceiling * (month / seedMonths)));
      }
    }
    state.ramp.restorePending = false;
    state.ramp.selectedPointId = state.ramp.points.at(-1).id;
  } else {
    state.ramp.ceiling = ceiling;
    state.ramp.points.forEach((point) => {
      point.month = Math.round(clamp(point.month, 1, state.ramp.months));
      point.monthly = clamp(point.monthly, 0, ceiling);
    });
  }
  renderConsumptionRamp();
}

function renderConsumptionRamp() {
  if (!els.rampChart) return;

  const ceiling = Math.max(0, Number(state.ramp.ceiling || 0));
  const months = state.ramp.months;
  const { width, height, pad, compact } = currentRampChartConfig();
  const innerWidth = width - pad.left - pad.right;
  const innerHeight = height - pad.top - pad.bottom;
  const baselineY = pad.top + innerHeight;
  const valueCeiling = Math.max(ceiling, 1);
  const xForMonth = (month) => pad.left + (clamp(month, 0, months) / months) * innerWidth;
  const yForValue = (value) => baselineY - (clamp(value, 0, valueCeiling) / valueCeiling) * innerHeight;
  const pathPoints = sortedRampPoints(true);
  const linePath = pathPoints
    .map((point, index) => `${index ? "L" : "M"} ${xForMonth(point.month).toFixed(2)} ${yForValue(point.monthly).toFixed(2)}`)
    .join(" ");
  const finalMonthly = pathPoints.at(-1)?.monthly || 0;
  const extendedPath = `${linePath} L ${xForMonth(months).toFixed(2)} ${yForValue(finalMonthly).toFixed(2)}`;
  const areaPath = `${extendedPath} L ${xForMonth(months).toFixed(2)} ${baselineY} L ${xForMonth(0).toFixed(2)} ${baselineY} Z`;
  const selected = selectedRampPoint();
  const selectedX = selected ? xForMonth(selected.month) : null;
  const selectedY = selected ? yForValue(selected.monthly) : null;
  const labelOnLeft = selectedX > width - (compact ? 145 : 230);
  const labelX = labelOnLeft ? selectedX - 14 : selectedX + 14;
  const labelY = selectedY == null ? 0 : Math.max(pad.top + 18, selectedY - 14);
  const labelAnchor = labelOnLeft ? "end" : "start";
  const yTicks = [0, 0.25, 0.5, 0.75, 1];
  const xTicks = [];
  // Short ramps (<=12 mo) label every month 1..N; longer ramps label every 12.
  if (months <= 12) {
    for (let m = 1; m <= months; m += 1) xTicks.push(m);
  } else {
    for (let m = 0; m <= months; m += 12) xTicks.push(m);
  }

  els.rampChart.setAttribute("viewBox", `0 0 ${width} ${height}`);
  els.rampCeilingLabel.textContent = `BOM Maximum ${formatCurrency(ceiling)}/mo`;
  els.rampPeakMonth.value = selected ? selected.month : "";
  els.rampPeakMonthly.max = ceiling.toFixed(2);
  els.rampPeakMonthly.value = selected ? selected.monthly.toFixed(2) : "";
  els.removeRampPoint.disabled = state.ramp.points.length <= 1;
  els.addRampPoint.disabled = state.ramp.points.length >= 12;

  const values = rampMonthlyValues();
  const rampYears = Math.max(1, Math.round(months / 12));
  // Short ramps (12/24 mo) still model a full 3-year contract: the ramp covers the
  // early months and every year after the ramp runs at the full BOM maximum.
  const contractYears = Math.max(rampYears, 3);
  const contractMonths = contractYears * 12;
  const fullYear = ceiling * 12;
  const yearListSpend = (y) =>
    y < rampYears
      ? values.slice(y * 12, (y + 1) * 12).reduce((sum, value) => sum + value, 0)
      : fullYear;
  let contractListTotal = 0;
  for (let y = 0; y < contractYears; y += 1) contractListTotal += yearListSpend(y);

  els.rampThreeYearTotal.textContent = formatCurrency(contractListTotal);
  els.rampAvgMonthly.textContent = formatCurrency(contractListTotal / contractMonths);
  [
    [els.rampYearOneTotal, yearListSpend(0)],
    [els.rampYearTwoTotal, yearListSpend(1)],
    [els.rampYearThreeTotal, yearListSpend(2)],
    [els.rampYearFourTotal, yearListSpend(3)],
    [els.rampYearFiveTotal, yearListSpend(4)],
  ].forEach(([element, listValue]) => {
    if (!element) return;
    element.textContent = formatCompactCurrency(listValue);
    element.title = formatCurrency(listValue);
  });
  // Show/hide years 4-5 for the chosen contract length.
  if (els.rampYearFourBox) els.rampYearFourBox.hidden = contractYears < 4;
  if (els.rampYearFiveBox) els.rampYearFiveBox.hidden = contractYears < 5;
  const years = contractYears;
  if (els.rampHeading) {
    const m = state.ramp.months || 36;
    els.rampHeading.textContent = `Build a ${m}-Month Ramp`;
    // Keep the "selected dot month" control in step with the chosen ramp length.
    if (els.rampPeakMonth) {
      els.rampPeakMonth.max = String(m);
      const hint = els.rampPeakMonth.parentElement?.querySelector("small");
      if (hint) hint.textContent = `Month 1 to month ${m}`;
    }
  }
  if (els.rampContractNote) {
    els.rampContractNote.textContent = Array.from({ length: years }, (_, i) => `Year ${i + 1}`).join(" + ");
  }
  // The on-prem baseline readout compares against the OCI monthly total, which the discount
  // and licensing toggles move, so refresh it whenever the ramp block re-renders.
  if (typeof renderExistingInfraHint === "function") renderExistingInfraHint();

  const handleMarkup = sortedRampPoints(false)
    .map((point) => {
      const x = xForMonth(point.month);
      const y = yForValue(point.monthly);
      const selectedClass = point.id === state.ramp.selectedPointId ? " is-selected" : "";
      return `
        <circle class="ramp-handle-pulse${selectedClass}" cx="${x}" cy="${y}" r="16" data-ramp-point-id="${point.id}"></circle>
        <circle class="ramp-handle${selectedClass}" cx="${x}" cy="${y}" r="7" data-ramp-point-id="${point.id}"></circle>
      `;
    })
    .join("");

  els.rampChart.innerHTML = `
    <title id="rampChartTitle">Three year consumption ramp</title>
    <desc id="rampChartDesc">Drag any dot to shape the monthly spend ramp. Click the chart or use Add ramp dot to add another adjustable section.</desc>
    <rect class="ramp-plot-bg" x="${pad.left}" y="${pad.top}" width="${innerWidth}" height="${innerHeight}" rx="8"></rect>
    ${yTicks
      .map((tick) => {
        const y = yForValue(ceiling * tick);
        return `
          <line class="ramp-grid-line" x1="${pad.left}" y1="${y}" x2="${width - pad.right}" y2="${y}"></line>
          <text class="ramp-axis-label ramp-y-label" x="${pad.left - 12}" y="${y + 4}">${formatCompactCurrency(ceiling * tick)}</text>
        `;
      })
      .join("")}
    ${xTicks
      .map((month) => {
        const x = xForMonth(month);
        const anchor = month === 0 ? "start" : month === months ? "end" : "middle";
        return `
          <line class="ramp-grid-line ramp-grid-line-vertical" x1="${x}" y1="${pad.top}" x2="${x}" y2="${baselineY}"></line>
          <text class="ramp-axis-label" x="${x}" y="${height - 16}" text-anchor="${anchor}">${month} mo</text>
        `;
      })
      .join("")}
    <line class="ramp-ceiling-line" x1="${pad.left}" y1="${pad.top}" x2="${width - pad.right}" y2="${pad.top}"></line>
    <path class="ramp-area" d="${areaPath}"></path>
    <path class="ramp-line" d="${extendedPath}"></path>
    ${
      selected
        ? `<line class="ramp-peak-guide" x1="${selectedX}" y1="${selectedY}" x2="${selectedX}" y2="${baselineY}"></line>`
        : ""
    }
    ${handleMarkup}
    ${
      selected && !compact
        ? `<text class="ramp-peak-label" x="${labelX}" y="${labelY}" text-anchor="${labelAnchor}">
            ${formatCurrency(selected.monthly)}/mo in month ${selected.month}
          </text>`
        : ""
    }
  `;
}

function rampPointFromEvent(event) {
  const rect = els.rampChart.getBoundingClientRect();
  const { width, height } = currentRampChartConfig();
  return {
    x: ((event.clientX - rect.left) / rect.width) * width,
    y: ((event.clientY - rect.top) / rect.height) * height,
  };
}

function chartValueFromPointer(event) {
  if (!state.ramp.ceiling) return;
  const { pad, width, height } = currentRampChartConfig();
  const innerWidth = width - pad.left - pad.right;
  const innerHeight = height - pad.top - pad.bottom;
  const point = rampPointFromEvent(event);
  const month = Math.round(clamp(((point.x - pad.left) / innerWidth) * state.ramp.months, 1, state.ramp.months));
  const monthly = clamp(((pad.top + innerHeight - point.y) / innerHeight) * state.ramp.ceiling, 0, state.ramp.ceiling);
  return { month, monthly };
}

function updateRampFromPointer(event, pointId) {
  const value = chartValueFromPointer(event);
  if (!value) return;
  setRampPoint(pointId, value.month, value.monthly);
}

function startRampDrag(event) {
  if (!state.pricing) return;
  event.preventDefault();
  const targetHandle = event.target.closest?.("[data-ramp-point-id]");
  let pointId = targetHandle?.dataset.rampPointId;
  if (!pointId) {
    const value = chartValueFromPointer(event);
    if (!value) return;
    pointId = addRampPoint(value.month, value.monthly)?.id;
  }
  if (!pointId) return;
  selectRampPoint(pointId);
  rampDragPointId = pointId;
  rampDragPointerId = event.pointerId;
  els.rampChart.setPointerCapture?.(event.pointerId);
  els.rampChart.classList.add("is-dragging");
  updateRampFromPointer(event, pointId);
}

function moveRampDrag(event) {
  if (rampDragPointerId !== event.pointerId) return;
  event.preventDefault();
  updateRampFromPointer(event, rampDragPointId);
}

function endRampDrag(event) {
  if (rampDragPointerId !== event.pointerId) return;
  els.rampChart.releasePointerCapture?.(event.pointerId);
  rampDragPointerId = null;
  rampDragPointId = null;
  els.rampChart.classList.remove("is-dragging");
}

function nudgeRamp(event) {
  if (!state.pricing) return;
  const selected = selectedRampPoint();
  if (!selected) return;
  const monthlyStep = Math.max(state.ramp.ceiling * 0.05, 1);
  const handlers = {
    ArrowLeft: () => setRampPoint(selected.id, selected.month - 1, selected.monthly),
    ArrowRight: () => setRampPoint(selected.id, selected.month + 1, selected.monthly),
    ArrowDown: () => setRampPoint(selected.id, selected.month, selected.monthly - monthlyStep),
    ArrowUp: () => setRampPoint(selected.id, selected.month, selected.monthly + monthlyStep),
    Home: () => setRampPoint(selected.id, 1, selected.monthly),
    End: () => setRampPoint(selected.id, state.ramp.months, selected.monthly),
  };
  if (!handlers[event.key]) return;
  event.preventDefault();
  handlers[event.key]();
}

function resultKpiCard({ label, value, meta, accent = "#c74634", fill = 72, primary = false, title = "" }) {
  const safeFill = clamp(fill, 4, 100);
  const titleAttr = title ? ` title="${escapeHtml(title)}"` : "";
  return `
    <div class="result-kpi ${primary ? "primary" : ""}" style="--kpi-accent:${escapeHtml(accent)}; --kpi-fill:${safeFill}%"${titleAttr}>
      <div class="kpi-topline">
        <span>${escapeHtml(label)}</span>
      </div>
      <strong>${escapeHtml(value)}</strong>
      <em>${escapeHtml(meta)}</em>
      <div class="kpi-meter" aria-hidden="true"><b></b></div>
    </div>
  `;
}

function renderResults(pricing) {
  const topRows = pricing.rows.slice().sort((a, b) => b.monthly - a.monthly);
  const skuCosts = aggregateSkuCosts(pricing);
  const maxMonthly = topRows[0]?.monthly || 1;
  const engineLabel = "local deterministic";
  const shape = pricing.selectedShape || selectedShape();
  const cloudBill = pricing.intakeMode === "cloud_bill" || pricing.cloudBillMode;
  const convertedBom = Boolean(pricing.converted);
  const serviceRows = pricing.totals.mappedServiceRows || 0;
  const reviewRows = pricing.totals.unpricedServiceRows || 0;

  els.resultsShape.textContent = shape.label || "Selected shape";
  els.resultsSubtitle.textContent = cloudBill
    ? `${pricing.rows.length} source bill lines reviewed; ${serviceRows} mapped to OCI-equivalent products and ${reviewRows} need review before they affect totals.`
    : pricing.fullServiceBeta
    ? `${pricing.rows.length} approved items priced with OCI service mapping; ${serviceRows} service mappings priced and ${reviewRows} items need review.`
    : `${pricing.rows.length} approved workloads priced on ${shape.label} with ${engineLabel} SKU validation.`;
  els.topListHeading.textContent = cloudBill
    ? "Top Source Lines"
    : convertedBom
      ? "Top OCI Line Items"
      : "Top Workloads";
  els.detailHeading.textContent = cloudBill
    ? "Cloud Bill Mapping Detail"
    : convertedBom
      ? "OCI BOM Line Details"
      : "Application Cost Details";
  els.resultRowCount.textContent = cloudBill
    ? `${pricing.rows.length} source lines`
    : pricing.fullServiceBeta
      ? `${pricing.rows.length} priced items`
      : `${pricing.rows.length} workloads`;
  const totalRows = Math.max(1, pricing.rows.length);
  const mappedShare = (serviceRows / totalRows) * 100;
  const reviewShare = (reviewRows / totalRows) * 100;
  const monthlyScale = Math.min(100, Math.max(18, Math.log10(Math.max(10, pricing.totals.monthly || 0)) * 22));
  const computeScale = Math.min(100, Math.max(10, (pricing.totals.ocpus || serviceRows || 0) / Math.max(1, pricing.totals.ocpus || serviceRows || reviewRows || 1) * 100));
  const memoryScale = Math.min(100, Math.max(12, (pricing.totals.memoryGb || reviewRows || 0) / Math.max(1, pricing.totals.memoryGb || serviceRows || reviewRows || 1) * 100));
  const storageGb = Number(pricing.totals.blockStorageGb || 0) + Number(pricing.totals.fileStorageGb || 0)
    + Number(pricing.totals.cloudStorageGb || 0);
  const hasComputeSizing =
    Number(pricing.totals.ocpus || 0) > 0 ||
    Number(pricing.totals.memoryGb || 0) > 0;
  const hasIdentifiedSpecs =
    hasComputeSizing ||
    storageGb > 0;
  const storageScale = Math.min(100, Math.max(12, Math.log10(Math.max(10, storageGb || 0)) * 20));
  const extras = extraServicesMonthly();
  // Windows licensing is already included in pricing.totals.monthly. Keep its amount
  // visible in the supporting text without adding it to the headline a second time.
  const windowsLicensing = windowsLicensingMonthly(pricing);
  const ociEff = ociMonthlyTotal(pricing);
  const ociEffAnnual = ociEff * 12;
  const extrasMeta = (extras ? ` · incl. ${formatCompactCurrency(extras)} added services` : "")
    + (windowsLicensing ? ` · incl. ${formatCompactCurrency(windowsLicensing)} Windows licensing` : "");
  const shapeOrSourceCard = convertedBom && !hasComputeSizing
    ? resultKpiCard({
        label: "Imported BOM",
        value: "Service pricing",
        meta: "No workload-level compute sizing was present",
        accent: "#2f6f73",
        fill: mappedShare || 62,
      })
    : resultKpiCard({
        label: "Flex shape",
        value: shape.shortLabel || shape.label,
        meta: `$${Number(shape.computeRate || 0).toFixed(4)} OCPU/hr and $${Number(shape.memoryRate || 0).toFixed(4)} GB/hr`,
        accent: shape.accent || "#164f68",
        fill: 62,
      });
  const pricingCards = `
    ${resultKpiCard({
      label: cloudBill ? "OCI-equivalent monthly" : "Monthly run rate",
      value: formatCompactCurrency(ociEff),
      meta: `${formatCompactCurrency(ociEffAnnual)} annualized${extrasMeta}`,
      accent: "#c74634",
      fill: monthlyScale,
      primary: true,
      title: `${formatCurrency(ociEff)} monthly; ${formatCurrency(ociEffAnnual)} annualized`,
    })}
    ${shapeOrSourceCard}
    ${
      cloudBill
        ? `${resultKpiCard({
            label: "Mapped bill lines",
            value: formatNumber(serviceRows),
            meta: `${formatCurrency(pricing.totals.fullServiceMonthly)} from deterministic OCI rates`,
            accent: "#2f6f73",
            fill: mappedShare || 8,
          })}
          ${resultKpiCard({
            // Dollars on top, and beneath it the count of lines that produced them. Lines that
            // mapped AND got a rate are already excluded - they have a cost, so they never
            // reach this bucket. What's left is spend the estimate can't account for.
            //
            // These are counted by "no OCI price", not "no OCI mapping": on real bills almost
            // every reviewable line does carry a mapping (OCI Block Volumes, OCI Object
            // Storage) and simply never resolved to a rate, so counting only the mapping-less
            // ones would report 0 lines beside a non-zero amount. unmappedRows tracks that
            // narrower set for the tooltip.
            label: "Needs review",
            value: formatCurrency(Number(pricing.totals.unpricedSourceMonthly || 0)),
            meta: `${formatNumber(pricing.totals.unpricedRows || 0)} ${Number(pricing.totals.unpricedRows || 0) === 1 ? "line needs" : "lines need"} to be reviewed`,
            accent: Number(pricing.totals.unpricedSourceMonthly || 0) > 0 ? "#d97706" : "#067647",
            fill: reviewRows ? reviewShare : 100,
            title: `${formatCurrency(Number(pricing.totals.zeroOciSourceMonthly || 0))} of source spend produces no OCI cost: ${formatCurrency(Number(pricing.totals.freeOnOciSourceMonthly || 0))} genuinely free on OCI (Savings Plans, support, VCN, Audit, included egress) and ${formatCurrency(Number(pricing.totals.unpricedSourceMonthly || 0))} that should have cost something - that portion understates the OCI estimate. Of it, ${formatCurrency(Number(pricing.totals.unmappedZeroSourceMonthly || 0))} across ${formatNumber(pricing.totals.unmappedRows || 0)} lines has no OCI mapping at all; the rest mapped to a chargeable OCI product but never got a rate.`,
          })}`
      : pricing.fullServiceBeta
        ? `${resultKpiCard({
            label: "Mapped services",
            value: formatNumber(serviceRows),
            meta: `${formatCurrency(pricing.totals.fullServiceMonthly)} from service catalog rows`,
            accent: "#2f6f73",
            fill: mappedShare || 8,
          })}
          ${resultKpiCard({
            label: "Needs review",
            value: formatNumber(reviewRows),
            meta: "Recognized without usable OCI quantity",
            accent: reviewRows ? "#d97706" : "#067647",
            fill: reviewRows ? reviewShare : 100,
          })}`
        : ""
    }
  `;
  const specCards = `
    ${resultKpiCard({
      label: "Compute",
      value: formatKpiQuantity(pricing.totals.ocpus, "OCPUs"),
      meta: "Converted from spreadsheet vCPUs",
      accent: "#2f6f73",
      fill: computeScale,
      title: `${formatNumber(pricing.totals.ocpus)} OCPUs`,
    })}
    ${resultKpiCard({
      label: "Memory",
      value: formatKpiQuantity(pricing.totals.memoryGb, "GB"),
      meta: "Uses row hours, 730 default",
      accent: "#d4b483",
      fill: memoryScale,
      title: `${formatNumber(pricing.totals.memoryGb)} GB`,
    })}
    ${resultKpiCard({
      label: "Storage",
      value: formatKpiQuantity(storageGb, "GB"),
      meta: "Block + file storage",
      accent: "#7a5c1f",
      fill: storageScale,
      title: `${formatNumber(storageGb)} GB`,
    })}
  `;

  const specsSection = hasIdentifiedSpecs
    ? `
    <section class="kpi-section" aria-label="Pricing Summary">
      <div class="kpi-section-heading">
        <span>Pricing summary</span>
        <em>Calculated from approved rows</em>
      </div>
      <div class="kpi-row pricing-kpi-row">${pricingCards}</div>
    </section>
    <section class="kpi-section" aria-label="Specs Identified">
      <div class="kpi-section-heading">
        <span>Specs identified</span>
        <em>Normalized from the uploaded table</em>
      </div>
      <div class="kpi-row specs-kpi-row">${specCards}</div>
    </section>`
    : `
    <section class="kpi-section" aria-label="Pricing Summary">
      <div class="kpi-section-heading">
        <span>Pricing summary</span>
        <em>Calculated from approved rows</em>
      </div>
      <div class="kpi-row pricing-kpi-row">${pricingCards}</div>
    </section>
  `;
  els.resultsKpis.innerHTML = specsSection;

  initializeConsumptionRamp(pricing);
  // Match the headline KPI: added services are part of the effective OCI monthly, so the
  // donut's centre total (and therefore its segment shares) must include them.
  renderCostMix(skuCosts, ociMonthlyTotal(pricing));
  renderTopWorkloads(topRows, maxMonthly, cloudBill, convertedBom);
  // Detail table defaults to the document's VM order (not cost-sorted).
  renderResultsTable(pricing.rows.slice(), pricing.fullServiceBeta, cloudBill, convertedBom);
  // Refresh the other-cloud tile if it's currently expanded.
  if (els.otherCloudsPage && !els.otherCloudsPage.classList.contains("is-hidden")) renderCrossCloud();
}

function renderCostMix(skuCosts, total) {
  const colors = ["#c74634", "#2f6f73", "#d4b483", "#7a3126"];
  let running = 0;
  const stops = skuCosts
    .map((item, index) => {
      const start = running;
      const share = percent(item.monthly, total);
      running += share;
      const color = colors[index % colors.length];
      return `${color} ${start}% ${running}%`;
    })
    .join(", ");
  const fullTotal = formatCurrency(total);
  const displayTotal = formatCompactCurrency(total);
  els.costDonut.style.background = `conic-gradient(${stops || "var(--cost-donut-empty) 0 100%"})`;
  els.costDonut.title = `${fullTotal}/mo`;
  els.costDonut.setAttribute("aria-label", `Cost Mix Chart, ${fullTotal} per month`);
  els.costDonut.innerHTML = `<span title="${escapeHtml(`${fullTotal}/mo`)}"><strong>${escapeHtml(displayTotal)}</strong><em>/mo</em></span>`;
  els.costLegend.innerHTML = skuCosts
    .map((item, index) => {
      const color = colors[index % colors.length];
      const monthly = formatCurrency(item.monthly);
      const skuLabel = item.sku ? `SKU ${item.sku}` : "No SKU";
      return `
        <div class="legend-row" title="${escapeHtml(`${item.description} - ${skuLabel}: ${monthly}`)}">
          <i style="background:${color}"></i>
          <span>${escapeHtml(item.description)}</span>
          <strong>${monthly}</strong>
          <em>${escapeHtml(skuLabel)}</em>
        </div>
      `;
    })
    .join("");

  // Collapse the SKU list to the top few (keeps the panel even with the "Top source lines"
  // box); the toggle expands to show every SKU. CSS hides rows 9+ when .is-collapsed.
  const toggle = document.querySelector("#costMixToggle");
  const legend = els.costLegend;
  const COLLAPSE_AT = 8;
  if (toggle && legend) {
    const many = skuCosts.length > COLLAPSE_AT;
    toggle.hidden = !many;
    toggle.dataset.count = String(skuCosts.length);
    if (many) {
      legend.classList.add("is-collapsed");
      toggle.setAttribute("aria-expanded", "false");
      toggle.textContent = `Show All ${skuCosts.length} SKUs`;
    } else {
      legend.classList.remove("is-collapsed");
    }
    if (!toggle.dataset.wired) {
      toggle.dataset.wired = "1";
      toggle.addEventListener("click", () => {
        const collapsed = legend.classList.toggle("is-collapsed");
        toggle.setAttribute("aria-expanded", String(!collapsed));
        toggle.textContent = collapsed ? `Show All ${toggle.dataset.count} SKUs` : "Show Less";
      });
    }
  }
}

function fallbackEntityName(row, noun = "Workload") {
  const rawName = String(row?.name || "").trim();
  if (rawName && !/^row-\d+$/i.test(rawName)) {
    return rawName;
  }
  const rowId = String(row?.rowId || "").trim();
  const sourceRow = String(row?.sourceRow || "").trim();
  const rowIdNumber = rowId.match(/^row-(\d+)$/i)?.[1];
  const suffix = /^\d+$/.test(sourceRow) ? sourceRow : rowIdNumber || "";
  return suffix ? `${noun} ${suffix}` : noun;
}

function osBadge(os) {
  const v = String(os || "").toLowerCase();
  if (v === "windows") return `<span class="os-badge os-windows">Windows</span>`;
  if (v === "linux") return `<span class="os-badge os-linux">Linux</span>`;
  return "-";
}

function cloudRowLabel(row) {
  const mapping = row.fullServiceMapping || {};
  return mapping.sourceService || fallbackEntityName(row, "Source line");
}

function cloudRowContext(row) {
  const mapping = row.fullServiceMapping || {};
  return [mapping.sourceProvider, mapping.sourceProduct, mapping.sourceRegion].filter(Boolean).join(" / ") || row.environment || "No source context";
}

function renderTopWorkloads(rows, maxMonthly, cloudBill = false, convertedBom = false) {
  els.topWorkloads.innerHTML = rows
    .slice(0, 8)
    .map((row) => {
      const width = Math.max(4, percent(row.monthly, maxMonthly));
      const label = cloudBill
        ? cloudRowLabel(row)
        : convertedBom
          ? row.fullServiceMapping?.ociProduct || row.lineItems?.[0]?.description || fallbackEntityName(row, "Line item")
          : fallbackEntityName(row);
      const context = cloudBill
        ? cloudRowContext(row)
        : convertedBom
          ? serviceSourceLabel(row.fullServiceMapping)
          : row.environment || "No environment";
      return `
        <div class="bar-row">
          <div class="bar-copy" title="${escapeHtml(label)}">
            <strong>${escapeHtml(label)}</strong>
            <span>${escapeHtml(context)}</span>
          </div>
          <div class="bar-track"><i style="width:${width}%"></i></div>
          <em>${formatCurrency(row.monthly)}</em>
        </div>
      `;
    })
    .join("");
}

function serviceSourceLabel(mapping) {
  if (!mapping) return "-";
  return [mapping.sourceProvider, mapping.sourceService || mapping.sourceProduct].filter(Boolean).join(" / ") || "-";
}

function serviceQuantityLabel(mapping, row) {
  if (mapping?.quantity) {
    return `${formatNumber(mapping.quantity)} ${mapping.unit || ""}`.trim();
  }
  const specs = row?.specs || {};
  const storage = Number(specs.blockStorageGb || 0) + Number(specs.fileStorageGb || 0);
  return storage ? `${formatNumber(storage)} GB` : "-";
}

function sortComparableValue(value) {
  if (value == null || value === "") {
    return { empty: true, value: "" };
  }
  if (typeof value === "number") {
    return { empty: !Number.isFinite(value), value };
  }
  const text = String(value).trim();
  return { empty: !text || text === "-", value: text.toLowerCase() };
}

function compareSortValues(left, right, direction = "asc") {
  const a = sortComparableValue(left);
  const b = sortComparableValue(right);
  if (a.empty && b.empty) return 0;
  if (a.empty) return 1;
  if (b.empty) return -1;
  const multiplier = direction === "asc" ? 1 : -1;
  if (typeof a.value === "number" && typeof b.value === "number") {
    return (a.value - b.value) * multiplier;
  }
  return String(a.value).localeCompare(String(b.value), undefined, {
    numeric: true,
    sensitivity: "base",
  }) * multiplier;
}

function activeResultSort(columns) {
  // "document" (the default) means keep the original upload/order - no column sort.
  if (state.resultSort.key === "document") return { column: null, direction: "asc" };
  const requestedColumn = columns.find((column) => column.key === state.resultSort.key);
  if (!requestedColumn) return { column: null, direction: "asc" };
  return { column: requestedColumn, direction: state.resultSort.direction === "asc" ? "asc" : "desc" };
}

function sortResultRows(rows, columns) {
  const { column: activeColumn, direction } = activeResultSort(columns);
  if (!activeColumn) return rows.slice();
  return rows
    .map((row, index) => ({ row, index }))
    .sort((left, right) => {
      const comparison = compareSortValues(
        activeColumn.sortValue(left.row),
        activeColumn.sortValue(right.row),
        direction,
      );
      return comparison || left.index - right.index;
    })
    .map((item) => item.row);
}

function renderSortableHead(columns) {
  const { column: activeColumn, direction } = activeResultSort(columns);
  return `
    <thead>
      <tr>
        ${columns
          .map((column) => {
            if (column.selector) {
              return `<th class="select-col"><input type="checkbox" id="selectAllRows" aria-label="Select All Rows"/></th>`;
            }
            const active = activeColumn?.key === column.key;
            const ariaSort = active ? (direction === "asc" ? "ascending" : "descending") : "none";
            return `
              <th class="${active ? `is-sorted is-${direction}` : "is-sortable"}" aria-sort="${ariaSort}">
                <button type="button" class="sort-header" data-result-sort="${escapeHtml(column.key)}">
                  <span>${escapeHtml(column.label)}</span>
                  <i aria-hidden="true"></i>
                </button>
              </th>
            `;
          })
          .join("")}
      </tr>
    </thead>
  `;
}

function renderColumnPicker(allColumnsRaw) {
  const menu = document.querySelector("#columnPickerMenu");
  if (!menu) return;
  const allColumns = allColumnsRaw.filter((c) => !c.selector);
  const hiddenCount = allColumns.filter((c) => isColumnHidden(c.key)).length;
  const heading = hiddenCount
    ? `<div class="column-picker-head">${hiddenCount} column${hiddenCount === 1 ? "" : "s"} hidden - check to show</div>`
    : `<div class="column-picker-head">All columns shown</div>`;
  menu.innerHTML =
    heading +
    allColumns
      .map((c) => {
        const checked = isColumnHidden(c.key) ? "" : "checked";
        return `<label class="column-picker-item"><input type="checkbox" data-col-key="${escapeHtml(c.key)}" ${checked}/> <span>${escapeHtml(c.label)}</span></label>`;
      })
      .join("");
  menu.querySelectorAll("input[data-col-key]").forEach((cb) => {
    cb.addEventListener("change", (e) => {
      const key = e.target.dataset.colKey;
      if (!state.columnPrefs) state.columnPrefs = {};
      state.columnPrefs[key] = e.target.checked ? "show" : "hide";
      saveColumnPrefs();
      rerenderResultsTable();
    });
  });
}

function rerenderResultsTable() {
  if (!state.pricing) return;
  renderResultsTable(
    state.pricing.rows || [],
    state.pricing.fullServiceBeta,
    state.pricing.intakeMode === "cloud_bill" || state.pricing.cloudBillMode,
  );
}

// Build the left-hand source-service filter (distinct names + row counts).
function renderSourceFilter(rows) {
  if (!els.sourceFilterPanel || !els.sourceFilterList) return;
  els.sourceFilterPanel.hidden = false;
  const counts = new Map();
  rows.forEach((r) => {
    const name = rowSourceName(r);
    counts.set(name, (counts.get(name) || 0) + 1);
  });
  const names = [...counts.keys()].sort((a, b) => a.localeCompare(b));
  els.sourceFilterList.innerHTML = names
    .map((name) => {
      const checked = state.hiddenSources[name] ? "" : "checked";
      return `<label class="source-filter-item"><input type="checkbox" data-source-name="${escapeHtml(name)}" ${checked}/> <span class="source-filter-name">${escapeHtml(name)}</span> <span class="source-filter-count">${counts.get(name)}</span></label>`;
    })
    .join("");
}

// Show/refresh the bulk-action bar based on current selection.
function syncBulkBar() {
  if (!els.bulkActionBar) return;
  const ids = Object.keys(state.selectedRows).filter((id) => state.selectedRows[id]);
  const n = ids.length;
  els.bulkActionBar.hidden = n === 0;
  if (els.bulkSelCount) els.bulkSelCount.textContent = `${n} selected`;
  const selectAll = document.querySelector("#selectAllRows");
  if (selectAll) {
    const visibleIds = (state.pricing?.rows || [])
      .filter((r) => !state.hiddenSources[rowSourceName(r)])
      .map((r) => String(r.rowId));
    const selectedVisible = visibleIds.filter((id) => state.selectedRows[id]).length;
    selectAll.checked = visibleIds.length > 0 && selectedVisible === visibleIds.length;
    selectAll.indeterminate = selectedVisible > 0 && selectedVisible < visibleIds.length;
  }
}

function applyBulkCostAction(value) {
  const ids = Object.keys(state.selectedRows).filter((id) => state.selectedRows[id]);
  if (!ids.length) return Promise.resolve();
  ids.forEach((id) => {
    if (value === "estimate" || !value) delete state.costOverrides[id];
    else state.costOverrides[id] = value;
  });
  return priceRows({ keepView: true });
}

function renderResultTableFromColumns(rows, columns) {
  renderColumnPicker(columns);
  const visible = columns.filter((c) => !isColumnHidden(c.key));
  const cols = visible.length ? visible : columns;
  const sortedRows = sortResultRows(rows, cols);
  const body = sortedRows
    .map((row) => `
      <tr>
        ${cols.map((column) => `<td>${column.render(row)}</td>`).join("")}
      </tr>
    `)
    .join("");
  els.resultsTable.innerHTML = `${renderSortableHead(cols)}<tbody>${body}</tbody>`;
}

const BEST_SHAPE_BY_VENDOR_JS = { amd: "e6-standard-ax", intel: "x12-standard-ax", arm: "a4-standard-ax" };

function familySelectHtml(row) {
  const cur = normalizeVendorKey(row.shapeUsed?.vendor) || "amd";
  const opts = [["amd", "AMD"], ["intel", "Intel"], ["arm", "Arm"]]
    .map(([v, l]) => `<option value="${v}" ${v === cur ? "selected" : ""}>${l}</option>`)
    .join("");
  return `<select class="cell-select" data-shape-row="${escapeHtml(String(row.rowId))}" data-shape-kind="family">${opts}</select>`;
}

function shapeSelectHtml(row) {
  const vendor = normalizeVendorKey(row.shapeUsed?.vendor) || "amd";
  const cur = row.shapeUsed?.key;
  const shapes = (state.rateCards || []).filter((s) => normalizeVendorKey(s.processorVendor) === vendor);
  const list = shapes.length ? shapes : state.rateCards || [];
  const opts = list
    .map((s) => `<option value="${escapeHtml(s.key)}" ${s.key === cur ? "selected" : ""}>${escapeHtml(s.shortLabel || s.label)}</option>`)
    .join("");
  return `<select class="cell-select" data-shape-row="${escapeHtml(String(row.rowId))}" data-shape-kind="shape">${opts}</select>`;
}

// Converted OCI BOM: a per-server compute VM carries an editable OCI shape. Changing
// it re-prices that VM client-side (the BOM is already priced, so we don't round-trip
// the pricing engine).
function convertedShapeSelectHtml(row) {
  if (!row.isConvertedCompute) return "";
  const list = state.rateCards || [];
  if (!list.length) return "";
  const cur = row.shapeUsed?.key;
  const opts = list
    .map((s) => `<option value="${escapeHtml(s.key)}" ${s.key === cur ? "selected" : ""}>${escapeHtml(s.shortLabel || s.label)}</option>`)
    .join("");
  return ` <select class="cell-select converted-shape-select" data-converted-shape="${escapeHtml(String(row.rowId))}" title="Re-map this server's VM to a different OCI shape (re-prices its compute)">${opts}</select>`;
}

function round2(n) {
  return Math.round((Number(n) || 0) * 100) / 100;
}

function recomputeConvertedTotals(pricing) {
  let monthly = 0, ocpus = 0, mem = 0, blk = 0, fil = 0;
  for (const r of pricing.rows) {
    monthly += Number(r.monthly || 0);
    ocpus += Number(r.specs?.ocpus || 0);
    mem += Number(r.specs?.memoryGb || 0);
    blk += Number(r.specs?.blockStorageGb || 0);
    fil += Number(r.specs?.fileStorageGb || 0);
  }
  pricing.totals.monthly = round2(monthly);
  pricing.totals.annual = round2(monthly * 12);
  pricing.totals.fullServiceMonthly = round2(monthly);
  pricing.totals.ocpus = round2(ocpus);
  pricing.totals.memoryGb = round2(mem);
  pricing.totals.blockStorageGb = round2(blk);
  pricing.totals.fileStorageGb = round2(fil);
}

// Mutate one converted compute VM row to a shape (no re-render).
function applyShapeToVm(row, shape) {
  if (!row || !row.isConvertedCompute || !shape) return;
  const hours = Number(row.computeHours || 730);
  const ocpu = Number(row.originalOcpus || row.specs?.ocpus || 0);
  const mem = Number(row.originalMemoryGb || row.specs?.memoryGb || 0);
  const cRate = Number(shape.computeRate || 0);
  const mRate = Number(shape.memoryRate || 0);
  const ocpuMonthly = round2(ocpu * hours * cRate);
  const memMonthly = round2(mem * hours * mRate);
  const lbl = shape.shortLabel || shape.label;
  row.lineItems = [
    { sku: shape.computeSku || "", description: `OCI Compute ${lbl} - OCPU`, quantity: ocpu,
      unit: "OCPU Per Hour", rate: cRate, monthly: ocpuMonthly,
      mapping: `Re-mapped to ${shape.label}: ${ocpu} OCPU x ${hours} hrs x $${cRate}/OCPU-hr.` },
    { sku: shape.memorySku || "", description: `OCI Compute ${lbl} - Memory`, quantity: mem,
      unit: "Gigabyte Per Hour", rate: mRate, monthly: memMonthly,
      mapping: `Re-mapped to ${shape.label}: ${mem} GB x ${hours} hrs x $${mRate}/GB-hr.` },
  ];
  row.monthly = round2(ocpuMonthly + memMonthly);
  row.annual = round2(row.monthly * 12);
  row.shapeUsed = shape;
  row.specs.ocpus = ocpu;
  row.specs.memoryGb = mem;
  row.ociProduct = `OCI Compute VM - ${lbl} (${ocpu} OCPU / ${mem} GB)`;
  if (row.fullServiceMapping) row.fullServiceMapping.ociProduct = row.ociProduct;
}

function repriceConvertedCompute(rowId, shapeKey) {
  const pricing = state.pricing;
  if (!pricing || !pricing.converted) return;
  const row = pricing.rows.find((r) => String(r.rowId) === String(rowId));
  const shape = (state.rateCards || []).find((s) => s.key === shapeKey);
  if (!row || !shape) return;
  applyShapeToVm(row, shape);
  recomputeConvertedTotals(pricing);
  renderPricing(pricing);
  renderResults(pricing);
}

// Set EVERY converted compute VM to one shape (used by the page-3 shape picker so a
// converted BOM prices its compute on the shape you choose there). Returns count.
function applyBulkVmShape(shapeKey) {
  const pricing = state.pricing;
  if (!pricing || !pricing.converted) return 0;
  const shape = (state.rateCards || []).find((s) => s.key === shapeKey);
  if (!shape) return 0;
  let n = 0;
  for (const row of pricing.rows) {
    if (row.isConvertedCompute) { applyShapeToVm(row, shape); n += 1; }
  }
  recomputeConvertedTotals(pricing);
  return n;
}

document.addEventListener("change", (event) => {
  const sel = event.target.closest("[data-converted-shape]");
  if (!sel) return;
  repriceConvertedCompute(sel.dataset.convertedShape, sel.value);
});

function applyShapeOverride(rowId, kind, value) {
  if (!rowId) return;
  if (kind === "family") {
    const vendor = normalizeVendorKey(value) || "amd";
    const shapes = (state.rateCards || []).filter((s) => normalizeVendorKey(s.processorVendor) === vendor);
    const def = shapes.find((s) => s.key === BEST_SHAPE_BY_VENDOR_JS[vendor]) || shapes[0];
    if (def) state.shapeOverrides[rowId] = def.key;
  } else {
    state.shapeOverrides[rowId] = value;
  }
  priceRows({ keepView: true });
}

function flagActive(row) {
  return Boolean(row.mappingFlag) && !state.approvedFlags[row.rowId];
}

// Show the OCI shape an EC2/compute row was mapped to (e.g. "E6 Standard Ax").
// Only compute rows (those sized with OCPUs) carry a flex shape; storage/DBaaS/
// networking rows don't, so they get no badge.
function computeShapeBadge(row) {
  const ocpus = Number(row.specs?.ocpus || 0);
  if (ocpus <= 0) return "";
  const shape = row.shapeUsed?.shortLabel || row.shapeUsed?.label;
  if (!shape) return "";
  return ` <span class="shape-map-badge" title="OCI shape mapped for this compute line">${escapeHtml(shape)}</span>`;
}

function mappingFlagBadge(row) {
  if (row.costAction === "remove") {
    return ` <span class="size-flag size-flag-removed" title="Removed from both sides of the BOM">REMOVED</span>`;
  }
  if (row.costAction === "carry") {
    return ` <span class="size-flag size-flag-carried" title="OCI cost set equal to the source AWS cost">CARRIED OVER</span>`;
  }
  if (state.approvedFlags[row.rowId]) {
    return ` <span class="size-flag size-flag-approved" title="Mapping approved">✓ approved</span>`;
  }
  if (row.mappingFlag) {
    let html = ` <span class="size-flag size-flag-review flag-clickable" data-flag-row="${escapeHtml(String(row.rowId))}" title="Click to approve this mapping">⚠ ${escapeHtml(row.mappingFlag)}</span>`;
    if (String(state.flagMenuRow) === String(row.rowId)) {
      html += ` <button type="button" class="flag-approve-btn" data-approve-row="${escapeHtml(String(row.rowId))}">Approve mapping</button>`;
    }
    return html;
  }
  return "";
}

function costActionSelectHtml(row) {
  const cur = row.costAction || "estimate";
  const opts = [
    ["estimate", "Use OCI estimate"],
    ["carry", "Carry over AWS cost"],
    ["remove", "Remove from BOM"],
  ].map(([v, l]) => `<option value="${v}" ${v === cur ? "selected" : ""}>${l}</option>`).join("");
  return `<select class="cell-select cost-action-select" data-cost-row="${escapeHtml(String(row.rowId))}">${opts}</select>`;
}

function applyCostOverride(rowId, value) {
  if (!rowId) return;
  if (value === "estimate" || !value) delete state.costOverrides[rowId];
  else state.costOverrides[rowId] = value;
  priceRows({ keepView: true });
}

function sizeFlagBadge(row) {
  const badges = [];
  const check = row.sizeCheck || {};
  if (check.status === "impossible") {
    badges.push(` <span class="size-flag size-flag-impossible" title="${escapeHtml(check.message || "")}">IMPOSSIBLE</span>`);
  } else if (check.status === "baremetal") {
    // A workload that fits a larger flex shape isn't truly bare metal - label it so, and reserve
    // the "BARE METAL" badge (which now bills a full physical server) for genuine overflow.
    const label = check.flexAlt ? "LARGER SHAPE" : "BARE METAL";
    badges.push(` <span class="size-flag size-flag-baremetal" title="${escapeHtml(check.message || "")}">${label}</span>`);
  }
  if (Array.isArray(row.lineItems) && row.lineItems.some((li) => li && li.isGpu)) {
    badges.push(` <span class="size-flag size-flag-gpu" title="Mapped to an OCI GPU shape">GPU</span>`);
  }
  return badges.join("");
}

function renderResultsTable(rows, fullServiceBeta = false, cloudBill = false, convertedBom = false) {
  // Source-service filter + bulk row actions are cloud-bill only.
  if (!cloudBill) {
    if (els.sourceFilterPanel) els.sourceFilterPanel.hidden = true;
    if (els.bulkActionBar) els.bulkActionBar.hidden = true;
  }
  if (cloudBill) {
    const columns = [
      {
        key: "select",
        label: "",
        selector: true,
        sortValue: () => 0,
        render: (row) => `<input type="checkbox" class="row-select" data-row-select="${escapeHtml(String(row.rowId))}" ${state.selectedRows[row.rowId] ? "checked" : ""} aria-label="Select Row"/>`,
      },
      {
        key: "sourceService",
        label: "Source Service",
        sortValue: (row) => rowSourceName(row),
        render: (row) => escapeHtml(rowSourceName(row)),
      },
      {
        key: "sourceProduct",
        label: "Source SKU / Meter",
        sortValue: (row) => row.fullServiceMapping?.sourceProduct || "",
        render: (row) => escapeHtml(row.fullServiceMapping?.sourceProduct || "-"),
      },
      {
        key: "ociTarget",
        label: "OCI Target",
        sortValue: (row) => row.fullServiceMapping?.ociProduct || row.lineItems?.[0]?.description || "Needs review",
        render: (row) => escapeHtml(row.fullServiceMapping?.ociProduct || row.lineItems?.[0]?.description || "Needs review") + computeShapeBadge(row) + mappingFlagBadge(row),
      },
      {
        key: "usage",
        label: "Usage",
        sortValue: (row) => Number(row.fullServiceMapping?.quantity || row.specs?.blockStorageGb || row.specs?.fileStorageGb || 0),
        render: (row) => escapeHtml(serviceQuantityLabel(row.fullServiceMapping || {}, row)),
      },
      {
        key: "sourceCost",
        label: sourceCostLabel(),
        sortValue: (row) => Number(row.fullServiceMapping?.sourceMonthlyCost || 0),
        render: (row) => formatCurrency(row.fullServiceMapping?.sourceMonthlyCost || 0),
      },
      {
        key: "monthly",
        label: "OCI Monthly",
        sortValue: (row) => Number(row.monthly || 0),
        render: (row) => formatCurrency(row.monthly),
      },
      {
        key: "costAction",
        label: "Cost Action",
        sortValue: (row) => row.costAction || "",
        render: (row) => costActionSelectHtml(row),
      },
      {
        key: "status",
        label: "Status",
        sortValue: (row) => {
          const mapping = row.fullServiceMapping || {};
          return mapping.reviewRequired ? "Review" : mapping.confidence ? `${Math.round(mapping.confidence * 100)}% match` : "Unmapped";
        },
        render: (row) => {
        const mapping = row.fullServiceMapping || {};
        const status = mapping.reviewRequired
          ? "Review"
          : mapping.confidence
            ? `${Math.round(mapping.confidence * 100)}% match`
            : "Unmapped";
          return escapeHtml(status);
        },
      },
    ];
    // Left-sidebar source-service filter (built from ALL rows so you can re-check).
    renderSourceFilter(rows);
    const filtered = rows.filter((r) => !state.hiddenSources[rowSourceName(r)]);
    // Default order: flagged ("may not be optimal") rows on top, then everything
    // by total cost on the bill (source cost) descending.
    const billCost = (r) => Number(r.sourceMonthlyCost || 0);
    const ordered = filtered.slice().sort((a, b) => {
      const fa = flagActive(a) ? 1 : 0;
      const fb = flagActive(b) ? 1 : 0;
      if (fa !== fb) return fb - fa;
      return billCost(b) - billCost(a);
    });
    renderResultTableFromColumns(ordered, columns);
    syncBulkBar();
    return;
  }

  if (fullServiceBeta) {
    const columns = [
      {
        key: "workload",
        label: convertedBom ? "Line Item" : "Workload",
        sortValue: (row) => fallbackEntityName(row),
        render: (row) => escapeHtml(fallbackEntityName(row)),
      },
      {
        key: "source",
        label: "Source",
        sortValue: (row) => serviceSourceLabel(row.fullServiceMapping),
        render: (row) => escapeHtml(serviceSourceLabel(row.fullServiceMapping)),
      },
      {
        key: "ociProduct",
        label: "OCI Product",
        sortValue: (row) => row.fullServiceMapping?.ociProduct || row.lineItems?.[0]?.description || "Needs review",
        render: (row) => escapeHtml(row.fullServiceMapping?.ociProduct || row.lineItems?.[0]?.description || "Needs review") + convertedShapeSelectHtml(row),
      },
      {
        key: "quantity",
        label: "Quantity",
        sortValue: (row) => Number(row.fullServiceMapping?.quantity || row.specs?.blockStorageGb || row.specs?.fileStorageGb || 0),
        render: (row) => escapeHtml(serviceQuantityLabel(row.fullServiceMapping, row)),
      },
      {
        key: "monthly",
        label: "Monthly",
        sortValue: (row) => Number(row.monthly || 0),
        render: (row) => formatCurrency(row.monthly),
      },
      {
        key: "annual",
        label: "Annual",
        sortValue: (row) => Number(row.annual || 0),
        render: (row) => formatCurrency(row.annual),
      },
    ];
    renderResultTableFromColumns(rows, columns);
    return;
  }

  const columns = [
    {
      key: "workload",
      label: "Workload",
      sortValue: (row) => fallbackEntityName(row),
      render: (row) => escapeHtml(fallbackEntityName(row)) + sizeFlagBadge(row),
    },
    {
      key: "environment",
      label: "Env",
      sortValue: (row) => row.environment || "",
      render: (row) => escapeHtml(row.environment || "-"),
    },
    {
      key: "os",
      label: "OS",
      sortValue: (row) => row.osDetected || "",
      render: (row) => osBadge(row.osDetected),
    },
    {
      key: "region",
      label: "Region",
      sortValue: (row) => row.region || "",
      render: (row) => escapeHtml(row.region || "-"),
    },
    {
      key: "family",
      label: "Processor Family",
      sortValue: (row) => row.shapeUsed?.vendor || "",
      render: (row) => familySelectHtml(row),
    },
    {
      key: "shape",
      label: "OCI Shape",
      sortValue: (row) => row.shapeUsed?.label || "",
      render: (row) => shapeSelectHtml(row),
    },
    {
      key: "ocpus",
      label: "OCPUs",
      sortValue: (row) => Number(row.specs?.ocpus || 0),
      render: (row) => formatNumber(row.specs?.ocpus),
    },
    {
      key: "memory",
      label: "Memory",
      sortValue: (row) => Number(row.specs?.memoryGb || 0),
      render: (row) => `${formatNumber(row.specs?.memoryGb)} GB`,
    },
    {
      key: "storage",
      label: "Storage",
      sortValue: (row) => Number(row.specs?.blockStorageGb || 0) + Number(row.specs?.fileStorageGb || 0),
      render: (row) => `${formatNumber(Number(row.specs?.blockStorageGb || 0) + Number(row.specs?.fileStorageGb || 0))} GB`,
    },
    {
      key: "hours",
      label: "Hrs/mo",
      sortValue: (row) => Number(row.hoursPerMonth || 0),
      render: (row) => formatNumber(row.hoursPerMonth || 730),
    },
    {
      key: "monthly",
      label: "Monthly",
      sortValue: (row) => Number(row.monthly || 0),
      render: (row) => formatCurrency(row.monthly),
    },
    {
      key: "annual",
      label: "Annual",
      sortValue: (row) => Number(row.annual || 0),
      render: (row) => formatCurrency(row.annual),
    },
  ];
  renderResultTableFromColumns(rows, columns);
}

if (els.resultsTable) {
  els.resultsTable.addEventListener("click", (event) => {
    // Approve an "may not be optimal" mapping (clears the flag for that row).
    const approveBtn = event.target.closest("[data-approve-row]");
    if (approveBtn) {
      state.approvedFlags[approveBtn.dataset.approveRow] = true;
      state.flagMenuRow = null;
      rerenderResultsTable();
      return;
    }
    // Click the flag badge to reveal the "Approve mapping" action.
    const flagBadge = event.target.closest("[data-flag-row]");
    if (flagBadge) {
      const rid = flagBadge.dataset.flagRow;
      state.flagMenuRow = String(state.flagMenuRow) === String(rid) ? null : rid;
      rerenderResultsTable();
      return;
    }
    const button = event.target.closest("[data-result-sort]");
    if (!button) return;
    const key = button.dataset.resultSort;
    const direction = state.resultSort.key === key && state.resultSort.direction === "asc" ? "desc" : "asc";
    state.resultSort = { key, direction };
    rerenderResultsTable();
  });
  // Editable shape / shape-family dropdowns -> set per-row override and re-price.
  els.resultsTable.addEventListener("change", (event) => {
    const shapeSel = event.target.closest("select[data-shape-row]");
    if (shapeSel) {
      applyShapeOverride(shapeSel.dataset.shapeRow, shapeSel.dataset.shapeKind, shapeSel.value);
      return;
    }
    const costSel = event.target.closest("select[data-cost-row]");
    if (costSel) {
      applyCostOverride(costSel.dataset.costRow, costSel.value);
      return;
    }
    // Per-row selection checkbox.
    const rowCb = event.target.closest("input[data-row-select]");
    if (rowCb) {
      const id = rowCb.dataset.rowSelect;
      if (rowCb.checked) state.selectedRows[id] = true;
      else delete state.selectedRows[id];
      syncBulkBar();
      return;
    }
    // Select-all (currently visible rows).
    if (event.target.id === "selectAllRows") {
      const visible = (state.pricing?.rows || []).filter((r) => !state.hiddenSources[rowSourceName(r)]);
      if (event.target.checked) visible.forEach((r) => { state.selectedRows[r.rowId] = true; });
      else visible.forEach((r) => { delete state.selectedRows[r.rowId]; });
      rerenderResultsTable();
    }
  });
}

// Source-service filter (left sidebar).
els.sourceFilterList?.addEventListener("change", (event) => {
  const cb = event.target.closest("input[data-source-name]");
  if (!cb) return;
  const name = cb.dataset.sourceName;
  if (cb.checked) delete state.hiddenSources[name];
  else state.hiddenSources[name] = true;
  rerenderResultsTable();
});
els.sourceFilterAll?.addEventListener("click", () => {
  state.hiddenSources = {};
  rerenderResultsTable();
});
els.sourceFilterNone?.addEventListener("click", () => {
  (state.pricing?.rows || []).forEach((r) => { state.hiddenSources[rowSourceName(r)] = true; });
  rerenderResultsTable();
});

// Bulk cost-action controls.
els.bulkApply?.addEventListener("click", async () => {
  const n = Object.keys(state.selectedRows).filter((id) => state.selectedRows[id]).length;
  if (!n) return;
  const overlay = document.querySelector("#tableLoadingOverlay");
  const text = document.querySelector("#tableLoadingText");
  if (text) text.textContent = `Applying to ${n} selected row${n === 1 ? "" : "s"}…`;
  if (overlay) overlay.hidden = false;
  // Let the overlay paint before the heavy re-price/re-render.
  await new Promise((r) => requestAnimationFrame(() => r()));
  try {
    await applyBulkCostAction(els.bulkCostAction ? els.bulkCostAction.value : "estimate");
  } finally {
    if (overlay) overlay.hidden = true;
  }
});
els.bulkClear?.addEventListener("click", () => {
  state.selectedRows = {};
  rerenderResultsTable();
});

const columnPickerBtn = document.querySelector("#columnPickerBtn");
const columnPickerMenu = document.querySelector("#columnPickerMenu");
function setColumnPickerOpen(open) {
  if (!columnPickerMenu) return;
  columnPickerMenu.hidden = !open;
  if (columnPickerBtn) {
    columnPickerBtn.textContent = open ? "Columns ▴" : "Columns ▾";
    columnPickerBtn.setAttribute("aria-expanded", String(open));
    columnPickerBtn.classList.toggle("is-open", open);
  }
}
columnPickerBtn?.addEventListener("click", (e) => {
  e.stopPropagation();
  setColumnPickerOpen(columnPickerMenu ? columnPickerMenu.hidden : false);
});
document.addEventListener("click", (e) => {
  if (columnPickerMenu && !columnPickerMenu.hidden && !e.target.closest("#columnPicker")) {
    setColumnPickerOpen(false);
  }
});

els.fileInput.addEventListener("change", () => {
  uploadFile(els.fileInput.files[0]).catch(setUploadingError);
});
els.selectedDocClear?.addEventListener("click", () => {
  state.lastUploadFile = null;
  state.fields = [];
  state.rows = [];
  state.pricing = null;
  state.uploadMetadata = {};
  resetWorkflowProgress();
  if (els.fileInput) els.fileInput.value = "";
  showSelectedDoc(null);
  els.uploadStatus.textContent = "";
});
els.switchToOnPrem?.addEventListener("click", () => {
  state.intakeMode = "on_prem";
  state.providerHint = "auto";
  state.fullServiceBeta = false;
  syncModeUi();
  if (els.inventoryNotice) els.inventoryNotice.hidden = true;
  if (state.lastUploadFile) uploadFile(state.lastUploadFile).catch(setUploadingError);
});

["dragenter", "dragover"].forEach((eventName) => {
  els.dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    els.dropZone.classList.add("is-dragging");
  });
});

["dragleave", "drop"].forEach((eventName) => {
  els.dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    els.dropZone.classList.remove("is-dragging");
  });
});

els.dropZone.addEventListener("drop", (event) => {
  const [file] = event.dataTransfer.files;
  // A dropped .json is normally a saved workflow - but in Other OCI Bill mode it's the cost
  // estimator's own JSON export, which the converter reads directly.
  if (file && /\.json$/i.test(file.name) && !isOtherOciBillMode()) {
    loadWorkflowFromFile(file);
    return;
  }
  uploadFile(file).catch(setUploadingError);
});

// The "Load previous BOM" area is also a drop target for workflow files.
const loadBomZone = document.querySelector(".load-prev-bom");
if (loadBomZone) {
  ["dragenter", "dragover"].forEach((ev) =>
    loadBomZone.addEventListener(ev, (e) => { e.preventDefault(); e.stopPropagation(); loadBomZone.classList.add("is-dragging"); }));
  ["dragleave", "drop"].forEach((ev) =>
    loadBomZone.addEventListener(ev, (e) => { e.preventDefault(); e.stopPropagation(); loadBomZone.classList.remove("is-dragging"); }));
  loadBomZone.addEventListener("drop", (e) => {
    const [file] = e.dataTransfer.files;
    if (file) loadWorkflowFromFile(file);
  });
}

els.addRow.addEventListener("click", addBlankRow);
els.addColumn?.addEventListener("click", showAddColumnForm);
els.addColumnForm?.addEventListener("submit", submitAddColumn);
els.cancelAddColumn?.addEventListener("click", hideAddColumnForm);
els.missingOnlyToggle?.addEventListener("change", () => {
  state.showMissingOnly = els.missingOnlyToggle.checked;
  renderTable();
});
function completeReviewStep() {
  if (!state.rows.some((row) => row.__approved !== false)) return;
  unlockWorkflowStep("shape");
  showShapePage();
}

els.priceButton.addEventListener("click", completeReviewStep);
function onPriceShapeClick() {
  // A converted BOM is already priced - the page-3 shape choice re-prices its compute
  // VMs on that shape (client-side); no pricing-engine round-trip.
  if (state.pricing && state.pricing.converted) {
    applyBulkVmShape(state.selectedShape);
    renderPricing(state.pricing);
    renderResults(state.pricing);
    unlockWorkflowStep("networking");
    showNetworkingPage();
    return;
  }
  priceRows({ destination: "networking" });
}
els.priceShapeButton.addEventListener("click", onPriceShapeClick);
els.rerunPricing?.addEventListener("click", priceRows);
// The discount re-prices the whole estimate, so debounce it: typing "27" must not fire a request
// for "2" then "27". Wait until typing stops, then show a brief loading state while it refreshes.
const OVERLAY_MIN_MS = 500;   // keep any "working" overlay up long enough to be readable
let _discountTimer = null;
function onDiscountInput(raw) {
  let pct = Number(raw);
  if (!Number.isFinite(pct) || pct < 0) pct = 0;
  if (pct > 99) pct = 99;
  state.ociDiscount = pct;
  // Reflect the clamped value back so the field can't sit on something out of range.
  if (els.ociDiscount && String(pct) !== String(els.ociDiscount.value)) {
    els.ociDiscount.value = String(pct);
  }
  if (!state.pricing) return;
  // Show the overlay SYNCHRONOUSLY on commit so it appears the instant Enter is pressed,
  // rather than a tick later once the timer runs. The discount is a presentation/export factor,
  // not a re-price, so the work itself is just a re-render of the KPIs, Cost Mix and ramp.
  const overlay = document.querySelector("#exportOverlay");
  const overlayText = overlay ? overlay.querySelector(".export-overlay-text") : null;
  if (overlayText) overlayText.textContent = `Applying ${pct}% OCI discount…`;
  if (overlay) overlay.hidden = false;
  const shownAt = Date.now();
  clearTimeout(_discountTimer);
  // Two frames: the first lets the browser actually paint the overlay before the (synchronous)
  // re-render blocks the main thread, so it can't be skipped entirely.
  window.requestAnimationFrame(() => {
    window.requestAnimationFrame(() => {
      try {
        refreshResultsTotals();
      } finally {
        // Re-rendering is near-instant, so without a floor the overlay just flickers. Hold it
        // for a minimum visible moment so the change reads as "applied" rather than a glitch.
        const wait = Math.max(0, OVERLAY_MIN_MS - (Date.now() - shownAt));
        _discountTimer = setTimeout(() => { if (overlay) overlay.hidden = true; }, wait);
      }
    });
  });
}
if (els.ociDiscount) {
  // Apply only when the field is committed - blur or Enter. Re-pricing while the user is still
  // typing meant an intermediate value (a bare "1" on the way to "15") briefly became the
  // estimate, and the field fought the caret.
  els.ociDiscount.addEventListener("change", (e) => onDiscountInput(e.target.value));
  els.ociDiscount.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      onDiscountInput(e.target.value);
      e.target.blur();
    }
  });
}

function syncSizingModeUi() {
  const mode = state.rightsize ? "rightsize" : "one-to-one";
  document.querySelectorAll(".sizing-switch .mode-opt").forEach((b) => {
    b.classList.toggle("is-active", b.dataset.sizing === mode);
  });
  if (els.sizingModeHint) {
    els.sizingModeHint.textContent = state.rightsize
      ? "OCPU and RAM trimmed for the newer OCI generation"
      : "Sizes match the source 1:1";
  }
}
if (els.sizingSwitch) {
  els.sizingSwitch.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-sizing]");
    if (!btn) return;
    const next = btn.dataset.sizing === "rightsize";
    if (next === !!state.rightsize) return;
    state.rightsize = next;
    syncSizingModeUi();
    // Sizing changes the OCPU/RAM behind every line, so the existing estimate is stale.
    repriceIfEstimated();
  });
}

// Current on-prem spend: the baseline the BOM's Pricing Overview compares OCI against, the
// on-prem counterpart of the uploaded bill in cloud mode. Only shown on-prem - a cloud bill
// already carries its own cost - and it stays editable in the exported workbook, so a blank
// here is a cell the customer can fill in later rather than a dead end.
// Show what the entered baseline actually does, so the figure isn't a write-only box the user
// has to export a workbook to see the effect of. This is the same arithmetic the BOM's Pricing
// Overview "Current On-Prem Spend vs. OCI Estimate" block performs. Kept separate from
// syncExistingInfraUi so it can run on every keystroke WITHOUT rewriting the input's value -
// round-tripping the value through Number() mid-typing would eat a half-entered decimal.
function renderExistingInfraHint() {
  if (!els.existingInfraHint) return;
  const baseline = Number(state.existingInfraCost || 0);
  const oci = state.pricing ? ociMonthlyTotal(state.pricing) : 0;
  if (!baseline) {
    els.existingInfraHint.textContent =
      "Monthly. Baseline for the BOM comparison - editable in the workbook too";
    return;
  }
  if (!oci) {
    els.existingInfraHint.textContent = "Monthly. Price the estimate to see the savings";
    return;
  }
  const delta = baseline - oci;
  const pct = (delta / baseline) * 100;
  els.existingInfraHint.textContent = delta >= 0
    ? `Saves ${formatCurrency(delta)}/mo vs OCI (${pct.toFixed(1)}%)`
    : `${formatCurrency(-delta)}/mo above the OCI estimate`;
}

function syncExistingInfraUi() {
  if (els.existingInfraControl) els.existingInfraControl.hidden = isCloudBillMode();
  if (els.existingInfraCost) els.existingInfraCost.value = String(Number(state.existingInfraCost || 0));
  renderExistingInfraHint();
}
if (els.existingInfraCost) {
  const commitExistingInfra = () => {
    const next = Math.max(0, Number(els.existingInfraCost.value) || 0);
    if (next === Number(state.existingInfraCost || 0)) return;
    state.existingInfraCost = next;
    // The baseline is a comparison input, not a rate, so nothing needs re-pricing - only the
    // savings readout beside the field and the exported Pricing Overview cell change.
    renderExistingInfraHint();
  };
  els.existingInfraCost.addEventListener("change", commitExistingInfra);
  els.existingInfraCost.addEventListener("input", commitExistingInfra);
  els.existingInfraCost.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); els.existingInfraCost.blur(); }
  });
}

function syncOptChips() {
  // Mirror each option checkbox onto its chip so the switch styling works without :has().
  [els.hideGpuToggle, els.hideWindowsToggle, els.hideSqlToggle].forEach((cb) => {
    const chip = cb && cb.closest(".opt-chip");
    if (chip) chip.classList.toggle("is-on", !!cb.checked);
  });
}

function repriceIfEstimated() {
  // A licensing / GPU toggle changes the money, so the existing estimate is immediately stale.
  // Drop it and re-lock the downstream steps so Price, Compare, Architecture and Deliverables
  // can't be opened on numbers that no longer reflect the toggles, then re-price to refresh.
  if (!state.pricing) return;
  state.pricing = null;
  syncWorkflowAvailability();
  const overlay = document.querySelector("#exportOverlay");
  const overlayText = overlay ? overlay.querySelector(".export-overlay-text") : null;
  if (overlayText) overlayText.textContent = "Re-pricing this estimate…";
  if (overlay) overlay.hidden = false;
  const shownAt = Date.now();
  if (typeof priceRows === "function") {
    Promise.resolve(priceRows({ keepView: true })).finally(() => {
      const wait = Math.max(0, OVERLAY_MIN_MS - (Date.now() - shownAt));
      setTimeout(() => { if (overlay) overlay.hidden = true; }, wait);
      syncWorkflowAvailability();
    });
  } else if (overlay) {
    overlay.hidden = true;
  }
}
els.hideGpuToggle?.addEventListener("change", (event) => {
  state.hideGpuPricing = event.target.checked;
  syncOptChips();
  repriceIfEstimated();
});
els.hideSqlToggle?.addEventListener("change", (event) => {
  state.hideSqlPricing = event.target.checked;
  syncOptChips();
  repriceIfEstimated();
});
els.hideWindowsToggle?.addEventListener("change", (event) => {
  state.hideWindowsPricing = event.target.checked;
  syncOptChips();
  repriceIfEstimated();
});
function setCpuUnit(value) {
  const v = ["auto", "vcpu", "ocpu"].includes(value) ? value : "auto";
  state.cpuUnit = v;
  document.querySelectorAll(".cpuunit-switch .mode-opt").forEach((b) => {
    b.classList.toggle("is-active", b.dataset.cpuunit === v);
  });
  updateCpuUnitHint();
  // Re-render the review table so the OCPUs column reflects the chosen unit.
  if ((state.fields || []).length) renderTable();
}
els.cpuUnitSwitches?.forEach((sw) => {
  sw.addEventListener("click", (event) => {
    const opt = event.target.closest("[data-cpuunit]");
    if (!opt) return;
    setCpuUnit(opt.dataset.cpuunit);
    // If we've already priced, re-price so results/export stay in sync.
    if (typeof priceRows === "function" && state.pricing) priceRows({ keepView: true });
  });
});

function setRampMonths(months) {
  const newMonths = Math.max(1, Math.min(60, Math.round(months)));
  const oldMonths = state.ramp.months || 36;
  if (newMonths === oldMonths) return;
  const factor = newMonths / oldMonths;
  state.ramp.months = newMonths;
  // Rescale existing dots so the curve shape is preserved over the new horizon.
  state.ramp.points = (state.ramp.points || []).map((point) => ({
    ...point,
    month: Math.min(newMonths, Math.max(1, Math.round(point.month * factor))),
  }));
  // Make sure the final dot lands on the last month at the ceiling.
  const sorted = state.ramp.points.slice().sort((a, b) => a.month - b.month);
  if (sorted.length) {
    sorted[sorted.length - 1].month = newMonths;
    sorted[sorted.length - 1].monthly = state.ramp.ceiling;
  }
  if (els.rampPeakMonth) els.rampPeakMonth.max = String(newMonths);
  document.querySelectorAll(".ramp-months-switch .mode-opt").forEach((b) => {
    b.classList.toggle("is-active", Number(b.dataset.rampMonths) === newMonths);
  });
  renderConsumptionRamp();
}
document.querySelector(".ramp-months-switch")?.addEventListener("click", (event) => {
  const opt = event.target.closest("[data-ramp-months]");
  if (!opt) return;
  setRampMonths(Number(opt.dataset.rampMonths));
});
els.oicMessagePacks?.addEventListener("change", (event) => {
  let v = Math.round(Number(event.target.value));
  if (!(v >= 1)) v = 1;
  event.target.value = v;
  state.oicMessagePacks = v;
  // Message-pack sizing changes the OCI cost server-side (cloud-bill mode) - re-price
  // so the app view + export reflect the new Oracle Integration Cloud line.
  if (state.pricing) priceRows({ keepView: true });
});
els.exportFullBom?.addEventListener("click", () => exportToExcel(els.exportFullBom));
els.deliverablesFullBom?.addEventListener(
  "click",
  () => exportToExcel(els.deliverablesFullBom),
);

// Download only the architecture diagram. The ZIP always includes editable draw.io and
// includes PNG when the server runtime has a compatible renderer.
function setArchitectureExportStatus(message, tone = "") {
  setDeliverableStatus(els.architectureExportStatus, message, tone);
  setDeliverableStatus(els.deliverablesArchitectureStatus, message, tone);
}

async function downloadDiagram(triggerButton = null) {
  if (!state.pricing) {
    els.engineStatus.textContent = "Run \"Reprice estimate\" first, then download the diagram.";
    setArchitectureExportStatus("Choose a shape and prepare the estimate first.", "error");
    return;
  }
  if (state.pricing.diagramAvailable === false) {
    setArchitectureExportStatus(
      state.pricing.diagramUnavailableReason ||
        "This converted BOM does not contain workload-level sizing for an architecture diagram.",
      "error",
    );
    return;
  }
  const btn = triggerButton || els.downloadDiagram || els.deliverablesDiagram;
  const original = btn ? btn.innerHTML : "";
  if (btn) { btn.disabled = true; btn.textContent = "Rendering diagram..."; }
  els.engineStatus.textContent = state.openaiApiConnected
    ? "OpenAI is planning the architecture before deterministic rendering..."
    : "Rendering the OCI architecture diagram...";
  setArchitectureExportStatus(
    state.openaiApiConnected
      ? "Planning the topology, then rendering draw.io and PNG..."
      : "Rendering the editable draw.io file and PNG when available...",
  );
  try {
    const diagramPayload = {
      // /api/diagram re-prices too (the diagram is sized off the pricing), so it takes the
      // same shared inputs - it was missing rightsize for the same reason the export was.
      ...pricingInputs(),
      extraServices: state.extraServices || [],
      diagramOptions: state.diagramOptions || {},
      bomName: state.bomName || "",
      convertedPricing: state.pricing?.converted ? state.pricing : null,
    };
    const res = await fetch("/api/diagram", {
      ...await jsonRequestOptions(diagramPayload),
    });
    if (!res.ok) {
      let msg = "Diagram build failed.";
      try { msg = (await res.json()).error || msg; } catch (e) { /* non-JSON */ }
      throw new Error(msg);
    }
    const formats = (res.headers.get("X-Architecture-Formats") || "drawio")
      .split(",")
      .map((format) => format.trim())
      .filter(Boolean);
    const aiStatus = res.headers.get("X-Architecture-AI") || "deterministic_fallback";
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    const safe = (state.bomName || "OCI").trim().replace(/[\\/:*?"<>|]+/g, "_").replace(/\s+/g, "_") || "OCI";
    link.download = `${safe}_architecture.zip`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    const formatLabel = formats.map((format) => format === "drawio" ? "draw.io" : format.toUpperCase()).join(" + ");
    const planningLabel = aiStatus === "assisted" ? "OpenAI-planned, validated" : "deterministic fallback";
    els.engineStatus.textContent = `Diagram downloaded: ${link.download} (${formatLabel}; ${planningLabel})`;
    setArchitectureExportStatus(
      `Downloaded ${link.download} (${formatLabel}; ${planningLabel})`,
      "success",
    );
  } catch (error) {
    els.engineStatus.textContent = `Diagram download failed - ${error.message}`;
    setArchitectureExportStatus(`Download failed: ${error.message}`, "error");
    console.error("diagram download failed", error);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = original; }
  }
}
els.downloadDiagram?.addEventListener("click", () => downloadDiagram(els.downloadDiagram));
els.deliverablesDiagram?.addEventListener(
  "click",
  () => downloadDiagram(els.deliverablesDiagram),
);
els.loadWorkflow?.addEventListener("click", () => els.loadWorkflowFile?.click());
els.loadPrevBom?.addEventListener("click", () => els.loadWorkflowFile?.click());
els.loadWorkflowFile?.addEventListener("change", (event) => {
  const file = event.target.files && event.target.files[0];
  loadWorkflowFromFile(file);
  event.target.value = "";
});

// Convert an alternate OCI BOM -> recognize SKUs -> load live into results.
function setConvertStatus(name, message, phase) {
  const el = els.convertBomStatus;
  if (!el) return;
  el.hidden = false;
  el.className = `load-workflow-status lws-${phase}`;
  el.querySelector(".lws-icon").textContent = phase === "ok" ? "✓" : phase === "error" ? "✕" : "⏳";
  el.querySelector(".lws-name").textContent = name || "";
  el.querySelector(".lws-state").textContent = message || "";
}
// An imported BOM can disagree with itself (a hand-added adjustment column, a non-USD currency,
// SKUs the catalog has never seen). The converter reports those rather than papering over them,
// so show them - a silently wrong total is far worse than a visible caveat.
function renderOtherBillWarnings(payload) {
  const box = els.otherBillWarnings;
  if (!box) return;
  const list = (payload && payload.conversionWarnings) || [];
  box.hidden = list.length === 0;
  box.innerHTML = list.map((w) => `<p>⚠ ${escapeHtml(String(w))}</p>`).join("");
}

// Workbooks with several proposals get a picker instead of a silent pick of one.
function renderOtherBillSheets(payload, file) {
  const wrap = els.otherBillSheets;
  const sel = els.otherBillSheetSelect;
  if (!wrap || !sel) return;
  const opts = (payload && payload.sheetOptions) || [];
  if (opts.length < 2) {
    wrap.hidden = true;
    return;
  }
  // The estimator splits one estimate across a sheet per service type (IAAS/PAAS/SAAS), so
  // "all combined" has to be offered - otherwise picking a sheet quietly halves the deal.
  const combinedTotal = opts.reduce((t, o) => t + Number(o.statedMonthly || 0), 0);
  const isCombined = Array.isArray(payload.combinedSheets);
  sel.innerHTML =
    `<option value="__all__"${isCombined ? " selected" : ""}>All ${opts.length} sheets combined${combinedTotal ? ` - ${formatCurrency(combinedTotal)}/mo` : ""}</option>` +
    opts
      .map((o) => {
        const money = o.statedMonthly ? ` - ${formatCurrency(o.statedMonthly)}/mo` : "";
        const label = o.label && o.label !== o.sheet ? ` (${o.label})` : "";
        const selected = !isCombined && o.sheet === payload.sheetName ? " selected" : "";
        return `<option value="${escapeHtml(o.sheet)}"${selected}>${escapeHtml(o.sheet)}${escapeHtml(label)}${escapeHtml(money)}</option>`;
      })
      .join("");
  wrap.hidden = false;
  sel.onchange = () => {
    if (file) convertBomFromFile(file, sel.value);
  };
}

async function convertBomFromFile(file, sheet = "") {
  if (!file) return;
  clearIntakeStatuses();   // switching to convert - clear the load banner
  const nm = file.name || "bom";
  state.lastUploadFile = file;
  if (els.fileInput) els.fileInput.value = "";   // so re-picking the same file fires change again
  const okExt = /\.(xlsx|xls|csv|tsv|json)$/i.test(nm);
  setConvertStatus(nm, okExt ? "converting…" : "not an .xlsx / .csv / .json file", okExt ? "loading" : "error");
  if (!okExt) {
    showSelectedDoc(nm, "Not an .xlsx / .csv / .json file");
    return;
  }
  showSelectedDoc(nm, "Reading OCI BOM…");
  // An .xlsx this app exported carries its full state on a hidden _workflow sheet. Restoring
  // that is strictly better than re-deriving a BOM from its own printed numbers, so try it
  // first and only fall through to SKU conversion when there's no embedded state.
  if (/\.xlsx?$/i.test(nm) && (await tryRestoreOwnExport(file))) return;
  if (els.priceSpinner) {
    els.priceSpinner.querySelector(".price-spinner-text").textContent = "Converting OCI BOM…";
    els.priceSpinner.hidden = false;
  }
  try {
    const fd = new FormData();
    fd.append("file", file);
    if (sheet) fd.append("sheet", sheet);
    const resp = await fetch("/api/convert-bom", { method: "POST", body: fd });
    const payload = await resp.json();
    if (!resp.ok) throw new Error(payload.error || "Could not convert this BOM.");
    renderOtherBillSheets(payload, file);
    renderOtherBillWarnings(payload);
    // Load the converted pricing live into the app and jump to results (page 4).
    // Keep the Other OCI Bill card selected when that's the path the user chose - the backend
    // reports the converted result as on-prem (that's how it prices and exports), but flipping
    // state.intakeMode here would silently deselect the card the user is looking at.
    state.intakeMode = isOtherOciBillMode() ? "other_oci_bill" : (payload.intakeMode || "on_prem");
    state.fullServiceBeta = payload.fullServiceBeta !== false;
    state.fields = [];
    state.rows = payload.rows || [];
    if (Array.isArray(payload.rateCards)) state.rateCards = payload.rateCards;
    state.selectedShape = payload.selectedShape?.key || state.selectedShape;
    state.pricing = payload;
    state.uploadReady = state.rows.length > 0;
    state.workflowMaxUnlockedStep = workflowStepIndex("shape");
    syncWorkflowAvailability();
    // Don't seed the BOM name from the uploaded filename - the export name comes from
    // what the user actually types at the top, nothing else.
    // A converted BOM starts on the Shape page (page 3): pick a shape (or keep the
    // detected per-server shapes) and continue to results. Pages 2 & 3 are navigable.
    renderPricing(payload);
    renderResults(payload);
    showShapePage();
    const rec = payload.recognizedSkus || 0;
    const rev = payload.unrecognizedSkus || 0;
    const sheetBit = payload.sheetName ? `"${payload.sheetName}" - ` : "";
    const aiBit = payload.aiAssist?.applied ? `, ${payload.aiAssist.applied} classified by AI` : "";
    const status = payload.comparisonSummary
      ? `imported comparison summary - ${payload.rows.length} service lines. Choose a shape →`
      : `converted ${sheetBit}${payload.rows.length} line items, ${rec} SKUs recognized${rev ? `, ${rev} for review` : ""}${aiBit}. Choose a shape →`;
    setConvertStatus(nm, status, "ok");
  } catch (error) {
    setConvertStatus(nm, error.message || "conversion failed", "error");
  } finally {
    if (els.priceSpinner) els.priceSpinner.hidden = true;
  }
}
els.convertBomBtn?.addEventListener("click", () => els.convertBomFile?.click());
els.convertBomFile?.addEventListener("change", (event) => {
  const file = event.target.files && event.target.files[0];
  convertBomFromFile(file);
  event.target.value = "";
});
els.bomName?.addEventListener("input", (event) => {
  state.bomName = event.target.value;
});
function renderCrossCloud() {
  const wrap = els.crossCloudResults;
  if (!wrap) return;
  const raw = state.pricing?.crossCloud;
  // Same effective OCI monthly the headline and the export use (adds Windows in cloud-bill
  // mode, where the backend keeps it out of totals.monthly, plus client-side added services).
  const ociMonthly = ociMonthlyTotal(state.pricing);
  if (!raw) {
    if (state.pricing?.converted) {
      wrap.innerHTML = `
        <div class="cross-cloud-grid">
          <div class="cross-cloud-card cross-cloud-oci">
            <span class="cross-cloud-card-name">Oracle Cloud (converted BOM)</span>
            <span class="cross-cloud-card-monthly">${formatCurrency(ociMonthly)}<small>/mo</small></span>
            <span class="cross-cloud-card-annual">${formatCurrency(ociMonthly * 12)}/yr</span>
          </div>
          <div class="cross-cloud-card cross-cloud-muted">
            <span class="cross-cloud-card-name">Other-cloud estimates unavailable</span>
            <span class="cross-cloud-card-note">Upload the original raw AWS or Azure bill in Cloud Bill mode to calculate equivalent-cloud pricing.</span>
          </div>
        </div>
        <p class="cross-cloud-note">A converted OCI BOM contains OCI line-item pricing, but not the source-cloud usage details required for a defensible AWS or Azure comparison.</p>
      `;
      return;
    }
    wrap.innerHTML = `<p class="cross-cloud-empty">Run a pricing estimate first to compare other clouds.</p>`;
    return;
  }
  // Support both the new {bestMatch, topTier} shape and the older flat shape.
  const hasModes = raw.bestMatch || raw.topTier;
  const cc = hasModes ? (state.crossCloudTopTier ? raw.topTier : raw.bestMatch) : raw;
  const bestTip = raw.cloudBillMode
    ? "Best match: your source cloud stays at its ACTUAL billed cost; the other cloud is estimated on the closest equivalent shape."
    : "Best match: price every workload on the closest equivalent shape, using your real source-cloud instance prices where known.";
  const topTip = raw.cloudBillMode
    ? "Top of the line: a what-if - re-estimate EVERY cloud (including your source bill) on each cloud's newest-generation shape. Non-compute services stay at billed cost."
    : "Top of the line: price every workload on each cloud's newest-generation shape.";
  const toggle = hasModes
    ? `<div class="mode-switch cross-cloud-switch" role="group" aria-label="Equivalent Shape Mode">
         <button type="button" class="mode-opt ${state.crossCloudTopTier ? "" : "is-active"}" data-cc-tier="best" title="${escapeHtml(bestTip)}">Best Match</button>
         <button type="button" class="mode-opt ${state.crossCloudTopTier ? "is-active" : ""}" data-cc-tier="top" title="${escapeHtml(topTip)}">Top of the Line</button>
       </div>`
    : "";
  const cards = [];
  // Services added on the Services step are OCI-side additions that the OCI headline already
  // includes. An ESTIMATED cloud must carry the same scope or the comparison stops being
  // like-for-like and OCI reads worse the more you add. A cloud shown at its ACTUAL BILLED
  // COST is the customer's real invoice with its own architecture and agreed price - that
  // figure is left exactly as it is.
  const extrasMonthly = extraServicesMonthly();
  const isActualBilled = (v) =>
    v.basis === "actual bill" || v.basis === "imported comparison total";
  const cloudMonthly = (v) =>
    Number(v.monthlyTotal || 0) + (isActualBilled(v) ? 0 : extrasMonthly);
  // Short display names: "AWS" and "Azure" read better than the vendors' full product names.
  const shortCloud = (key, label) =>
    key === "azure" ? "Azure" : key === "aws" ? "AWS" : (label || key.toUpperCase());

  // How OCI compares with each priced cloud, as a share of THAT cloud's cost: green when OCI
  // is cheaper, red when it isn't. Only rendered for clouds that are actually priced (GCP is
  // sizing-only), and skipped when the other side is 0 so we never divide by zero.
  const ociDeltas = ["aws", "azure"]
    .map((key) => {
      const v = cc[key];
      if (!v || !v.priced) return "";
      const other = cloudMonthly(v);
      if (!(other > 0)) return "";
      const pct = ((other - ociMonthly) / other) * 100;
      const cheaper = pct > 0;
      return `<span class="cross-cloud-delta ${cheaper ? "is-cheaper" : "is-pricier"}">${
        Math.abs(pct).toFixed(0)}% ${cheaper ? "less" : "more"} than ${
        escapeHtml(shortCloud(key, v.label))}</span>`;
    })
    .join("");
  cards.push(`
    <div class="cross-cloud-card cross-cloud-oci">
      <span class="cross-cloud-card-name">Oracle Cloud (this estimate)</span>
      <span class="cross-cloud-card-monthly">${formatCurrency(ociMonthly)}<small>/mo</small></span>
      <span class="cross-cloud-card-annual">${formatCurrency(ociMonthly * 12)}/yr</span>
      ${ociDeltas ? `<div class="cross-cloud-deltas">${ociDeltas}</div>` : ""}
    </div>
  `);
  const tier = state.crossCloudTopTier;
  const basisLabel = (v) => {
    if (v.basis === "actual bill") return sourceCostIsEstimated() ? "App Estimate - from usage (bill had no pricing)" : "your actual billed cost";
    if (v.basis === "imported comparison total") return "imported comparison total";
    if (v.basis === "what-if: bill re-shaped on newest-gen") return "what-if: your bill re-shaped on newest-gen";
    if (v.basis && v.basis.startsWith("compute + services re-priced")) return v.basis;
    if (v.carriedRows) return `compute estimated · ${v.carriedRows} services at billed cost`;
    if (v.liveRows) return `live AWS Price List API (${v.liveRows} priced live)`;
    if (tier) return "newest-generation equivalent shape";
    if (v.basis === "actual") return "from your source-cloud instances";
    if (v.basis === "mixed") return `${v.actualRows} actual · ${v.estimatedRows} equivalent`;
    return "equivalent shape match";
  };
  ["aws", "azure"].forEach((key) => {
    const v = cc[key];
    if (!v || !v.priced) return;
    const monthly = cloudMonthly(v);
    const addedHere = monthly - Number(v.monthlyTotal || 0);
    const delta = monthly - ociMonthly;
    const deltaLabel = ociMonthly > 0
      ? `${delta >= 0 ? "+" : "−"}${formatCurrency(Math.abs(delta))}/mo vs OCI`
      : "";
    // Reversed: other cloud cheaper than OCI (negative) = red; pricier = green.
    const deltaClass = delta >= 0 ? "cross-cloud-down" : "cross-cloud-up";
    // The source cloud's card is an App Estimate when the bill had no pricing - flag it.
    const nameSuffix = (sourceCostIsEstimated() && key === raw.sourceCloud) ? " (App Estimate)" : "";
    cards.push(`
      <div class="cross-cloud-card">
        <span class="cross-cloud-card-name">${escapeHtml((v.label || key.toUpperCase()) + nameSuffix)}</span>
        <span class="cross-cloud-card-monthly">${formatCurrency(monthly)}<small>/mo</small></span>
        <span class="cross-cloud-card-annual">${formatCurrency(monthly * 12)}/yr</span>
        ${deltaLabel ? `<span class="cross-cloud-delta ${deltaClass}">${deltaLabel}</span>` : ""}
        <span class="cross-cloud-basis">${escapeHtml(basisLabel(v))}${
          addedHere > 0 ? ` · incl. ${formatCompactCurrency(addedHere)} added services` : ""}</span>
      </div>
    `);
  });
  const gcp = cc.gcp;
  if (gcp && !gcp.priced) {
    cards.push(`
      <div class="cross-cloud-card cross-cloud-muted">
        <span class="cross-cloud-card-name">${escapeHtml(gcp.label || "Google Cloud")}</span>
        <span class="cross-cloud-card-note">${escapeHtml(gcp.note || "Sizing only")}</span>
      </div>
    `);
  }
  const srcCloud = raw.sourceCloud;
  const srcName = srcCloud === "azure" ? "Azure" : "AWS";
  const note = raw.importedComparison
    ? `Imported from the finished ${srcName}-to-OCI comparison workbook. These are the source-cloud and OCI totals recorded in that file; another cloud cannot be estimated without the original raw usage export.`
    : raw.cloudBillMode
    ? (tier
        ? `Top-of-the-line (what-if): every cloud - including your ${srcName} bill - is re-estimated on that cloud's newest-generation equivalent shape, so you can see what the same workloads would cost re-shaped. Non-compute services (storage, data transfer, managed services) stay at their actual billed cost. For directional comparison only - not a quote.`
        : `Best match: your ${srcName} total is your actual billed cost - no estimate. The other cloud estimates compute line items against an equivalent shape and carries non-compute services at their billed cost. Switch to Top of the line to re-estimate your bill on newest-generation shapes. For directional comparison only - not a quote.`)
    : tier
    ? "Top-of-the-line mode prices every workload against each cloud's newest-generation equivalent shape (Linux baseline, plus attached block storage, Windows, and SQL Server licensing where detected). For directional comparison only - not a quote."
    : "Best-match mode uses your actual source-cloud shape prices where known, otherwise the closest equivalent shape on each cloud (Linux baseline, plus attached block storage, Windows, and SQL Server licensing where detected). For directional comparison only - not a quote.";
  const estNote = sourceCostIsEstimated()
    ? ` Your ${srcName} bill contained usage/SKUs but no pricing, so the ${srcName} total shown is an App Estimate reconstructed from usage - not a billed figure.`
    : "";
  wrap.innerHTML = `
    ${toggle ? `<div class="cross-cloud-toolbar">${toggle}</div>` : ""}
    <div class="cross-cloud-grid">${cards.join("")}</div>
    <p class="cross-cloud-note">${note}${estNote}</p>
  `;
  wrap.querySelectorAll("[data-cc-tier]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.crossCloudTopTier = btn.dataset.ccTier === "top";
      renderCrossCloud();
    });
  });
}

els.backToReview?.addEventListener("click", showIntakePage);
els.continueToReviewFromUpload?.addEventListener("click", showReviewPage);
els.backToUploadFromReview?.addEventListener("click", showUploadPage);
els.backToReviewFromShape.addEventListener("click", showIntakePage);
els.backToShapeFromNetworking?.addEventListener("click", showShapePage);
els.continueToPriceFromServices?.addEventListener("click", () => {
  if (!state.pricing) return;
  unlockWorkflowStep("price");
  openPriceStep();
});
els.backToCompareFromArchitecture?.addEventListener("click", showOtherCloudsPage);
els.continueToDeliverables?.addEventListener("click", () => {
  if (!state.pricing) return;
  unlockWorkflowStep("deliverables");
  openDeliverablesStep();
});
els.backToArchitectureFromDeliverables?.addEventListener("click", showArchitecturePage);
els.backToServicesFromPrice?.addEventListener("click", showNetworkingPage);
els.continueToOtherClouds?.addEventListener("click", () => {
  if (!state.pricing) return;
  unlockWorkflowStep("other-clouds");
  openOtherCloudsStep();
});
els.backToPriceFromOtherClouds?.addEventListener("click", showResultsPage);
els.continueToArchitectureFromOtherClouds?.addEventListener("click", () => {
  if (!state.pricing) return;
  unlockWorkflowStep("architecture");
  showArchitecturePage();
});
// The "forward"/primary button that advances FROM each step. Clicking the tab immediately
// after the current one runs that button - same as the page's Continue button - so users can
// advance from the tab bar, not just the in-page button.
const STEP_FORWARD_BTN = {
  upload: "continueToReviewFromUpload",
  review: "priceButton",
  shape: "priceShapeButton",
  networking: "continueToPriceFromServices",
  price: "continueToOtherClouds",
  "other-clouds": "continueToArchitectureFromOtherClouds",
  architecture: "continueToDeliverables",
};
function currentActiveStep() {
  return document.querySelector(".step.is-active")?.dataset.step || null;
}
els.steps.forEach((step) => {
  step.addEventListener("click", () => {
    const target = step.dataset.step;
    const current = currentActiveStep();
    const ci = workflowStepIndex(current);
    const ti = workflowStepIndex(target);
    // The IMMEDIATELY-next tab always runs the page's forward button, whether or not that step
    // has been unlocked before. Jumping straight to the next page would skip the work the
    // button does - re-pricing with the shape and toggles chosen on this page - so a user who
    // changed something here and clicked the tab would land on stale numbers.
    if (ci >= 0 && ti === ci + 1) {
      const btn = document.getElementById(STEP_FORWARD_BTN[current] || "");
      if (btn && !btn.disabled) { btn.click(); return; }
    }
    if (!step.disabled && isWorkflowStepUnlocked(target)) navigateStep(target);
  });
});
els.modeOnPrem?.addEventListener("click", () => setIntakeMode("on_prem"));
els.modeCloudBill?.addEventListener("click", () => setIntakeMode("cloud_bill"));
els.modeOtherOciBill?.addEventListener("click", () => setIntakeMode("other_oci_bill"));
els.providerHint?.addEventListener("change", () => {
  state.providerHint = els.providerHint.value || "auto";
  syncModeUi();
  // If a cloud bill is already loaded, re-parse it with the chosen provider so
  // mapping uses the right cloud (no need to re-pick the file).
  if (state.intakeMode === "cloud_bill" && state.lastUploadFile) {
    uploadFile(state.lastUploadFile);
  }
});
syncModeUi();
syncIntakeLayout();
syncWorkflowAvailability();

if (els.rampChart) {
  els.rampChart.addEventListener("pointerdown", startRampDrag);
  els.rampChart.addEventListener("pointermove", moveRampDrag);
  els.rampChart.addEventListener("pointerup", endRampDrag);
  els.rampChart.addEventListener("pointercancel", endRampDrag);
  els.rampChart.addEventListener("keydown", nudgeRamp);
}

if (els.rampPeakMonth) {
  els.rampPeakMonth.addEventListener("input", () => {
    if (els.rampPeakMonth.value === "") return;
    const selected = selectedRampPoint();
    if (selected) {
      setRampPoint(selected.id, els.rampPeakMonth.value, selected.monthly);
    }
  });
}

if (els.rampPeakMonthly) {
  els.rampPeakMonthly.addEventListener("input", () => {
    if (els.rampPeakMonthly.value === "") return;
    const selected = selectedRampPoint();
    if (selected) {
      setRampPoint(selected.id, selected.month, els.rampPeakMonthly.value);
    }
  });
}

if (els.addRampPoint) {
  els.addRampPoint.addEventListener("click", addRampPointInLargestGap);
}

if (els.removeRampPoint) {
  els.removeRampPoint.addEventListener("click", removeSelectedRampPoint);
}

window.addEventListener("resize", () => {
  if (state.pricing) {
    renderConsumptionRamp();
  }
});

// ===========================================================================
// "Add OCI services" panel - search the OCI catalog, size a service, add it to
// the BOM. Added services flow into the results total and both exports.
// ===========================================================================
// Monthly list price of added services.
function extraServicesMonthly() {
  return (state.extraServices || []).reduce((t, s) => t + Number(s.monthly || 0), 0);
}

async function fetchCatalog() {
  const q = state.catalog.query || "";
  const g = state.catalog.group || "";
  try {
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (g) params.set("group", g);
    const r = await fetch(`/api/catalog?${params.toString()}`);
    const d = await r.json();
    state.catalog.groups = d.groups || [];
    state.catalog.results = d.results || [];
  } catch (e) {
    state.catalog.results = [];
  }
  renderServiceChips();
  renderServiceResults();
}

function renderServiceChips() {
  if (!els.serviceChips) return;
  const chips = [{ group: "", count: 0, label: "All" }].concat(
    (state.catalog.groups || []).map((g) => ({ ...g, label: g.group })),
  );
  els.serviceChips.innerHTML = chips
    .map((c) => {
      const active = (state.catalog.group || "") === c.group ? " is-active" : "";
      const count = c.group ? ` <span class="chip-count">${c.count}</span>` : "";
      return `<button type="button" class="service-chip${active}" data-group="${escapeHtml(c.group)}">${escapeHtml(c.label)}${count}</button>`;
    })
    .join("");
}

// --- Combined "OCI Generative AI" card (Models + Search & Retrieval sections) --------------
function genaiSelectHtml(i, key, label, options, def, condAttr = "") {
  const opts = options
    .map((o) => `<option value="${escapeHtml(o.value)}"${o.value === def ? " selected" : ""}>${escapeHtml(o.label)}</option>`)
    .join("");
  return `<label class="svc-field"${condAttr}><span>${escapeHtml(label)}</span>
    <select class="svc-input" data-idx="${i}" data-key="${escapeHtml(key)}">${opts}</select></label>`;
}
function genaiNumHtml(i, key, label, def = 0, condAttr = "", unit = "", spanAttr = "") {
  return `<label class="svc-field"${condAttr}><span${spanAttr}>${escapeHtml(label)}</span>
    <input type="number" class="svc-input" data-idx="${i}" data-key="${escapeHtml(key)}" value="${def}" min="0" step="1" />${unit ? `<em>${escapeHtml(unit)}</em>` : ""}</label>`;
}
function genaiModelsHtml(e, i) {
  const providers = e.genaiProviders || [];
  const defProv = providers[0] || "";
  const modelOpts = (e.genaiModelOptions || {})[defProv] || [];
  const defModel = modelOpts[0]?.value || "";
  const lenUnit = ((e.genaiModelInfo || {})[defModel] || {}).lengthUnit || "characters";
  const dedOpts = Object.entries((e.genaiDedicated || {}).labels || {}).map(([v, l]) => ({ value: v, label: l }));
  const hide = ` data-hidewhen-field="metric" data-hidewhen-value="dedicated"`;
  const show = ` data-showwhen-field="metric" data-showwhen-value="dedicated"`;
  return `
    <div class="genai-section">
      <div class="genai-section-title">OCI Generative AI - Models</div>
      <div class="genai-row">
        ${genaiSelectHtml(i, "metric", "Service Metric", [{ value: "on_demand", label: "On Demand" }, { value: "dedicated", label: "Dedicated" }], "on_demand")}
        ${genaiSelectHtml(i, "provider", "Model Provider", providers.map((p) => ({ value: p, label: p })), defProv, hide)}
        ${genaiSelectHtml(i, "model", "Model", modelOpts, defModel, hide)}
        ${genaiSelectHtml(i, "ded_cluster", "Cluster type", dedOpts, dedOpts[0]?.value || "", show)}
        ${genaiNumHtml(i, "ded_units", "AI units", 1, show, "unit")}
      </div>
      <div class="genai-row"${hide}>
        ${genaiNumHtml(i, "requests", "Expected number of requests per month", 0, "", "")}
        ${genaiNumHtml(i, "prompt_len", `Prompt length (in ${lenUnit})`, 0, "", "", ` data-genai-lenlabel="prompt"`)}
        ${genaiNumHtml(i, "response_len", `Model Response length (in ${lenUnit})`, 0, "", "", ` data-genai-lenlabel="response"`)}
      </div>
    </div>`;
}
function genaiRetrievalHtml(e, i) {
  const meters = e.genaiRetrieval || [];
  const order = [];
  const bySec = {};
  meters.forEach((m) => { if (!bySec[m.section]) { bySec[m.section] = []; order.push(m.section); } bySec[m.section].push(m); });
  const groups = order.map((sec) => `
      <div class="genai-subgroup">
        <div class="genai-subhead">${escapeHtml(sec)}</div>
        <div class="genai-row">${bySec[sec].map((m) => genaiNumHtml(i, m.key, m.label, 0, "", "")).join("")}</div>
      </div>`).join("");
  const hrs = Number(e.retrievalHoursDefault ?? 744) || 744;
  return `
    <div class="genai-section">
      <div class="genai-section-head">
        <div class="genai-section-title">OCI Generative AI - Search and Retrieval</div>
        <label class="svc-field genai-hours"><span>Utilization (hrs/mo)</span>
          <input type="number" class="svc-input" data-idx="${i}" data-key="__hours" value="${hrs}" min="1" step="1" /><em>hrs</em></label>
      </div>
      ${groups}
    </div>`;
}
function genaiAgentsHtml(e, i) {
  const hrs = Number(e.retrievalHoursDefault ?? 744) || 744;
  return `
    <div class="genai-section">
      <div class="genai-section-title">Retrieval-Augmented Generation (RAG)</div>
      <div class="genai-row">
        ${genaiNumHtml(i, "rag_requests", "Requests per month", 0, "", "requests")}
        ${genaiNumHtml(i, "rag_chars", "Average characters processed per request", 0, "", "chars")}
      </div>
    </div>
    <div class="genai-section">
      <div class="genai-section-head">
        <div class="genai-section-title">Managed Knowledge Base (optional)</div>
        <label class="svc-field genai-hours"><span>Utilization (hrs/mo)</span>
          <input type="number" class="svc-input" data-idx="${i}" data-key="__hours" value="${hrs}" min="1" step="1" /><em>hrs</em></label>
      </div>
      <div class="genai-row">
        ${genaiNumHtml(i, "kb_storage", "Storage capacity", 0, "", "GB")}
        ${genaiNumHtml(i, "kb_jobs", "Data ingestion jobs per month", 0, "", "jobs")}
        ${genaiNumHtml(i, "kb_chars", "Average characters processed per job", 0, "", "chars")}
      </div>
    </div>`;
}
// Repopulate the Model dropdown for the selected provider, and refresh the "in tokens/characters"
// length labels for the selected model. Called when a genai card's provider/model select changes.
// Base Database: Processor, Shape, OCPU count and Storage describe ONE machine, so they have
// to agree. Left ungated the card let you pick AMD and then an Ampere shape, offered Arm's
// capped storage tiers to x86, and accepted "1 OCPU" on a fixed 24-OCPU shape while quietly
// pricing 24. Processor is the broadest choice, so it drives the rest.
function refreshBasedbDependents(card, entry) {
  const shapes = entry.basedbShapesByProcessor || {};
  const storage = entry.basedbStorageByProcessor || {};
  const procSel = card.querySelector('.svc-input[data-key="processor"]');
  const shapeSel = card.querySelector('.svc-input[data-key="shape"]');
  const storeSel = card.querySelector('.svc-input[data-key="storagegb"]');
  const ocpuInput = card.querySelector('.svc-input[data-key="ecpu"]');
  const edSel = card.querySelector('.svc-input[data-key="edition_ocpu"]');
  if (!procSel || !shapeSel) return;
  const proc = procSel.value || "amd";

  // Shape list follows the processor; keep the selection if it survives the switch.
  const opts = shapes[proc] || [];
  const curShape = shapeSel.value;
  shapeSel.innerHTML = opts
    .map((o) => `<option value="${escapeHtml(o.value)}">${escapeHtml(o.label)}</option>`)
    .join("");
  shapeSel.value = opts.some((o) => o.value === curShape) ? curShape : (opts[0]?.value || "");
  const shape = opts.find((o) => o.value === shapeSel.value) || {};

  // Storage tiers follow the processor, and drop again when the shape is a fixed one.
  if (storeSel && storage[proc]) {
    const tiers = (shape.fixed ? storage[proc].fixed : storage[proc].flex) || [];
    const curTier = storeSel.value;
    storeSel.innerHTML = tiers
      .map((o) => `<option value="${escapeHtml(o.value)}">${escapeHtml(o.label)}</option>`)
      .join("");
    storeSel.value = tiers.some((o) => o.value === curTier)
      ? curTier
      : (tiers[tiers.length - 1]?.value || "");
  }

  // OCPU count: a fixed shape dictates it, so show the real number and lock the box rather
  // than accepting one figure and billing another. A Flex shape caps by edition.
  if (ocpuInput) {
    if (shape.fixed) {
      ocpuInput.value = shape.ocpu;
      ocpuInput.readOnly = true;
      ocpuInput.title = `${shapeSel.value} is a fixed shape - it always runs ${shape.ocpu} OCPU.`;
      ocpuInput.removeAttribute("max");
    } else {
      const cap = Number((shape.maxOcpuByEdition || {})[edSel?.value] || shape.maxOcpu || 0);
      ocpuInput.readOnly = false;
      if (cap > 0) {
        ocpuInput.max = cap;
        ocpuInput.title = `${shapeSel.value} supports up to ${cap} OCPU on this edition.`;
        if (Number(ocpuInput.value) > cap) ocpuInput.value = cap;
      } else {
        ocpuInput.removeAttribute("max");
        ocpuInput.title = "";
      }
    }
  }
}

function refreshGenaiModelOptions(card, entry) {
  const provSel = card.querySelector('.svc-input[data-key="provider"]');
  const modelSel = card.querySelector('.svc-input[data-key="model"]');
  if (!provSel || !modelSel) return;
  const opts = (entry.genaiModelOptions || {})[provSel.value] || [];
  const cur = modelSel.value;
  modelSel.innerHTML = opts
    .map((o) => `<option value="${escapeHtml(o.value)}"${o.value === cur ? " selected" : ""}>${escapeHtml(o.label)}</option>`)
    .join("");
  if (!opts.some((o) => o.value === cur)) modelSel.value = opts[0]?.value || "";
}
function refreshGenaiLenLabels(card, entry) {
  const modelSel = card.querySelector('.svc-input[data-key="model"]');
  const mi = (entry.genaiModelInfo || {})[modelSel?.value] || {};
  const lu = mi.lengthUnit || "characters";
  card.querySelectorAll("[data-genai-lenlabel]").forEach((el) => {
    el.textContent = el.getAttribute("data-genai-lenlabel") === "prompt"
      ? `Prompt length (in ${lu})` : `Model Response length (in ${lu})`;
  });
}

function genaiServiceCardShell(e, i, innerHtml) {
  return `
    <div class="service-card genai-card" data-idx="${i}">
      <div class="service-card-head">
        <div>
          <strong>${escapeHtml(e.name)}</strong>
          <span class="service-card-meta" data-service-meta="${i}">${escapeHtml(e.group)} · like Oracle Cost Estimator</span>
        </div>
        <span class="service-card-cost" data-cost="${i}">$0.00/mo</span>
      </div>
      ${e.note ? `<p class="service-card-note">${escapeHtml(e.note)}</p>` : ""}
      <div class="service-card-fields genai-fields">${innerHtml}</div>
      <div class="service-card-actions">
        <button type="button" class="ghost-button svc-add" data-idx="${i}">Add to BOM</button>
      </div>
    </div>`;
}

// Options carrying a `group` are clustered under an <optgroup>, so a 20-shape compute list
// reads by processor family the way Oracle's estimator does instead of as one flat run.
// Ungrouped options render exactly as before.
function optionListHtml(options, selected) {
  const opt = (o) =>
    `<option value="${escapeHtml(o.value)}"${o.value === selected ? " selected" : ""}>${escapeHtml(o.label)}</option>`;
  if (!options.some((o) => o.group)) return options.map(opt).join("");
  const order = [];
  const byGroup = new Map();
  options.forEach((o) => {
    const g = o.group || "Other";
    if (!byGroup.has(g)) { byGroup.set(g, []); order.push(g); }
    byGroup.get(g).push(o);
  });
  return order
    .map((g) => `<optgroup label="${escapeHtml(g)}">${byGroup.get(g).map(opt).join("")}</optgroup>`)
    .join("");
}

function serviceCardHtml(e, i) {
  if (e.id === "genai") {
    return genaiServiceCardShell(e, i, genaiModelsHtml(e, i) + genaiRetrievalHtml(e, i));
  }
  if (e.id === "genai_agents") {
    return genaiServiceCardShell(e, i, genaiAgentsHtml(e, i));
  }
  let fields = (e.fields || [])
    .map((f) => {
      // hideWhen is the mirror of showWhen. The catalog has always emitted both, but only
      // showWhen was ever rendered, so a hideWhen field stayed permanently visible - which is
      // why Base Database showed its ECPU and OCPU edition pickers at the same time.
      const showAttr = (f.showWhen
        ? ` data-showwhen-field="${escapeHtml(f.showWhen.field)}" data-showwhen-value="${escapeHtml(f.showWhen.value)}"`
        : "")
        + (f.hideWhen
        ? ` data-hidewhen-field="${escapeHtml(f.hideWhen.field)}" data-hidewhen-value="${escapeHtml(f.hideWhen.value)}"`
        : "");
      const control = f.options
        ? `<select class="svc-input" data-idx="${i}" data-key="${escapeHtml(f.key)}">
             ${optionListHtml(f.options, f.default)}
           </select>`
        : `<input type="number" class="svc-input" data-idx="${i}" data-key="${escapeHtml(f.key)}"
                  value="${f.default ?? 0}" min="${f.min ?? 0}" step="${f.step ?? 1}" />`;
      return `<label class="svc-field"${showAttr}><span>${escapeHtml(f.label)}</span>
                ${control}${f.unit ? `<em>${escapeHtml(f.unit)}</em>` : ""}</label>`;
    })
    .join("");
  // Per-hour services get an editable Hours/month input. Curated cards default to the app's
  // 730; estimator-generated cards default to that service's own utilisation in Oracle's
  // estimator (31 days x 24 hrs = 744), so the card opens on the same number Oracle shows.
  if (e.basis === "hour") {
    const defHours = Number(e.estimatorHoursDefault) > 0 ? Number(e.estimatorHoursDefault) : 730;
    fields +=
      `<label class="svc-field"><span>Hours / month</span>
         <input type="number" class="svc-input" data-idx="${i}" data-key="__hours"
                value="${defHours}" min="1" step="1" />
         <em>hrs</em></label>`;
  }
  const rateTxt = `$${Number(e.rate).toLocaleString(undefined, { maximumFractionDigits: 4 })} / ${escapeHtml(e.unit)}`;
  return `
    <div class="service-card" data-idx="${i}">
      <div class="service-card-head">
        <div>
          <strong>${escapeHtml(e.name)}</strong>
          <span class="service-card-meta" data-service-meta="${i}">${escapeHtml(e.group)} · ${escapeHtml(e.sku)} · ${rateTxt}</span>
        </div>
        <span class="service-card-cost" data-cost="${i}">$0.00/mo</span>
      </div>
      ${e.note ? `<p class="service-card-note">${escapeHtml(e.note)}</p>` : ""}
      <div class="service-card-fields">${fields}</div>
      <div class="service-card-actions">
        <button type="button" class="ghost-button svc-add" data-idx="${i}">Add to BOM</button>
      </div>
    </div>`;
}

function renderServiceResults() {
  if (!els.serviceResults) return;
  const items = state.catalog.results || [];
  if (!items.length) {
    els.serviceResults.innerHTML =
      `<p class="service-empty">${state.catalog.query ? "No services match that search." : "Pick a category or search to add services."}</p>`;
    return;
  }
  // Group results by category into collapsible sections. Remembering open/closed per group
  // means browsing stays tidy - expand only the category you care about.
  const groupsInOrder = [];
  const byGroup = new Map();
  items.forEach((e, i) => {
    if (!byGroup.has(e.group)) {
      byGroup.set(e.group, []);
      groupsInOrder.push(e.group);
    }
    byGroup.get(e.group).push({ e, i });
  });

  // Default open state: open everything on a text search or a single group; otherwise
  // start every category collapsed so the "All" list opens compact.
  const openState = state.catalog.groupsOpen || {};
  const singleOrSearch = groupsInOrder.length === 1 || !!state.catalog.query;

  els.serviceResults.innerHTML = groupsInOrder
    .map((g, gi) => {
      const entries = byGroup.get(g);
      const open = g in openState ? openState[g] : singleOrSearch;
      const cards = entries.map(({ e, i }) => serviceCardHtml(e, i)).join("");
      return `
        <section class="service-group${open ? " is-open" : ""}" data-group="${escapeHtml(g)}">
          <button type="button" class="service-group-head" data-group-toggle="${escapeHtml(g)}" aria-expanded="${open}">
            <span class="service-group-caret" aria-hidden="true">▸</span>
            <span class="service-group-title">${escapeHtml(g)}</span>
            <span class="service-group-count">${entries.length}</span>
          </button>
          <div class="service-group-body"${open ? "" : " hidden"}>${cards}</div>
        </section>`;
    })
    .join("");

  els.serviceResults.querySelectorAll(".service-card").forEach((card) => {
    const idx = Number(card.dataset.idx);
    // Gate the dependent dropdowns on first paint too, not only on change - otherwise a card
    // opens showing options its own defaults do not allow.
    const entry = (state.catalog.results || [])[idx];
    if (entry && entry.basedbShapesByProcessor) refreshBasedbDependents(card, entry);
    updateCardCost(idx);
  });
  applyCardFieldVisibility();
}

function cardValues(idx) {
  const vals = {};
  els.serviceResults
    .querySelectorAll(`.svc-input[data-idx="${idx}"]`)
    .forEach((inp) => {
      // Dropdowns carry a string value (e.g. workload/deployment); numeric inputs a number.
      vals[inp.dataset.key] = inp.tagName === "SELECT" ? inp.value : (Number(inp.value) || 0);
    });
  return vals;
}

function fastConnectSelection(entry, values = {}) {
  const speed = String(values.speed || "10G").toUpperCase();
  const normalizedSpeed = ["1G", "10G", "100G", "400G"].includes(speed) ? speed : "10G";
  const fallbackRates = { "1G": 0.2125, "10G": 1.275, "100G": 10.75, "400G": 20 };
  const fallbackSkus = {
    "1G": "B88325",
    "10G": "B88326",
    "100G": "B93126",
    "400G": "B107975",
  };
  const labels = entry.speedLabels || {
    "1G": "1 Gbps",
    "10G": "10 Gbps",
    "100G": "100 Gbps",
    "400G": "400 Gbps",
  };
  return {
    speed: normalizedSpeed,
    label: labels[normalizedSpeed],
    rate: Number(entry.speedRates?.[normalizedSpeed] ?? fallbackRates[normalizedSpeed]),
    sku: entry.speedSkus?.[normalizedSpeed] || fallbackSkus[normalizedSpeed],
  };
}

// Show/hide conditional fields (data-showwhen-*) based on the current dropdown selection.
function applyCardFieldVisibility(scope) {
  (scope || els.serviceResults).querySelectorAll("[data-hidewhen-field]").forEach((el) => {
    const card = el.closest(".service-card");
    const ctrl = card && card.querySelector(`.svc-input[data-key="${el.dataset.hidewhenField}"]`);
    const hideOn = String(el.dataset.hidewhenValue || "").split("|");
    el.style.display = ctrl && hideOn.includes(ctrl.value) ? "none" : "";
  });
  (scope || els.serviceResults).querySelectorAll("[data-showwhen-field]").forEach((el) => {
    const card = el.closest(".service-card");
    const ctrl = card && card.querySelector(`.svc-input[data-key="${el.dataset.showwhenField}"]`);
    const showOn = String(el.dataset.showwhenValue || "").split("|");
    el.style.display = ctrl && showOn.includes(ctrl.value) ? "" : "none";
  });
}

// Mirror of oci_catalog.line_cost so the preview is instant (server recomputes on add/export).
// Per-hour services use the app's hours-per-month setting, not a static 730.
function clientLineCost(entry, v) {
  const rate = Number(entry.rate || 0);
  const free = entry.free || {};
  // Add-ins default to 730 hours/month, editable per SKU via the "__hours" input.
  const hours = Number(v.__hours) > 0 ? Number(v.__hours) : 730;
  const cid = entry.id || entry.catalogId;
  // Flexible Load Balancer: both meters tier on quantity x HOURS (744 LB-hours, 7,440
  // Mbps-hours), so the free allowance is monthly. Mirror of the lbMeters branch in
  // oci_catalog.line_cost.
  if (entry.lbMeters) {
    const lh = Number(v.__hours) > 0 ? Number(v.__hours) : hours;
    let total = 0;
    Object.values(entry.lbMeters).forEach((m) => {
      let qty = Number(v[m.field] || 0) * lh;
      if (m.field === "mbps") qty *= Number(v.count || 1);
      (m.tiers || []).forEach((t) => {
        const lo = Number(t.min || 0);
        if (qty <= lo) return;
        const hi = t.max === null || t.max === undefined ? null : Number(t.max);
        const span = hi === null ? qty - lo : Math.min(qty, hi) - lo;
        if (span > 0) total += span * Number(t.rate || 0);
      });
    });
    return Math.round(total * 100) / 100;
  }
  // Compute cards: the shape selection picks the SKUs, quantities follow it. Mirror of the
  // computeCard branch in oci_catalog.line_cost.
  if (entry.computeCard) {
    const ch = Number(v.__hours) > 0
      ? Number(v.__hours)
      : (Number(entry.estimatorHoursDefault) > 0 ? Number(entry.estimatorHoursDefault) : hours);
    if (entry.computeCard === "ocvs") {
      const plan = (entry.ocvsPlans || {})[String(v.plan || "")] || {};
      const exp = (entry.ocvsExpansion || {})[String(v.expansion || "")] || {};
      const hcx = entry.ocvsHcx || {};
      const qty = plan.node ? Number(v.nodes || 0) : Number(v.ocpu || 0);
      const t = (qty * Number(plan.rate || 0)
        + Number(v.expansion_ocpu || 0) * Number(exp.rate || 0)
        + Number(v.hcx_ocpu || 0) * Number(hcx.rate || 0)) * ch;
      return Math.round(t * 100) / 100;
    }
    if (entry.computeCard === "gpu") {
      const g = (entry.gpuOptions || {})[String(v.gpu || "")] || {};
      const l = (entry.aieOptions || {})[String(v.aie || "")] || {};
      const t = (Number(v.gpus || 0) * Number(g.rate || 0)
        + Number(v.aie_gpus || 0) * Number(l.rate || 0)) * ch;
      return Math.round(t * 100) / 100;
    }
    const shape = (entry.shapeOptions || {})[String(v.shape || "")] || {};
    const per = Number(v.ocpu || 0) * Number((shape.ocpu || {}).rate || 0)
      + Number(v.memory || 0) * Number((shape.memory || {}).rate || 0)
      + Number(v.nvme || 0) * Number((shape.nvme || {}).rate || 0);
    // Instances defaults to 1 so an untouched card prices one machine, not zero.
    const count = v.instances === undefined ? 1 : Number(v.instances || 0);
    return Math.round(per * ch * Math.max(count, 0) * 100) / 100;
  }
  // Estimator-generated card: graduated tiers per SKU. Mirror of the skuMeters branch in
  // oci_catalog.line_cost - the server re-prices authoritatively on export, this is the live
  // figure while the card is being filled in.
  if (entry.skuMeters) {
    const estHours = Number(v.__hours) > 0
      ? Number(v.__hours)
      : (Number(entry.estimatorHoursDefault) > 0 ? Number(entry.estimatorHoursDefault) : hours);
    const total = entry.skuMeters.reduce((sum, m) => {
      const qty = Number(v[m.key] || 0);
      if (!(qty > 0)) return sum;
      // Graduated: only the units above a tier's floor pay that tier's rate, so an OCI free
      // allowance (first 500M datapoints, first 10 GB) really is free.
      let cost = 0;
      (m.tiers || []).forEach((t) => {
        const lo = Number(t.min || 0);
        if (qty <= lo) return;
        const hi = t.max === null || t.max === undefined ? null : Number(t.max);
        const span = hi === null ? qty - lo : Math.min(qty, hi) - lo;
        if (span > 0) cost += span * Number(t.rate || 0);
      });
      return sum + (m.hourly ? cost * estHours : cost);
    }, 0);
    return Math.round(total * 100) / 100;
  }
  if (cid === "block") {
    const gb = Number(v.gb || 0), vpus = Number(v.vpus || 10);
    return Math.round((gb * 0.0255 + gb * vpus * 0.0017) * 100) / 100;
  }
  if (cid === "fsdr") {
    // Full Stack DR: member OCPUs (compute+DB, both regions) + DB ECPUs + OIC packs.
    const ocpu = Number(v.p_compute || 0) + Number(v.p_db_ocpu || 0) + Number(v.s_compute || 0) + Number(v.s_db_ocpu || 0);
    const ecpu = Number(v.p_db_ecpu || 0) + Number(v.s_db_ecpu || 0);
    const oic = Number(v.p_oic || 0) + Number(v.s_oic || 0);
    return Math.round((ocpu * 0.0128 + ecpu * 0.0032 + oic * 0.192) * hours * 100) / 100;
  }
  // Base Database Service: mirror of the "basedb" branch in oci_catalog.line_cost - the
  // edition licence and the shared compute infrastructure ($0.0251/ECPU-hr) both bill per
  // ECPU-hour, plus database storage at $0.12/GB-month.
  if (cid === "basedb") {
    const rates = entry.editionRates || { standard: 0.0538, enterprise: 0.1075, high_perf: 0.2218, byol: 0.0484 };
    const ed = Number(rates[String(v.edition || "enterprise")] ?? 0.1075);
    return Math.round((Number(v.ecpu || 0) * (ed + 0.0251) * hours
      + Number(v.storagegb || 0) * 0.12) * 100) / 100;
  }
  // Combined "OCI Generative AI" card: mirror of the genaiCombined branch in
  // oci_catalog.line_cost - Models (per-request math) + Search & Retrieval (six meters).
  if (entry.genaiCombined || cid === "genai") {
    const gh = Number(v.__hours) || 744;
    let total = 0;
    if (String(v.metric || "on_demand") === "dedicated") {
      const dk = String(v.ded_cluster || "");
      total += Number(v.ded_units || 0) * Number((entry.genaiDedicated?.rates || {})[dk] || 0) * gh;
    } else {
      const mi = (entry.genaiModelInfo || {})[String(v.model || "")];
      if (mi) {
        const div = Number(mi.divisor || 1) || 1;
        total += Number(v.requests || 0)
          * (Number(v.prompt_len || 0) / div * Number(mi.inRate || 0)
             + Number(v.response_len || 0) / div * Number(mi.outRate || 0));
      }
    }
    (entry.genaiRetrieval || []).forEach((r) => {
      const q = Number(v[r.key] || 0);
      total += r.hourly ? q * Number(r.rate) * gh : q / Number(r.divisor || 1) * Number(r.rate);
    });
    return Math.round(total * 100) / 100;
  }
  // OCI Generative AI Agents (separate card): RAG transactions + optional Managed KB.
  if (entry.genaiAgents || cid === "genai_agents") {
    const gh = Number(v.__hours) || 744;
    const m = entry.genaiAgentMeters || {};
    const txn = m.txn || {}, kb = m.kb || {}, ing = m.ingest || {};
    const total = Number(v.rag_requests || 0) * Number(v.rag_chars || 0) / Number(txn.divisor || 1) * Number(txn.rate || 0)
      + Number(v.kb_storage || 0) * Number(kb.rate || 0) * gh
      + Number(v.kb_jobs || 0) * Number(v.kb_chars || 0) / Number(ing.divisor || 1) * Number(ing.rate || 0);
    return Math.round(total * 100) / 100;
  }
  // Variant-priced entries (Generative AI): mirror of the variantRates branch in
  // oci_catalog.line_cost. The server re-prices authoritatively on export; this is the live
  // figure shown while the user is filling the card in.
  if (entry.variantRates) {
    const key = String(v[entry.variantKey] || "");
    const rate = Number(entry.variantRates[key] ?? entry.rate ?? 0);
    // Mirror of the variantFree subtraction in oci_catalog.line_cost - a tier-priced meter's
    // free allowance comes off the quantity before the rate applies.
    const qty = Math.max(0, Number(v[entry.variantField || "units"] || 0)
      - Number((entry.variantFree || {})[key] || 0));
    const hourly = Boolean((entry.variantHourly || {})[key]);
    return Math.round(qty * rate * (hourly ? hours : 1) * 100) / 100;
  }
  if (cid === "fastconnect") {
    const selected = fastConnectSelection(entry, v);
    return Math.round(Number(v.ports || 0) * selected.rate * hours * 100) / 100;
  }
  if (cid === "adb") {
    // Autonomous AI Database: ECPU + storage + backup (mirror of oci_catalog.line_cost).
    const ecpuCost = Number(v.ecpu || 0) * 0.336 * hours;
    const bak = Number(v.bakgb || 0);
    if (String(v.deployment || "serverless") === "dedicated") {
      const infra = (Number(v.dbservers || 0) * 6.3014 + Number(v.storageservers || 0) * 5.4795) * hours;
      const backup = Math.max(0, bak - 10) * 0.0255;
      return Math.round((ecpuCost + infra + backup) * 100) / 100;
    }
    const storeRate = String(v.workload || "atp") === "adw" ? 0.0299 : 0.1953;
    return Math.round((ecpuCost + Number(v.dbgb || 0) * storeRate + bak * 0.0299) * 100) / 100;
  }
  if (cid === "desktops") {
    // Secure Desktops: per-desktop fee ($20) + compute + boot + optional block per desktop.
    // DVH (Windows-BYOL-on-DVH) runs on E4.128 host(s); VM modes use E6 per desktop.
    const n = Number(v.desktops || 0), ocpu = Number(v.ocpu || 0);
    let cost = n * 20.0
      + Number(v.optgb || 0) * n * 0.0255
      + Number(v.optgb || 0) * Number(v.optvpu || 0) * n * 0.0017;
    if (String(v.os || "linux") === "win_dvh") {
      const hosts = ocpu ? Math.max(1, Math.ceil(n * ocpu / 124)) : 1;
      cost += hosts * 128 * hours * 0.025 + hosts * 2048 * hours * 0.0015
        + Number(v.bootgb || 0) * hosts * 0.0255
        + Number(v.bootgb || 0) * Number(v.bootvpu || 0) * hosts * 0.0017;
    } else {
      cost += ocpu * n * hours * 0.03 + Number(v.memory || 0) * n * hours * 0.002
        + Number(v.bootgb || 0) * n * 0.0255
        + Number(v.bootgb || 0) * Number(v.bootvpu || 0) * n * 0.0017;
    }
    return Math.round(cost * 100) / 100;
  }
  if (cid === "sqllic") {
    // SQL Server license: per-edition OCPU-hour rate (Express is free).
    const ed = String(v.edition || "enterprise");
    const sqlRate = ed === "standard" ? 0.37 : ed === "express" ? 0 : 1.47;
    return Math.round(Number(v.ocpu || 0) * sqlRate * hours * 100) / 100;
  }
  if (cid === "kms") {
    // Key Management: vaults + external key mgmt + dedicated HSM (software keys free).
    return Math.round((Number(v.vaults || 0) * hours * 3.724
      + Number(v.external || 0) * 3.0
      + Number(v.hsm || 0) * hours * 1.75) * 100) / 100;
  }
  if (cid === "waf") {
    // WAF: instances (first free) + incoming requests per 1M (first 10M free).
    return Math.round((Math.max(0, Number(v.instances || 0) - 1) * 5.0
      + Math.max(0, Number(v.requests || 0) - 10) * 0.6) * 100) / 100;
  }
  if (cid === "object") {
    // Object Storage: GB (first 10 free) + requests per 10k (first 50k free).
    return Math.round((Math.max(0, Number(v.gb || 0) - 10) * 0.0255
      + Math.max(0, Number(v.requests || 0) - 5) * 0.0034) * 100) / 100;
  }
  if (cid === "object_ia") {
    // Infrequent Access: stored GB + retrieved GB + shared object-request meter.
    return Math.round((Math.max(0, Number(v.gb || 0) - 10) * 0.01
      + Math.max(0, Number(v.retrievalGb || 0) - 10) * 0.01
      + Math.max(0, Number(v.requests || 0) - 5) * 0.0034) * 100) / 100;
  }
  if (cid === "archive") {
    // Archive: stored GB + shared object-request meter.
    return Math.round((Math.max(0, Number(v.gb || 0) - 10) * 0.0026
      + Math.max(0, Number(v.requests || 0) - 5) * 0.0034) * 100) / 100;
  }
  if (cid === "pg") {
    // Database with PostgreSQL: managed OCPU + storage + underlying compute (per-processor) + VPU.
    const ocpu = Number(v.ocpu || 0), nodes = Number(v.nodes || 1) || 1, storage = Number(v.storage || 0);
    const intel = String(v.processor || "amd") === "intel";
    const cOcpu = intel ? 0.04 : 0.03, cMem = intel ? 0.0015 : 0.002;
    const cost = ocpu * nodes * hours * 0.098
      + storage * 0.072
      + ocpu * nodes * hours * cOcpu
      + Number(v.memory || 0) * nodes * hours * cMem
      + storage * Number(v.vpu || 0) * 0.0017;
    return Math.round(cost * 100) / 100;
  }
  if (cid === "mysql") {
    // MySQL HeatWave: ECPU + storage + backup + egress; HA triples ECPU+storage; +HeatWave.
    const mult = String(v.ha || "no") === "yes" ? 3 : 1;
    let cost = Number(v.ecpu || 0) * 0.0366 * hours * mult
      + Number(v.storage || 0) * 0.04 * mult
      + Number(v.backup || 0) * 0.04
      + Number(v.egress || 0) * 0.04;
    if (String(v.heatwave || "no") === "yes") {
      cost += Number(v.hwcapacity || 0) * 0.011 * hours + Number(v.hwstorage || 0) * 0.02;
    }
    return Math.round(cost * 100) / 100;
  }
  if (cid === "oic") {
    // Oracle Integration Cloud: auto-size message packs then × hours × per-edition rate.
    const oicRate = String(v.edition || "standard") === "enterprise" ? 1.2903 : 0.6452;
    const peak = Number(v.peakday || 0), monthly = Number(v.monthlymsgs || 0);
    let packs;
    if (peak > 0) packs = Math.ceil(peak / (24 * 5000));
    else if (monthly > 0) packs = Math.ceil(monthly / (hours * 5000));
    else packs = Number(v.packs || 0);
    return Math.round(packs * oicRate * hours * 100) / 100;
  }
  const fkey = entry.fields?.[0]?.key;
  let qty = fkey ? Number(v[fkey] || 0) : 0;
  if (fkey in free) qty = Math.max(0, qty - free[fkey]);
  const m = entry.basis === "hour" ? rate * qty * hours : rate * qty;
  return Math.round(m * 100) / 100;
}

// Re-price every already-added service when the hours setting changes, so the cart, the
// results total and the exports all stay on the same hours basis.
function repriceExtraServices() {
  (state.extraServices || []).forEach((s) => {
    s.monthly = clientLineCost(s, s.values || {});
  });
  renderServiceCart();
  renderServiceResults();
  refreshResultsTotals();
}

function updateCardCost(idx) {
  const entry = state.catalog.results[idx];
  if (!entry) return;
  const values = cardValues(idx);
  const cost = clientLineCost(entry, values);
  const el = els.serviceResults.querySelector(`[data-cost="${idx}"]`);
  if (el) el.textContent = `${formatCurrency(cost)}/mo`;
  if (entry.id === "fastconnect") {
    const selected = fastConnectSelection(entry, values);
    const meta = els.serviceResults.querySelector(`[data-service-meta="${idx}"]`);
    if (meta) {
      meta.textContent = `${entry.group} · ${selected.sku} · $${selected.rate.toLocaleString(undefined, {
        maximumFractionDigits: 4,
      })} / port hour`;
    }
  }
}

function renderServiceCart() {
  if (!els.serviceCartList) return;
  const items = state.extraServices || [];
  els.serviceCartCount.textContent = String(items.length);
  els.serviceCartTotal.textContent = formatCurrency(extraServicesMonthly());
  els.serviceCartReview?.classList.toggle("is-empty", items.length === 0);
  if (!items.length) {
    els.serviceCartList.innerHTML = `<p class="service-empty">No services added yet. Use the catalog above to build this part of the BOM.</p>`;
    return;
  }
  els.serviceCartList.innerHTML = items
    .map((s, i) => {
      const sizing = Object.entries(s.values || {})
        .map(([k, val]) => `${val} ${k}`)
        .join(" · ");
      return `
        <div class="cart-item">
          <div class="cart-item-main">
            <strong>${escapeHtml(s.name)}</strong>
            <span>${escapeHtml(sizing)}</span>
          </div>
          <span class="cart-item-cost">${formatCurrency(s.monthly)}/mo</span>
          <button type="button" class="cart-item-remove" data-remove="${i}" aria-label="Remove">✕</button>
        </div>`;
    })
    .join("");
}

// "Adding…" feedback on an Add-to-BOM button. The work itself is instant, so without a floor
// the spinner would flash for a frame and read as a glitch rather than a confirmation. The
// progress track fills left-to-right exactly ONCE and this hold matches its duration, so the bar
// reaches 100% - signalling done - before the button resolves. Look lives in .btn-track.
const ADDING_MIN_MS = 900;
function showAddingState(btn, work) {
  if (!btn || btn.dataset.adding === "1") return;   // ignore double-clicks mid-add
  const original = btn.innerHTML;
  const width = btn.offsetWidth;                    // pin the box so the card doesn't jump
  const height = btn.offsetHeight;
  btn.dataset.adding = "1";
  btn.disabled = true;
  btn.style.minWidth = `${width}px`;
  btn.style.minHeight = `${height}px`;
  btn.innerHTML = '<span class="btn-adding">Adding…<span class="btn-track" aria-hidden="true"><i></i></span></span>';
  const startedAt = Date.now();
  const finish = () => {
    const wait = Math.max(0, ADDING_MIN_MS - (Date.now() - startedAt));
    setTimeout(() => {
      btn.innerHTML = original;
      btn.disabled = false;
      btn.style.minWidth = "";
      delete btn.dataset.adding;
    }, wait);
  };
  // Let the browser paint the spinner before the (synchronous) add runs.
  window.requestAnimationFrame(() => {
    window.requestAnimationFrame(() => {
      try {
        Promise.resolve(work()).finally(finish);
      } catch (err) {
        finish();
        throw err;
      }
    });
  });
}

function addServiceFromCard(idx) {
  const entry = state.catalog.results[idx];
  if (!entry) return;
  const values = cardValues(idx);
  const monthly = clientLineCost(entry, values);
  const fastConnect = entry.id === "fastconnect" ? fastConnectSelection(entry, values) : null;
  // Variant cards (Generative AI) carry the chosen meter's name, SKU, rate and unit onto the
  // cart line, so the deliverable reads "Generative AI - Meta Llama 4 Scout", not the card name.
  const vKey = entry.variantRates ? String(values[entry.variantKey] || "") : "";
  const variant = vKey && entry.variantRates[vKey] !== undefined
    ? { label: entry.variantLabels[vKey], sku: entry.variantSkus[vKey] || "",
        rate: entry.variantRates[vKey], unit: entry.variantUnits[vKey] || entry.unit }
    : null;
  state.extraServices.push({
    catalogId: entry.id,
    name: fastConnect
      ? `FastConnect port (${fastConnect.label})`
      : variant ? `${entry.name.split(" - ")[0]} - ${variant.label}` : entry.name,
    group: entry.group,
    sku: fastConnect?.sku || variant?.sku || entry.sku,
    unit: variant?.unit || entry.unit,
    basis: entry.basis,
    rate: fastConnect?.rate ?? variant?.rate ?? entry.rate,
    free: entry.free || {},
    fields: entry.fields,
    speedRates: entry.speedRates,
    speedSkus: entry.speedSkus,
    speedLabels: entry.speedLabels,
    variantKey: entry.variantKey,
    variantField: entry.variantField,
    variantRates: entry.variantRates,
    variantSkus: entry.variantSkus,
    variantLabels: entry.variantLabels,
    variantUnits: entry.variantUnits,
    variantHourly: entry.variantHourly,
    variantFree: entry.variantFree,
    thirdParty: !!entry.thirdParty || entry.group === "Licensing",
    values,
    monthly,
  });
  renderServiceCart();
  refreshResultsTotals();
  els.engineStatus.textContent = `Added ${entry.name} (${formatCurrency(monthly)}/mo) to the BOM.`;
}

// Re-render the KPI tiles + subtitle so an added service shows up in the total immediately,
// without a full server reprice.
function refreshResultsTotals() {
  if (state.pricing) renderResults(state.pricing);
}

// Add-OCI-services toggle is wired via a delegated document listener (see top of file)
// so it keeps working even if the results DOM is re-rendered.
if (els.serviceChips) {
  els.serviceChips.addEventListener("click", (e) => {
    const btn = e.target.closest(".service-chip");
    if (!btn) return;
    state.catalog.group = btn.dataset.group || "";
    state.catalog.groupsOpen = {};   // reset accordions to defaults for the new view
    fetchCatalog();
  });
}
if (els.serviceSearch) {
  let t = null;
  els.serviceSearch.addEventListener("input", (e) => {
    state.catalog.query = e.target.value.trim();
    clearTimeout(t);
    t = setTimeout(fetchCatalog, 200);
  });
}
if (els.serviceResults) {
  els.serviceResults.addEventListener("input", (e) => {
    if (e.target.classList.contains("svc-input")) {
      const card = e.target.closest(".service-card");
      // A dropdown change can show/hide dependent fields (e.g. Serverless vs Dedicated).
      if (e.target.tagName === "SELECT") applyCardFieldVisibility(card);
      // GenAI Models: provider drives the Model list; provider/model drive the length-unit labels.
      const idx = Number(e.target.dataset.idx);
      const entry = state.catalog.results?.[idx];
      if (entry && entry.id === "genai" && e.target.tagName === "SELECT") {
        if (entry.basedbShapesByProcessor
            && ["processor", "shape", "edition_ocpu", "metric"].includes(e.target.dataset.key)) {
          refreshBasedbDependents(card, entry);
        }
        if (e.target.dataset.key === "provider") refreshGenaiModelOptions(card, entry);
        if (e.target.dataset.key === "provider" || e.target.dataset.key === "model") refreshGenaiLenLabels(card, entry);
      }
      updateCardCost(idx);
    }
  });
  els.serviceResults.addEventListener("click", (e) => {
    const add = e.target.closest(".svc-add");
    if (add) {
      showAddingState(add, () => addServiceFromCard(Number(add.dataset.idx)));
      return;
    }
    const head = e.target.closest(".service-group-head");
    if (head) {
      const g = head.dataset.groupToggle;
      const section = head.closest(".service-group");
      const body = section.querySelector(".service-group-body");
      const open = section.classList.toggle("is-open");
      head.setAttribute("aria-expanded", String(open));
      if (open) body.removeAttribute("hidden");
      else body.setAttribute("hidden", "");
      state.catalog.groupsOpen[g] = open;   // remember so re-render keeps your choice
    }
  });
}
if (els.serviceCartList) {
  els.serviceCartList.addEventListener("click", (e) => {
    const rm = e.target.closest(".cart-item-remove");
    if (!rm) return;
    state.extraServices.splice(Number(rm.dataset.remove), 1);
    renderServiceCart();
    refreshResultsTotals();
  });
}

fetch("/api/health")
  .then((response) => response.json())
  .then((payload) => {
    state.rateCards = payload.rateCards || [];
    state.bareMetalShapes = payload.bareMetalShapes || {};
    state.fullServiceCatalog = payload.fullServiceCatalog || [];
    state.openaiApiEnabled = Boolean(payload.openaiApiEnabled);
    state.openaiApiConfigured = Boolean(payload.openaiApiConfigured);
    state.openaiApiConnected = Boolean(payload.openaiApiConnected);
    state.openaiModel = payload.openaiModel || "";
    state.selectedShape = payload.selectedShape?.key || state.selectedShape;
    state.rateCard = selectedShape().rateCard || payload.rateCard || [];
    syncVendorForSelectedShape();
    syncApiUi();
    renderRateCard();
    renderProcessorPicker();
    renderShapeChoices();
    renderShapeDetail();
  })
  .catch(() => {
    els.engineStatus.textContent = "Backend unavailable";
  });
