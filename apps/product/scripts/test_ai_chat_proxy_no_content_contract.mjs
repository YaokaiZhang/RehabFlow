import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const repoRoot = resolve(new URL("../../..", import.meta.url).pathname);
const routeSource = readFileSync(resolve(repoRoot, "apps/product/app/ai-chat/[session_id]/route.ts"), "utf8");
const typescript = await import(pathToFileURL(resolve(repoRoot, "node_modules/typescript/lib/typescript.js")).href);
const routeModule = await import(`data:text/javascript,${encodeURIComponent(typescript.default.transpileModule(routeSource, {
	compilerOptions: {
		module: typescript.default.ModuleKind.ESNext,
		target: typescript.default.ScriptTarget.ES2022,
	},
}).outputText)}`);

function assert(condition, message) {
	if (!condition) throw new Error(message);
}

assert(routeSource.includes("response.status === 204"), "AI chat proxy should recognize 204 No Content responses");
assert(routeSource.includes("response.status === 205"), "AI chat proxy should recognize 205 Reset Content responses");
assert(routeSource.includes("response.status === 304"), "AI chat proxy should recognize 304 Not Modified responses");
assert(routeSource.includes("new Response(bodylessStatus ? null : responseBody"), "AI chat proxy must not attach a body to bodyless responses");

const originalFetch = globalThis.fetch;
globalThis.fetch = async () => new Response(null, { status: 204 });
try {
	const response = await routeModule.DELETE(
		new Request("http://product.test/ai-chat/chat-1", {
			method: "DELETE",
			headers: { Authorization: "Bearer test-token" },
		}),
		{ params: Promise.resolve({ session_id: "chat-1" }) },
	);
	assert(response.status === 204, "AI chat proxy should preserve backend 204 responses");
	assert((await response.text()) === "", "AI chat proxy should return an empty 204 body");
} finally {
	globalThis.fetch = originalFetch;
}

console.log("AI chat proxy no-content contract ok");
