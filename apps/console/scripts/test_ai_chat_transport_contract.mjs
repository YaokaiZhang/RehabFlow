#!/usr/bin/env node
import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(scriptDir, "../../..");
const page = fs.readFileSync(resolve(repoRoot, "apps/console/app/page.tsx"), "utf8");
const proxy = fs.readFileSync(resolve(repoRoot, "apps/console/app/ai-chat/[session_id]/route.ts"), "utf8");

assert(page.includes("sendAiChatTurn"), "console AI chat should use the shared HTTP turn helper");
assert(page.includes("loadAuth"), "console AI chat should load authenticated browser state");
assert(page.includes('auth.role !== "patient"'), "console AI chat should require a patient auth role");
assert(page.includes("auth.access_token"), "console AI chat should pass the patient access token");
assert(page.includes("crypto.randomUUID()"), "console AI chat should generate a required idempotency key");
assert(page.includes("idempotency_key"), "console AI chat should send the idempotency key");
assert(!page.includes("aiSocketRef"), "console AI chat must not keep an AI WebSocket ref");
assert(!page.includes('new WebSocket(`${getWsBase()}/ai/chat'), "console AI chat must not construct an AI WebSocket");
assert(!page.includes("patient_id:"), "console AI chat must not send patient_id");
assert(!page.includes("debug:"), "console AI chat must not send caller-controlled debug");
assert(!page.includes("debug_trace"), "console AI chat must not expose graph debug traces");
assert(page.includes("connectPatientStream(ticket)"), "patient movement must use the shared ticketed WebSocket helper");
assert(page.includes("connectDoctorEpisodeMonitor(careEpisodeSnapshot, ticket)"), "doctor monitor must use the shared ticketed episode helper");
assert(!page.includes("/rehab/stream/"), "patient movement must not construct patient-id socket endpoints");
assert(!page.includes("/doctor/monitor/"), "doctor monitor must not construct legacy patient-id socket endpoints");
assert(page.includes("pendingSubmissionRef"), "console must retain the pending AI submission for retries");
assert(page.includes("setAiStatus(currentAuth?.role === \"patient\" ? \"Ready\" : \"Sign in as patient\")"), "AI status must derive from current saved auth");
assert(proxy.includes("REHAB_BACKEND_BASE"), "console browser AI chat route must proxy to the backend");
assert(proxy.includes("Authorization"), "console browser AI chat route must forward Authorization");
assert(!proxy.includes("console.log"), "console proxy must not log bearer tokens");

console.log("console AI chat transport contract ok");
