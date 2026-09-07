import { readFileSync } from "node:fs";
import { join } from "node:path";

const root = process.cwd();
const runtime = readFileSync(join(root, "packages/shared/src/runtime.ts"), "utf8");
const productConfig = readFileSync(join(root, "apps/product/next.config.js"), "utf8");
const consoleConfig = readFileSync(join(root, "apps/console/next.config.js"), "utf8");
const failures = [];

function expect(condition, message) {
  if (!condition) failures.push(message);
}

for (const [name, config] of [["product", productConfig], ["console", consoleConfig]]) {
  expect(config.includes("async rewrites()"), `${name} Next config should define rewrites for same-origin API proxying.`);
  expect(config.includes("source: '/api/:path*'"), `${name} Next config should proxy /api/:path*.`);
	  expect(config.includes("resolveFrontendPublicEnv") && config.includes("destination: frontendEnv.REHAB_BACKEND_BASE +"), "Next config should resolve a dynamic backend proxy target.");
}

expect(runtime.includes('return "/api";'), "Browser API default should be same-origin /api so remote browsers do not need direct :8000 access.");
expect(runtime.includes("Request timed out while contacting RehabFlow"), "Fetch timeout should show a friendly backend reachability message.");
expect(!runtime.includes("controller.abort()"), "Fetch timeout should not abort without a reason because browsers expose that raw message.");

if (failures.length) {
  console.error(failures.map((failure) => "- " + failure).join("\n"));
  process.exit(1);
}
console.log("runtime API proxy contract ok");
