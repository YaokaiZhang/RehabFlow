import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const productRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const railPath = resolve(productRoot, "components/EpisodeRail.tsx");
const rail = readFileSync(railPath, "utf8");

function assert(condition, message) {
	if (!condition) throw new Error(message);
}

assert(rail.includes("listCareEpisodes"), "EpisodeRail should load Care Episodes");
assert(rail.includes("listAiChatSessions"), "EpisodeRail should load persisted AI triage chats");
assert(rail.includes("deleteAiChatSession"), "EpisodeRail should delete persisted AI triage chats");
assert(rail.includes("useSearchParams"), "EpisodeRail should read chat and episode query context");
assert(rail.includes("querySessionId"), "EpisodeRail should identify the active historical chat");
assert(rail.includes("Unassigned chats"), "EpisodeRail should expose chats that are not attached to an episode");
assert(rail.includes("Not attached to an episode"), "EpisodeRail should label chats without a Care Episode");
assert(rail.includes("episodeChatsForEpisode"), "EpisodeRail should scope episode chats to the expanded episode");
assert(rail.includes("session.care_episode_id === episode.care_episode_id"), "EpisodeRail should place episode chats under their owning episode");
assert(rail.includes("episode-chat-group-nested"), "EpisodeRail should render episode chats inside the episode session branch");
assert(rail.includes("No chats in this episode yet."), "EpisodeRail should provide an episode chat empty state");
assert(rail.includes('href={`/?episode_id=${episode.care_episode_id}`}'), "Episode chats should offer a new chat scoped to the episode");
assert(rail.includes("params.set(\"episode_id\", session.care_episode_id)"), "Episode chat links should keep the owning episode expanded");
assert(rail.includes("onConfirmDelete"), "EpisodeRail should require a deletion confirmation step");
assert(rail.includes("href={chatHref(session)}"), "EpisodeRail should link to the persisted AI triage chat");
assert(rail.includes("Unable to load Care Episodes."), "EpisodeRail should distinguish fetch errors from an empty list");
assert(rail.includes("Unable to load AI triage chats."), "EpisodeRail should distinguish chat fetch errors from an empty list");
assert(!rail.includes("episodeChats.map"), "EpisodeRail should not render episode chats in the global directory");
assert(!rail.includes("listEpisodeRehabSessions"), "EpisodeRail must not load AI Daily Rehab sessions as unfinished chats");
assert(!rail.includes("Unfinished sessions"), "EpisodeRail must not label rehab sessions as unfinished chats");

console.log("episode rail contract ok");
