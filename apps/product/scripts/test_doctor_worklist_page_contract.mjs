import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const productRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(productRoot, "../..");
const page = readFileSync(resolve(productRoot, "app/doctor/page.tsx"), "utf8");
const api = readFileSync(resolve(repoRoot, "packages/shared/src/api.ts"), "utf8");
const hook = readFileSync(resolve(productRoot, "app/doctor/useDoctorCareWorklist.ts"), "utf8");
const accordionPath = resolve(productRoot, "components/AccordionSection.tsx");
const accordion = existsSync(accordionPath) ? readFileSync(accordionPath, "utf8") : "";

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function directApiFunctionImportCount(source) {
  const match = source.match(/import\s*\{([\s\S]*?)\}\s*from\s*["']@rehab\/shared\/api["'];/);
  if (!match) return 0;
  return match[1]
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean)
    .filter((item) => !item.startsWith("type "))
    .length;
}

assert(page.includes("useDoctorCareWorklist"), "doctor page should import the worklist hook");
assert(directApiFunctionImportCount(page) <= 2, "doctor page should not orchestrate shared API calls directly");
assert(hook.includes("export function useDoctorCareWorklist"), "doctor hook should export useDoctorCareWorklist");
assert(hook.includes("getCareWorklist"), "doctor hook should load the Care Worklist");
assert(hook.includes("acceptCareConnectionRequest"), "doctor hook should own accept actions");
assert(hook.includes("rejectCareConnectionRequest"), "doctor hook should own reject actions");
assert(hook.includes("listCareConversationMessages"), "doctor hook should load Care Conversation messages");
assert(hook.includes("generateAICareSummary"), "doctor hook should own AI Care Summary loading");
assert(hook.includes(`(prev[careEpisodeId] || "") === "" ? content : prev[careEpisodeId]`), "doctor hook should not overwrite newer draft text on failed send");
assert(api.includes("accepted_requests: CareConnectionRequest[]"), "CareWorklist should expose accepted requests awaiting patient selection");
assert(hook.includes("accepted_requests: []"), "doctor hook should initialize accepted request state");
assert(page.includes("worklist.accepted_requests"), "doctor page should render accepted requests from the worklist");
assert(page.includes("Awaiting patient selection"), "accepted requests should be visibly distinct from active relationships");
assert(hook.includes("The patient can now select you for this episode"), "acceptance copy should explain the next patient action");
assert(hook.includes("selectedRelationship"), "doctor hook should expose selectedRelationship");
assert(hook.includes("setSelectedRelationshipId"), "doctor hook should expose setSelectedRelationshipId");
assert(hook.includes("workspaceByEpisode"), "doctor hook should expose workspace summaries by episode");
assert(hook.includes("getCareEpisodeWorkspaceSummary"), "doctor hook should load Care Episode workspace summaries");
assert(page.includes("selectedRelationship"), "doctor page should render one selected active relationship detail");
assert(page.includes("setSelectedRelationshipId"), "doctor page should let doctors select one active relationship");
assert(page.includes("AccordionSection"), "doctor page should use AccordionSection for selected relationship detail panels");
assert(accordion.includes("aria-expanded"), "AccordionSection should expose aria-expanded state");
assert(page.includes("CareEpisodeBrief"), "doctor detail should keep the Care Episode Brief visible");
assert(page.includes("Full Triage Summary"), "doctor detail should include Full Triage Summary accordion");
assert(page.includes("Rehab History"), "doctor detail should include Rehab History accordion");
assert(page.includes("Care Conversation"), "doctor detail should include Care Conversation accordion");
assert(page.includes("AI Care Summary"), "doctor detail should include AI Care Summary accordion");
assert(page.includes("Optional Live Movement Monitoring"), "doctor detail should include Optional Live Movement Monitoring accordion");
assert(page.includes("Optional Live Movement Monitoring stays secondary to the episode narrative."), "doctor summary copy should use the current movement-monitoring product language");
assert(!page.includes("Raw movement score trend"), "doctor summary copy should not expose the raw movement score trend label");
assert(!page.includes("worklist.active_relationships.map((relationship) => {"), "doctor page should not expand every active relationship into a full detail card");
assert(page.includes("relationship.issue_title || \"Care Episode\""), "active relationship cards should show the episode issue title");
assert(page.includes("displayPatientName(relationship.patient_name)"), "active relationship cards should use a human-readable patient label");
assert(!page.includes("relationship.patient_name || relationship.patient_id"), "active relationship cards should not expose raw patient IDs");
assert(page.includes("relationship.last_activity_label"), "active relationship cards should show last activity meaning");
assert(page.includes("new Date(relationship.last_activity_at).toLocaleString()"), "active relationship cards should show last activity time");
assert(page.includes("relationship.latest_triage_summary"), "active relationship cards should include latest triage summary when available");
assert(page.includes("/professional-care/live-monitor"), "active cards should link to the episode-scoped live monitor route");
assert(page.includes("Open Optional Live Movement Monitoring"), "active cards should use Optional Live Movement Monitoring language");
assert(!page.includes("<span className=\"font-semibold text-slate-950\">Episode:</span> {relationship.care_episode_id}"), "active cards should not lead with raw episode UUIDs");
assert(!page.includes("href={`/dashboard/monitor/${relationship.patient_id}`}"), "doctor page must not link to patient-global monitor route");

console.log("doctor worklist page contract ok");
