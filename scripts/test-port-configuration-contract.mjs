import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { tmpdir } from "node:os";

const require = createRequire(import.meta.url);
const root = resolve(import.meta.dirname, "..");
const tempRoot = mkdtempSync(join(tmpdir(), "port-contract-"));
const envKeys = ["BACKEND_PORT", "NEXT_PUBLIC_BACKEND_PORT", "FRONTEND_PORT", "PRODUCT_FRONTEND_PORT", "CONSOLE_FRONTEND_PORT", "REHAB_BACKEND_BASE", "NEXT_PUBLIC_API_BASE", "NEXT_PUBLIC_WS_BASE"];
const typescript = require(resolve(root, "node_modules/typescript/lib/typescript.js"));

async function withEnv(values, fn) {
	const previous = {};
	for (const key of envKeys) {
		previous[key] = process.env[key];
		delete process.env[key];
	}
	Object.assign(process.env, values);
	try {
		return await fn();
	} finally {
		for (const key of envKeys) {
			if (previous[key] === undefined) delete process.env[key];
			else process.env[key] = previous[key];
		}
	}
}

async function rewrites(relativePath, values) {
	return withEnv(values, async () => {
		const configPath = resolve(root, relativePath);
		delete require.cache[require.resolve(configPath)];
		return require(configPath).rewrites();
	});
}

assert.equal((await rewrites("apps/product/next.config.js", { BACKEND_PORT: "8123" }))[0].destination, "http://127.0.0.1:8123/:path*");
assert.equal((await rewrites("apps/console/next.config.js", { BACKEND_PORT: "8123" }))[0].destination, "http://127.0.0.1:8123/:path*");
assert.equal((await rewrites("apps/product/next.config.js", { NEXT_PUBLIC_BACKEND_PORT: "8124" }))[0].destination, "http://127.0.0.1:8124/:path*");
assert.equal((await rewrites("apps/console/next.config.js", { REHAB_BACKEND_BASE: "https://backend.test/root/" }))[0].destination, "https://backend.test/root/:path*");

for (const relativePath of ["apps/product/next.config.js", "apps/console/next.config.js"]) {
  await withEnv({}, async () => {
    delete require.cache[require.resolve(resolve(root, relativePath))];
    const config = require(resolve(root, relativePath));
    const publicKeys = Object.keys(config.env || {});
    assert(publicKeys.every((key) => [
      "BACKEND_PORT",
      "NEXT_PUBLIC_BACKEND_PORT",
      "REHAB_BACKEND_BASE",
      "NEXT_PUBLIC_API_BASE",
      "NEXT_PUBLIC_WS_BASE",
    ].includes(key)));
    assert(publicKeys.every((key) => !/(DATABASE|REDIS|QDRANT|QWEN|JWT|TRACE|SECRET|PASSWORD|TOKEN|API_KEY)/i.test(key)));
  });
}

const runtimeSource = readFileSync(resolve(root, "packages/shared/src/runtime.ts"), "utf8");
const routeSources = [
	["product", "apps/product/app/ai-chat/[session_id]/route.ts"],
	["console", "apps/console/app/ai-chat/[session_id]/route.ts"],
].map(([name, path]) => [name, readFileSync(resolve(root, path), "utf8")]);
const consolePage = readFileSync(resolve(root, "apps/console/app/page.tsx"), "utf8");
assert(runtimeSource.includes("process.env.BACKEND_PORT"));
assert(runtimeSource.includes("NEXT_PUBLIC_BACKEND_PORT"));
assert(runtimeSource.includes('DEFAULT_BACKEND_PORT = "8000"'));
assert(consolePage.includes("setRuntimeApiBase(getApiBase())"));
assert(consolePage.includes("setRuntimeWsBase(getWsBase())"));
assert(!consolePage.includes("localhost:8001"));
for (const [name, source] of routeSources) {
	assert(source.includes("process.env.BACKEND_PORT"), name + " route should read BACKEND_PORT");
	assert(!source.includes("8001"), name + " route should not retain 8001");
}

function writeModule(name, source) {
	const output = typescript.transpileModule(source, {
		compilerOptions: { module: typescript.ModuleKind.ESNext, target: typescript.ScriptTarget.ES2022 },
	}).outputText;
	const path = join(tempRoot, name);
	writeFileSync(path, output);
	return path;
}

async function exerciseRoute(name, source, values, expected) {
	await withEnv(values, async () => {
		const captured = [];
		const originalFetch = globalThis.fetch;
		globalThis.fetch = async (input) => {
			captured.push(input);
			return new Response("ok", { status: 200 });
		};
		try {
			const route = await import(pathToFileURL(writeModule(name, source)).href + "?" + name);
			const response = await route.POST(
				new Request("http://frontend.test/ai-chat/session", {
					method: "POST",
					headers: { Authorization: "Bearer token" },
					body: "{}",
				}),
				{ params: Promise.resolve({ session_id: "session" }) },
			);
			assert.equal(response.status, 200);
			assert.equal(captured[0], expected);
		} finally {
			globalThis.fetch = originalFetch;
		}
	});
}

await exerciseRoute("product-route.mjs", routeSources[0][1], { BACKEND_PORT: "8123" }, "http://127.0.0.1:8123/ai/chat/session");
await exerciseRoute("console-route.mjs", routeSources[1][1], { BACKEND_PORT: "8123" }, "http://127.0.0.1:8123/ai/chat/session");
await exerciseRoute("product-override-route.mjs", routeSources[0][1], { REHAB_BACKEND_BASE: "http://backend.test" }, "http://backend.test/ai/chat/session");

const runtime = await import(pathToFileURL(writeModule("runtime.mjs", runtimeSource)).href + "?runtime");
await withEnv({ BACKEND_PORT: "8123" }, async () => {
	delete globalThis.window;
	assert.equal(runtime.getBackendPort(), "8123");
	assert.equal(runtime.getApiBase(), "http://127.0.0.1:8123");
	assert.equal(runtime.getWsBase(), "ws://localhost:8123");
});
await withEnv({ NEXT_PUBLIC_BACKEND_PORT: "8124" }, async () => {
	delete globalThis.window;
	assert.equal(runtime.getBackendPort(), "8124");
});
await withEnv({ BACKEND_PORT: "8125", NEXT_PUBLIC_API_BASE: "https://api.test", NEXT_PUBLIC_WS_BASE: "wss://ws.test" }, async () => {
	globalThis.window = { location: { protocol: "http:", hostname: "frontend.test" } };
	assert.equal(runtime.getApiBase(), "https://api.test");
	assert.equal(runtime.getWsBase(), "wss://ws.test");
});
await withEnv({ BACKEND_PORT: "8126" }, async () => {
	globalThis.window = { location: { protocol: "http:", hostname: "frontend.test" } };
	assert.equal(runtime.getApiBase(), "/api");
	assert.equal(runtime.getWsBase(), "ws://frontend.test:8126");
});
delete globalThis.window;
rmSync(tempRoot, { recursive: true, force: true });
console.log("port propagation contract ok");
