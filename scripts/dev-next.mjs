import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { resolveFrontendPort } = require("./port-config.cjs");

const scriptDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = process.env.DEV_NEXT_REPO_ROOT ? resolve(process.env.DEV_NEXT_REPO_ROOT) : resolve(scriptDir, "..");
const port = resolveFrontendPort({ cwd: process.cwd(), rootDir: repoRoot, cliValue: process.argv[2] });
const nextBin = resolve(repoRoot, "node_modules", ".bin", process.platform === "win32" ? "next.cmd" : "next");
const restartWindowMs = 10000;
const maxUnexpectedRestarts = 3;

if (!existsSync(nextBin)) {
	console.error(`Next binary not found at ${nextBin}. Run npm install from the repo root.`);
	process.exit(127);
}

let child = null;
let stopping = false;
let stopSignal = null;
let unexpectedRestartTimes = [];

const signalChild = (signal) => {
	if (!child || child.killed || child.pid === undefined) return;

	try {
		if (process.platform === "win32") {
			child.kill(signal);
		} else {
			process.kill(-child.pid, signal);
		}
	} catch (error) {
		if (error.code !== "ESRCH") throw error;
	}
};

const canRestartUnexpectedExit = () => {
	const now = Date.now();
	unexpectedRestartTimes = unexpectedRestartTimes.filter((time) => now - time < restartWindowMs);

	if (unexpectedRestartTimes.length >= maxUnexpectedRestarts) {
		return false;
	}

	unexpectedRestartTimes.push(now);
	return true;
};

const restartUnexpectedExit = (description) => {
	if (!canRestartUnexpectedExit()) {
		console.error(`[dev-next] Next ${description} ${maxUnexpectedRestarts} times in ${restartWindowMs / 1000}s; not restarting again.`);
		process.exit(1);
	}

	console.error(`[dev-next] Next ${description}; restarting dev server.`);
	startNext();
};

const startNext = () => {
	child = spawn(nextBin, ["dev", "-p", port], {
		cwd: process.cwd(),
		detached: process.platform !== "win32",
		env: {
			...process.env,
			NEXT_TELEMETRY_DISABLED: "1",
		},
		stdio: ["pipe", "inherit", "inherit"],
	});

	child.on("error", (error) => {
		console.error(`[dev-next] Failed to start Next: ${error.message}`);
		process.exit(1);
	});

	child.on("exit", (code, signal) => {
		if (signal) {
			if (stopping) {
				console.error(`[dev-next] Next exited from signal ${signal}.`);
				process.exit(0);
			}

			restartUnexpectedExit(`exited from signal ${signal}`);
			return;
		}

		if (stopping) {
			process.exit(code ?? (stopSignal ? 0 : 1));
		}

		if (code === 0) {
			restartUnexpectedExit("exited unexpectedly with code 0");
			return;
		}

		console.error(`[dev-next] Next exited with code ${code}.`);
		process.exit(code ?? 1);
	});
};

const stop = (signal) => {
	if (stopping) return;
	stopping = true;
	stopSignal = signal;
	signalChild(signal);
};

process.on("SIGINT", () => stop("SIGINT"));
process.on("SIGTERM", () => stop("SIGTERM"));
process.on("SIGHUP", () => {
	console.error("[dev-next] Ignoring SIGHUP; use Ctrl-C or SIGTERM to stop the dev server.");
});
process.on("exit", () => {
	if (!stopping) signalChild("SIGTERM");
});

startNext();
