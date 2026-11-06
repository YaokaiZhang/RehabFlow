// One place for patient-facing Rehab Safety Gate copy (overview, rehab, history).
// Any status we don't recognise is treated as "a clinician should look first".

export type SafetyTone = "success" | "attention" | "risk";

const CLEARED_STATUSES = new Set(["triage_complete", "clinician_reviewed"]);

export function isSafetyCleared(status: string | null | undefined) {
	return CLEARED_STATUSES.has(String(status || ""));
}

export function needsClinicianReview(status: string | null | undefined) {
	const value = String(status || "");
	return Boolean(value) && value !== "needs_triage" && !CLEARED_STATUSES.has(value);
}

export function safetyStatus(status: string | null | undefined): { label: string; tone: SafetyTone } {
	if (status === "needs_triage" || !status) return { label: "Triage first", tone: "attention" };
	if (status === "clinician_reviewed") return { label: "Cleared by a clinician", tone: "success" };
	if (status === "triage_complete") return { label: "Cleared by AI triage", tone: "success" };
	return { label: "Clinician check needed", tone: "risk" };
}

// Older summaries stored a "No explicit safety signals..." note as if it were a
// signal. Drop those so a clean triage is not shown as a red flag.
export function realSafetySignals(signals: string[] | null | undefined): string[] {
	return (signals || []).filter((signal) => signal && signal.trim() && !/^no explicit (safety signals|red-flag symptoms)\b/i.test(signal.trim()));
}
