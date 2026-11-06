import type { CareEpisodeWorkspaceSummary, TriageSummaryArtifact } from "@rehab/shared/api";

type Props = {
	workspace: CareEpisodeWorkspaceSummary;
	role: "patient" | "doctor";
	compact?: boolean;
	showTitle?: boolean;
	showContext?: boolean;
};

/** The circled arrow that rides inside a black primary pill. */
export function ArrowIcon() {
	return (
		<span className="btn-arrow" aria-hidden="true">
			<svg width="12" height="12" viewBox="0 0 12 12" fill="none">
				<path d="M2.5 6h7M6.5 3l3 3-3 3" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
			</svg>
		</span>
	);
}

function listText(items: string[] | undefined, fallback: string) {
	return items && items.length ? items.join(" ") : fallback;
}

function summaryRisk(summary: TriageSummaryArtifact | null | undefined) {
	if (!summary) return "Not triaged yet";
	if (summary.clinician_review_needed) return "Clinician review recommended";
	if (summary.safety_signals.length) return summary.safety_signals.join(" ");
	return "No major safety signals captured.";
}

function riskTone(summary: TriageSummaryArtifact | null | undefined) {
	if (!summary) return "peach";
	if (summary.clinician_review_needed || summary.safety_signals.length) return "rose";
	return "mint";
}

function triageContextSummary(summary: TriageSummaryArtifact | null | undefined) {
	return summary?.relevant_context?.trim() || "";
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
		<section
			className="ep-brief"
			aria-labelledby={showTitle ? `ep-brief-${episode.care_episode_id}` : undefined}
			aria-label={showTitle ? undefined : episode.issue_title}
		>
			{showTitle ? <h2 id={`ep-brief-${episode.care_episode_id}`} className="ep-brief-title">{episode.issue_title}</h2> : null}
			<ul className="ep-chips" aria-label="Episode details">
				<li className="ep-chip"><span className="ep-chip-label">Body area</span>{episode.body_area}</li>
				<li className="ep-chip"><span className="ep-chip-label">Goal</span>{episode.goal}</li>
			</ul>
			{showContext && triageContext ? (
				<details className="ep-disclosure">
					<summary>Triage context summary</summary>
					<p className="whitespace-pre-line">{triageContext}</p>
				</details>
			) : null}

			<dl className="ep-brief-facts">
				{role === "doctor" ? (
					<div className="ep-brief-fact" data-tone={riskTone(triage)}>
						<dt><span className="session-dot" />Safety</dt>
						<dd>{summaryRisk(triage)}</dd>
					</div>
				) : null}
				<div className="ep-brief-fact" data-tone="mint">
					<dt><span className="session-dot" />Rehab</dt>
					<dd>{total ? `${completed} of ${total} exercises done` : "Not started"}</dd>
				</div>
				<div className="ep-brief-fact" data-tone="sky">
					<dt><span className="session-dot" />Professional Care</dt>
					<dd>{professionalCare.has_active_relationship ? "Working with a doctor" : "No doctor yet"}</dd>
				</div>
			</dl>

			{triage && (role === "doctor" || compact) ? (
				<div className="ep-brief-triage" data-tone="peach">
					<span className="tag">Triage</span>
					<p>{triage.recommendation}</p>
				</div>
			) : null}

			{role === "doctor" && !compact && triage ? (
				<div className="ep-brief-note">
					<p className="ep-brief-note-title">Patient situation</p>
					<p>{triage.concern}</p>
					<p><strong>Limitations:</strong> {listText(triage.limitations, "None captured.")}</p>
					<p><strong>Unresolved questions:</strong> {listText(unresolvedQuestions, "None captured.")}</p>
				</div>
			) : null}
		</section>
	);
}
