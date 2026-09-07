#!/usr/bin/env node
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { tmpdir } from "node:os";

const repoRoot = resolve(new URL("../../..", import.meta.url).pathname);
process.env.REHAB_BACKEND_BASE = "http://backend.test";

const tempRoot = mkdtempSync(join(tmpdir(), "rehab-ai-chat-runtime-"));
writeFileSync(join(tempRoot, "package.json"), JSON.stringify({ type: "module" }));
const typescript = await import(pathToFileURL(resolve(repoRoot, "node_modules/typescript/lib/typescript.js")).href);
const transpile = (source) => typescript.default.transpileModule(source, {
  compilerOptions: { module: typescript.default.ModuleKind.ESNext, target: typescript.default.ScriptTarget.ES2022 },
}).outputText;
const readSource = (relativePath) => readFileSync(resolve(repoRoot, relativePath), "utf8");
const writeModule = (name, source) => writeFileSync(join(tempRoot, name), transpile(source).replaceAll('from "./auth"', 'from "./auth.mjs"').replaceAll('from "./runtime"', 'from "./runtime.mjs"'));

writeModule("route.mjs", readSource("apps/product/app/ai-chat/[session_id]/route.ts"));
writeModule("api.mjs", readSource("packages/shared/src/api.ts"));
writeModule("runtime.mjs", readSource("packages/shared/src/runtime.ts"));
writeModule("auth.mjs", readSource("packages/shared/src/auth.ts"));

globalThis.window = {};
const capturedRequests = [];
const originalFetch = globalThis.fetch;
globalThis.fetch = async (input, init = {}) => {
  capturedRequests.push({ input, init });
  return new Response(JSON.stringify({ event: "response", response: "ok" }), {
    status: 207,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
};

try {
  const route = await import(pathToFileURL(join(tempRoot, "route.mjs")).href + "?route");
  const sharedApi = await import(pathToFileURL(join(tempRoot, "api.mjs")).href + "?api");

  const missingAuthResponse = await route.POST(
    new Request("http://product.test/ai-chat/new", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: "hello" }),
    }),
    { params: Promise.resolve({ session_id: "new" }) },
  );
  assert.equal(missingAuthResponse.status, 401, "missing Authorization should return 401");
  assert.equal(capturedRequests.length, 0, "missing Authorization should not call backend fetch");

  const proxyBody = JSON.stringify({
    message: "proxy body",
    care_episode_id: "episode-1",
    idempotency_key: "key-proxy",
  });
  const proxyResponse = await route.POST(
    new Request("http://product.test/ai-chat/session/one", {
      method: "POST",
      headers: {
        Authorization: "Bearer proxy-token",
        "Content-Type": "application/json",
      },
      body: proxyBody,
    }),
    { params: Promise.resolve({ session_id: "session/one" }) },
  );
  assert.equal(proxyResponse.status, 207, "proxy should preserve backend status");
  assert.equal(await proxyResponse.text(), JSON.stringify({ event: "response", response: "ok" }), "proxy should preserve backend body");
  assert.equal(capturedRequests.length, 1, "authorized proxy request should call backend fetch once");
  assert.equal(capturedRequests[0].input, "http://backend.test/ai/chat/session%2Fone", "proxy should encode the session id");
  assert.equal(capturedRequests[0].init.headers.Authorization, "Bearer proxy-token", "proxy should forward the exact bearer header");
  assert.equal(capturedRequests[0].init.body, proxyBody, "proxy should forward the exact request body");

  capturedRequests.length = 0;
  const sharedResponse = await sharedApi.sendAiChatTurn(
    "session one",
    { message: "shared body", care_episode_id: "episode-2", idempotency_key: "key-shared" },
    "shared-token",
  );
  assert.equal(sharedResponse.response, "ok", "shared client should parse the backend response");
  assert.equal(capturedRequests.length, 1, "shared client should issue one request");
  assert.equal(capturedRequests[0].input, "/ai-chat/session%20one", "shared client should use the browser proxy route");
  assert.equal(capturedRequests[0].init.headers.Authorization, "Bearer shared-token", "shared client should send the exact bearer header");
  assert.equal(
    capturedRequests[0].init.body,
    JSON.stringify({ message: "shared body", care_episode_id: "episode-2", idempotency_key: "key-shared" }),
    "shared client should send the exact reduced body",
  );
  assert.equal(capturedRequests[0].init.signal, undefined, "shared client should not install an abort signal when no timeout is configured");

  console.log("AI chat proxy runtime contract ok");
} finally {
  globalThis.fetch = originalFetch;
  rmSync(tempRoot, { recursive: true, force: true });
}
