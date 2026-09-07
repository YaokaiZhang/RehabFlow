
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const page = readFileSync(resolve("apps/product/app/episodes/[episode_id]/rehab/page.tsx"), "utf8");
const hook = readFileSync(resolve("apps/product/app/rehab/useAiDailyRehabWorkspace.ts"), "utf8");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function extractBracedBlock(source, openBrace, label) {
  assert(openBrace >= 0, `${label} should have an opening brace`);
  let depth = 0;
  let quote = null;
  let escaped = false;
  for (let index = openBrace; index < source.length; index += 1) {
    const character = source[index];
    if (quote) {
      if (escaped) {
        escaped = false;
      } else if (character === "\\") {
        escaped = true;
      } else if (character === quote) {
        quote = null;
      }
      continue;
    }
    if (character === '"' || character === "'" || character === "`") {
      quote = character;
    } else if (character === "{") {
      depth += 1;
    } else if (character === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(openBrace, index + 1);
    }
  }
  throw new Error(`${label} is not closed`);
}

function extractFunctionSource(source, functionName) {
  const start = source.indexOf(`function ${functionName}(`);
  assert(start >= 0, `${functionName} should exist`);
  const openBrace = source.indexOf("{", start);
  assert(openBrace >= 0, `${functionName} should have a body`);
  const body = extractBracedBlock(source, openBrace, `${functionName} body`);
  return source.slice(start, openBrace + body.length);
}

function loadPageFunction(source, functionName) {
  const declaration = extractFunctionSource(source, functionName).replace(
    new RegExp(`function ${functionName}\\(status: [^)]+\\)`),
    `function ${functionName}(status)`,
  );
  return new Function(`${declaration}\nreturn ${functionName};`)();
}

function assertExactMappings(source, functionName, mappings, label) {
  const mapper = loadPageFunction(source, functionName);
  for (const [input, expected] of Object.entries(mappings)) {
    assert(mapper(input) === expected, `${label} should map ${input} to ${expected}`);
  }
}

function assertSafetyGateSourceIsMapped(source, sourceExpression, label) {
  const references = source.split(sourceExpression).length - 1;
  assert(references === 1, label + " should reference " + sourceExpression + " only through safetyCopy");
  assert(
    source.includes("{safetyCopy(" + sourceExpression + ")}"),
    label + " should allowlist the safetyCopy mapper call for " + sourceExpression,
  );
}

for (const required of [
  "useAiDailyRehabWorkspace",
  "Recommended from AI Triage",
  "Search exercises",
  "Start Session",
]) {
  assert(page.includes(required), "rehab recommendations page should include " + required);
}
assert(
  page.includes("This Care Episode has not cleared the Rehab Safety Gate."),
  "AI Daily Rehab should use neutral blocked copy for unknown safety statuses",
);

for (const required of [
  "getAIDailyRehabRecommendation",
  "getAIDailyRehabList",
  "updateAIDailyRehabList",
  "searchExerciseCatalog",
  "getExerciseCatalogItem",
  "loadExerciseDetails",
  "router.push(`/rehab/session?",
  "session_id=${encodeURIComponent(session.session_id)}",
  "exercise_ids=${encodeURIComponent(savedListItems.map((item) => item.exercise_id).join(","))}",
  "useRef",
  "loadRehabRequestRef",
  "catalogRequestRef",
]) {
  assert(hook.includes(required), "useAiDailyRehabWorkspace should include " + required);
}

assert(hook.includes("function isSafetyGateCleared(status: string)"), "AI Daily Rehab should define a positive safety gate allowlist");
assertExactMappings(hook, "isSafetyGateCleared", {
  triage_complete: true,
  clinician_reviewed: true,
  needs_triage: false,
  unsupported_status: false,
}, "AI Daily Rehab safety gate eligibility");
assert(
  hook.includes("const blocked = episode ? !isSafetyGateCleared(episode.safety_gate_status) : false;"),
  "AI Daily Rehab should block every loaded episode status outside the cleared allowlist",
);

for (const forbidden of [
  "Exercises, one per line",
  "setExerciseText",
  "exerciseText.split",
  "find alternatives",
  "toggleItem(item.label)",
  "Body structures",
  "Related conditions",
  "getExerciseCatalogFacets",
  "structureFilter",
  "conditionFilter",
  "stop if symptoms worsen",
  "{item.snapshot.introduction}",
  "Patient notes",
  "Session checklist",
  "Generate Session Summary",
  "latestSession",
  "toggleItem",
  "summarize",
  "<textarea",
]) {
  assert(!page.includes(forbidden), "rehab recommendations page must not include " + forbidden);
}

const sharedApiCallNames = [
  "getAIDailyRehabRecommendation",
  "getAIDailyRehabList",
  "updateAIDailyRehabList",
  "searchExerciseCatalog",
  "createEpisodeRehabSession",
  "listEpisodeRehabSessions",
  "updateEpisodeRehabSession",
  "summarizeEpisodeRehabSession",
  "getCareEpisode",
];
const directSharedApiCalls = sharedApiCallNames.reduce((count, name) => {
  const matches = page.match(new RegExp(`\\b${name}\\s*\\(`, "g"));
  return count + (matches?.length || 0);
}, 0);
assert(page.includes("from \"../../../rehab/useAiDailyRehabWorkspace\""), "rehab page should import useAiDailyRehabWorkspace");
assert(!page.includes("@rehab/shared/api"), "rehab page should not import the shared API directly");
assert(directSharedApiCalls <= 2, "rehab page should contain no more than two direct shared API function calls");

assert(page.includes("function ExerciseCatalogCard("), "catalog cards should use a focused component");
assert(page.includes("aria-expanded={infoOpen}"), "catalog Info should expose disclosure state");
assert(page.includes("aria-controls={detailsId}"), "catalog Info should identify its details region");
assert(page.includes(">Info</button>"), "catalog cards should expose a compact Info action");
assert(page.includes("Search exercises"), "catalog search label should be concise");
assert(page.includes("Search by goal, symptom, movement, or name"), "catalog placeholder should be concise");
assert(!page.includes("structures={item.structures_involved}"), "recommendation cards should not render structure chips");
assert(page.includes("function ExerciseRecommendationCard("), "recommendation cards should use the compact Info design");
assert(page.includes("recommendation-info-"), "recommendation Info should identify its details region");
assert(page.includes("function ExerciseInfoDetails("), "recommendation and catalog Info should share full details rendering");
assert(page.includes("details={exerciseDetails[item.exercise_id]}"), "recommendation Info should receive canonical exercise details");
assert(page.includes("detailLoading={Boolean(detailLoadingIds[item.exercise_id])}"), "recommendation Info should expose detail loading state");
assert(page.includes("onLoadDetails={() => loadExerciseDetails(item.exercise_id)}"), "recommendation Info should load canonical exercise details");
assert(page.includes("<FullText>{details.introduction}</FullText>"), "Info should render the full catalog introduction");
assert(page.includes("Full exercise details unavailable."), "Info should fail clearly when canonical details are unavailable");
assert(!page.includes("<FullText>{item.reason}</FullText>"), "Info should not render the short recommendation reason");
assert(!page.includes("item.dosage"), "Info should not render the short recommendation dosage");
assert(!page.includes("function ShortText"), "Info descriptions should not use the trimmed text helper");
assert(!page.includes("children.length > 260"), "Info descriptions should not be truncated");
assert(page.includes("function statusCopy(status: string)"), "AI Daily Rehab should map recommendation statuses to readable copy");
assert(page.includes("{statusCopy(recommendation.status)}"), "AI Daily Rehab should render readable recommendation status copy");
assert(page.includes("function safetyCopy(status: string)"), "AI Daily Rehab should map safety gate values to readable copy");
assert(page.includes("{safetyCopy(episode.safety_gate_status)}"), "AI Daily Rehab should render readable safety gate copy");
assertExactMappings(page, "statusCopy", {
  ready: "Ready",
  empty: "Empty",
}, "AI Daily Rehab recommendation status copy");
assertExactMappings(page, "safetyCopy", {
  needs_triage: "Needs AI Triage Intake before AI Daily Rehab",
  triage_complete: "AI triage context available",
  clinician_reviewed: "Clinician-reviewed context available",
}, "AI Daily Rehab safety gate copy");
assert(
  loadPageFunction(page, "safetyCopy")("unsupported_status") === "Not recorded",
  "AI Daily Rehab should keep unsupported safety values neutral instead of treating them as affirmative",
);
assert(!page.includes("recommendation.status}</span>"), "AI Daily Rehab should not render raw recommendation status enums");
assertSafetyGateSourceIsMapped(page, "episode.safety_gate_status", "AI Daily Rehab");

assert(
  page.includes(`<span className="block font-semibold text-slate-950">{item.label}</span>`),
  "saved daily rehab list should show exercise labels"
);

assert(
  /onClick\s*=\s*\{\s*createSession\s*\}/.test(page),
  "AI Daily Rehab List should launch a session through createSession"
);
assert(
  page.includes("onAdd={() => addExercise(item.exercise_id)}") &&
  page.includes("onRemove={() => removeExercise(item.exercise_id)}") &&
  page.includes("onAdd={() => addExercise(exercise.exercise_id)}") &&
  page.includes("onRemove={() => removeExercise(exercise.exercise_id)}"),
  "recommendation and catalog items should support add/remove list management"
);
assert(!hook.includes("listEpisodeRehabSessions"), "list workspace should not load active rehab sessions");
assert(!hook.includes("updateEpisodeRehabSession"), "list workspace should not update active rehab sessions");
assert(!hook.includes("summarizeEpisodeRehabSession"), "list workspace should not summarize active rehab sessions");
assert(!hook.includes("patientNotes"), "list workspace should not own active-session patient notes");

assert(
  /createEpisodeRehabSession\(\s*episode\.care_episode_id,\s*\{\s*patient_notes:\s*\"\"\s*\},\s*auth\.access_token\s*\)/s.test(hook),
  "list workspace should create sessions with empty initial notes"
);

assert(hook.includes("loadRehabRequestRef.current += 1"), "workspace loads should use request sequencing");
assert(hook.includes("catalogRequestRef.current += 1"), "catalog loads should use request sequencing");
assert(hook.includes("if (requestId !== loadRehabRequestRef.current) return"), "stale workspace responses should be ignored");
assert(hook.includes("if (requestId !== catalogRequestRef.current) return"), "stale catalog responses should be ignored");
assert(!page.includes("recommendation.active ? \"active\" : recommendation.status"), "recommendation rendering should prefer the actual status text");

function countMatches(source, pattern) {
  return source.match(pattern)?.length || 0;
}

function blockBetween(source, startNeedle, endNeedle) {
  const start = source.indexOf(startNeedle);
  const end = source.indexOf(endNeedle, start);
  assert(start >= 0 && end > start, `expected block from ${startNeedle} to ${endNeedle}`);
  return source.slice(start, end);
}

const loadCatalogBlock = blockBetween(hook, "const loadCatalog = useCallback", "const loadRehab = useCallback");
const loadRehabBlock = blockBetween(hook, "const loadRehab = useCallback", "useEffect");
assert(
  loadRehabBlock.includes("if (isSafetyGateCleared(nextEpisode.safety_gate_status)) {"),
  "AI Daily Rehab recommendation and list loads should be inside the positive safety gate allowlist",
);
const safetyGateBlockStart = loadRehabBlock.indexOf("if (isSafetyGateCleared(nextEpisode.safety_gate_status)) {");
const safetyGateBlock = extractBracedBlock(
  loadRehabBlock,
  safetyGateBlockStart + loadRehabBlock.slice(safetyGateBlockStart).indexOf("{"),
  "AI Daily Rehab safety gate branch",
);
for (const fetchName of ["getAIDailyRehabRecommendation", "getAIDailyRehabList"]) {
  const fetchPattern = new RegExp(`\\b${fetchName}\\s*\\(`, "g");
  assert(
    countMatches(safetyGateBlock, fetchPattern) === 1,
    `${fetchName} should be called exactly once inside the positive safety gate branch`,
  );
  assert(
    countMatches(loadRehabBlock, fetchPattern) === 1,
    `${fetchName} should be called exactly once while loading rehab`,
  );
}
assert(
  !loadRehabBlock.includes('nextEpisode.safety_gate_status !== "needs_triage"'),
  "AI Daily Rehab should not treat every non-needs_triage status as cleared",
);
const catalogFetchCalls = countMatches(hook, /\bsearchExerciseCatalog\s*\(/g);
const catalogWriterCalls = countMatches(hook, /\bsetCatalogExercises\s*\(/g);
assert(catalogFetchCalls === 1, "useAiDailyRehabWorkspace should have exactly one catalog fetch call");
assert(catalogWriterCalls === 1, "useAiDailyRehabWorkspace should have exactly one catalogExercises writer");
assert(countMatches(loadCatalogBlock, /\bsearchExerciseCatalog\s*\(/g) === 1, "loadCatalog should own the catalog fetch call");
assert(countMatches(loadCatalogBlock, /\bsetCatalogExercises\s*\(/g) === 1, "loadCatalog should own the catalogExercises writer");
assert(!loadRehabBlock.includes("setCatalogExercises"), "loadRehab should not own catalog exercise results");
assert(!loadRehabBlock.includes("searchExerciseCatalog"), "loadRehab should not fetch catalog results separately from loadCatalog");

console.log("rehab recommendations page contract ok");
