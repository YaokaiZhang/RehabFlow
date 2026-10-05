import type { CareEpisodeWorkspaceSummary, TriageSummaryArtifact } from "@rehab/shared/api";

type Props = {
	workspace: CareEpisodeWorkspaceSummary;
	role: "patient" | "doctor";
	compact?: boolean;
	showTitle?: boolean;
	showContext?: boolean;
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
		<details className="episode-context mt-3 text-sm text-slate-600">
			<summary className="cursor-pointer text-xs font-semibold uppercase tracking-wide text-slate-500">Triage context summary</summary>
			<p className="mt-2 whitespace-pre-line leading-6">{text}</p>
		</details>
	);
}

export function CareEpisodeBrief({ workspace, role, compact = false, showTitle = true, showContext = true }: Props) {
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
		<section className="episode-brief">
			<div className="episode-brief-header">
				<div className="min-w-0">
					<h2 className="episode-brief-title">Care Episode Brief</h2>
					{showTitle ? <p className="episode-brief-subtitle">{episode.issue_title}</p> : null}
					{showContext && triageContext ? <TriageContextSummary text={triageContext} /> : null}
				</div>
				<span className="episode-brief-view">
					{role === "doctor" ? "Doctor review" : "Patient view"}
				</span>
			</div>

			<div className="episode-brief-facts">
				<p className="episode-brief-fact"><span className="episode-brief-label">Body area</span><span>{episode.body_area}</span></p>
				<p className="episode-brief-fact"><span className="episode-brief-label">Goal</span><span>{episode.goal}</span></p>
				{role === "doctor" ? <p className="episode-brief-fact"><span className="episode-brief-label">Safety</span><span>{summaryRisk(triage)}</span></p> : null}
				<p className="episode-brief-fact"><span className="episode-brief-label">Rehab</span><span>{total ? `${completed}/${total} complete` : "No rehab session yet"}</span></p>
				<p className="episode-brief-fact"><span className="episode-brief-label">Professional Care</span><span>{professionalCare.has_active_relationship ? "Active Care Relationship" : "No active relationship"}</span></p>
			</div>

			<div className="episode-brief-triage">
				<span className="episode-brief-label">Triage</span>
				<p>{triage ? triage.recommendation : "No saved summary"}</p>
			</div>

			{role === "doctor" && !compact && triage ? (
				<div className="episode-brief-doctor-note text-sm text-slate-700">
					<p className="font-semibold text-slate-950">Patient situation</p>
					<p className="mt-1">{triage.concern}</p>
					<p className="mt-2"><span className="font-semibold text-slate-950">Limitations:</span> {listText(triage.limitations, "None captured.")}</p>
					<p className="mt-1"><span className="font-semibold text-slate-950">Unresolved questions:</span> {listText(unresolvedQuestions, "None captured.")}</p>
				</div>
			) : null}
		</section>
	);
}
