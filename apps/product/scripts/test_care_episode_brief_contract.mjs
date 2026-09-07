import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const productRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const component = readFileSync(resolve(productRoot, "components/CareEpisodeBrief.tsx"), "utf8");
const overviewPage = readFileSync(resolve(productRoot, "app/episodes/[episode_id]/page.tsx"), "utf8");

function assert(condition, message) {
	if (!condition) throw new Error(message);
}

assert(component.includes("Care Episode Brief"), "component should use Care Episode Brief language");
assert(component.includes("latest_triage_summary"), "brief should render triage summary context");
assert(component.includes("latest_rehab_session"), "brief should render rehab activity context");
assert(component.includes("professional_care"), "brief should render Professional Care status");
assert(component.includes("unresolved_questions"), "brief should include unresolved questions");
assert(component.includes("TriageContextSummary"), "brief should render triage context through an expandable summary component");
assert(!component.includes("episode.short_description"), "brief should not print the raw episode short description on the overview");
assert(overviewPage.includes("TriageContextSummary"), "episode overview hero should render the expandable triage context summary");
assert(!overviewPage.includes("{episode.short_description}"), "episode overview hero should not print raw episode short_description");
assert(overviewPage.includes("Full AI triage history"), "latest triage summary card should expose full AI triage history where the expandable block lives");
assert(overviewPage.includes("source_conversation_transcript"), "latest triage summary card should use the source conversation transcript for full history");
assert(overviewPage.includes("CareEpisodeBrief"), "episode overview should use shared CareEpisodeBrief");
assert(!overviewPage.includes("<h2 className=\"section-title\">Episode Summary</h2>"), "episode overview should not keep the old thin Episode Summary section");
assert(!overviewPage.includes("Safety signals:"), "patient episode overview should not expose internal safety signals");
assert(!overviewPage.includes(">Missing:</span>"), "patient episode overview should not expose missing-information internals");
assert(!overviewPage.includes(">Questions:</span>"), "patient episode overview should not expose unresolved questions");
assert(component.includes('role === "doctor" && !compact && triage'), "Care Episode Brief should keep unresolved questions limited to doctor review");

console.log("care episode brief contract ok");
