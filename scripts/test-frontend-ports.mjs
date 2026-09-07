import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { spawn } from "node:child_process";
import { tmpdir } from "node:os";

const repoRoot = resolve(import.meta.dirname, "..");
const devWrapper = join(repoRoot, "scripts", "dev-next.mjs");
const startWrapper = join(repoRoot, "scripts", "start-next.mjs");

const waitForCapture = async (captureFile) => {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    if (existsSync(captureFile)) return JSON.parse(readFileSync(captureFile, "utf8"));
    await new Promise((resolveWait) => setTimeout(resolveWait, 50));
  }
  throw new Error("fake Next did not write its argument capture");
};

const run = async ({
  mode,
  packageName,
  rootEnv = "",
  legacyLocalEnv = "",
  legacyEnv = "",
  args = [],
  extraEnv = {},
}) => {
  const fixtureRoot = mkdtempSync(join(tmpdir(), "frontend-port-test-"));
  const appDir = join(fixtureRoot, "apps", packageName === "@rehab/console" ? "console" : "product");
  const binDir = join(fixtureRoot, "node_modules", ".bin");
  const captureFile = join(appDir, "capture.json");
  mkdirSync(appDir, { recursive: true });
  mkdirSync(binDir, { recursive: true });
  writeFileSync(join(fixtureRoot, ".env"), rootEnv);
  writeFileSync(join(appDir, "package.json"), JSON.stringify({ name: packageName }));
  writeFileSync(join(appDir, ".env.local"), legacyLocalEnv);
  writeFileSync(join(appDir, ".env"), legacyEnv);
  writeFileSync(
    join(binDir, "next"),
    "#!" + process.execPath + String.fromCharCode(10) +
    "const { writeFileSync } = require('node:fs');\n" +
    "writeFileSync(process.env.CAPTURE_FILE, JSON.stringify(process.argv.slice(2)));\n" +
    "if (process.argv[2] === 'dev') setInterval(() => {}, 1000);\n",
    { mode: 0o755 },
  );
  const env = {
    ...process.env,
    DEV_NEXT_REPO_ROOT: fixtureRoot,
    CAPTURE_FILE: captureFile,
    ...extraEnv,
  };
  for (const key of ["FRONTEND_PORT", "PRODUCT_FRONTEND_PORT", "CONSOLE_FRONTEND_PORT"]) {
    if (!Object.prototype.hasOwnProperty.call(extraEnv, key)) delete env[key];
  }
  const wrapper = mode === "dev" ? devWrapper : startWrapper;
  const child = spawn(process.execPath, [wrapper, ...args], { cwd: appDir, env, stdio: "ignore" });
  try {
    const captured = await waitForCapture(captureFile);
    if (mode === "dev") {
      child.kill("SIGTERM");
      await new Promise((resolveExit) => {
        const timer = setTimeout(() => {
          if (child.exitCode === null) child.kill("SIGKILL");
          resolveExit();
        }, 500);
        child.once("exit", () => {
          clearTimeout(timer);
          resolveExit();
        });
      });
    } else {
      const exitCode = child.exitCode ?? await new Promise((resolveExit) => child.once("exit", resolveExit));
      assert.equal(exitCode, 0);
    }
    return captured;
  } finally {
    if (child.exitCode === null) child.kill("SIGTERM");
    rmSync(fixtureRoot, { recursive: true, force: true });
  }
};

assert.deepEqual(
  await run({ mode: "dev", packageName: "@rehab/product", rootEnv: "PRODUCT_FRONTEND_PORT=4123\n" }),
  ["dev", "-p", "4123"],
);
assert.deepEqual(
  await run({ mode: "dev", packageName: "@rehab/console", rootEnv: "CONSOLE_FRONTEND_PORT=4124\n" }),
  ["dev", "-p", "4124"],
);
assert.deepEqual(
  await run({
    mode: "dev",
    packageName: "@rehab/product",
    rootEnv: "# partial root\n",
    legacyLocalEnv: "FRONTEND_PORT=4125\n",
  }),
  ["dev", "-p", "4125"],
);
assert.deepEqual(
  await run({
    mode: "dev",
    packageName: "@rehab/product",
    rootEnv: "PRODUCT_FRONTEND_PORT=4126\n",
    legacyLocalEnv: "FRONTEND_PORT=4127\n",
  }),
  ["dev", "-p", "4126"],
);
assert.deepEqual(
  await run({
    mode: "dev",
    packageName: "@rehab/product",
    rootEnv: "PRODUCT_FRONTEND_PORT=4128\n",
    extraEnv: { PRODUCT_FRONTEND_PORT: "4129" },
  }),
  ["dev", "-p", "4129"],
);
assert.deepEqual(
  await run({
    mode: "dev",
    packageName: "@rehab/product",
    rootEnv: "PRODUCT_FRONTEND_PORT=4130\n",
    extraEnv: { FRONTEND_PORT: "4131" },
    args: ["4132"],
  }),
  ["dev", "-p", "4132"],
);
assert.deepEqual(
  await run({ mode: "start", packageName: "@rehab/product", rootEnv: "PRODUCT_FRONTEND_PORT=4133\n" }),
  ["start", "-p", "4133"],
);
assert.deepEqual(
  await run({
    mode: "start",
    packageName: "@rehab/console",
    rootEnv: "CONSOLE_FRONTEND_PORT=4134\n",
    extraEnv: { FRONTEND_PORT: "4135" },
  }),
  ["start", "-p", "4135"],
);
assert.deepEqual(
  await run({
    mode: "start",
    packageName: "@rehab/console",
    rootEnv: "# no app-specific port\n",
    legacyEnv: "FRONTEND_PORT=4136\n",
  }),
  ["start", "-p", "4136"],
);
assert.deepEqual(
  await run({ mode: "start", packageName: "@rehab/console", rootEnv: "" }),
  ["start", "-p", "3001"],
);
console.log("frontend root-env launcher contract ok");
