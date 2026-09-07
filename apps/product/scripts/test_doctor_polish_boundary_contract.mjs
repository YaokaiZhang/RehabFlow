import { existsSync, readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const productRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function read(relativePath) {
  const absolutePath = resolve(productRoot, relativePath);
  assert(existsSync(absolutePath), `expected ${relativePath} to exist`);
  return readFileSync(absolutePath, "utf8");
}

const worklist = read("app/doctor/page.tsx");
const dashboard = read("app/doctor/dashboard/page.tsx");
const liveMonitor = read("app/episodes/[episode_id]/professional-care/live-monitor/page.tsx");
const shell = read("components/ProductShell.tsx");

assert(worklist.includes("useDoctorCareWorklist"), "Care Worklist should keep its existing worklist hook");
assert(worklist.includes("Care Worklist"), "doctor landing page should remain the Care Worklist");
assert(worklist.includes("worklist.incoming_requests"), "Care Worklist should keep incoming request actions");
assert(!worklist.includes("useDoctorDashboard"), "Care Worklist should stay distinct from Doctor Dashboard");
assert(!worklist.includes("Patient Panel Briefing"), "Care Worklist should not absorb Doctor Dashboard briefing copy");
assert(worklist.includes('href="/doctor"'), "Care Worklist should retain its primary route");
for (const action of ['respond(request, "accept")', 'respond(request, "reject")', 'sendMessage(selectedRelationship.care_episode_id)', 'summarize(selectedRelationship.care_episode_id)']) {
  assert(worklist.includes(action), `Care Worklist should retain primary action: ${action}`);
}
assert(worklist.includes("/professional-care/live-monitor"), "Care Worklist should retain the episode live-monitor route");

assert(dashboard.includes("useDoctorDashboard"), "Doctor Dashboard should keep its existing dashboard hook");
assert(dashboard.includes("Doctor Dashboard"), "Doctor Dashboard heading should remain present");
assert(dashboard.includes("Patient Panel Briefing"), "Doctor Dashboard should remain briefing-focused");
assert(dashboard.includes("Attention Map"), "Doctor Dashboard should remain attention-map focused");
assert(dashboard.includes("care_relationships"), "Doctor Dashboard should remain cross-episode");
assert(dashboard.includes("upcoming_appointments"), "Doctor Dashboard should retain cross-episode appointments");
assert(dashboard.includes("/doctor"), "Doctor Dashboard should retain its Care Worklist route action");
assert(dashboard.includes("Refresh Briefing"), "Doctor Dashboard should retain briefing refresh");
assert(dashboard.includes("Save Doctor Intelligence Artifact"), "Doctor Dashboard should retain artifact save");
assert(!dashboard.includes("useDoctorCareWorklist"), "Doctor Dashboard should stay distinct from Care Worklist");
assert(!dashboard.includes("usePatientMemoryDocument"), "Doctor Dashboard should not become Patient Memory settings");
assert(!dashboard.includes("section=patient-memory"), "Doctor Dashboard should not become Settings");

assert(liveMonitor.includes("Optional Live Movement Monitoring"), "live monitor should use the current monitoring vocabulary");
assert(liveMonitor.includes("Professional Care"), "live monitor should remain inside Professional Care");
assert(liveMonitor.includes("connectDoctorEpisodeMonitor"), "live monitor should keep the existing episode websocket action");
assert(liveMonitor.includes("getCareEpisodeWorkspaceSummary"), "live monitor should keep the existing episode workspace load");
assert(liveMonitor.includes('href="/doctor"'), "live monitor should retain its Care Worklist return route");
assert(liveMonitor.includes("Connect to Optional Live Movement Monitoring"), "live monitor should retain its primary connect action");
for (const legacyCopy of ["Doctor Monitor Dashboard", "professional monitoring", "monitoring path", "Score Trend"]) {
  assert(!liveMonitor.includes(legacyCopy), `live monitor should not use legacy copy: ${legacyCopy}`);
}
assert(!existsSync(resolve(productRoot, "app/doctor/live-monitor/page.tsx")), "live monitor should remain episode-scoped instead of adding a patient-global doctor route");

assert(shell.includes('href="/settings"'), "doctor navigation should include Settings");
assert(shell.includes("Settings"), "doctor navigation should label Settings");
assert(!shell.includes('href="/memory"'), "doctor navigation should not link to Patient Memory");
assert(!shell.includes(">Patient Memory<"), "doctor navigation should not label Patient Memory");

for (const relativePath of [
  "components/ui.tsx",
  "app/doctor/page.tsx",
  "app/doctor/dashboard/page.tsx",
  "app/episodes/[episode_id]/professional-care/live-monitor/page.tsx",
]) {
  const source = read(relativePath);
  assert(source.includes("DashboardCard"), `${relativePath} should use the existing DashboardCard primitive`);
  assert(source.includes("SectionHeader"), `${relativePath} should use the existing SectionHeader primitive`);
  assert(source.includes("StatusBadge"), `${relativePath} should use the existing StatusBadge primitive`);
  assert(source.includes("AppButton"), `${relativePath} should use the existing AppButton primitive`);
}

for (const relativePath of [
  "app/doctor/page.tsx",
  "app/doctor/dashboard/page.tsx",
  "app/episodes/[episode_id]/professional-care/live-monitor/page.tsx",
]) {
  const source = read(relativePath);
  assert(!source.includes("useDoctorLiveMonitor"), `${relativePath} should not introduce a new live-monitor data-fetching abstraction`);
}

const executedContractNames = [
  "test_doctor_worklist_page_contract.mjs",
  "test_doctor_dashboard_page_contract.mjs",
  "test_doctor_triage_access_contract.mjs",
  "test_login_role_contract.mjs",
  "test_professional_care_live_monitor_contract.mjs",
];
assert(executedContractNames.includes("test_professional_care_live_monitor_contract.mjs"), "doctor polish boundary should execute the existing live-monitor contract");

for (const scriptName of executedContractNames) {
  const scriptPath = resolve(productRoot, "scripts", scriptName);
  assert(existsSync(scriptPath), `existing doctor access contract should remain available: ${scriptName}`);
  execFileSync(process.execPath, [scriptPath], { cwd: productRoot, stdio: "inherit" });
}

console.log("doctor polish boundary contract ok");
