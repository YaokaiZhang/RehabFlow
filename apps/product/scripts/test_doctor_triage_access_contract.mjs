import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const productRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const triagePagePath = resolve(productRoot, "app/episodes/[episode_id]/triage/page.tsx");
const shellPath = resolve(productRoot, "components/ProductShell.tsx");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

const triagePage = readFileSync(triagePagePath, "utf8");
const shell = readFileSync(shellPath, "utf8");

assert(triagePage.includes('import { useParams, useRouter } from "next/navigation";'), "triage page should import useRouter with useParams");
assert(triagePage.includes("const router = useRouter();"), "triage page should create a router");
assert(triagePage.includes('nextAuth.role === "doctor"'), "triage page should check for doctor auth");
assert(triagePage.includes('router.replace("/doctor/dashboard")'), "triage page should redirect doctors to /doctor/dashboard");
assert(triagePage.indexOf('nextAuth.role === "doctor"') < triagePage.indexOf('nextAuth.role !== "patient"'), "doctor redirect should run before patient-only error");

assert(shell.includes('href="/doctor/dashboard"'), "product shell should link doctors to /doctor/dashboard");
assert(shell.includes("Doctor Dashboard"), "product shell should label the doctor dashboard link Doctor Dashboard");
assert(shell.includes('href="/doctor"'), "product shell should link doctors to /doctor");
assert(shell.includes("Care Worklist"), "product shell should label the /doctor link Care Worklist");
assert(shell.includes('auth?.role === "doctor"'), "product shell should branch for doctors");
assert(shell.includes("useState<AuthState | null | undefined>(undefined)"), "product shell auth state should start unresolved");
assert(shell.includes('auth === null || auth?.role === "patient"'), "product shell should show Triage only after auth resolves as guest or patient");
assert(!shell.includes('!auth || auth.role === "patient"'), "product shell should not treat unresolved auth as guest/patient Triage access");
assert(shell.includes("auth === null ?"), "product shell guest links should render only after auth resolves as guest");
assert(shell.includes('href="/settings"'), "product shell should link authenticated doctors to Settings");
assert(!shell.includes('href="/memory"'), "product shell should not keep Patient Memory as a top-level nav link");

console.log("doctor triage access contract ok");
