#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFileSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { tmpdir } from "node:os";
import { pathToFileURL } from "node:url";

const repoRoot = resolve(new URL("../../..", import.meta.url).pathname);
const tempRoot = mkdtempSync(join(tmpdir(), "rehab-ai-chat-retry-"));
try {
  const typescript = await import(pathToFileURL(resolve(repoRoot, "node_modules/typescript/lib/typescript.js")).href);
  const source = readFileSync(resolve(repoRoot, "apps/console/app/ai-chat-submission.ts"), "utf8");
  const output = typescript.default.transpileModule(source, {
    compilerOptions: {
      module: typescript.default.ModuleKind.ESNext,
      target: typescript.default.ScriptTarget.ES2022,
    },
  }).outputText;
  const modulePath = join(tempRoot, "submission.mjs");
  writeFileSync(modulePath, output);
  const { resolveAiChatSubmission } = await import(pathToFileURL(modulePath).href);

  const first = resolveAiChatSubmission(null, "  knee stiffness  ", "  ", () => "key-1");
  assert.deepEqual(first.submission, {
    message: "knee stiffness",
    sessionId: "new",
    idempotencyKey: "key-1",
  });
  assert.equal(first.isRetry, false);

  const retry = resolveAiChatSubmission(first.submission, "knee stiffness", "new", () => "key-2");
  assert.equal(retry.isRetry, true, "same message/session should be a retry");
  assert.equal(retry.submission.idempotencyKey, "key-1", "same-submission retry must reuse its key");

  const changedMessage = resolveAiChatSubmission(first.submission, "different message", "new", () => "key-3");
  assert.equal(changedMessage.isRetry, false);
  assert.equal(changedMessage.submission.idempotencyKey, "key-3", "changed message must get a new key");

  const changedSession = resolveAiChatSubmission(first.submission, "knee stiffness", "session-2", () => "key-4");
  assert.equal(changedSession.isRetry, false);
  assert.equal(changedSession.submission.idempotencyKey, "key-4", "changed session must get a new key");

  console.log("console AI chat retry contract ok");
} finally {
  rmSync(tempRoot, { recursive: true, force: true });
}
