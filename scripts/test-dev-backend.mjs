import assert from "node:assert/strict";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { spawn } from "node:child_process";
import { tmpdir } from "node:os";

const repoRoot = resolve(import.meta.dirname, "..");
const tempDir = mkdtempSync(join(tmpdir(), "dev-backend-test-"));
const backendDir = join(tempDir, "backend");
const fakePython = join(tempDir, "fake-python");
const overrideEnvFile = join(tempDir, "override.env");
const captureFile = join(tempDir, "capture.json");
const launcher = join(repoRoot, "scripts", "dev-backend.mjs");
mkdirSync(backendDir, { recursive: true });

writeFileSync(
  fakePython,
  "#!" + process.execPath + String.fromCharCode(10) +
  "const { writeFileSync } = require('node:fs');\n" +
  "writeFileSync(process.env.CAPTURE_FILE, JSON.stringify({ args: process.argv.slice(2), cwd: process.cwd() }));\n",
  { mode: 0o755 },
);
chmodSync(fakePython, 0o755);

const run = async ({
  args = [],
  extraEnv = {},
  rootContents = "# root has no backend port\n",
  legacyContents = "# legacy has no backend port\n",
  overrideContents,
} = {}) => {
  writeFileSync(join(tempDir, ".env"), rootContents);
  writeFileSync(join(backendDir, ".env"), legacyContents);
  if (overrideContents !== undefined) writeFileSync(overrideEnvFile, overrideContents);
  rmSync(captureFile, { force: true });
  const env = {
    ...process.env,
    DEV_BACKEND_REPO_ROOT: tempDir,
    BACKEND_PYTHON: fakePython,
    CAPTURE_FILE: captureFile,
    ...extraEnv,
  };
  if (!Object.prototype.hasOwnProperty.call(extraEnv, "BACKEND_PORT")) delete env.BACKEND_PORT;
  if (!Object.prototype.hasOwnProperty.call(extraEnv, "BACKEND_ENV_FILE")) delete env.BACKEND_ENV_FILE;
  const child = spawn(process.execPath, [launcher, ...args], { cwd: repoRoot, env, stdio: "inherit" });
  const exitCode = await new Promise((resolveExit, reject) => {
    child.once("error", reject);
    child.once("exit", (code) => resolveExit(code));
  });
  assert.equal(exitCode, 0);
  assert.ok(existsSync(captureFile), "fake backend should capture launcher arguments");
  return JSON.parse(readFileSync(captureFile, "utf8"));
};

const expectedArgs = (port) => [
  "-m",
  "uvicorn",
  "app.main:app",
  "--host",
  "127.0.0.1",
  "--port",
  port,
  "--loop",
  "asyncio",
  "--http",
  "h11",
];

try {
  assert.deepEqual(
    (await run({ rootContents: "BACKEND_PORT=8123\n" })).args,
    expectedArgs("8123"),
    "backend launcher should load a root-only backend port",
  );
  assert.deepEqual(
    (await run({ legacyContents: "BACKEND_PORT=8133\n" })).args,
    expectedArgs("8133"),
    "backend launcher should fall back to backend/.env",
  );
  assert.deepEqual(
    (await run({ rootContents: "BACKEND_PORT=8143\n", legacyContents: "BACKEND_PORT=8144\n" })).args,
    expectedArgs("8143"),
    "root backend dotenv should beat the legacy backend dotenv",
  );
  assert.deepEqual(
    (await run({ rootContents: "BACKEND_PORT=8153\n", extraEnv: { BACKEND_PORT: "8222" } })).args,
    expectedArgs("8222"),
    "process env should override root dotenv",
  );
  assert.deepEqual(
    (await run({ rootContents: "BACKEND_PORT=8163\n", extraEnv: { BACKEND_PORT: "8222" }, args: ["8333"] })).args,
    expectedArgs("8333"),
    "CLI should override process env",
  );
  assert.deepEqual(
    (await run({
      rootContents: "BACKEND_PORT=8180\n",
      extraEnv: { BACKEND_ENV_FILE: overrideEnvFile },
      overrideContents: "BACKEND_PORT=8173\n",
    })).args,
    expectedArgs("8173"),
    "BACKEND_ENV_FILE should override root backend dotenv",
  );
  assert.deepEqual(
    (await run({
      rootContents: "BACKEND_PORT=8180\n",
      extraEnv: { BACKEND_ENV_FILE: "override.env" },
      overrideContents: "BACKEND_PORT=8174\n",
    })).args,
    expectedArgs("8174"),
    "relative BACKEND_ENV_FILE should remain valid for the backend child",
  );
  assert.deepEqual(
    (await run({
      rootContents: "BACKEND_PORT=8180\n",
      extraEnv: { BACKEND_ENV_FILE: overrideEnvFile, BACKEND_PORT: "8222" },
      overrideContents: "BACKEND_PORT=8173\n",
    })).args,
    expectedArgs("8222"),
    "process backend port should override BACKEND_ENV_FILE",
  );
  assert.deepEqual(
    (await run({})).args,
    expectedArgs("8000"),
    "backend default should remain 8000",
  );
  console.log("backend root-env launcher contract ok");
} finally {
  rmSync(tempDir, { recursive: true, force: true });
}
