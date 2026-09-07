import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const productRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const pagePath = resolve(productRoot, "app/episodes/page.tsx");
const uiPath = resolve(productRoot, "components/ui.tsx");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

assert(existsSync(pagePath), "/episodes/page.tsx should exist");
assert(existsSync(uiPath), "shared UI primitives file should exist");

const page = readFileSync(pagePath, "utf8");
const ui = readFileSync(uiPath, "utf8");

assert(page.includes("use client"), "/episodes should preserve the existing client-side auth/data flow");
assert(page.includes("loadAuth"), "/episodes should preserve existing auth flow");
assert(page.includes("listCareEpisodes"), "/episodes should keep existing episode fetch behavior");
assert(page.includes("deleteCareEpisode"), "/episodes should keep existing episode delete behavior");

assert(page.includes("Patient Dashboard"), "/episodes should present as Patient Dashboard");
assert(page.includes("Care Episode List"), "Patient Dashboard should include Care Episode List as a section");
assert(page.indexOf("Patient Dashboard") < page.indexOf("Care Episode List"), "Care Episode List should sit inside the Patient Dashboard, not replace it");

for (const primitive of ["DashboardCard", "SectionHeader", "StatusBadge"]) {
  assert(page.includes(primitive), `/episodes should use shared dashboard primitive ${primitive}`);
  assert(ui.includes(`export function ${primitive}`) || ui.includes(`export const ${primitive}`), `ui.tsx should export ${primitive}`);
}

const linkElements = [...page.matchAll(/<Link\b[\s\S]*?<\/Link>/g)].map((match) => match[0]);
function includesCreationLink(destination, copy) {
  return linkElements.some((link) => link.includes(destination) && link.includes(copy));
}

assert(
  includesCreationLink('href="/"', "Create Episode With AI Triage"),
  "Patient Dashboard should retain the AI Triage episode creation path",
);
assert(
  includesCreationLink('href="/episodes/new"', "New Manual Episode") || includesCreationLink('href="/episodes/new"', "Create Manual Episode"),
  "Patient Dashboard should retain the Manual Episode creation path",
);

const metricsStart = page.indexOf('aria-label="Patient dashboard status"');
const metricsEnd = page.indexOf('<section className="space-y-4" aria-label="Care Episode List">', metricsStart);
assert(metricsStart >= 0 && metricsEnd > metricsStart, "Patient Dashboard should retain a bounded metrics section");
const metricsSection = page.slice(metricsStart, metricsEnd);
assert(page.includes("episodesLoaded"), "Dashboard metrics should track whether episode data loaded successfully");
assert(
  /episodesLoaded[\s\S]{0,240}(?:Loading|Unknown|Unavailable)/.test(page),
  "Dashboard metrics should present a loading or unknown state before a successful episode fetch",
);
assert(
  (metricsSection.match(/metricValue\(/g) || []).length === 3,
  "Every dashboard metric should use the loaded-data metric display helper",
);

assert(page.includes("safetyCopy"), "Episode cards should keep compact safety/status hints");
assert(page.includes("episode.status") || page.includes("statusCopy"), "Episode cards should expose compact episode status");
assert(page.includes("updated_at") || page.includes("Latest activity"), "Episode cards should show latest activity when available");
assert(page.includes("created_at") || page.includes("Created"), "Episode cards should show created date context when available");
assert(page.includes("body_area"), "Episode cards should show body area context");
const episodeCardsStart = page.indexOf("{episodes.map((episode) => (");
assert(episodeCardsStart >= 0, "Patient Dashboard should render Care Episode cards from the fetched episode list");
const episodeCards = page.slice(episodeCardsStart);
for (const segment of ["/triage", "/rehab", "/professional-care", "/history"]) {
  assert(!episodeCards.includes(segment), `Patient Dashboard cards should not link directly to deep episode action ${segment}`);
}
const cardActions = [...episodeCards.matchAll(/<(Link|AppButton)\b[\s\S]*?<\/\1>/g)].map((match) => match[0]);
assert(cardActions.length === 2, "Care Episode cards should expose only their intended dashboard-level actions");
assert(cardActions.some((action) => action.includes("Open") && action.includes("episode.care_episode_id")), "Episode cards should provide a clear Open action to the episode dashboard");
assert(cardActions.some((action) => action.includes("Delete") && action.includes("deleteEpisode")), "Episode cards should retain their existing delete action");

for (const forbiddenCopy of [
  "Start Session",
  "Generate Session Summary",
  "Session notes",
  "Mark checklist",
  "AI Daily Rehab List",
]) {
  assert(!page.includes(forbiddenCopy), `/episodes should not add exercise/session controls: ${forbiddenCopy}`);
}

console.log("patient dashboard contract ok");
