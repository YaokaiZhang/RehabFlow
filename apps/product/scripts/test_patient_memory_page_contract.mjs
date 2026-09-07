import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const productRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(productRoot, "../..");
const memoryPagePath = resolve(productRoot, "app/memory/page.tsx");
const settingsPagePath = resolve(productRoot, "app/settings/page.tsx");
const settingsPageClientPath = resolve(productRoot, "app/settings/SettingsPageClient.tsx");
const patientMemorySectionPath = resolve(productRoot, "app/settings/PatientMemorySettingsSection.tsx");
const hookPath = resolve(productRoot, "app/memory/usePatientMemoryDocument.ts");
const shellPath = resolve(productRoot, "components/ProductShell.tsx");
const sharedApiPath = resolve(repoRoot, "packages/shared/src/api.ts");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

assert(existsSync(memoryPagePath), "Legacy /memory page should exist as a backwards-compatible redirect");
assert(existsSync(settingsPagePath), "Settings page should own the Patient Memory UI route");
assert(existsSync(settingsPageClientPath), "Settings page should delegate Patient Memory UI to a client inner component");
assert(existsSync(patientMemorySectionPath), "Patient Memory settings section should exist");
assert(existsSync(hookPath), "Patient Memory hook should exist");

const memoryPage = readFileSync(memoryPagePath, "utf8");
const settingsPage = readFileSync(settingsPagePath, "utf8");
const settingsPageClient = readFileSync(settingsPageClientPath, "utf8");
const patientMemorySection = readFileSync(patientMemorySectionPath, "utf8");
const hook = readFileSync(hookPath, "utf8");
const shell = existsSync(shellPath) ? readFileSync(shellPath, "utf8") : "";
const sharedApi = readFileSync(sharedApiPath, "utf8");

assert(sharedApi.includes("export type MemoryDocument"), "shared API should export MemoryDocument");
assert(sharedApi.includes("export type MemoryDocumentFieldInput"), "shared API should export MemoryDocumentFieldInput");
assert(sharedApi.includes("editable_fields"), "shared API should expose editable Patient Memory fields");
assert(sharedApi.includes("getPatientMemoryDocument"), "shared API should expose getPatientMemoryDocument");
assert(sharedApi.includes("patchPatientMemoryFields"), "shared API should expose patchPatientMemoryFields");
assert(sharedApi.includes("replacePatientMemoryFields"), "shared API should expose replacePatientMemoryFields");
assert(sharedApi.includes("deletePatientMemoryField"), "shared API should expose deletePatientMemoryField");
assert(sharedApi.includes('"/memory/patient-document"'), "shared API should call patient memory document endpoint");
assert(sharedApi.includes('"/memory/patient-document/fields"'), "shared API should call patient memory field endpoint");
assert(!sharedApi.includes('"/memory/patient-document/items"'), "shared API should not call the removed patient memory item endpoint");

assert(memoryPage.includes("redirect"), "/memory page should redirect after Patient Memory moves to Settings");
assert(memoryPage.includes("/settings?section=patient-memory"), "/memory redirect should preserve backwards compatibility to the Patient Memory settings section");
assert(!memoryPage.includes("usePatientMemoryDocument"), "/memory page should not own Patient Memory data loading after the Settings move");
assert(!memoryPage.includes("<form"), "/memory page should not render Patient Memory forms after the Settings move");
assert(!memoryPage.includes("compiled_text"), "/memory page should not render the compiled_text UI after the Settings move");

assert(settingsPage.includes("SettingsPageClient"), "Settings page should render the client settings surface");
assert(settingsPage.includes("<Suspense"), "Settings page should keep client search-param behavior behind Suspense");
assert(settingsPageClient.includes("PatientMemorySettingsSection"), "Settings client should render the Patient Memory settings section");
assert(settingsPageClient.includes("role === \"patient\""), "Settings client should show Patient Memory only to patient users");
assert(settingsPageClient.includes("role === \"doctor\""), "Settings client should support doctor users with account/settings behavior");
assert(settingsPageClient.includes("/login?next=/settings"), "Settings client should preserve auth flow for signed-out users");

assert(patientMemorySection.includes("usePatientMemoryDocument"), "Patient Memory settings section should use the Patient Memory hook");
assert(patientMemorySection.includes("Patient Memory"), "Patient Memory settings section should render Patient Memory heading");
assert(patientMemorySection.includes("compiled_text"), "Patient Memory settings section should render the compiled_text prose view");
assert(patientMemorySection.includes("memoryDocument?.compiled_text"), "Patient Memory should read compiled_text from the API document rather than inventing a second field");
assert(sharedApi.includes("compiled_text"), "shared API should retain compiled_text as an underlying memory document field");
const patientMemorySectionWithoutApiRead = patientMemorySection.replace(/memoryDocument\?\.\s*compiled_text\b/g, "");
assert(
  !/\bcompiled_text\b|\bcompiled[\s-]+text\b/i.test(patientMemorySectionWithoutApiRead),
  "Patient Memory should not expose compiled_text as a label or indirect visible-text token",
);
assert(patientMemorySection.includes("Patient Memory summary"), "Patient Memory settings section should use a product-facing summary label");
assert(!patientMemorySection.includes(">compiled_text<"), "Patient Memory settings section should not expose the compiled_text API field name");
const userFacingCompiledTextPatterns = [
  />[^<{]*(?:compiled_text|compiled text|compiled-text)[^<{]*</i,
  />\s*\{\s*compiled_text\s*\}\s*</i,
  /\{\s*["'`][^"'`]*(?:compiled_text|compiled text|compiled-text)[^"'`]*["'`]\s*\}/i,
  /(?:aria-label|title|placeholder|alt|label)\s*=\s*(?:\{\s*)?["'`][^"'`]*(?:compiled_text|compiled text|compiled-text)[^"'`]*["'`]/i,
];
for (const pattern of userFacingCompiledTextPatterns) {
  assert(!pattern.test(patientMemorySection), `Patient Memory should not expose the compiled_text field name through ${pattern}`);
}
assert(patientMemorySection.includes("Directly edit"), "Patient Memory settings section should cue that patients can directly edit memory items");
assert(patientMemorySection.includes("Delete"), "Patient Memory settings section should expose Delete controls for patient memory items");
assert(patientMemorySection.includes("<form"), "Patient Memory settings section should provide an add or edit form");

assert(hook.includes("export function usePatientMemoryDocument"), "hook should export usePatientMemoryDocument");
assert(hook.includes("getPatientMemoryDocument"), "hook should load patient memory through shared API");
assert(hook.includes("patchPatientMemoryFields"), "hook should patch patient memory fields through shared API");
assert(hook.includes("replacePatientMemoryFields"), "hook should replace patient memory fields through shared API");
assert(hook.includes("deletePatientMemoryField"), "hook should delete patient memory fields through shared API");
assert(hook.includes("loadAuth"), "hook should use auth state");
assert(hook.includes('role !== "patient"'), "hook should be patient-only");
assert(hook.includes("loading"), "hook should expose loading state");
assert(hook.includes("error"), "hook should expose error state");
assert(hook.includes("busy"), "hook should expose busy state");
assert(hook.includes("options?: { quiet?: boolean }): Promise<boolean>"), "hook document loader should return success and accept quiet reload options");
assert(!hook.includes("const reloadAfterMutation = async"), "hook mutation reloads should not bypass the guarded document loader");
assert(hook.includes("return true;"), "hook mutation actions should return true after successful persistence and reload");
assert(hook.includes("return false;"), "hook mutation actions should return false after caught errors");
assert(patientMemorySection.includes("const saved = await addItem"), "section should inspect addItem success before clearing the new item form");
assert(patientMemorySection.includes("if (saved) setNewItem(emptyForm);"), "section should only clear the add form after a successful save");
assert(patientMemorySection.includes("const updated = await editItem"), "section should inspect editItem success before leaving edit mode");
assert(patientMemorySection.includes("if (updated) {"), "section should only leave edit mode after a successful update");

assert(shell.includes('href="/settings"'), "product shell should link patients to Settings instead of top-level Patient Memory");
assert(shell.includes("Settings"), "product shell should label the replacement nav item Settings");
assert(!shell.includes('href="/memory"'), "product shell should not link top-level nav to /memory");
assert(!shell.includes(">Patient Memory<"), "product shell should not show Patient Memory as a top-level nav item");

const mutableEpisodeMemoryPattern = /(create|update|delete)[A-Za-z]*EpisodeMemory|episode-document\/items|care-episodes\/\$\{[^}]+\}\/memory\/items/i;
assert(!mutableEpisodeMemoryPattern.test(sharedApi), "shared API should not expose Episode Memory item editing for this task");
assert(!mutableEpisodeMemoryPattern.test(hook), "hook should not edit Episode Memory");
assert(!mutableEpisodeMemoryPattern.test(patientMemorySection), "Patient Memory settings section should not edit Episode Memory");
assert(!patientMemorySection.includes("Edit Episode Memory"), "Patient Memory settings section should not offer Episode Memory editing");
assert(!patientMemorySection.includes("Delete Episode Memory"), "Patient Memory settings section should not offer Episode Memory deletion");

console.log("patient memory page contract ok");
