"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

import { deleteTriageSummaryDraft, getCareEpisodeWorkspaceSummary, type CareEpisode, type CareEpisodeWorkspaceSummary, type TriageSummaryArtifact, type TriageSummaryDraft } from "@rehab/shared/api";
import { loadAuth, type AuthState } from "@rehab/shared/auth";

import { ArrowIcon } from "../../../components/CareEpisodeBrief";
import { AppButton } from "../../../components/ui";

type Params = { episode_id?: string };
type Tone = "peach" | "mint" | "sky" | "lilac";

function safetyLabel(status: CareEpisode["safety_gate_status"]) {
	if (status === "needs_triage") return "Opens after AI triage";
	if (status === "clinician_reviewed") return "Cleared by a clinician";
	return "Cleared by AI triage";
}

function tierLabel(tier: string) {
	if (tier === "professional_care") return "Professional Care plan";
	if (tier === "self_serve") return "Self-guided plan";
	return tier.replace(/_/g, " ");
}

function requestSummary(counts: Record<string, number>) {
	const entries = Object.entries(counts).filter(([, count]) => count > 0);
	if (!entries.length) return "No doctor requests yet";
	return entries.map(([status, count]) => `${count} ${status.replace(/_/g, " ")}`).join(", ");
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
			<p className="whitespace-pre-line">{text || "No full AI triage history was saved for this summary."}</p>
		</details>
	);
}

export default function CareEpisodeOverviewPage() {
	const params = useParams<Params>();
	const episodeId = params?.episode_id || "";
	const [workspace, setWorkspace] = useState<CareEpisodeWorkspaceSummary | null>(null);
	const [auth, setAuth] = useState<AuthState | null>(null);
	const [error, setError] = useState("");
	const [statusMessage, setStatusMessage] = useState("");
	const [loading, setLoading] = useState(true);
	const [deletingDraftId, setDeletingDraftId] = useState<string | null>(null);

	useEffect(() => {
		const auth = loadAuth();
		setAuth(auth);
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

	const deleteUnsavedSummary = async (draftId: string) => {
		if (!auth || !workspace || deletingDraftId) return;
		setError("");
		setStatusMessage("");
		setDeletingDraftId(draftId);
		try {
			await deleteTriageSummaryDraft(workspace.episode.care_episode_id, draftId, auth.access_token);
			setWorkspace({
				...workspace,
				unsaved_triage_summaries: workspace.unsaved_triage_summaries.filter((draft) => draft.triage_summary_draft_id !== draftId),
			});
			setStatusMessage("Unsaved Triage Summary deleted.");
		} catch (err) {
			setError((err as Error).message);
		} finally {
			setDeletingDraftId(null);
		}
	};

	if (loading) return <p className="ep-loading">Loading Care Episode...</p>;
	if (error) return <section className="page-column-narrow"><p className="notice notice-danger">{error}</p></section>;
	if (!workspace) return null;

	const { episode, latest_triage_summary: triage, unsaved_triage_summaries: unsavedTriageSummaries, latest_rehab_session: rehab, latest_ai_care_summary: careSummary, professional_care: professionalCare } = workspace;
	const base = `/episodes/${episode.care_episode_id}`;
	const selectedDoctorLabel = professionalCare.selected_doctor_name || (professionalCare.selected_doctor_id ? "Selected doctor" : "No doctor yet");
	const completedCount = rehab ? countCompleted(rehab.checklist) : 0;
	const rehabTotal = rehab ? rehab.checklist.length : 0;
	const triageContext = triageContextSummary(triage);
	const latestFullHistory = fullTriageHistory(triage);
	const startedOn = shortDate(episode.symptom_started_on);
	const updatedOn = shortDate(episode.updated_at);

	const nextAction: { tone: Tone; title: string; line: string; href: string; cta: string; note: string } = episode.safety_gate_status === "needs_triage"
		? {
			tone: "peach",
			title: "Complete AI Triage Intake",
			line: "A few safety questions first.",
			href: `${base}/triage`,
			cta: "Open Triage Intake",
			note: "start here",
		}
		: !rehab
			? {
				tone: "mint",
				title: "Start today's rehab",
				line: "Your first guided session.",
				href: `${base}/rehab`,
				cta: "Start today's rehab",
				note: "you're cleared",
			}
			: completedCount < rehabTotal
				? {
					tone: "mint",
					title: "Continue today's rehab",
					line: `${rehabTotal - completedCount} of ${rehabTotal} exercises left.`,
					href: `${base}/rehab`,
					cta: "Continue rehab",
					note: "nearly there",
				}
				: {
					tone: "lilac",
					title: "Review episode history",
					line: "See what has changed.",
					href: `${base}/history`,
					cta: "Review history",
					note: "all done today",
				};

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

			<section className={`ep-next sticker tone-${nextAction.tone}`} aria-live="polite" aria-labelledby="ep-next-title">
				<div className="ep-next-copy">
					<span className="tag">Next step</span>
					<h2 id="ep-next-title" className="ep-next-title">{nextAction.title}</h2>
					<p className="ep-next-line">{nextAction.line}</p>
				</div>
				<div className="ep-next-action">
					<span className="scribble ep-next-note" aria-hidden="true">{nextAction.note}</span>
					<Link className="btn-primary ep-next-cta" href={nextAction.href}>{nextAction.cta}<ArrowIcon /></Link>
				</div>
			</section>

			<section className="ep-sessions" aria-label="Sessions in this episode">
				<Link className="sticker tone-peach tilt-l ep-session" href={`${base}/triage`}>
					<span className="ep-session-top">
						<span className="tag"><span className="session-dot" />AI Triage Intake</span>
						<span className="ep-session-status">{triage ? "Summary saved" : "Not started"}</span>
					</span>
					{triage ? (
						<span className="ep-session-quote">{triage.recommendation}</span>
					) : (
						<span className="ep-session-empty">nothing saved yet</span>
					)}
					<span className="ep-session-foot">
						{unsavedTriageSummaries.length ? `${unsavedTriageSummaries.length} unsaved draft${unsavedTriageSummaries.length === 1 ? "" : "s"}` : triage ? `Version ${triage.version}` : ""}
						<span className="ep-session-go">{triage ? "Review or update triage" : "Open triage intake"}</span>
					</span>
				</Link>

				<Link className="sticker tone-mint tilt-r ep-session" href={`${base}/rehab`}>
					<span className="ep-session-top">
						<span className="tag"><span className="session-dot" />AI Daily Rehab</span>
						<span className="ep-session-status">{safetyLabel(episode.safety_gate_status)}</span>
					</span>
					{rehab ? (
						<>
							<span className="ep-session-datum"><strong>{completedCount} of {rehabTotal}</strong> exercises done</span>
							{rehabTotal ? <span className="ep-progress" aria-hidden="true"><span style={{ width: `${Math.round((completedCount / rehabTotal) * 100)}%` }} /></span> : null}
							{rehab.session_summary ? <span className="ep-session-quote ep-session-quote-sm">{rehab.session_summary}</span> : null}
						</>
					) : (
						<span className="ep-session-empty">{episode.safety_gate_status === "needs_triage" ? "waiting on triage" : "no sessions yet"}</span>
					)}
					<span className="ep-session-foot">
						{rehab ? `Last session ${shortDate(rehab.created_at)}` : ""}
						<span className="ep-session-go">Open rehab</span>
					</span>
				</Link>

				<Link className="sticker tone-sky tilt-r ep-session" href={`${base}/professional-care`}>
					<span className="ep-session-top">
						<span className="tag"><span className="session-dot" />Professional Care</span>
						<span className="ep-session-status">{tierLabel(professionalCare.subscription_tier)}</span>
					</span>
					<span className="ep-session-datum"><strong>{selectedDoctorLabel}</strong></span>
					<span className="ep-session-meta">{requestSummary(professionalCare.request_counts)}</span>
					{careSummary ? <span className="ep-session-quote ep-session-quote-sm">{careSummary.plan_digest || careSummary.conversation_digest}</span> : null}
					<span className="ep-session-foot">
						{professionalCare.has_active_relationship ? "Working with a doctor" : ""}
						<span className="ep-session-go">Open Professional Care</span>
					</span>
				</Link>

				<Link className="sticker tone-lilac tilt-l ep-session" href={`${base}/history`}>
					<span className="ep-session-top">
						<span className="tag"><span className="session-dot" />History</span>
						<span className="ep-session-status">Timeline and memory</span>
					</span>
					<span className="ep-session-datum"><strong>{updatedOn ? `Updated ${updatedOn}` : "Everything saved"}</strong></span>
					<span className="ep-session-meta">{[triage ? "Triage" : null, rehab ? "Rehab" : null, careSummary ? "Care summary" : null].filter(Boolean).join(" · ") || "Summaries, sessions and memory"}</span>
					<span className="ep-session-foot">
						{shortDate(episode.created_at) && shortDate(episode.created_at) !== updatedOn ? `Opened ${shortDate(episode.created_at)}` : ""}
						<span className="ep-session-go">Open history</span>
					</span>
				</Link>
			</section>

			{triage || unsavedTriageSummaries.length || statusMessage || error ? (
				<section className="ep-papers" aria-labelledby="ep-papers-title">
					<h2 id="ep-papers-title" className="ep-papers-title"><span className="session-dot" data-tone="peach" />Triage notes</h2>
					{statusMessage ? <p className="notice" role="status">{statusMessage}</p> : null}
					{error ? <p className="notice notice-danger">{error}</p> : null}
					{triage ? <FullAiTriageHistory text={latestFullHistory} /> : null}
					{unsavedTriageSummaries.length ? (
						<div className="ep-drafts">
							<p className="ep-drafts-label">Draft summaries you haven&apos;t saved</p>
							{unsavedTriageSummaries.map((draft) => {
								const draftFullHistory = fullTriageHistory(draft);
								return (
									<article key={draft.triage_summary_draft_id} className="ep-draft">
										<p>{draft.recommendation}</p>
										<FullAiTriageHistory text={draftFullHistory} />
										<AppButton variant="danger" className="ep-draft-delete" onClick={() => deleteUnsavedSummary(draft.triage_summary_draft_id)} disabled={deletingDraftId === draft.triage_summary_draft_id}>
											{deletingDraftId === draft.triage_summary_draft_id ? "Deleting" : "Delete draft"}
										</AppButton>
									</article>
								);
							})}
						</div>
					) : null}
				</section>
			) : null}
		</div>
	);
}
