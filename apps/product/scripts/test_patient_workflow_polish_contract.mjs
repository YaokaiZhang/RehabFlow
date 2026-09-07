import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

const productRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const page = (relativePath) => {
  const pagePath = resolve(productRoot, relativePath);
  assert(existsSync(pagePath), `${relativePath} should exist`);
  return readFileSync(pagePath, "utf8");
};

const episode = page("app/episodes/[episode_id]/page.tsx");
const triage = page("app/episodes/[episode_id]/triage/page.tsx");
const professionalCare = page("app/episodes/[episode_id]/professional-care/page.tsx");
const history = page("app/episodes/[episode_id]/history/page.tsx");
const login = page("app/login/page.tsx");
const register = page("app/register/page.tsx");
const shell = page("components/ProductShell.tsx");

for (const route of ["/triage", "/rehab", "/professional-care", "/history"]) {
  assert(episode.includes(route), `episode overview should retain its ${route} action`);
}
assert(triage.includes("Continue AI Triage Chat"), "triage should retain the chat continuation action");
assert(triage.includes("Episode Overview"), "triage should retain the episode return action");
assert(professionalCare.includes("Episode Overview"), "Professional Care should retain the episode return action");
assert(professionalCare.includes("Triage Summary"), "Professional Care should retain the triage return action");
assert(history.includes("Episode Overview"), "episode history should retain the episode return action");

for (const [name, source] of Object.entries({ episode, triage, professionalCare, history })) {
  assert(source.includes("StatusBadge"), `${name} workflow should use the shared StatusBadge primitive`);
}
for (const [name, source] of Object.entries({ login, register })) {
  assert(source.includes("AppButton"), `${name} workflow should use the shared AppButton primitive`);
}

for (const [name, source] of Object.entries({ login, register })) {
  assert(source.includes("safeNextPath"), `${name} should validate its next redirect`);
  assert(source.includes("safeInitialRole"), `${name} should accept a safe role query`);
  assert(source.includes("doctorRedirectPath(nextPath)"), `${name} should preserve the doctor return path`);
  assert(source.includes('nextPath || "/episodes"'), `${name} should preserve the patient return path`);
}
assert(login.includes('const LOGIN_ROLE_KEY = "rehab_login_role"'), "login should preserve sticky role state");
assert(register.includes('const LOGIN_ROLE_KEY = "rehab_login_role"'), "registration should preserve sticky role state");

assert(!shell.includes("Patient Memory"), "top-level navigation should not reintroduce Patient Memory");
assert(professionalCare.includes("Optional Live Movement Monitoring"), "Professional Care should use the current monitoring term");
assert(!professionalCare.includes("Raw movement score trend"), "Professional Care should not retain legacy score-trend copy");
assert(!professionalCare.includes("episode.selected_doctor_id ||"), "Professional Care should not expose a selected doctor identifier");
assert(!professionalCare.includes("request.doctor_name || request.doctor_id"), "Professional Care should not expose doctor identifiers as names");
assert(!episode.includes("professionalCare.selected_doctor_id ||"), "episode overview should not expose a selected doctor identifier");

const templateMark = String.fromCharCode(96);
const episodeIdExpression = "$" + "{episode.care_episode_id}";
const exactEpisodeHref = (path) => "href={" + templateMark + "/episodes/" + episodeIdExpression + path + templateMark + "}";
const exactEpisodeQueryHref = "href={" + templateMark + "/?episode_id=" + episodeIdExpression + templateMark + "}";
for (const route of ["/triage", "/rehab", "/professional-care", "/history"]) {
  assert(episode.includes(exactEpisodeHref(route)), "episode overview should retain its exact " + route + " action");
}
assert(episode.includes('href="/episodes"'), "episode overview should retain the all-episodes route");
assert(triage.includes(exactEpisodeQueryHref), "triage should return to the exact episode chat route");
assert(triage.includes(exactEpisodeHref("")), "triage should return to the exact episode overview route");
assert(professionalCare.includes(exactEpisodeHref("")), "Professional Care should return to the exact episode overview route");
assert(professionalCare.includes(exactEpisodeHref("/triage")), "Professional Care should return to the exact triage route");
assert(history.includes(exactEpisodeHref("")), "episode history should return to the exact episode overview route");
assert(history.includes('href="/episodes"'), "episode history should retain the all-episodes route");

const patientWorkflowPages = { episode, triage, professionalCare, history };
for (const [name, source] of Object.entries(patientWorkflowPages)) {
  assert(source.includes("StatusBadge"), name + " workflow should use the shared StatusBadge primitive");
  assert(!source.includes("function StatusBadge"), name + " workflow should not define a local StatusBadge primitive");
  assert(!source.includes("function AppButton"), name + " workflow should not define a local AppButton primitive");
  assert(!source.includes("fetch("), name + " workflow should keep fetching behind the existing API or workspace boundary");
}
assert(episode.includes("getCareEpisodeWorkspaceSummary"), "episode overview should use the shared workspace-summary API boundary");
assert(triage.includes("getCareEpisode"), "triage should use the shared episode API boundary");
assert(history.includes("getCareEpisodeHistory"), "episode history should use the shared history API boundary");
assert(professionalCare.includes("usePatientProfessionalCareWorkspace"), "Professional Care should use the existing workspace hook boundary");

for (const [name, source] of Object.entries({ login, register })) {
  assert(source.includes('const requestedRole = safeInitialRole(params.get("role"));'), name + " should accept an explicit role query");
  assert(source.includes('setNextPath(safeNextPath(params.get("next")));'), name + " should validate and store its next redirect");
  assert(source.includes('return nextPath.startsWith("/doctor") ? nextPath : "/doctor";'), name + " should keep doctors on the Care Worklist route");
  assert(source.includes('router.push(auth.role === "doctor" ? doctorRedirectPath(nextPath) : nextPath || "/episodes");'), name + " should preserve role-specific next redirect semantics");
}
assert(login.includes('const [role, setRole] = useState<LoginRole>(() => readStoredRole());'), "login should initialize from sticky role state");
assert(register.includes('const [role, setRole] = useState<LoginRole>("patient");'), "registration should default to patient regardless of sticky login role");
assert(!register.includes("readStoredRole"), "registration should not initialize from sticky login role state");
assert(register.includes("setRole(requestedRole);"), "registration should allow an explicit role query to override its patient default");
assert(register.includes("window.sessionStorage.setItem(LOGIN_ROLE_KEY, requestedRole);"), "registration should persist an explicit role query");

assert(professionalCare.includes('const selectedDoctorLabel = selectedDoctorRequest?.doctor_name || selectedDoctor?.display_name || (episode.selected_doctor_id ? "Selected doctor" : "No doctor selected yet");'), "Professional Care should use a generic fallback when selected doctor display data is missing");
assert(professionalCare.includes("{selectedDoctorLabel}"), "Professional Care should render the selected doctor label");
assert(episode.includes('const selectedDoctorLabel = professionalCare.selected_doctor_name || (professionalCare.selected_doctor_id ? "Selected doctor" : "None selected");'), "episode overview should use a generic fallback when selected doctor display data is missing");
assert(episode.includes("{selectedDoctorLabel}"), "episode overview should render the selected doctor label");

const rawDoctorIdDisplayPatterns = [
  /\b(?:doctor_name|display_name|selected_doctor_name)\s*\|\|\s*(?:[A-Za-z_$][\w$]*\.)?(?:doctor_id|selected_doctor_id)\b/,
  /<[^>]+>\s*\{\s*(?:[A-Za-z_$][\w$]*\.)?(?:doctor_id|selected_doctor_id)\s*\}\s*<\/[^>]+>/,
  /\b(?:selectedDoctorRequest|selectedDoctor|professionalCare)\??\.(?:doctor_id|selected_doctor_id)\s*\|\|/,
];
for (const [name, source] of Object.entries({ episode, professionalCare })) {
  for (const pattern of rawDoctorIdDisplayPatterns) {
    assert(!pattern.test(source), name + " should never use a raw doctor ID as visible fallback data");
  }
}

assert(history.includes('function safetyCopy(status: CareEpisode["safety_gate_status"])'), "history should map safety statuses to readable copy");
assert(history.includes('if (status === "needs_triage") return "Needs AI Triage Intake before AI Daily Rehab";'), "history should use attention copy for episodes that need triage");
assert(history.includes('if (status === "clinician_reviewed") return "Clinician-reviewed context available";'), "history should use readable clinician-reviewed copy");
assert(history.includes('function safetyTone(status: CareEpisode["safety_gate_status"])'), "history should map safety statuses to shared badge tones");
assert(history.includes('if (status === "needs_triage") return "attention";'), "history should use attention tone for episodes that need triage");
assert(history.includes('return "success";'), "history should use success tone for completed or cleared safety context");
assert(history.includes("tone={safetyTone(episode.safety_gate_status)}"), "history should apply the status-specific safety tone");
assert(history.includes("{safetyCopy(episode.safety_gate_status)}"), "history should render readable status copy");
assert(!history.includes("{episode.safety_gate_status}"), "history should not render raw safety enum values");

console.log("patient workflow polish contract ok");
