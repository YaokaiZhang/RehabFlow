import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const apiPath = resolve(scriptDir, "../src/api.ts");
const source = readFileSync(apiPath, "utf8");

function assert(condition, message) {
	if (!condition) {
		throw new Error(message);
	}
}

function functionBody(name) {
	const declaration = `export async function ${name}`;
	const start = source.indexOf(declaration);
	assert(start !== -1, `${name} export is missing`);
	const nextExport = source.indexOf("\nexport ", start + declaration.length);
	return source.slice(start, nextExport === -1 ? source.length : nextExport);
}

const requiredExports = [
	"searchExerciseCatalog",
	"getExerciseCatalogFacets",
	"getAIDailyRehabRecommendation",
	"getAIDailyRehabList",
	"updateAIDailyRehabList",
	"createEpisodeRehabSession",
	"getCareEpisodeHistory",
	"sendAiChatTurn",
];

for (const name of requiredExports) {
	assert(source.includes(`export async function ${name}`), `${name} export is missing`);
}


const chatTurnBody = functionBody("sendAiChatTurn");
assert(
	chatTurnBody.includes("/ai-chat/") && chatTurnBody.includes("typeof window !== \"undefined\""),
	"sendAiChatTurn must use the product-owned long-turn proxy route in the browser"
);
assert(
	!chatTurnBody.includes("NEXT_PUBLIC_API_BASE"),
	"sendAiChatTurn must not let NEXT_PUBLIC_API_BASE bypass the product-owned browser AI chat route"
);
assert(
	!chatTurnBody.includes("180000"),
	"sendAiChatTurn should not keep the old 180s triage timeout"
);
assert(
	!chatTurnBody.includes("timeoutMs"),
	"sendAiChatTurn should not define a client-side timeout"
);
assert(
	chatTurnBody.includes("}, null);"),
	"sendAiChatTurn should disable the fetch timeout"
);

assert(
	source.includes("source_ai_session_id"),
	"triage summary types should expose source_ai_session_id"
);
assert(
	source.includes("source_conversation_transcript"),
	"triage summary types should expose source_conversation_transcript"
);
assert(
	source.includes("export type CareEpisodeHistory"),
	"shared API should export CareEpisodeHistory"
);

const historyBody = functionBody("getCareEpisodeHistory");
assert(
	historyBody.includes("/history"),
	"getCareEpisodeHistory must call the care episode history endpoint"
);

const createSessionBody = functionBody("createEpisodeRehabSession");
assert(
	!createSessionBody.includes("recommended_exercises"),
	"createEpisodeRehabSession must not send recommended_exercises"
);
assert(
	createSessionBody.includes("JSON.stringify({ patient_notes: input.patient_notes })"),
	"createEpisodeRehabSession must send only patient_notes"
);

const updateListBody = functionBody("updateAIDailyRehabList");
assert(
	updateListBody.includes("JSON.stringify({ exercise_ids: exerciseIds })"),
	"updateAIDailyRehabList must send exercise_ids"
);

console.log("shared api contract ok");
