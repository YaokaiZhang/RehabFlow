import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const productRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");

function read(relativePath) {
  return readFileSync(resolve(productRoot, relativePath), "utf8");
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

const worklist = read("app/doctor/page.tsx");
const dashboard = read("app/doctor/dashboard/page.tsx");

assert(worklist.includes("displayPatientName"), "Care Worklist should use a human-readable patient label helper");
assert(worklist.includes('displayPatientName(request.patient_name)'), "Care Worklist request labels should use display names or a stable fallback");
assert(worklist.includes('displayPatientName(relationship.patient_name)'), "Care Worklist relationship labels should use display names or a stable fallback");
assert(!worklist.includes("request.patient_name || request.patient_id"), "Care Worklist should not expose raw patient IDs when names are unavailable");
assert(!worklist.includes("relationship.patient_name || relationship.patient_id"), "Care Worklist should not expose raw patient IDs when names are unavailable");

assert(dashboard.includes('textValue(relationship.patient_name, "Patient")'), "Doctor Dashboard should use a human-readable patient fallback");
assert(!dashboard.includes("relationship.patient_name || relationship.patient_id"), "Doctor Dashboard should not expose raw patient IDs when names are unavailable");

console.log("product ID label boundary contract ok");
