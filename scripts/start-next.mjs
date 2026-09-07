import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { spawn } from "node:child_process";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { resolveFrontendPort } = require("./port-config.cjs");
const repoRoot = process.env.DEV_NEXT_REPO_ROOT
	? resolve(process.env.DEV_NEXT_REPO_ROOT)
	: resolve(import.meta.dirname, "..");
const nextBin = resolve(repoRoot, "node_modules", ".bin", process.platform === "win32" ? "next.cmd" : "next");
const port = resolveFrontendPort({ cwd: process.cwd(), rootDir: repoRoot, cliValue: process.argv[2] });

if (!existsSync(nextBin)) {
	console.error("Next binary not found at " + nextBin + ". Run npm install from the repo root.");
	process.exit(127);
}

const child = spawn(nextBin, ["start", "-p", port], {
	cwd: process.cwd(),
	env: { ...process.env, NEXT_TELEMETRY_DISABLED: "1" },
	stdio: "inherit",
});

child.on("error", (error) => {
	console.error("[start-next] Failed to start Next: " + error.message);
	process.exit(1);
});

child.on("exit", (code, signal) => {
	if (signal) process.exit(1);
	process.exit(code ?? 1);
});
