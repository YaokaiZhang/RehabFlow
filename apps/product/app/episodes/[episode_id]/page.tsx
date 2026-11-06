"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

import { getCareEpisodeWorkspaceSummary, type CareEpisodeWorkspaceSummary, type ProfessionalCareWorkspaceSummary, type TriageSummaryArtifact, type TriageSummaryDraft } from "@rehab/shared/api";
import { loadAuth } from "@rehab/shared/auth";

import { NextStep, nextStepFor } from "../../../components/NextStep";
import { safetyStatus } from "../../../components/safetyStatus";

type Params = { episode_id?: string };

function careStatusLabel(care: ProfessionalCareWorkspaceSummary) {
	if (care.has_active_relationship) return "With your doctor";
	if (care.subscription_tier === "professional_care") return "Subscribed";
	return "Not subscribed";
}

const REQUEST_STATUS_WORDS: Record<string, [string, string]> = {
	pending: ["request waiting", "requests waiting"],
	accepted: ["doctor accepted", "doctors accepted"],
	rejected: ["declined", "declined"],
};

function requestSummary(counts: Record<string, number>) {
	const entries = Object.entries(counts || {}).filter(([, count]) => count > 0);
	if (!entries.length) return "";
	return entries
		.map(([status, count]) => {
			const words = REQUEST_STATUS_WORDS[status];
			return words ? `${count} ${count === 1 ? words[0] : words[1]}` : `${count} ${status.replace(/_/g, " ")}`;
		})
		.join(" · ");
}

function countCompleted(checklist: Array<{ completed?: boolean }>) {
	return checklist.filter((item) => item.completed).length;
}

function shortDate(value?: string | null) {
	if (!value) return "";
	const date = new Date(value);
	if (Number.isNaN(date.getTime())) return "";
	const sameYear = date.getFullYear() === new Date().getFullYear();
	return date.toLocaleDateString(undefined, sameYear ? { day: "numeric", month: "short" } : { day: "numeric", month: "short", year: "numeric" });
}

type SummaryWithContext = TriageSummaryArtifact | TriageSummaryDraft;

function triageContextSummary(summary: SummaryWithContext | null | undefined) {
	return summary?.relevant_context?.trim() || "";
}

function fullTriageHistory(summary: SummaryWithContext | null | undefined) {
	return summary?.source_conversation_transcript?.trim() || "";
}

function FullAiTriageHistory({ text }: { text: string }) {
	return (
		<details className="ep-disclosure">
			<summary>Full AI triage history</summary>
			<p className="whitespace-pre-line">{text}</p>
		</details>
	);
}

export default function CareEpisodeOverviewPage() {
	const params = useParams<Params>();
	const episodeId = params?.episode_id || "";
	const [workspace, setWorkspace] = useState<CareEpisodeWorkspaceSummary | null>(null);
	const [error, setError] = useState("");
	const [loading, setLoading] = useState(true);

	useEffect(() => {
		const auth = loadAuth();
		if (!auth || auth.role !== "patient") {
			setError("Login as a patient to open this Care Episode.");
			setLoading(false);
			return;
		}
		getCareEpisodeWorkspaceSummary(episodeId, auth.access_token)
			.then(setWorkspace)
			.catch((err) => setError((err as Error).message))
			.finally(() => setLoading(false));
	}, [episodeId]);

	if (loading) return <p className="ep-loading">Loading...</p>;
	if (error) return <section className="page-column-narrow"><p className="notice notice-danger">{error}</p></section>;
	if (!workspace) return null;

	const { episode, latest_triage_summary: triage, unsaved_triage_summaries: unsavedTriageSummaries, latest_rehab_session: rehab, latest_ai_care_summary: careSummary, professional_care: professionalCare } = workspace;
	const base = `/episodes/${episode.care_episode_id}`;
	const doctorLabel = professionalCare.selected_doctor_name || (professionalCare.selected_doctor_id ? "Your doctor" : "No doctor yet");
	const completedCount = rehab ? countCompleted(rehab.checklist) : 0;
	const rehabTotal = rehab ? rehab.checklist.length : 0;
	const triageContext = triageContextSummary(triage);
	const latestFullHistory = fullTriageHistory(triage);
	const startedOn = shortDate(episode.symptom_started_on);
	const updatedOn = shortDate(episode.updated_at);
	const step = nextStepFor(workspace);
	// The next-step sticker already says "triage"; don't repeat it as a session card.
	const hideTriageCard = step.kind === "start-triage" || step.kind === "save-triage";
	const safety = safetyStatus(episode.safety_gate_status);
	const requests = requestSummary(professionalCare.request_counts);

	return (
		<div className="ep-overview">
			<header className="ep-head">
				<h1 className="ep-title">{episode.issue_title}</h1>
				<ul className="ep-chips" aria-label="Episode details">
					<li className="ep-chip"><span className="ep-chip-label">Body area</span>{episode.body_area}</li>
					<li className="ep-chip"><span className="ep-chip-label">Goal</span>{episode.goal}</li>
					{startedOn ? <li className="ep-chip"><span className="ep-chip-label">Since</span>{startedOn}</li> : null}
				</ul>
				{triageContext ? (
					<details className="ep-disclosure ep-head-context">
						<summary>Triage context summary</summary>
						<p className="whitespace-pre-line">{triageContext}</p>
					</details>
				) : null}
			</header>

			<NextStep step={step} />

			<section className="ep-sessions" aria-label="Sessions in this episode">
				{hideTriageCard ? null : (
					<Link className="sticker tone-peach tilt-l ep-session" href={`${base}/triage`}>
						<span className="ep-session-top">
							<span className="tag"><span className="session-dot" />AI Triage Intake</span>
							<span className="ep-session-status">{triage ? "Summary saved" : "Not started"}</span>
						</span>
						{triage ? <span className="ep-session-quote">{triage.recommendation}</span> : null}
						{unsavedTriageSummaries.length ? (
							<span className="ep-session-foot">{`${unsavedTriageSummaries.length} unsaved draft${unsavedTriageSummaries.length === 1 ? "" : "s"}`}</span>
						) : null}
					</Link>
				)}

				<Link className="sticker tone-mint tilt-r ep-session" href={`${base}/rehab`}>
					<span className="ep-session-top">
						<span className="tag"><span className="session-dot" />AI Daily Rehab</span>
						<span className="ep-session-status">{safety.label}</span>
					</span>
					{rehab ? (
						<>
							<span className="ep-session-datum"><strong>{completedCount} of {rehabTotal}</strong> exercises done</span>
							{rehabTotal ? <span className="ep-progress" aria-hidden="true"><span style={{ width: `${Math.round((completedCount / rehabTotal) * 100)}%` }} /></span> : null}
							{rehab.session_summary ? <span className="ep-session-quote ep-session-quote-sm">{rehab.session_summary}</span> : null}
						</>
					) : (
						<span className="ep-session-empty">no sessions yet</span>
					)}
					{rehab ? <span className="ep-session-foot">{`Last session ${shortDate(rehab.created_at)}`}</span> : null}
				</Link>

				<Link className="sticker tone-sky tilt-r ep-session" href={`${base}/professional-care`}>
					<span className="ep-session-top">
						<span className="tag"><span className="session-dot" />Professional Care</span>
						<span className="ep-session-status">{careStatusLabel(professionalCare)}</span>
					</span>
					<span className="ep-session-datum"><strong>{doctorLabel}</strong></span>
					{requests ? <span className="ep-session-meta">{requests}</span> : null}
					{careSummary ? <span className="ep-session-quote ep-session-quote-sm">{careSummary.plan_digest || careSummary.conversation_digest}</span> : null}
				</Link>

				<Link className="sticker tone-lilac tilt-l ep-session" href={`${base}/history`}>
					<span className="ep-session-top">
						<span className="tag"><span className="session-dot" />History</span>
					</span>
					{updatedOn ? <span className="ep-session-datum"><strong>{`Updated ${updatedOn}`}</strong></span> : null}
				</Link>
			</section>

			{latestFullHistory ? (
				<section className="ep-papers" aria-labelledby="ep-papers-title">
					<h2 id="ep-papers-title" className="ep-papers-title"><span className="session-dot" data-tone="peach" />Triage notes</h2>
					<FullAiTriageHistory text={latestFullHistory} />
				</section>
			) : null}
		</div>
	);
}
