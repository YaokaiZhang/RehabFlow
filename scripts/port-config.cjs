const fs = require("node:fs");
const path = require("node:path");

const DEFAULT_BACKEND_PORT = "8000";
const DEFAULT_PRODUCT_FRONTEND_PORT = "3000";
const DEFAULT_CONSOLE_FRONTEND_PORT = "3001";

function clean(value) {
	if (typeof value !== "string") return "";
	return value.trim().replace(/^['"]|['"]$/g, "").trim();
}

function parseDotenv(content) {
	const values = {};
	for (const line of content.split(/\r?\n/)) {
		const match = line.match(/^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/);
		if (match) values[match[1]] = clean(match[2]);
	}
	return values;
}

function readDotenvFile(filePath) {
	if (!filePath || !fs.existsSync(filePath)) return {};
	return parseDotenv(fs.readFileSync(filePath, "utf8"));
}

function resolveRootDir(cwd = process.cwd(), rootDir) {
	if (rootDir) return path.resolve(rootDir);
	const normalizedCwd = path.resolve(cwd);
	if (path.basename(normalizedCwd) === "backend") return path.dirname(normalizedCwd);
	try {
		const packageJson = JSON.parse(fs.readFileSync(path.join(normalizedCwd, "package.json"), "utf8"));
		if (Array.isArray(packageJson.workspaces)) return normalizedCwd;
	} catch {
		// Continue with the workspace-app layout fallback.
	}
	return path.resolve(normalizedCwd, "../..");
}

function readRootDotenv(rootDir) {
	return readDotenvFile(path.join(rootDir, ".env"));
}

function readLegacyDotenvValue(name, cwd, fileNames) {
	return clean(readLegacyDotenv(cwd, fileNames)[name]);
}

function readLegacyDotenv(cwd, fileNames) {
	const values = {};
	for (const fileName of [...fileNames].reverse()) {
		Object.assign(values, readDotenvFile(path.join(cwd, fileName)));
	}
	return values;
}

function getExplicitProcessEnv({ nextEnvPreload = false } = {}) {
	if (!nextEnvPreload) return process.env;
	try {
		const nextEnv = require("@next/env");
		if (nextEnv.initialEnv !== undefined) return nextEnv.initialEnv;
	} catch {
		// Standalone launchers do not load Next's environment snapshot.
	}
	return process.env;
}

function readExplicitProcessValue(name, legacyDotenv, { nextEnvPreload = false } = {}) {
	const explicitEnv = getExplicitProcessEnv({ nextEnvPreload });
	const value = clean(explicitEnv[name]);
	if (!value) return "";
	return value;
}

function firstValue(...values) {
	for (const value of values) {
		const normalized = clean(value);
		if (normalized) return normalized;
	}
	return "";
}

function readDotenvValue(name, cwd = process.cwd(), fileNames = [".env.local", ".env"], rootDir) {
	const rootValue = readRootDotenv(resolveRootDir(cwd, rootDir))[name];
	return firstValue(rootValue, readLegacyDotenvValue(name, cwd, fileNames));
}

function workspaceDefaultFrontendPort(cwd = process.cwd()) {
	try {
		const packageJson = JSON.parse(fs.readFileSync(path.join(cwd, "package.json"), "utf8"));
		if (packageJson.name === "@rehab/console") return DEFAULT_CONSOLE_FRONTEND_PORT;
		if (packageJson.name === "@rehab/product") return DEFAULT_PRODUCT_FRONTEND_PORT;
	} catch {
		// Fall back to the Product default for generic callers and test fixtures.
	}
	return DEFAULT_PRODUCT_FRONTEND_PORT;
}

function resolveFrontendPort({ cwd = process.cwd(), rootDir, cliValue, defaultPort, nextEnvPreload = false } = {}) {
	const resolvedRootDir = resolveRootDir(cwd, rootDir);
	const legacyDotenv = readLegacyDotenv(cwd, [".env.local", ".env"]);
	const packageName = (() => {
		try {
			return JSON.parse(fs.readFileSync(path.join(cwd, "package.json"), "utf8")).name;
		} catch {
			return "";
		}
	})();
	const rootFrontendPortName = packageName === "@rehab/console"
		? "CONSOLE_FRONTEND_PORT"
		: "PRODUCT_FRONTEND_PORT";
	return firstValue(
		cliValue,
		readExplicitProcessValue("FRONTEND_PORT", legacyDotenv, { nextEnvPreload }),
		readExplicitProcessValue(rootFrontendPortName, legacyDotenv, { nextEnvPreload }),
		readRootDotenv(resolvedRootDir)[rootFrontendPortName],
		legacyDotenv.FRONTEND_PORT,
		defaultPort,
		workspaceDefaultFrontendPort(cwd),
	);
}

function resolveBackendPort({ cwd = process.cwd(), rootDir, cliValue, envFile, overrideEnvFile } = {}) {
	const rootDotenv = readRootDotenv(resolveRootDir(cwd, rootDir));
	const overrideDotenv = readDotenvFile(overrideEnvFile);
	const legacyDotenv = readDotenvFile(envFile || path.join(cwd, ".env"));
	return firstValue(
		cliValue,
		process.env.BACKEND_PORT,
		overrideDotenv.BACKEND_PORT,
		rootDotenv.BACKEND_PORT,
		legacyDotenv.BACKEND_PORT,
		DEFAULT_BACKEND_PORT,
	);
}

function resolveFrontendBackendPort({ cwd = process.cwd(), rootDir, nextEnvPreload = false } = {}) {
	const resolvedRootDir = resolveRootDir(cwd, rootDir);
	const rootDotenv = readRootDotenv(resolvedRootDir);
	const legacyDotenv = readLegacyDotenv(cwd, [".env.local", ".env"]);
	return firstValue(
		readExplicitProcessValue("BACKEND_PORT", legacyDotenv, { nextEnvPreload }),
		readExplicitProcessValue("NEXT_PUBLIC_BACKEND_PORT", legacyDotenv, { nextEnvPreload }),
		rootDotenv.BACKEND_PORT,
		rootDotenv.NEXT_PUBLIC_BACKEND_PORT,
		legacyDotenv.BACKEND_PORT,
		legacyDotenv.NEXT_PUBLIC_BACKEND_PORT,
		DEFAULT_BACKEND_PORT,
	);
}

function resolveBackendBase({ cwd = process.cwd(), rootDir, port, nextEnvPreload = false } = {}) {
	const resolvedRootDir = resolveRootDir(cwd, rootDir);
	const rootDotenv = readRootDotenv(resolvedRootDir);
	const legacyDotenv = readLegacyDotenv(cwd, [".env.local", ".env"]);
	const configuredBase = firstValue(
		readExplicitProcessValue("REHAB_BACKEND_BASE", legacyDotenv, { nextEnvPreload }),
		rootDotenv.REHAB_BACKEND_BASE,
		legacyDotenv.REHAB_BACKEND_BASE,
	);
	return (configuredBase || "http://127.0.0.1:" + (port || resolveFrontendBackendPort({ cwd, rootDir, nextEnvPreload }))).replace(/\/+$/, "");
}

function resolveFrontendPublicEnv({ cwd = process.cwd(), rootDir, nextEnvPreload = false } = {}) {
	const resolvedRootDir = resolveRootDir(cwd, rootDir);
	const backendPort = resolveFrontendBackendPort({ cwd, rootDir: resolvedRootDir, nextEnvPreload });
	const rootDotenv = readRootDotenv(resolvedRootDir);
	const legacyDotenv = readLegacyDotenv(cwd, [".env.local", ".env"]);
	return {
		BACKEND_PORT: backendPort,
		NEXT_PUBLIC_BACKEND_PORT: firstValue(
			readExplicitProcessValue("NEXT_PUBLIC_BACKEND_PORT", legacyDotenv, { nextEnvPreload }),
			rootDotenv.NEXT_PUBLIC_BACKEND_PORT,
			legacyDotenv.NEXT_PUBLIC_BACKEND_PORT,
			backendPort,
		),
		REHAB_BACKEND_BASE: resolveBackendBase({ cwd, rootDir: resolvedRootDir, port: backendPort, nextEnvPreload }),
		NEXT_PUBLIC_API_BASE: firstValue(
			readExplicitProcessValue("NEXT_PUBLIC_API_BASE", legacyDotenv, { nextEnvPreload }),
			rootDotenv.NEXT_PUBLIC_API_BASE,
			legacyDotenv.NEXT_PUBLIC_API_BASE,
		),
		NEXT_PUBLIC_WS_BASE: firstValue(
			readExplicitProcessValue("NEXT_PUBLIC_WS_BASE", legacyDotenv, { nextEnvPreload }),
			rootDotenv.NEXT_PUBLIC_WS_BASE,
			legacyDotenv.NEXT_PUBLIC_WS_BASE,
		),
	};
}

module.exports = {
	DEFAULT_BACKEND_PORT,
	DEFAULT_CONSOLE_FRONTEND_PORT,
	DEFAULT_PRODUCT_FRONTEND_PORT,
	parseDotenv,
	readDotenvFile,
	readDotenvValue,
	resolveBackendBase,
	resolveBackendPort,
	resolveFrontendBackendPort,
	resolveFrontendPort,
	resolveFrontendPublicEnv,
	resolveRootDir,
	workspaceDefaultFrontendPort,
};
