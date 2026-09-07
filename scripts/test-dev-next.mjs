import { existsSync, mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";

const repoRoot = resolve(import.meta.dirname, "..");

const withFakeNext = async (script, fn) => {
	const tempDir = mkdtempSync(join(tmpdir(), "dev-next-test-"));

	try {
		const binDir = join(tempDir, "node_modules", ".bin");
		mkdirSync(binDir, { recursive: true });
		writeFileSync(join(binDir, "next"), script, { mode: 0o755 });

		return await fn(tempDir);
	} finally {
		rmSync(tempDir, { recursive: true, force: true });
	}
};

const startWrapper = (tempDir, args = ["3999"]) => {
	const child = spawn(process.execPath, [join(repoRoot, "scripts", "dev-next.mjs"), ...args], {
		cwd: tempDir,
		env: {
			...process.env,
			DEV_NEXT_REPO_ROOT: tempDir,
		},
		stdio: ["ignore", "ignore", "pipe"],
	});

	let stderr = "";
	child.stderr.setEncoding("utf8");
	child.stderr.on("data", (chunk) => {
		stderr += chunk;
	});

	return { child, getStderr: () => stderr };
};

const stopWrapper = async (child) => {
	if (child.exitCode === null) {
		child.kill("SIGTERM");
		await new Promise((resolveExit) => child.once("exit", resolveExit));
	}
};

const assertUnexpectedCleanExitRestarts = async () => {
	const fakeNext = "#" + "!" + process.execPath + String.fromCharCode(10) +
		"const { existsSync, writeFileSync } = require(\"node:fs\");\n" +
		"const marker = \"./first-run-done\";\n" +
		"if (!existsSync(marker)) {\n" +
		"\twriteFileSync(marker, \"1\");\n" +
		"\tprocess.exit(0);\n" +
		"}\n" +
		"setInterval(() => {}, 1000);\n";

	await withFakeNext(fakeNext, async (tempDir) => {
		const { child, getStderr } = startWrapper(tempDir);

		try {
			await delay(1000);

			if (child.exitCode !== null) {
				throw new Error(`expected wrapper to keep running after clean child exit, exited ${child.exitCode}. stderr: ${getStderr()}`);
			}

			if (!existsSync(join(tempDir, "first-run-done"))) {
				throw new Error("expected fake Next to run once before restart");
			}

			if (!getStderr().includes("Next exited unexpectedly with code 0; restarting dev server")) {
				throw new Error(`expected restart diagnostic. stderr: ${getStderr()}`);
			}
		} finally {
			await stopWrapper(child);
		}
	});
};



const assertChildStdinStaysOpen = async () => {
	const fakeNext = "#" + "!" + process.execPath + String.fromCharCode(10) +
		"const { writeFileSync } = require(\"node:fs\");\n" +
		"process.stdin.resume();\n" +
		"process.stdin.on(\"end\", () => {\n" +
		"\twriteFileSync(\"./stdin-ended\", \"1\");\n" +
		"\tprocess.exit(0);\n" +
		"});\n" +
		"setInterval(() => {}, 1000);\n";

	await withFakeNext(fakeNext, async (tempDir) => {
		const { child, getStderr } = startWrapper(tempDir);

		try {
			await delay(1000);

			if (existsSync(join(tempDir, "stdin-ended"))) {
				throw new Error(`expected wrapper to keep child stdin open. stderr: ${getStderr()}`);
			}

			if (child.exitCode !== null) {
				throw new Error(`expected wrapper to keep running while child waits on stdin, exited ${child.exitCode}. stderr: ${getStderr()}`);
			}
		} finally {
			await stopWrapper(child);
		}
	});
};

const assertUnexpectedSignalRestarts = async () => {
	if (process.platform === "win32") return;

	const fakeNext = "#" + "!" + process.execPath + String.fromCharCode(10) +
		"const { existsSync, writeFileSync } = require(\"node:fs\");\n" +
		"const marker = \"./signal-run-done\";\n" +
		"if (!existsSync(marker)) {\n" +
		"\twriteFileSync(marker, \"1\");\n" +
		"\tprocess.kill(process.pid, \"SIGKILL\");\n" +
		"}\n" +
		"setInterval(() => {}, 1000);\n";

	await withFakeNext(fakeNext, async (tempDir) => {
		const { child, getStderr } = startWrapper(tempDir);

		try {
			await delay(1000);

			if (child.exitCode !== null) {
				throw new Error(`expected wrapper to keep running after child signal exit, exited ${child.exitCode}. stderr: ${getStderr()}`);
			}

			if (!existsSync(join(tempDir, "signal-run-done"))) {
				throw new Error("expected fake Next to run once before signal restart");
			}

			if (!getStderr().includes("Next exited from signal SIGKILL; restarting dev server")) {
				throw new Error(`expected signal restart diagnostic. stderr: ${getStderr()}`);
			}
		} finally {
			await stopWrapper(child);
		}
	});
};

const assertEnvFrontendPortIsUsedWhenNoCliPort = async () => {
	const fakeNext = "#" + "!" + process.execPath + String.fromCharCode(10) +
		"const { writeFileSync } = require(\"node:fs\");\n" +
		"writeFileSync(\"./next-args.json\", JSON.stringify(process.argv.slice(2)));\n" +
		"setInterval(() => {}, 1000);\n";

	await withFakeNext(fakeNext, async (tempDir) => {
		writeFileSync(join(tempDir, ".env"), "FRONTEND_PORT=4123\n");
		const { child, getStderr } = startWrapper(tempDir, []);

		try {
			await delay(500);
			const argsPath = join(tempDir, "next-args.json");
			if (!existsSync(argsPath)) {
				throw new Error(`expected fake Next to receive args. stderr: ${getStderr()}`);
			}

			const args = JSON.parse(readFileSync(argsPath, "utf8"));
			if (JSON.stringify(args) !== JSON.stringify(["dev", "-p", "4123"])) {
				throw new Error(`expected .env FRONTEND_PORT to set Next port, got ${JSON.stringify(args)}`);
			}
		} finally {
			await stopWrapper(child);
		}
	});
};

const assertSighupDoesNotStopDevServer = async () => {
	await withFakeNext("#" + "!" + process.execPath + String.fromCharCode(10) + "setInterval(() => {}, 1000);" + String.fromCharCode(10), async (tempDir) => {
		const { child, getStderr } = startWrapper(tempDir);

		try {
			await delay(500);
			process.kill(child.pid, "SIGHUP");
			await delay(500);

			if (child.exitCode !== null) {
				throw new Error(`expected wrapper to survive SIGHUP, exited ${child.exitCode}. stderr: ${getStderr()}`);
			}

			if (!getStderr().includes("Ignoring SIGHUP")) {
				throw new Error(`expected SIGHUP diagnostic. stderr: ${getStderr()}`);
			}
		} finally {
			await stopWrapper(child);
		}
	});
};

await assertUnexpectedCleanExitRestarts();
await assertChildStdinStaysOpen();
await assertUnexpectedSignalRestarts();
await assertEnvFrontendPortIsUsedWhenNoCliPort();
await assertSighupDoesNotStopDevServer();
