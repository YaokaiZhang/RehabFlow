import { readFileSync } from "node:fs";
import { join } from "node:path";

const root = process.cwd();
const page = readFileSync(join(root, "apps/product/app/episodes/[episode_id]/page.tsx"), "utf8");
const api = readFileSync(join(root, "packages/shared/src/api.ts"), "utf8");
const failures = [];

function expect(condition, message) {
  if (!condition) failures.push(message);
}

expect(api.includes("CareEpisodeWorkspaceSummary"), "Shared API should define CareEpisodeWorkspaceSummary.");
expect(api.includes("getCareEpisodeWorkspaceSummary"), "Shared API should expose getCareEpisodeWorkspaceSummary.");
expect(page.includes("getCareEpisodeWorkspaceSummary"), "Episode overview should load the workspace summary snapshot.");
expect(page.includes("Episode Summary"), "Episode workspace should render Episode Summary.");
expect(page.includes("Latest Triage Summary"), "Episode workspace should render Latest Triage Summary.");
expect(page.includes("Latest Rehab Summary"), "Episode workspace should render Latest Rehab Summary.");
expect(page.includes("Professional Care Summary"), "Episode workspace should render Professional Care Summary.");
expect(page.includes("Complete triage history"), "Episode workspace should label the full transcript as Complete triage history.");
expect(page.includes("episodeSafetySummary"), "Episode summary should derive Safety from triage safety signals, not only workflow gate status.");
expect(page.includes("AI Triage Intake"), "Episode workspace should keep AI Triage Intake action on the page.");
expect(page.includes("AI Daily Rehab"), "Episode workspace should keep AI Daily Rehab action on the page.");
expect(page.includes("Professional Care"), "Episode workspace should keep Professional Care action on the page.");
expect(page.includes("Open Optional Live Monitor"), "Episode workspace should expose optional live monitor only from summary state.");
expect(!page.includes("getCareEpisode,"), "Episode overview should not depend on the old single episode-only fetch.");

if (failures.length) {
  console.error(failures.map((failure) => "- " + failure).join("\n"));
  process.exit(1);
}
console.log("episode workspace page contract ok");
