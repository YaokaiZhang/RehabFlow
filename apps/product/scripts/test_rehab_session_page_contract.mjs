import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const page = readFileSync(resolve("apps/product/app/rehab/session/page.tsx"), "utf8");
const runtime = readFileSync(resolve("packages/shared/src/runtime.ts"), "utf8");
const rootEnvExample = readFileSync(resolve(".env.example"), "utf8");
const productEnvExample = readFileSync(resolve("apps/product/.env.example"), "utf8");
const productPackage = readFileSync(resolve("apps/product/package.json"), "utf8");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function blockBetween(source, startNeedle, endNeedle) {
  const start = source.indexOf(startNeedle);
  const end = source.indexOf(endNeedle, start);
  assert(start >= 0 && end > start, `expected block from ${startNeedle} to ${endNeedle}`);
  return source.slice(start, end);
}

for (const required of [
  "getExerciseCatalogItem",
  "new URLSearchParams(search)",
  "parseExerciseIds(window.location.search)",
  "exercise_ids",
  "session_id",
  "catalogFallbackVideos",
  "onlineVideoUrl",
  "<iframe",
  "Reference video only",
  "Pose overlay appears when downloaded pose data is available.",
  "listEpisodeRehabSessions",
  "updateEpisodeRehabSession",
  "summarizeEpisodeRehabSession",
  "Generate Session Summary",
  "Patient notes",
  "Session checklist",
]) {
  assert(page.includes(required), "rehab session page should include " + required);
}

assert(
  /const\s+hasRealSessionContext\s*=\s*Boolean\(\s*episodeId\s*&&\s*sessionId\s*\)/.test(page),
  "session controls should require both episode and session context"
);
assert(
  /listEpisodeRehabSessions\(\s*episodeId,\s*auth\.access_token\s*\)/s.test(page)
    && /\.find\(\s*\([^)]*\)\s*=>\s*[^)]*\.session_id\s*===\s*sessionId\s*\)/s.test(page),
  "real-session mode should load the requested EpisodeRehabSession"
);
assert(
  /hasRealSessionContext\s*&&\s*rehabSession\s*\?/.test(page),
  "active-session controls should render only for a loaded real session"
);
assert(
  /updateEpisodeRehabSession\([\s\S]*completed_items:[\s\S]*patient_notes:/m.test(page),
  "session mutations should persist checklist completion and patient notes together"
);
assert(
  /summarizeEpisodeRehabSession\(\s*rehabSession\.session_id,\s*auth\.access_token\s*\)/s.test(page),
  "Generate Session Summary should use the existing summarize API"
);
assert(/checklist\.map\(\s*\(item,\s*index\)/.test(page), "real-session mode should render the session checklist");
assert(/<textarea[\s\S]*value=\{patientNotes\}/m.test(page), "real-session mode should render editable patient notes");
assert(
  /const\s+patientNotesRef\s*=\s*useRef<string>\(\s*""\s*\)/.test(page),
  "patient notes should have a synchronous ref for request reconciliation"
);
assert(
  /onChange=\{\(event\)\s*=>\s*updatePatientNotes\(event\.target\.value\)\}/.test(page),
  "patient note edits should update state and the reconciliation ref together"
);
const persistBlock = blockBetween(page, "const persistSession = async", "const toggleChecklistItem");
assert(
  /const\s+submittedPatientNotes\s*=\s*patientNotesRef\.current/.test(persistBlock)
    && /patient_notes:\s*submittedPatientNotes/.test(persistBlock)
    && /if\s*\(\s*patientNotesRef\.current\s*===\s*submittedPatientNotes\s*\)\s*\{\s*updatePatientNotes\(updated\.patient_notes\)/s.test(persistBlock),
  "session persistence should only reconcile server notes when the local draft still matches the submitted snapshot"
);
assert(!/if\s*\(\s*!hasRealSessionContext\s*\)\s*return\s+null/.test(page), "demo mode must not be hard-gated without real session context");
const summarizeBlock = blockBetween(page, "const summarizeSession = async", "useEffect(() => {");
assert(
  summarizeBlock.indexOf("updateEpisodeRehabSession") >= 0
    && summarizeBlock.indexOf("updateEpisodeRehabSession") < summarizeBlock.indexOf("summarizeEpisodeRehabSession"),
  "summary generation should persist current notes and checklist before summarizing"
);
assert(
  /const\s+submittedPatientNotes\s*=\s*patientNotesRef\.current/.test(summarizeBlock)
    && /patient_notes:\s*submittedPatientNotes/.test(summarizeBlock)
    && /if\s*\(\s*patientNotesRef\.current\s*===\s*submittedPatientNotes\s*\)\s*\{\s*updatePatientNotes\(updated\.patient_notes\)/s.test(summarizeBlock),
  "summary generation should preserve notes typed after its submitted snapshot"
);

const startSessionBlock = blockBetween(page, "const startSession = async", "    return (");
const webcamStart = startSessionBlock.indexOf("await initWebcam()");
const anonymousBranch = startSessionBlock.indexOf('if (!auth || auth.role !== "patient" || !auth.access_token)');
assert(
  webcamStart >= 0 && anonymousBranch > webcamStart,
  "demo mode should start local pose detection before skipping authenticated scoring"
);
assert(
  startSessionBlock.includes("Pose detection continues locally."),
  "anonymous demo mode should explain that only scoring is unavailable"
);

assert(!page.includes("listAclKneeStiffnessVideos()"), "session page should not always default to ACL demo videos only");
assert(page.includes("listAclKneeStiffnessVideos"), "session page can still reuse demo videos when they match selected exercises");
assert(!page.includes("Session {sessionId}"), "session page must not expose the raw session UUID in the visible header");
assert(!page.includes("Patient: {patientId"), "session page must not expose the raw patient UUID");
assert(!page.includes("Episode: {episodeId"), "session page must not expose the raw episode UUID");
assert(page.includes("Today&apos;s guided movement session"), "session page should use human-readable session context copy");
assert(page.includes("detectionActiveRef"), "session page should track local pose detection independently of the websocket");
assert(!page.includes("if (wsRef.current?.readyState === WebSocket.OPEN) scheduleVideoFrame()"), "pose detection should not stop scheduling video frames when websocket closes");
assert(!page.includes("if (wsRef.current?.readyState === WebSocket.OPEN) rafRef.current = requestAnimationFrame(tick)"), "pose detection fallback loop should not depend on websocket open state");
assert(!page.includes("Disconnected"), "live pose status should not be overwritten with disconnected when only scoring stream closes");
assert(!page.includes("Scoring stream closed"), "missing reference pose data should not appear as a red scoring stream warning");
assert(!page.includes("Scoring stream is unavailable"), "missing reference pose data should not appear as a stream outage warning");
assert(page.includes("Scoring will be available when reference pose data is added for this movement."), "session page should show a subtle reference-pose scoring reminder");
assert(page.includes("scoringNotice"), "session page should keep scoring reminders separate from error state");
assert(runtime.includes("NEXT_PUBLIC_BACKEND_PORT"), "shared runtime should allow the backend port to be set from .env");
assert(runtime.includes("DEFAULT_BACKEND_PORT = \"8000\""), "shared runtime should default to the active FastAPI backend port");
assert(runtime.includes("`ws://localhost:${backendPort}`"), "server websocket runtime should derive its port from the backend port setting");
assert(runtime.includes("`${protocol}//${window.location.hostname}:${backendPort}`"), "browser websocket runtime should derive its port from the backend port setting");
assert(rootEnvExample.includes("BACKEND_PORT=8000"), "root env example should document the configurable backend port");
assert(rootEnvExample.includes("PRODUCT_FRONTEND_PORT=3000"), "root env example should document the Product frontend port");
assert(productEnvExample.includes("Root .env is canonical"), "product env example should point to the root template");
assert(productPackage.includes("node ../../scripts/dev-next.mjs\""), "product dev script should let dev-next read FRONTEND_PORT from .env");
assert(!productPackage.includes("dev-next.mjs 3002"), "product dev script should not hard-code the frontend port");
assert(!runtime.includes("8001"), "shared runtime must not point scoring streams at the unused 8001 port");

const heroStart = page.indexOf('<section className="overflow-hidden rounded-lg border border-slate-200 bg-white shadow-sm">');
const referenceMarker = page.indexOf('<section aria-label="Reference movement video"');
const checklistMarker = page.indexOf("            {hasRealSessionContext");
assert(heroStart >= 0, "rehab session should keep the live-session hero");
assert(page.includes("lg:grid-cols-[minmax(0,1fr)_360px]"), "live session hero should keep media wide beside the session controls");
assert(page.includes("flex flex-col gap-4 p-4"), "live session media should stack the patient and reference videos vertically");
assert(page.includes("aspect-video w-full scale-x-[-1] object-cover"), "live patient video should use a readable widescreen surface");
const liveVideoMarker = page.indexOf("<video ref={videoRef}");
assert(liveVideoMarker >= 0 && liveVideoMarker > heroStart && liveVideoMarker < referenceMarker, "live patient video should render first in the stacked media column");
assert(referenceMarker >= 0 && referenceMarker > liveVideoMarker && referenceMarker < checklistMarker, "reference video should render below the patient feed in the live-session hero");

assert((() => { const socketStart = startSessionBlock.indexOf("const ws = connectPatientStream(ticket);"); const catchStart = startSessionBlock.indexOf("} catch {", socketStart); return socketStart >= 0 && catchStart > socketStart && startSessionBlock.indexOf("setScoringNotice", catchStart) >= 0; })(), "socket construction failure should use the scoring-unavailable local-pose fallback");
console.log("rehab session page contract ok");
