import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const productRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const shellPath = resolve(productRoot, "components/ProductShell.tsx");
const uiPath = resolve(productRoot, "components/ui.tsx");
const navbarPath = resolve(productRoot, "components/Navbar.tsx");
const layoutPath = resolve(productRoot, "app/layout.tsx");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

assert(existsSync(shellPath), "ProductShell.tsx should exist");
assert(existsSync(uiPath), "shared UI primitives file should exist");

const shell = readFileSync(shellPath, "utf8");
const ui = readFileSync(uiPath, "utf8");
const navbar = existsSync(navbarPath) ? readFileSync(navbarPath, "utf8") : "";
const layout = readFileSync(layoutPath, "utf8");
const navSurface = `${shell}\n${navbar}`;

assert(shell.includes('"use client"') || shell.includes("'use client'"), "ProductShell should be a client component");
assert(shell.includes("loadAuth"), "ProductShell should read existing auth state");
assert(shell.includes("clearAuth"), "ProductShell should preserve sign-out behavior");
assert(shell.includes("usePathname"), "ProductShell should read route state for active navigation");
assert(shell.includes("useState<AuthState | null | undefined>(undefined)"), "ProductShell auth state should start unresolved");
assert(shell.includes("rehab-auth-changed"), "ProductShell should resync after auth changes");
assert(shell.includes("storage"), "ProductShell should resync after storage changes");

const activeMatcher = shell.match(/function isActive\(pathname: string, href: string\) \{([\s\S]*?)\n\}/);
assert(activeMatcher, "ProductShell should keep route active matching explicit and testable");
const isActive = new Function("pathname", "href", activeMatcher[1]);

assert(isActive("/doctor", "/doctor"), "Care Worklist should be active on the /doctor route");
assert(isActive("/doctor/queue", "/doctor"), "Care Worklist should stay active on worklist subroutes");
assert(isActive("/doctor/dashboard", "/doctor/dashboard"), "Doctor Dashboard should be active on /doctor/dashboard");
assert(!isActive("/doctor/dashboard", "/doctor"), "Doctor Dashboard should not also activate Care Worklist");

assert(navSurface.includes('href="/episodes"'), "patient shell nav should link to /episodes");
assert(navSurface.includes("Patient Dashboard"), "patient shell nav should label /episodes as Patient Dashboard");
assert(navSurface.includes('href="/settings"'), "shell nav should link to /settings");
assert(navSurface.includes("Settings"), "shell nav should label Settings");
assert(navSurface.includes('href="/doctor"'), "doctor shell nav should link to Care Worklist at /doctor");
assert(navSurface.includes("Care Worklist"), "doctor shell nav should label Care Worklist");
assert(navSurface.includes('href="/doctor/dashboard"'), "doctor shell nav should link to /doctor/dashboard");
assert(navSurface.includes("Doctor Dashboard"), "doctor shell nav should label Doctor Dashboard");

assert(!navSurface.includes('href="/memory"'), "top-level shell nav should not link to /memory");
assert(!navSurface.includes(">Patient Memory<"), "top-level shell nav should not label a nav item Patient Memory");

const forbiddenShellImports = [
  "@rehab/shared/api",
  "usePatientMemoryDocument",
  "useAiDailyRehabWorkspace",
  "useDoctorCareWorklist",
  "useDoctorDashboard",
  "usePatientProfessionalCareWorkspace",
  "getPatientMemoryDocument",
  "listCareEpisodes",
  "getCareEpisode",
  "getPatientProfessionalCareWorkspace",
];
for (const forbidden of forbiddenShellImports) {
  assert(!shell.includes(forbidden), `ProductShell should not import or call domain workflow data helpers: ${forbidden}`);
}
assert(!/\bfetch\s*\(/.test(shell), "ProductShell should not fetch domain workflow data");

for (const exportName of ["AppButton", "StatusBadge", "DashboardCard", "SectionHeader", "ProgressPill"]) {
  assert(ui.includes(`function ${exportName}`) || ui.includes(`const ${exportName}`), `ui.tsx should define ${exportName}`);
  assert(ui.includes(`export function ${exportName}`) || ui.includes(`export const ${exportName}`), `ui.tsx should export ${exportName}`);
}
assert(!ui.includes("@rehab/shared/api"), "shared UI primitives should stay presentational");

assert(layout.includes("ProductShell"), "app layout should render ProductShell");
assert(!layout.includes("<Navbar"), "app layout should not render Navbar as the primary product nav");
assert(navbar.includes("ProductShell") || !navbar.includes("<nav"), "Navbar should delegate to ProductShell or stop owning primary nav markup");

console.log("product shell contract ok");
