#!/usr/bin/env node
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { tmpdir } from "node:os";

const repoRoot = resolve(new URL("../../..", import.meta.url).pathname);
const routeSource = readFileSync(resolve(repoRoot, "apps/console/app/ai-chat/[session_id]/route.ts"), "utf8");
assert(routeSource.includes("REHAB_BACKEND_BASE"), "console proxy should support REHAB_BACKEND_BASE");
assert(routeSource.includes("Authorization"), "console proxy should forward Authorization");
assert(!routeSource.includes("console.log"), "console proxy must not log tokens");
assert(!routeSource.includes("logger."), "console proxy must not log tokens");

const previousBackendPort = process.env.BACKEND_PORT;
const previousBackendPortAlias = process.env.NEXT_PUBLIC_BACKEND_PORT;
delete process.env.REHAB_BACKEND_BASE;
delete process.env.BACKEND_PORT;
delete process.env.NEXT_PUBLIC_BACKEND_PORT;
const tempRoot = mkdtempSync(join(tmpdir(), "rehab-console-ai-chat-proxy-"));
writeFileSync(join(tempRoot, "package.json"), JSON.stringify({ type: "module" }));
try {
  const typescript = await import(pathToFileURL(resolve(repoRoot, "node_modules/typescript/lib/typescript.js")).href);
  const source = typescript.default.transpileModule(routeSource, {
    compilerOptions: {
      module: typescript.default.ModuleKind.ESNext,
      target: typescript.default.ScriptTarget.ES2022,
    },
  }).outputText;
  const modulePath = join(tempRoot, "route.mjs");
  writeFileSync(modulePath, source);

  const captured = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input, init = {}) => {
    captured.push({ input, init });
    return new Response("backend body", {
      status: 207,
      headers: { "Content-Type": "application/problem+json" },
    });
  };

  try {
    const route = await import(pathToFileURL(modulePath).href + "?console-proxy");
    const missing = await route.POST(
      new Request("http://console.test/ai-chat/new", { method: "POST", body: "{}" }),
      { params: Promise.resolve({ session_id: "new" }) },
    );
    assert.equal(missing.status, 401);
    assert.equal(captured.length, 0, "missing Authorization must not reach the backend");

    const response = await route.POST(
      new Request("http://console.test/ai-chat/session/one", {
        method: "POST",
        headers: {
          Authorization: "Bearer console-token",
          "Content-Type": "application/json",
        },
        body: "exact body",
      }),
      { params: Promise.resolve({ session_id: "session/one" }) },
    );
    assert.equal(response.status, 207);
    assert.equal(await response.text(), "backend body");
    assert.equal(response.headers.get("Content-Type"), "application/problem+json");
    assert.equal(captured[0].input, "http://127.0.0.1:8000/ai/chat/session%2Fone");
    assert.equal(captured[0].init.headers.Authorization, "Bearer console-token");
    assert.equal(captured[0].init.body, "exact body");
  } finally {
    globalThis.fetch = originalFetch;
  }
  console.log("console AI chat proxy runtime contract ok");
} finally {
  if (previousBackendPort === undefined) delete process.env.BACKEND_PORT;
  else process.env.BACKEND_PORT = previousBackendPort;
  if (previousBackendPortAlias === undefined) delete process.env.NEXT_PUBLIC_BACKEND_PORT;
  else process.env.NEXT_PUBLIC_BACKEND_PORT = previousBackendPortAlias;
  rmSync(tempRoot, { recursive: true, force: true });
}
