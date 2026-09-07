import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { spawn } from "node:child_process";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { resolveBackendPort } = require("./port-config.cjs");
const repoRoot = process.env.DEV_BACKEND_REPO_ROOT ? resolve(process.env.DEV_BACKEND_REPO_ROOT) : resolve(import.meta.dirname, "..");
const backendDir = resolve(repoRoot, "backend");
const configuredOverrideEnvFile = process.env.BACKEND_ENV_FILE?.trim() || "";
const overrideEnvFile = configuredOverrideEnvFile ? resolve(repoRoot, configuredOverrideEnvFile) : "";
const envFile = overrideEnvFile || resolve(backendDir, ".env");
const port = resolveBackendPort({
	cwd: backendDir,
	rootDir: repoRoot,
	envFile,
	overrideEnvFile,
	cliValue: process.argv[2],
});
const python = process.env.BACKEND_PYTHON?.trim() ||
	(existsSync(resolve(backendDir, ".venv", "bin", "python"))
		? resolve(backendDir, ".venv", "bin", "python")
		: "python");

const child = spawn(
	python,
	[
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
	],
	{
		cwd: backendDir,
		env: {
			...process.env,
			BACKEND_PORT: port,
			...(overrideEnvFile ? { BACKEND_ENV_FILE: overrideEnvFile } : {}),
		},
		stdio: "inherit",
	},
);

for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"]) {
	process.on(signal, () => child.kill(signal));
}

child.on("error", (error) => {
	console.error("[dev-backend] Failed to start backend: " + error.message);
	process.exit(1);
});

child.on("exit", (code, signal) => {
	if (signal) process.exit(1);
	process.exit(code ?? 1);
});
