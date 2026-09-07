import type { CareEpisodeWorkspaceSummary, TriageSummaryArtifact } from "@rehab/shared/api";

type Props = {
	workspace: CareEpisodeWorkspaceSummary;
	role: "patient" | "doctor";
	compact?: boolean;
};

function listText(items: string[] | undefined, fallback: string) {
	return items && items.length ? items.join(" ") : fallback;
}

function summaryRisk(summary: TriageSummaryArtifact | null | undefined) {
	if (!summary) return "No saved Triage Summary yet.";
	if (summary.clinician_review_needed) return "Clinician review recommended";
	if (summary.safety_signals.length) return summary.safety_signals.join(" ");
	return "No major safety signals captured.";
}

function triageContextSummary(summary: TriageSummaryArtifact | null | undefined) {
	return summary?.relevant_context?.trim() || "";
}

function TriageContextSummary({ text }: { text: string }) {
	return (
		<details className="mt-3 rounded-md border border-slate-200 bg-slate-50 p-3 text-sm text-slate-600">
			<summary className="cursor-pointer text-xs font-semibold uppercase tracking-wide text-slate-500">Triage context summary</summary>
			<p className="mt-2 whitespace-pre-line leading-6">{text}</p>
		</details>
	);
}

export function CareEpisodeBrief({ workspace, role, compact = false }: Props) {
	const {
		episode,
		latest_triage_summary: triage,
		latest_rehab_session: rehab,
		latest_ai_care_summary: careSummary,
		professional_care: professionalCare,
	} = workspace;
	const completed = Array.isArray(rehab?.checklist) ? rehab.checklist.filter((item) => item.completed).length : 0;
	const total = Array.isArray(rehab?.checklist) ? rehab.checklist.length : 0;
	const unresolvedQuestions = triage?.unresolved_questions.length
		? triage.unresolved_questions
		: careSummary?.unresolved_questions;
	const triageContext = triageContextSummary(triage);

	return (
		<section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm md:p-5">
			<div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
				<div className="min-w-0">
					<p className="text-xs font-semibold uppercase tracking-wide text-emerald-700">Care Episode Brief</p>
					<h2 className="mt-1 text-xl font-semibold text-slate-950">{episode.issue_title}</h2>
					{triageContext ? <TriageContextSummary text={triageContext} /> : null}
				</div>
				<span className="rounded-md bg-slate-100 px-3 py-1.5 text-xs font-semibold text-slate-700">
					{role === "doctor" ? "Doctor review" : "Patient view"}
				</span>
			</div>

			<div className="mt-4 grid gap-3 text-sm text-slate-700 md:grid-cols-3">
				<p><span className="font-semibold text-slate-950">Body area:</span> {episode.body_area}</p>
				<p><span className="font-semibold text-slate-950">Goal:</span> {episode.goal}</p>
				{role === "doctor" ? <p><span className="font-semibold text-slate-950">Safety:</span> {summaryRisk(triage)}</p> : null}
				<p><span className="font-semibold text-slate-950">Triage:</span> {triage ? triage.recommendation : "No saved summary"}</p>
				<p><span className="font-semibold text-slate-950">Rehab:</span> {total ? `${completed}/${total} complete` : "No rehab session yet"}</p>
				<p><span className="font-semibold text-slate-950">Professional Care:</span> {professionalCare.has_active_relationship ? "Active Care Relationship" : "No active relationship"}</p>
			</div>

			{role === "doctor" && !compact && triage ? (
				<div className="mt-4 rounded-md border border-emerald-100 bg-emerald-50 p-3 text-sm text-slate-700">
					<p className="font-semibold text-slate-950">Patient situation</p>
					<p className="mt-1">{triage.concern}</p>
					<p className="mt-2"><span className="font-semibold text-slate-950">Limitations:</span> {listText(triage.limitations, "None captured.")}</p>
					<p className="mt-1"><span className="font-semibold text-slate-950">Unresolved questions:</span> {listText(unresolvedQuestions, "None captured.")}</p>
				</div>
			) : null}
		</section>
	);
}
