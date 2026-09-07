import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { tmpdir } from "node:os";
import { createRequire } from "node:module";

const repoRoot = resolve(import.meta.dirname, "..");
const require = createRequire(import.meta.url);
const {
  resolveBackendBase,
  resolveFrontendBackendPort,
  resolveFrontendPort,
  resolveFrontendPublicEnv,
} = require(join(repoRoot, "scripts", "port-config.cjs"));

const fixtureRoot = mkdtempSync(join(tmpdir(), "root-env-contract-"));
const productDir = join(fixtureRoot, "apps", "product");
const consoleDir = join(fixtureRoot, "apps", "console");
const backendDir = join(fixtureRoot, "backend");
mkdirSync(productDir, { recursive: true });
mkdirSync(consoleDir, { recursive: true });
mkdirSync(backendDir, { recursive: true });
writeFileSync(join(productDir, "package.json"), JSON.stringify({ name: "@rehab/product" }));
writeFileSync(join(consoleDir, "package.json"), JSON.stringify({ name: "@rehab/console" }));

const envKeys = [
  "BACKEND_PORT",
  "NEXT_PUBLIC_BACKEND_PORT",
  "FRONTEND_PORT",
  "PRODUCT_FRONTEND_PORT",
  "CONSOLE_FRONTEND_PORT",
  "REHAB_BACKEND_BASE",
  "NEXT_PUBLIC_API_BASE",
  "NEXT_PUBLIC_WS_BASE",
];
const withEnv = async (values, fn) => {
  const previous = Object.fromEntries(envKeys.map((key) => [key, process.env[key]]));
  for (const key of envKeys) delete process.env[key];
  Object.assign(process.env, values);
  try {
    return await fn();
  } finally {
    for (const key of envKeys) {
      if (previous[key] === undefined) delete process.env[key];
      else process.env[key] = previous[key];
    }
  }
};

try {
  writeFileSync(
    join(fixtureRoot, ".env"),
    [
      "BACKEND_PORT=8100",
      "PRODUCT_FRONTEND_PORT=3100",
      "CONSOLE_FRONTEND_PORT=3101",
      "REHAB_BACKEND_BASE=https://root-backend.test/api",
      "NEXT_PUBLIC_API_BASE=https://root-api.test",
      "NEXT_PUBLIC_WS_BASE=wss://root-ws.test",
      "DATABASE_URL=postgresql://root-secret-must-not-be-public",
      "",
    ].join("\n"),
  );
  writeFileSync(
    join(backendDir, ".env"),
    [
      "BACKEND_PORT=8200",
      "PRODUCT_FRONTEND_PORT=3200",
      "FRONTEND_PORT=3202",
      "REHAB_BACKEND_BASE=https://legacy-backend.test",
      "",
    ].join("\n"),
  );
  writeFileSync(join(productDir, ".env.local"), "FRONTEND_PORT=3300\nPRODUCT_FRONTEND_PORT=3200\nBACKEND_PORT=8200\nREHAB_BACKEND_BASE=https://legacy-backend.test\nNEXT_PUBLIC_API_BASE=https://legacy-api.test\nNEXT_PUBLIC_WS_BASE=wss://legacy-ws.test\n");
  writeFileSync(join(consoleDir, ".env"), "FRONTEND_PORT=3301\n");

  await withEnv({}, async () => {
    assert.equal(resolveFrontendBackendPort({ cwd: productDir, rootDir: fixtureRoot }), "8100");
    assert.equal(resolveFrontendPort({ cwd: productDir, rootDir: fixtureRoot }), "3100");
    assert.equal(resolveFrontendPort({ cwd: consoleDir, rootDir: fixtureRoot }), "3101");
    assert.equal(resolveBackendBase({ cwd: productDir, rootDir: fixtureRoot }), "https://root-backend.test/api");
    const publicEnv = resolveFrontendPublicEnv({ cwd: productDir, rootDir: fixtureRoot });
    assert.deepEqual(Object.keys(publicEnv).sort(), [
      "BACKEND_PORT",
      "NEXT_PUBLIC_API_BASE",
      "NEXT_PUBLIC_BACKEND_PORT",
      "NEXT_PUBLIC_WS_BASE",
      "REHAB_BACKEND_BASE",
    ]);
    assert.equal(publicEnv.NEXT_PUBLIC_API_BASE, "https://root-api.test");
    assert.equal(publicEnv.NEXT_PUBLIC_WS_BASE, "wss://root-ws.test");
    assert.equal("DATABASE_URL" in publicEnv, false);
  });

  rmSync(join(fixtureRoot, ".env"));
  writeFileSync(join(fixtureRoot, ".env"), "PRODUCT_FRONTEND_PORT=3110\n");
  await withEnv({}, async () => {
    assert.equal(resolveFrontendPort({ cwd: productDir, rootDir: fixtureRoot }), "3110");
    assert.equal(resolveFrontendPort({ cwd: consoleDir, rootDir: fixtureRoot }), "3301");
    assert.equal(resolveFrontendBackendPort({ cwd: productDir, rootDir: fixtureRoot }), "8200");
    assert.equal(resolveBackendBase({ cwd: productDir, rootDir: fixtureRoot }), "https://legacy-backend.test");
  });


  await withEnv({
    BACKEND_PORT: "8400",
    FRONTEND_PORT: "3500",
    PRODUCT_FRONTEND_PORT: "3501",
    REHAB_BACKEND_BASE: "http://process-backend.test/",
    NEXT_PUBLIC_API_BASE: "https://process-api.test",
    NEXT_PUBLIC_WS_BASE: "wss://process-ws.test",
  }, async () => {
    assert.equal(resolveFrontendPort({ cwd: productDir, rootDir: fixtureRoot }), "3500");
    assert.equal(resolveFrontendBackendPort({ cwd: productDir, rootDir: fixtureRoot }), "8400");
    assert.equal(resolveBackendBase({ cwd: productDir, rootDir: fixtureRoot }), "http://process-backend.test");
    const publicEnv = resolveFrontendPublicEnv({ cwd: productDir, rootDir: fixtureRoot });
    assert.equal(publicEnv.NEXT_PUBLIC_API_BASE, "https://process-api.test");
    assert.equal(publicEnv.NEXT_PUBLIC_WS_BASE, "wss://process-ws.test");
  });

  await withEnv({
    BACKEND_PORT: "8200",
    FRONTEND_PORT: "3300",
    REHAB_BACKEND_BASE: "https://legacy-backend.test",
  }, async () => {
    assert.equal(resolveFrontendPort({ cwd: productDir, rootDir: fixtureRoot }), "3300");
    assert.equal(resolveFrontendBackendPort({ cwd: productDir, rootDir: fixtureRoot }), "8200");
    assert.equal(resolveBackendBase({ cwd: productDir, rootDir: fixtureRoot }), "https://legacy-backend.test");
  });

  writeFileSync(
    join(fixtureRoot, ".env"),
    [
      "BACKEND_PORT=8100",
      "PRODUCT_FRONTEND_PORT=3100",
      "CONSOLE_FRONTEND_PORT=3101",
      "REHAB_BACKEND_BASE=https://root-backend.test/api",
      "NEXT_PUBLIC_API_BASE=https://root-api.test",
      "NEXT_PUBLIC_WS_BASE=wss://root-ws.test",
      "",
    ].join("\n"),
  );

  const { loadEnvConfig } = require("@next/env");
  await withEnv({}, async () => {
    loadEnvConfig(productDir, true, console, true);
    assert.equal(resolveFrontendPort({ cwd: productDir, rootDir: fixtureRoot, nextEnvPreload: true }), "3100");
    assert.equal(resolveFrontendBackendPort({ cwd: productDir, rootDir: fixtureRoot, nextEnvPreload: true }), "8100");
    assert.equal(resolveBackendBase({ cwd: productDir, rootDir: fixtureRoot, nextEnvPreload: true }), "https://root-backend.test/api");
  });

  console.log("root dotenv precedence and public transport contract ok");
} finally {
  rmSync(fixtureRoot, { recursive: true, force: true });
}
