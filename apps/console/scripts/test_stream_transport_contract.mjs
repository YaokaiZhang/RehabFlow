import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const page = readFileSync(resolve("apps/console/app/page.tsx"), "utf8");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

assert(page.includes("issuePatientStreamTicket"), "console patient movement should use the shared patient ticket helper");
assert(page.includes("issueDoctorMonitorTicket"), "console doctor monitoring should use the shared doctor ticket helper");
assert(page.includes("connectPatientStream(ticket)"), "console patient movement should connect with a ticket");
assert(page.includes("connectDoctorEpisodeMonitor(careEpisodeSnapshot, ticket)"), "console doctor monitoring should connect by Care Episode with a ticket");
assert(page.includes("auth.access_token"), "console ticket issuance should pass the active auth token");
assert(!page.includes("new WebSocket("), "console movement should not construct socket URLs directly");
assert(!page.includes("/rehab/stream/"), "console movement must not use patient-id socket URLs");
assert(!page.includes("/doctor/monitor/"), "console doctor monitoring must not use legacy patient-id socket URLs");
assert(!page.includes("access_token="), "console socket URLs must not include access tokens");
assert(!page.includes("doctor_id="), "console socket URLs must not include caller doctor ids");
assert(page.includes("Care Episode ID"), "console doctor monitor should accept a Care Episode ID");
assert(!page.includes("Patient UUID To Monitor"), "console doctor monitor must not ask for a patient id");
assert(page.includes("if (!ticket ||"), "console movement should gate socket creation on ticket issuance");

const patientAttemptStart = page.indexOf("const socket = connectPatientStream(ticket)");
const patientAttemptBlock = page.slice(patientAttemptStart, page.indexOf("const startDoctorStream", patientAttemptStart));
const doctorAttemptStart = page.indexOf("const socket = connectDoctorEpisodeMonitor(careEpisodeSnapshot, ticket)");
const doctorAttemptBlock = page.slice(doctorAttemptStart);
assert(page.includes("patientStreamAttemptRef"), "console patient stream should track a separate connection attempt generation");
assert(page.includes("doctorStreamAttemptRef"), "console doctor stream should track a separate connection attempt generation");
assert(page.includes("patientStreamAttemptRef.current += 1"), "patient stream starts and closes should invalidate older attempts");
assert(page.includes("doctorStreamAttemptRef.current += 1"), "doctor stream starts and closes should invalidate older attempts");
assert(page.includes("const attempt = patientStreamAttemptRef.current + 1"), "patient ticket issuance should snapshot its attempt generation");
assert(page.includes("const attempt = doctorStreamAttemptRef.current + 1"), "doctor ticket issuance should snapshot its attempt generation");
assert(page.split("if (!ticket || !isCurrentAttempt()) return;").length === 3, "both ticket resolutions should guard stale attempts");
assert(patientAttemptBlock.includes("if (!isCurrentAttempt() || patientSocketRef.current !== socket) return;"), "patient socket callbacks should ignore stale attempts");
assert(doctorAttemptBlock.includes("if (!isCurrentAttempt() || doctorSocketRef.current !== socket) return;"), "doctor socket callbacks should ignore stale attempts");
console.log("console stream transport contract ok");
