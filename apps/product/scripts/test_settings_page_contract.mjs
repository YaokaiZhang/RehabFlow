import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const productRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(productRoot, "../..");
const settingsPagePath = resolve(productRoot, "app/settings/page.tsx");
const settingsPageClientPath = resolve(productRoot, "app/settings/SettingsPageClient.tsx");
const patientMemorySectionPath = resolve(productRoot, "app/settings/PatientMemorySettingsSection.tsx");
const memoryPagePath = resolve(productRoot, "app/memory/page.tsx");
const memoryHookPath = resolve(productRoot, "app/memory/usePatientMemoryDocument.ts");
const shellPath = resolve(productRoot, "components/ProductShell.tsx");
const sharedApiPath = resolve(repoRoot, "packages/shared/src/api.ts");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

assert(existsSync(settingsPagePath), "/settings/page.tsx should exist");
assert(existsSync(settingsPageClientPath), "Settings should isolate client search-param/auth behavior in a client inner component");
assert(existsSync(patientMemorySectionPath), "Settings should own a PatientMemorySettingsSection component");
assert(existsSync(memoryPagePath), "/memory/page.tsx should remain as a backwards-compatible route");
assert(existsSync(memoryHookPath), "Patient Memory hook should remain available for the settings section");

const settingsPage = readFileSync(settingsPagePath, "utf8");
const settingsPageClient = readFileSync(settingsPageClientPath, "utf8");
const patientMemorySection = readFileSync(patientMemorySectionPath, "utf8");
const memoryPage = readFileSync(memoryPagePath, "utf8");
const memoryHook = readFileSync(memoryHookPath, "utf8");
const shell = readFileSync(shellPath, "utf8");
const sharedApi = readFileSync(sharedApiPath, "utf8");

assert(!settingsPage.includes('"use client"') && !settingsPage.includes("'use client'"), "Settings route page should remain a server component so search params are dynamic-safe");
assert(settingsPage.includes("Suspense"), "Settings route page should wrap the client settings surface in Suspense");
assert(settingsPage.includes("<Suspense"), "Settings route page should render an explicit Suspense boundary");
assert(settingsPage.includes("SettingsPageClient"), "Settings route page should delegate interactive settings behavior to a client component");
assert(!settingsPage.includes("useSearchParams"), "Settings route page should not call useSearchParams directly");

assert(settingsPageClient.includes('"use client"') || settingsPageClient.includes("'use client'"), "SettingsPageClient should be a client component to preserve auth flow patterns");
assert(settingsPageClient.includes("useSearchParams"), "SettingsPageClient should read section query state inside the Suspense boundary");
assert(settingsPageClient.includes("loadAuth"), "Settings page should read existing auth state");
assert(settingsPageClient.includes("clearAuth"), "Settings page should expose account sign-out behavior");
assert(settingsPageClient.includes("/login?next=/settings"), "Settings page should route signed-out users through login with next=/settings");
assert(settingsPageClient.includes("role === \"patient\""), "Settings page should branch patient settings behavior by role");
assert(settingsPageClient.includes("role === \"doctor\""), "Settings page should branch doctor settings behavior by role");
assert(settingsPageClient.includes("PatientMemorySettingsSection"), "Settings page should render PatientMemorySettingsSection for patients");
assert(settingsPageClient.includes("Professional Profile"), "Settings page should include a doctor professional profile/account settings surface");
assert(settingsPageClient.includes("Account"), "Settings page should include account settings surface");
assert(settingsPageClient.includes("section=patient-memory"), "Settings page should understand the patient-memory section query");
assert(settingsPageClient.includes("SectionHeader"), "Settings page should use shared section primitives where practical");
assert(settingsPageClient.includes("DashboardCard"), "Settings page should use shared dashboard/card primitives where practical");
assert(settingsPageClient.includes("AppButton"), "Settings page should use shared button primitives where practical");

assert(patientMemorySection.includes('"use client"') || patientMemorySection.includes("'use client'"), "PatientMemorySettingsSection should be a client component");
assert(patientMemorySection.includes("usePatientMemoryDocument"), "PatientMemorySettingsSection should preserve the existing Patient Memory hook behavior");
assert(patientMemorySection.includes("Patient Memory"), "PatientMemorySettingsSection should render Patient Memory copy");
assert(patientMemorySection.includes("compiled_text"), "PatientMemorySettingsSection should preserve compiled_text prose view");
assert(patientMemorySection.includes("Directly edit"), "PatientMemorySettingsSection should preserve direct-edit affordance");
assert(patientMemorySection.includes("Delete"), "PatientMemorySettingsSection should preserve Delete controls");
assert(patientMemorySection.includes("const saved = await addItem"), "PatientMemorySettingsSection should inspect addItem success before clearing the add form");
assert(patientMemorySection.includes("if (saved) setNewItem(emptyForm);"), "PatientMemorySettingsSection should only clear the add form after a successful save");
assert(patientMemorySection.includes("const updated = await editItem"), "PatientMemorySettingsSection should inspect editItem success before leaving edit mode");
assert(patientMemorySection.includes("if (updated) {"), "PatientMemorySettingsSection should only leave edit mode after a successful update");
assert(!patientMemorySection.includes("Edit Episode Memory"), "PatientMemorySettingsSection should not edit Episode Memory");
assert(!patientMemorySection.includes("Delete Episode Memory"), "PatientMemorySettingsSection should not delete Episode Memory");

assert(memoryPage.includes("redirect"), "/memory/page.tsx should redirect rather than render Patient Memory UI");
assert(memoryPage.includes("/settings?section=patient-memory"), "/memory/page.tsx should redirect to Settings patient memory section");
assert(!memoryPage.includes("usePatientMemoryDocument"), "/memory/page.tsx should not own Patient Memory data behavior after the Settings move");
assert(!memoryPage.includes("<form"), "/memory/page.tsx should not render Patient Memory forms after the Settings move");

assert(memoryHook.includes("getPatientMemoryDocument"), "hook should continue loading Patient Memory through the shared API");
assert(memoryHook.includes("patchPatientMemoryFields"), "hook should continue creating Patient Memory fields through the shared API");
assert(memoryHook.includes("replacePatientMemoryFields"), "hook should continue updating Patient Memory fields through the shared API");
assert(memoryHook.includes("deletePatientMemoryField"), "hook should continue deleting Patient Memory fields through the shared API");
assert(sharedApi.includes('"/memory/patient-document"'), "shared API endpoint semantics should remain unchanged");
assert(sharedApi.includes('"/memory/patient-document/fields"'), "shared API field endpoint semantics should remain current");
assert(!sharedApi.includes('"/memory/patient-document/items"'), "shared API should not call the removed Patient Memory item endpoint");

assert(shell.includes('href="/settings"'), "product shell should link to Settings");
assert(!shell.includes('href="/memory"'), "product shell should not reintroduce /memory top-level nav");
assert(!shell.includes(">Patient Memory<"), "product shell should not reintroduce Patient Memory as top-level nav");

console.log("settings page contract ok");
