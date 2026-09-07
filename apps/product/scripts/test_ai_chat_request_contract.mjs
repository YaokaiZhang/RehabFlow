#!/usr/bin/env node
import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(scriptDir, "../../..");
const api = fs.readFileSync(resolve(repoRoot, "packages/shared/src/api.ts"), "utf8");
const page = fs.readFileSync(resolve(repoRoot, "apps/product/app/page.tsx"), "utf8");

const inputStart = api.indexOf("export type AiChatTurnInput");
const inputEnd = api.indexOf("export type AiChatTurnResponse");
assert(inputStart >= 0 && inputEnd > inputStart, "shared API should define the AI chat input contract");
const inputType = api.slice(inputStart, inputEnd);

assert(inputType.includes("message: string"), "AI chat input should require a message");
assert(inputType.includes("care_episode_id?: string"), "AI chat input should allow an optional Care Episode");
assert(inputType.includes("idempotency_key: string"), "AI chat input should require an idempotency key");
assert(!inputType.includes("patient_id"), "AI chat input must not accept a patient_id");
assert(!inputType.includes("debug"), "AI chat input must not accept caller-controlled debug");

assert(api.includes("accessToken: string"), "sendAiChatTurn should require an access token");
assert(api.includes("Authorization:") && api.includes("Bearer " + "$" + "{accessToken}"), "sendAiChatTurn should send the bearer token");
assert(api.includes("JSON.stringify({"), "sendAiChatTurn should build a reduced request payload");
assert(api.includes("idempotency_key: input.idempotency_key"), "sendAiChatTurn should forward the idempotency key");

const turnStart = api.indexOf("export async function sendAiChatTurn");
const turnEnd = api.indexOf("export async function registerPatient");
assert(turnStart >= 0 && turnEnd > turnStart, "shared API should define the AI chat request function");
const turnFunction = api.slice(turnStart, turnEnd);
assert(!turnFunction.includes("timeoutMs"), "AI chat should not define a client-side timeout");
assert(turnFunction.includes("}, null);"), "AI chat should disable the fetch timeout");

assert(page.includes("loadAuth"), "AI chat page should load browser auth before sending");
assert(page.includes('auth.role !== "patient"'), "AI chat page should require a patient role");
assert(page.includes("auth.access_token"), "AI chat page should pass the authenticated access token");
assert(page.includes("crypto.randomUUID()"), "AI chat page should generate an idempotency key per submission");
assert(page.includes("idempotencyKey"), "AI chat page should retain the submission idempotency key for retries");
assert(page.includes("pendingSubmissionRef"), "AI chat page should retain pending submission state across retries");
assert(!page.includes("patient_id:"), "AI chat page must not send patient_id");
assert(!page.includes("debug:"), "AI chat page must not send caller-controlled debug");

console.log("AI chat request contract ok");
