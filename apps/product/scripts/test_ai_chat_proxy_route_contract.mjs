#!/usr/bin/env node
import assert from "node:assert/strict";
import fs from "node:fs";

const routePath = "apps/product/app/ai-chat/[session_id]/route.ts";
assert(fs.existsSync(routePath), "product app should own a route handler outside the generic /api rewrite for long AI chat turns");

const route = fs.readFileSync(routePath, "utf8");
assert(route.includes("export async function POST"), "AI chat proxy route should handle POST requests");
assert(route.includes("params") && route.includes("session_id"), "AI chat proxy route should preserve the session_id path parameter");
assert(route.includes("BACKEND_PORT") && route.includes("8000"), "AI chat proxy route should resolve the configurable FastAPI backend port");
assert(!route.includes("AbortSignal.timeout"), "AI chat proxy route should not impose a fixed timeout on long AI chat turns");
assert(!route.includes("180000"), "AI chat proxy route should not keep the old 180s triage timeout");
assert(route.includes("response.text()"), "AI chat proxy route should pass through backend response bodies without reparsing debug payloads");
assert(route.includes("status: response.status"), "AI chat proxy route should preserve backend HTTP status codes");
assert(route.includes('request.headers.get("Authorization")'), "AI chat proxy route should read the browser Authorization header");
assert(route.includes("if (!authorization)"), "AI chat proxy route should reject requests without Authorization");
assert(route.includes('return jsonError("Missing Authorization header.", 401)'), "AI chat proxy route should return 401 when Authorization is absent");
assert(route.includes("Authorization: authorization"), "AI chat proxy route should forward Authorization to the backend");
assert(!route.includes("console.log") && !route.includes("console.error"), "AI chat proxy route must never log bearer tokens");

console.log("AI chat proxy route contract ok");
