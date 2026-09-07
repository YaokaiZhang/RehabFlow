import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const productRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(productRoot, "../..");
const settingsPageClientPath = resolve(productRoot, "app/settings/SettingsPageClient.tsx");
const profileSectionPath = resolve(productRoot, "app/settings/DoctorProfessionalProfileSection.tsx");
const profileHookPath = resolve(productRoot, "app/settings/useDoctorProfessionalProfile.ts");
const sharedApiPath = resolve(repoRoot, "packages/shared/src/api.ts");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

assert(existsSync(settingsPageClientPath), "SettingsPageClient should exist");
assert(existsSync(profileSectionPath), "doctor settings should move into a dedicated DoctorProfessionalProfileSection component");
assert(existsSync(profileHookPath), "doctor settings should own a dedicated useDoctorProfessionalProfile hook");

const settingsPageClient = readFileSync(settingsPageClientPath, "utf8");
const profileSection = readFileSync(profileSectionPath, "utf8");
const profileHook = readFileSync(profileHookPath, "utf8");
const sharedApi = readFileSync(sharedApiPath, "utf8");

assert(settingsPageClient.includes("DoctorProfessionalProfileSection"), "SettingsPageClient should integrate the doctor professional profile section component");
assert(settingsPageClient.includes("section=professional-profile"), "Settings page should support a dedicated professional-profile section query");
assert(settingsPageClient.includes("Professional Profile"), "Settings sidebar/link should say Professional Profile");
assert(!settingsPageClient.includes("Doctor preferences"), "Settings page should replace Doctor preferences wording");

assert(profileSection.includes('title="Professional Profile"'), "doctor settings section heading should say Professional Profile");
assert(profileSection.includes("Primary specialty"), "doctor settings should label the specialty field as Primary specialty");
assert(profileSection.includes("Expertise"), "doctor settings should label the tags field as Expertise");
assert(profileSection.includes("Add tag"), "doctor settings should provide a compact add-tag action");
assert(profileSection.includes("Remove"), "doctor settings should provide removable expertise chips");
assert(profileSection.includes(">Save<") || profileSection.includes("Save"), "doctor settings should expose a save action");
assert(profileSection.includes(">Cancel<") || profileSection.includes("Cancel"), "doctor settings should expose a cancel/reset action");
assert(profileSection.includes("Loading professional profile"), "doctor settings should surface a loading state");
assert(profileSection.includes("Professional profile saved") || profileHook.includes("Professional profile saved"), "doctor settings should surface a success state");
assert(profileSection.includes("Display name and verification status"), "doctor settings should keep display name and verification status read-only");
assert(!profileSection.includes("Care Worklist"), "doctor settings section should not move into Care Worklist copy");
assert(!profileSection.includes("Doctor Dashboard"), "doctor settings section should not move into Doctor Dashboard copy");

assert(profileHook.includes("getDoctorProfessionalProfile"), "doctor profile hook should load the current doctor profile through shared API");
assert(profileHook.includes("updateDoctorProfessionalProfile"), "doctor profile hook should save the current doctor profile through shared API");
assert(profileHook.includes("expertise_tags"), "doctor profile hook should own expertise tag state");
assert(profileHook.includes("setDraft"), "doctor profile hook should own editable draft state");
assert(profileHook.includes("setPendingTag"), "doctor profile hook should own pending expertise tag input state");

assert(sharedApi.includes("export type DoctorProfessionalProfile"), "shared API should export DoctorProfessionalProfile");
assert(sharedApi.includes("export type DoctorProfessionalProfileUpdateInput"), "shared API should export DoctorProfessionalProfileUpdateInput");
assert(sharedApi.includes('"/doctor/profile"'), "shared API should call the /doctor/profile endpoint");
assert(sharedApi.includes("getDoctorProfessionalProfile"), "shared API should expose getDoctorProfessionalProfile");
assert(sharedApi.includes("updateDoctorProfessionalProfile"), "shared API should expose updateDoctorProfessionalProfile");

console.log("doctor professional profile contract ok");
