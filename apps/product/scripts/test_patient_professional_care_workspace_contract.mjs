import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const productRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const pagePath = resolve(productRoot, "app/episodes/[episode_id]/professional-care/page.tsx");
const hookPath = resolve(productRoot, "app/episodes/[episode_id]/professional-care/usePatientProfessionalCareWorkspace.ts");
const sharedApiPath = resolve(productRoot, "../../packages/shared/src/api.ts");
const searchServicePath = resolve(productRoot, "../../backend/app/services/semantic_doctor_search.py");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function directApiFunctionImportCount(source) {
  const match = source.match(/import\s*\{([\s\S]*?)\}\s*from\s*["']@rehab\/shared\/api["'];/);
  if (!match) return 0;
  return match[1]
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean)
    .filter((item) => !item.startsWith("type "))
    .length;
}

assert(existsSync(hookPath), "patient Professional Care workspace hook should exist");

const page = readFileSync(pagePath, "utf8");
const hook = readFileSync(hookPath, "utf8");
const sharedApi = readFileSync(sharedApiPath, "utf8");
const searchService = readFileSync(searchServicePath, "utf8");

assert(page.includes("usePatientProfessionalCareWorkspace"), "patient Professional Care page should import the workspace hook");
assert(directApiFunctionImportCount(page) <= 2, "patient Professional Care page should not orchestrate shared API calls directly");
assert(hook.includes("export function usePatientProfessionalCareWorkspace"), "patient hook should export usePatientProfessionalCareWorkspace");
assert(page.includes("const DOCTORS_PER_PAGE = 6"), "Professional Care should display six doctors per page");
assert(page.includes("availableDoctorRows.slice("), "Professional Care should render only the current doctor page");
assert(page.includes("doctorPageCount"), "Professional Care should calculate the doctor page count");
assert(page.includes("Previous"), "Professional Care should expose a previous-page control");
assert(page.includes("Next"), "Professional Care should expose a next-page control");
assert(searchService.includes("limit: int | None = None"), "doctor search should support returning all ranked doctors for client-side pagination");
assert(searchService.includes("sorted(entries, key=rank_key)"), "doctor search should rank the full directory before optional limiting");
assert(page.includes("rankedAvailableDoctorResults.length"), "Professional Care should preserve the ranked-results fallback");
assert(!searchService.includes("_direct_profile_match(entry, query_text)"), "doctor search should not filter out directory doctors by direct token overlap");
assert(page.includes("availableDoctors.map"), "Professional Care should preserve all available doctors when ranked results are empty");


assert(hook.includes("listProfessionalCareDoctors"), "patient hook should load the doctor directory");
assert(hook.includes("searchProfessionalCareDoctors"), "patient hook should call backend Semantic Doctor Search");
assert(hook.includes("doctorSearchQuery"), "patient hook should own the doctor search query");
assert(hook.includes("doctorSearchResults"), "patient hook should expose ranked doctor search results");
assert(hook.includes("searchDoctors"), "patient hook should expose a doctor search action");
assert(hook.includes('searchProfessionalCareDoctors(nextEpisode.care_episode_id, "", nextAuth.access_token)') || hook.includes("searchProfessionalCareDoctors(nextEpisode.care_episode_id, searchQuery, nextAuth.access_token)"), "subscribed patient workspace should load empty-query ranked Semantic Doctor Search results");
assert(hook.includes("listCareConnectionRequests"), "patient hook should load Care Connection Requests");
assert(hook.includes("selectCareRelationshipDoctor"), "patient hook should own relationship selection");
assert(hook.includes("listCareConversationMessages"), "patient hook should load Care Conversation messages");
assert(hook.includes("generateAICareSummary"), "patient hook should load AI Care Summary state");
assert(hook.includes("createCareConversationMessage"), "patient hook should own message sending");
assert(hook.includes("optimisticMessage"), "patient hook should create a local optimistic message before the server response");
assert(hook.includes("client_message_id"), "patient hook should track optimistic messages with a client-side id");
assert(hook.includes("setMessages((prev) => [...prev, optimisticMessage])"), "patient hook should append the optimistic message before awaiting the server response");
assert(hook.includes("prev.map((item) => item.message_id === optimisticMessage.message_id ? message : item)"), "patient hook should reconcile the optimistic message with the server response");
assert(hook.includes("prev.filter((item) => item.message_id !== optimisticMessage.message_id)"), "patient hook should roll back the optimistic message on failure");
assert(hook.includes("loadRequestIdRef"), "patient hook should guard stale workspace loads");
assert(hook.includes("if (loadRequestIdRef.current !== requestId) return"), "patient hook should ignore stale workspace responses");
assert(hook.includes("doctorSearchRequestIdRef"), "patient hook should guard stale doctor search responses separately from workspace loads");
assert(hook.includes("doctorSearchQueryRef"), "patient hook should track doctor search query context for stale response checks");
assert(hook.includes("setDoctorSearchResults([])"), "patient hook should clear visible doctor search results when the query changes");
assert(hook.includes("doctorSearchRequestIdRef.current !== searchRequestId"), "patient hook should ignore older doctor search responses");
assert(hook.includes("doctorSearchQueryRef.current !== searchQuery"), "patient hook should not apply results for an outdated doctor search query");
assert(!hook.includes("finally {\n            if (doctorSearchRequestIdRef.current !== searchRequestId || doctorSearchQueryRef.current !== searchQuery) return;\n            setBusy(false);"), "patient hook should clear search busy state when the current request completes even if query text changed");
assert(hook.includes("finally {\n            if (doctorSearchRequestIdRef.current !== searchRequestId) return;\n            setBusy(false);"), "patient hook should only let the current search request clear busy state");
assert(hook.includes(`setMessageText((current) => current === "" ? content : current)`), "patient hook should not overwrite newer draft text on failed send");
assert(page.includes("Doctor Selection Queue"), "patient Professional Care should use Doctor Selection Queue language");
assert(page.includes("Selected doctor"), "queue should show selected doctor first");
assert(page.includes("Accepted doctors"), "queue should group accepted doctors");
assert(page.includes("Pending requests"), "queue should group pending requests");
assert(page.includes("Find another doctor"), "queue should keep extra doctors secondary");
assert(!page.includes(".slice(0, 4)"), "Find another doctor should not silently cap doctors without a discovery control");
assert(hook.includes("requestReasonByDoctor"), "request reason should be contextual per doctor");
assert(!page.includes("<h2 className=\"section-title\">Request a doctor</h2>"), "page should not lead with old request-a-doctor directory section");
assert(!page.includes("textarea className=\"textarea min-h-24\" value={requestReason}"), "request reason should not be a giant page-level textarea");
assert(page.includes("doctor.display_name"), "doctor rows should use display names from the directory response");
assert(page.includes("doctor.expertise_tags"), "doctor rows should show Doctor Expertise Tags");
assert(hook.includes("setRequestReasonByDoctor"), "patient hook should expose setRequestReasonByDoctor");
assert(hook.includes(`requestReasonByDoctor[doctorId] || ""`), "sendRequest should use the note for the requested doctor");
assert(hook.includes('setRequestReasonByDoctor((prev) => ({ ...prev, [doctorId]: "" }))'), "successful send should clear only that doctor note");
assert(page.includes("doctor.specialty || doctor.verification_status"), "doctor rows should render specialty or verification status");
assert(page.includes("requestReasonByDoctor[doctor.doctor_id]"), "doctor request note textarea should be bound per doctor");
assert(page.includes("setRequestReasonByDoctor((prev) => ({ ...prev, [doctor.doctor_id]: event.target.value }))"), "doctor request note textarea should update per doctor");
assert(sharedApi.includes("display_name: string"), "DoctorDirectoryEntry should include display_name in shared API type");
assert(sharedApi.includes("specialty?: string | null"), "DoctorDirectoryEntry should include specialty in shared API type");
assert(sharedApi.includes("expertise_tags: string[]"), "DoctorDirectoryEntry should include expertise_tags in shared API type");
assert(!sharedApi.includes("doctor_name: string;\n\tdisplay_name: string"), "DoctorDirectoryEntry should not expose raw doctor_name");
assert(!sharedApi.includes("real_info: Record<string, unknown>"), "DoctorDirectoryEntry should not expose raw real_info");

console.log("patient Professional Care workspace contract ok");

assert(sharedApi.includes("export type DoctorSearchResult"), "shared API should export DoctorSearchResult");
assert(sharedApi.includes("match_reason: string"), "DoctorSearchResult should expose match_reason");
assert(sharedApi.includes("searchProfessionalCareDoctors"), "shared API should expose backend Semantic Doctor Search");
assert(page.includes("doctorSearchQuery"), "Find another doctor should render the search query input");
assert(page.includes("doctorSearchResults"), "Find another doctor should render ranked search results");
assert(!page.includes("result.match_reason"), "Doctor rows should not display match-reason copy");
assert(page.includes("result.doctor.expertise_tags"), "Semantic Doctor Search rows should show Doctor Expertise Tags");
assert(!page.includes("availableDoctors.filter"), "Find another doctor should not rely on frontend-only filtering as the search path");
