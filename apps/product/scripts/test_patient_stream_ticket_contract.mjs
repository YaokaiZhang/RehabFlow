import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const repoRoot = resolve(".");
const api = readFileSync(resolve(repoRoot, "packages/shared/src/api.ts"), "utf8");
const ws = readFileSync(resolve(repoRoot, "packages/shared/src/ws.ts"), "utf8");
const page = readFileSync(resolve(repoRoot, "apps/product/app/rehab/session/page.tsx"), "utf8");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

assert(api.includes("issuePatientStreamTicket"), "shared HTTP client should export patient stream ticket issuance");
assert(api.includes("/stream-tickets/patient-rehab"), "patient ticket issuance should use the patient rehab endpoint");
assert(api.includes("Authorization: `Bearer ${accessToken}`"), "patient ticket issuance should use bearer auth");
assert(api.includes("typeof data.ticket !== \"string\""), "ticket helpers should validate the response ticket string");
assert(ws.includes("connectPatientStream(ticket: string)"), "patient websocket helper should accept a ticket");
assert(ws.includes("/rehab/stream?ticket="), "patient websocket helper should use the ticket-only endpoint");
assert(!ws.includes("localStorage"), "shared websocket module must not read localStorage");
assert(!ws.includes("access_token"), "patient websocket URL must not include an access token");
assert(!ws.includes("patientId"), "patient websocket helper must not accept a patient id");

assert(page.includes("issuePatientStreamTicket"), "patient session should issue a stream ticket");
assert(page.includes("const auth = loadAuth()"), "patient session should load current auth before scoring");
assert(page.includes("await issuePatientStreamTicket(auth.access_token)"), "patient session should await a fresh ticket before opening scoring");
assert(page.includes("connectPatientStream(ticket)"), "patient session should connect with the issued ticket");
assert(page.includes("Pose detection continues locally."), "patient session should preserve local pose fallback");
assert(page.includes("wsRef.current !== ws"), "patient session should ignore stale websocket callbacks");
assert(page.includes("if (!ticket)"), "patient session should gate socket creation on ticket issuance");

console.log("patient stream ticket contract ok");
