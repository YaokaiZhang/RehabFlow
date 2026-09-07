import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const productRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(productRoot, "../..");
const pagePath = resolve(productRoot, "app/doctor/dashboard/page.tsx");
const hookPath = resolve(productRoot, "app/doctor/dashboard/useDoctorDashboard.ts");
const sharedApiPath = resolve(repoRoot, "packages/shared/src/api.ts");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function extractFunctionSource(source, functionName) {
  const start = source.indexOf(`function ${functionName}(`);
  assert(start >= 0, `${functionName} should exist`);
  const openBrace = source.indexOf("{", start);
  assert(openBrace >= 0, `${functionName} should have a body`);
  let depth = 0;
  let quote = null;
  let escaped = false;
  for (let index = openBrace; index < source.length; index += 1) {
    const character = source[index];
    if (quote) {
      if (escaped) {
        escaped = false;
      } else if (character === "\\") {
        escaped = true;
      } else if (character === quote) {
        quote = null;
      }
      continue;
    }
    if (character === '"' || character === "'" || character === "`") {
      quote = character;
    } else if (character === "{") {
      depth += 1;
    } else if (character === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  throw new Error(`${functionName} body is not closed`);
}

function loadPageFunction(source, functionName) {
  const declaration = extractFunctionSource(source, functionName).replace(
    new RegExp(`function ${functionName}\\(status: [^)]+\\)`),
    `function ${functionName}(status)`,
  );
  return new Function(`${declaration}\nreturn ${functionName};`)();
}

function assertExactMappings(source, functionName, mappings, label) {
  const mapper = loadPageFunction(source, functionName);
  for (const [input, expected] of Object.entries(mappings)) {
    assert(mapper(input) === expected, `${label} should map ${input} to ${expected}`);
  }
}

function assertSafetyGateSourceIsMapped(source, sourceExpression, label) {
  const references = source.split(sourceExpression).length - 1;
  assert(references === 1, label + " should reference " + sourceExpression + " only through safetyCopy");
  assert(
    source.includes("{safetyCopy(" + sourceExpression + ")}"),
    label + " should allowlist the safetyCopy mapper call for " + sourceExpression,
  );
}

assert(existsSync(pagePath), "Doctor Dashboard page should exist");
assert(existsSync(hookPath), "Doctor Dashboard hook should exist");

const page = readFileSync(pagePath, "utf8");
const hook = readFileSync(hookPath, "utf8");
const sharedApi = readFileSync(sharedApiPath, "utf8");
const dashboardSource = `${page}\n${hook}`;

assert(sharedApi.includes("export type CareAppointment"), "shared API should export CareAppointment");
assert(sharedApi.includes("export type AttentionMapBucket"), "shared API should export AttentionMapBucket");
assert(sharedApi.includes("export type DoctorIntelligenceArtifact"), "shared API should export DoctorIntelligenceArtifact");
assert(sharedApi.includes("export type DoctorDashboard"), "shared API should export DoctorDashboard");
assert(sharedApi.includes("export type DoctorIntelligenceArtifactInput"), "shared API should export DoctorIntelligenceArtifactInput");
assert(sharedApi.includes("getDoctorDashboard"), "shared API should expose getDoctorDashboard");
assert(sharedApi.includes("refreshPatientPanelBriefing"), "shared API should expose refreshPatientPanelBriefing");
assert(sharedApi.includes("createDoctorIntelligenceArtifact"), "shared API should expose createDoctorIntelligenceArtifact");
assert(sharedApi.includes('"/doctor/dashboard"'), "shared API should call doctor dashboard endpoint");
assert(sharedApi.includes('"/doctor/dashboard/briefing/refresh"'), "shared API should call briefing refresh endpoint");
assert(sharedApi.includes('"/doctor/dashboard/artifacts"'), "shared API should call doctor artifacts endpoint");

assert(hook.includes("export function useDoctorDashboard"), "hook should export useDoctorDashboard");
assert(hook.includes("getDoctorDashboard"), "hook should load dashboard through shared API");
assert(hook.includes("refreshPatientPanelBriefing"), "hook should refresh Patient Panel Briefing through shared API");
assert(hook.includes("createDoctorIntelligenceArtifact"), "hook should save Doctor Intelligence Artifacts through shared API");
assert(hook.includes("loadAuth"), "hook should use auth state");
assert(hook.includes('role !== "doctor"'), "hook should be doctor-only");
assert(hook.includes("loading"), "hook should expose loading state");
assert(hook.includes("error"), "hook should expose error state");
assert(hook.includes("statusMessage"), "hook should expose status state");
assert(hook.includes("busyAction"), "hook should expose action-specific busy state");
assert(hook.includes("AuthState | null | undefined"), "hook auth state should represent unresolved hydration");
assert(hook.includes("useState<AuthState | null | undefined>(undefined)"), "hook should initialize auth as unresolved");
assert(hook.includes("if (!auth || loading || busyAction) return false;"), "refresh should be guarded while initial loading is in flight or another action is busy");
assert(hook.includes('setBusyAction("refresh")'), "refresh should mark only refresh as busy");
assert(hook.includes('setBusyAction("save")'), "save should mark only save as busy");
assert(!hook.includes("setBusy(true)"), "hook should not use one shared busy flag for refresh and save");

assert(page.includes("useDoctorDashboard"), "page should use the Doctor Dashboard hook");
assert(page.includes("Doctor Dashboard"), "page should render Doctor Dashboard heading");
assert(page.includes("Patient Panel Briefing"), "page should render Patient Panel Briefing");
assert(page.includes("Attention Map"), "page should render Attention Map");
assert(page.includes("Doctor Intelligence Artifact"), "page should render Doctor Intelligence Artifact controls");
assert(page.includes("Care Worklist"), "page should guide doctors to the Care Worklist");
assert(page.includes('href="/doctor"') || page.includes('href={dashboard?.care_worklist_href || "/doctor"}'), "page should link to /doctor for Care Worklist actions");
assert(page.includes("upcoming_appointments"), "page should render upcoming appointments");
assert(page.includes("care_relationships"), "page should render relationship intelligence summaries");
assert(page.includes("function statusCopy(status: string)"), "Doctor Dashboard should map appointment statuses to readable copy");
assert(page.includes("{statusCopy(appointment.status)}"), "Doctor Dashboard should render readable appointment status copy");
assert(page.includes("function safetyCopy(status: string | null | undefined)"), "Doctor Dashboard should map safety gate values to readable copy");
assert(page.includes("{safetyCopy(relationship.safety_gate_status)}"), "Doctor Dashboard should render readable safety gate copy");
assertExactMappings(page, "statusCopy", {
  scheduled: "Scheduled",
  confirmed: "Confirmed",
}, "Doctor Dashboard appointment status copy");
assertExactMappings(page, "safetyCopy", {
  needs_triage: "Needs AI Triage Intake before AI Daily Rehab",
  triage_complete: "AI triage context available",
  clinician_reviewed: "Clinician-reviewed context available",
}, "Doctor Dashboard safety gate copy");
assert(
  loadPageFunction(page, "safetyCopy")(null) === "Not recorded",
  "Doctor Dashboard should use a readable fallback when safety gate data is missing",
);
assert(
  loadPageFunction(page, "safetyCopy")("unsupported_status") === "Not recorded",
  "Doctor Dashboard should keep unsupported safety values neutral instead of treating them as affirmative",
);
assert(!page.includes("{appointment.status}"), "Doctor Dashboard should not render raw appointment status enums");
assertSafetyGateSourceIsMapped(page, "relationship.safety_gate_status", "Doctor Dashboard");
assert(page.includes("<textarea"), "page should provide an artifact textarea");
assert(page.includes("auth === undefined"), "page should keep dashboard in loading state while auth hydrates");
assert(page.includes('busyAction === \"refresh\"'), "page refresh button copy should depend on refresh action only");
assert(page.includes('busyAction === \"save\"'), "page save button copy should depend on save action only");
assert(page.includes("disabled={loading || busyAction !== null}"), "refresh button should be disabled while initial loading or any action is busy");
assert(page.includes("disabled={busyAction !== null || !artifactDraft.title.trim() || !artifactDraft.content.trim()}"), "save button should be disabled by save/refresh busy state and valid draft state");
assert(!page.includes("disabled={busy}"), "page should not disable buttons through one shared busy flag");

const forbidden = [
  "acceptCareConnectionRequest",
  "rejectCareConnectionRequest",
  "createCareConversationMessage",
  "selectCareRelationshipDoctor",
  "generateAICareSummary",
];
for (const token of forbidden) {
  assert(!dashboardSource.includes(token), `Doctor Dashboard must not include or import ${token}`);
}

console.log("doctor dashboard page contract ok");
