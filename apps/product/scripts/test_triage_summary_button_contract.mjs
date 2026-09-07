import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const page = readFileSync(resolve("app/page.tsx"), "utf8");
const loginPage = readFileSync(resolve("app/login/page.tsx"), "utf8");
const episodeTriagePage = readFileSync(resolve("app/episodes/[episode_id]/triage/page.tsx"), "utf8");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

assert(page.includes("Request Summary"), "triage page should render a user-facing Request Summary button");

assert(page.includes("sendAiChatTurn"), "triage intake should send chat turns through the shared HTTP API");
assert(page.includes("agentProgressSteps"), "triage intake should define dynamic agent progress steps");
assert(page.includes("agent-progress-card"), "triage intake should render a dynamic agent progress card while waiting");
assert(page.includes("setInterval"), "triage intake should advance the visible agent progress while a turn is in flight");
assert(page.includes("Checking safety"), "agent progress should expose a safety-check step to the user");
assert(!page.includes("triageHardTimeoutMs"), "triage intake should not enforce a hard 180s chat timeout");
assert(!page.includes("responseTimeoutRef"), "triage intake should not keep the old hard-timeout timer ref");
assert(!page.includes("Still reviewing your rehab context"), "long-running turns should use the live progress hint instead of appending stale system notices");
assert(!page.includes("new WebSocket"), "triage intake must not bypass the Next /api proxy with a browser-direct websocket");
assert(!page.includes("getWsBase"), "triage intake should not derive a browser-direct websocket host");
assert(page.includes("createEpisodeFromTriageSummaryRequest"), "triage page should use the Triage Summary Request API to create an episode when no episode_id is present");
assert(page.includes("if (!targetEpisodeId)"), "finishTriage should branch to summary-request episode creation when no episode exists");
assert(!page.includes("createCareEpisode"), "triage page must not call manual episode creation from the summary button");
assert(!page.includes("{episodeId ? ("), "summary request panel must not be hidden behind episodeId");

assert(page.includes("PENDING_TRIAGE_SUMMARY_KEY"), "triage page should use session storage for pending summary history");
assert(page.includes("sessionStorage.setItem(PENDING_TRIAGE_SUMMARY_KEY"), "unauthenticated summary requests should preserve triage history before login");
assert(page.includes("/login?next="), "unauthenticated summary requests should send patients through login with a return path");
assert(loginPage.includes("nextPath"), "login page should read a return path for interrupted patient flows");
assert(loginPage.includes("nextPath || \"/episodes\""), "patient login should return to the interrupted flow before falling back to episodes");
assert(page.includes('additional_context: aiSessionId ? ""'), "session-backed summary requests should not add generic request boilerplate to clinical context");
assert(episodeTriagePage.includes("summary.concern || episodeIssueTitle"), "episode triage summary card should prefer the concise summary concern");
assert(episodeTriagePage.includes("What we heard"), "episode triage summary should present a concise patient-context narrative");
assert(episodeTriagePage.includes("Suggested next step"), "episode triage summary should label its recommendation clearly");
assert(!episodeTriagePage.includes(">Missing information</h3>"), "patient summary card should not render a missing-information panel");
assert(!episodeTriagePage.includes(">Safety signals</h3>"), "patient summary card should not render a safety-signals panel");
assert(!episodeTriagePage.includes("Ready for rehab"), "an unsaved summary must not claim rehab readiness");

console.log("triage summary button contract ok");
