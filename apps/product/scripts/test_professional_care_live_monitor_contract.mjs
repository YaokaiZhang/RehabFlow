import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const productRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(productRoot, "../..");

const liveMonitorPagePath = resolve(productRoot, "app/episodes/[episode_id]/professional-care/live-monitor/page.tsx");
const oldMonitorPagePath = resolve(productRoot, "app/dashboard/monitor/[patient_id]/page.tsx");
const sharedWsPath = resolve(repoRoot, "packages/shared/src/ws.ts");
const sharedApiPath = resolve(repoRoot, "packages/shared/src/api.ts");
const doctorPagePath = resolve(productRoot, "app/doctor/page.tsx");
const episodeOverviewPagePath = resolve(productRoot, "app/episodes/[episode_id]/page.tsx");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

assert(existsSync(liveMonitorPagePath), "episode-scoped professional care live monitor page should exist");
assert(!existsSync(oldMonitorPagePath), "patient-global doctor monitor dashboard route should be deleted");

const liveMonitorPage = readFileSync(liveMonitorPagePath, "utf8");
const sharedWs = readFileSync(sharedWsPath, "utf8");
const sharedApi = readFileSync(sharedApiPath, "utf8");
const doctorPage = readFileSync(doctorPagePath, "utf8");
const episodeOverviewPage = readFileSync(episodeOverviewPagePath, "utf8");

assert(liveMonitorPage.includes("Optional Live Movement Monitoring"), "live monitor page should use glossary title");
assert(liveMonitorPage.includes("CareEpisodeBrief"), "live monitor page should show the Care Episode Brief");
assert(liveMonitorPage.includes("<h1"), "live monitor page should preserve an explicit page-level h1");
assert(liveMonitorPage.includes("issueDoctorMonitorTicket"), "live monitor should issue a doctor monitor ticket");
assert(liveMonitorPage.includes("await issueDoctorMonitorTicket(episodeId, nextAuth.access_token)"), "live monitor should issue a fresh episode ticket immediately before connecting");
assert(liveMonitorPage.includes("connectDoctorEpisodeMonitor(episodeId, ticket)"), "live monitor should connect with the episode ticket");
assert(liveMonitorPage.includes("try") && liveMonitorPage.includes("catch"), "live monitor should preserve issuance failure handling");
assert(!liveMonitorPage.includes("connectDoctorEpisodeMonitor(patientId)"), "live monitor must not connect by patient id");
assert(liveMonitorPage.includes("Latest signal"), "live monitor should show latest signal summary");
assert(liveMonitorPage.includes("Patient not streaming"), "live monitor should make empty stream state explicit");
assert(liveMonitorPage.includes("Recent movement events"), "live monitor should keep recent event review");
assert(liveMonitorPage.includes("terminalStatusRef"), "live monitor should preserve terminal websocket detail on close");
assert(liveMonitorPage.includes("terminalStatusRef.current = data.detail"), "live monitor should remember backend websocket detail");
assert(liveMonitorPage.includes("type ConnectionStatus"), "live monitor should track websocket connection status separately from received events");
assert(liveMonitorPage.includes("useState<ConnectionStatus>("), "live monitor should store the current websocket connection status");
assert(liveMonitorPage.includes('setConnectionStatus("connected")'), "live monitor should mark an open or active socket connected");
assert(liveMonitorPage.includes('setConnectionStatus("error")'), "live monitor should mark socket errors as an error state");
assert(liveMonitorPage.includes('setConnectionStatus("disconnected")'), "live monitor should mark socket closes as disconnected");
assert(liveMonitorPage.includes('tone={connectionStatus === "connected" ? "success" : connectionStatus === "error" ? "risk" : "neutral"}'), "live monitor badge tone should derive from current connection status");
assert(!liveMonitorPage.includes('ws.onclose = () => setStatusMessage("Disconnected from Care Episode monitor")'), "live monitor onclose must not blindly overwrite backend detail");
assert((liveMonitorPage.match(/wsRef\.current !== ws/g) || []).length >= 4, "live monitor websocket handlers should ignore stale socket callbacks");
assert(liveMonitorPage.includes('auth.role !== "doctor"'), "live monitor page should require doctor auth");
assert(liveMonitorPage.includes("Care Episode"), "live monitor page should frame monitoring by Care Episode");
assert(!liveMonitorPage.includes("Doctor Monitor Dashboard"), "live monitor page should avoid old dashboard language");
assert(!liveMonitorPage.includes("Score Trend"), "live monitor page should avoid old score trend language");

assert(sharedWs.includes("connectDoctorEpisodeMonitor"), "shared websocket module should preserve doctor monitor helper export");
assert(sharedWs.includes("ticket: string"), "shared websocket helpers should require tickets");
assert(!sharedWs.includes("localStorage"), "shared websocket module must not read localStorage");
assert(!sharedWs.includes("access_token"), "shared websocket URLs must not include access tokens");
assert(!sharedWs.includes("patientId"), "shared websocket module must not accept patient ids");
assert(sharedWs.includes("/doctor/monitor/episodes/"), "shared websocket helper should use episode-scoped endpoint");
assert(sharedWs.includes("encodeURIComponent(careEpisodeId)"), "shared websocket helper should encode care episode route segment");
assert(sharedWs.includes("?ticket="), "shared websocket helper should pass the ticket as the only socket query parameter");
assert(sharedApi.includes("issueDoctorMonitorTicket"), "shared HTTP client should export doctor monitor ticket issuance");
assert(sharedApi.includes("/stream-tickets/doctor-monitor/"), "shared HTTP client should use the doctor monitor ticket endpoint");
assert(sharedApi.includes("Authorization: `Bearer ${accessToken}`"), "ticket issuance should use bearer auth");

assert(doctorPage.includes("/professional-care/live-monitor"), "doctor page should link active relationships to episode-scoped live monitor");
assert(!doctorPage.includes("/dashboard/monitor/"), "doctor page should not link to patient-global monitor route");
assert(!episodeOverviewPage.includes("/dashboard/monitor/"), "episode overview should not link to patient-global monitor route");

console.log("professional care live monitor contract ok");
