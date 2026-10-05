import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const productRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const api = readFileSync(resolve(productRoot, "../../packages/shared/src/api.ts"), "utf8");
const page = readFileSync(resolve(productRoot, "app/page.tsx"), "utf8");
const proxy = readFileSync(resolve(productRoot, "app/ai-chat/[session_id]/route.ts"), "utf8");
const backend = readFileSync(resolve(productRoot, "../../backend/app/api/ws.py"), "utf8");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

for (const required of ["listAiChatSessions", "getAiChatSession", "deleteAiChatSession", "AiChatSessionSummary", "AiChatMessage"]) {
  assert(api.includes(required), "shared API should include " + required);
}
assert(page.includes("useSearchParams"), "AI triage should read the selected historical chat from the URL");
assert(page.includes("getAiChatSession"), "AI triage should load the selected historical chat");
assert(page.includes("session.messages.map"), "AI triage should render persisted chat messages");
assert(page.includes("rehab-ai-chat-changed"), "AI triage should refresh the chat directory after a chat changes");
assert(proxy.includes("export async function GET"), "AI chat proxy should support historical chat reads");
assert(proxy.includes("export async function DELETE"), "AI chat proxy should support chat deletion");
assert(proxy.includes("method: request.method"), "AI chat proxy should preserve the requested method");
assert(backend.includes('@router.get("/ai/chat/sessions")'), "backend should list patient AI chat sessions");
assert(backend.includes('@router.get("/ai/chat/{session_id}")'), "backend should read one patient AI chat session");
assert(backend.includes('@router.delete("/ai/chat/{session_id}", status_code=204)'), "backend should delete one patient AI chat session");
assert(backend.includes("care_episode_id"), "chat history should preserve episode ownership context");

console.log("AI chat history contract ok");
