import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const episodePage = readFileSync(resolve("app/episodes/[episode_id]/page.tsx"), "utf8");
const historyPage = readFileSync(resolve("app/episodes/[episode_id]/history/page.tsx"), "utf8");
const sharedApi = readFileSync(resolve("../../packages/shared/src/api.ts"), "utf8");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

assert(!episodePage.includes("Complete triage history"), "episode workspace must not label relevant_context as complete history");
assert(episodePage.includes("Triage context summary"), "episode workspace should label relevant_context as context summary");
assert(episodePage.includes("/history"), "episode workspace should link to the full history page");
assert(sharedApi.includes("source_ai_session_id"), "shared triage types should expose source_ai_session_id");
assert(sharedApi.includes("source_conversation_transcript"), "shared triage types should expose source_conversation_transcript");
assert(sharedApi.includes("export type CareEpisodeHistory"), "shared API should export CareEpisodeHistory");
assert(sharedApi.includes("getCareEpisodeHistory"), "shared API should export getCareEpisodeHistory");
assert(historyPage.includes("getCareEpisodeHistory"), "history page should load the composed episode history API");
assert(historyPage.includes("source_conversation_transcript"), "history page should render source transcript snapshots");
assert(historyPage.includes("No source transcript snapshot saved"), "history page should handle older summaries without source transcript snapshots");
assert(historyPage.includes("Patient Memory"), "history page should render Patient Memory");
assert(historyPage.includes("Episode Memory"), "history page should render Episode Memory");

console.log("episode history page contract ok");
