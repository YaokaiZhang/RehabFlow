"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

import { deleteTriageSummaryDraft, getCareEpisodeWorkspaceSummary, type CareEpisode, type CareEpisodeWorkspaceSummary, type TriageSummaryArtifact, type TriageSummaryDraft } from "@rehab/shared/api";
import { loadAuth, type AuthState } from "@rehab/shared/auth";

import { CareEpisodeBrief } from "../../../components/CareEpisodeBrief";
import { AppButton, StatusBadge } from "../../../components/ui";

type Params = { episode_id?: string };

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

type SummaryWithContext = TriageSummaryArtifact | TriageSummaryDraft;

function triageContextSummary(summary: SummaryWithContext | null | undefined) {
	return summary?.relevant_context?.trim() || "";
}

function fullTriageHistory(summary: SummaryWithContext | null | undefined) {
	return summary?.source_conversation_transcript?.trim() || "";
}

function TriageContextSummary({ text }: { text: string }) {
	return (
		<details className="episode-context mt-3 text-sm text-slate-600">
			<summary className="cursor-pointer text-sm font-semibold text-slate-500">Triage context summary</summary>
			<p className="mt-2 whitespace-pre-line leading-6">{text}</p>
		</details>
	);
}

function FullAiTriageHistory({ text }: { text: string }) {
	return (
		<details className="rounded-md border border-slate-200 bg-slate-50 p-3 text-slate-600">
			<summary className="cursor-pointer text-sm font-semibold text-slate-500">Full AI triage history</summary>
			<p className="mt-2 whitespace-pre-line text-sm leading-6">{text || "No full AI triage history was saved for this summary."}</p>
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

	if (loading) return <p className="text-sm text-slate-500">Loading Care Episode...</p>;
	if (error) return <section className="card max-w-2xl text-sm text-rose-700">{error}</section>;
	if (!workspace) return null;

	const { episode, latest_triage_summary: triage, unsaved_triage_summaries: unsavedTriageSummaries, latest_rehab_session: rehab, latest_ai_care_summary: careSummary, professional_care: professionalCare } = workspace;
	const selectedDoctorLabel = professionalCare.selected_doctor_name || (professionalCare.selected_doctor_id ? "Selected doctor" : "None selected");
	const completedCount = rehab ? countCompleted(rehab.checklist) : 0;
	const triageContext = triageContextSummary(triage);
	const latestFullHistory = fullTriageHistory(triage);
	const nextAction = episode.safety_gate_status === "needs_triage"
		? {
			title: "Complete AI Triage Intake",
			description: "Answer the safety questions so RehabFlow can determine the right path for this episode.",
			href: `/episodes/${episode.care_episode_id}/triage`,
			cta: "Open Triage Intake",
		}
		: !rehab
			? {
			title: "Start today's rehab",
				description: "Your safety route is known. Begin the first guided session for this episode.",
				href: `/episodes/${episode.care_episode_id}/rehab`,
				cta: "Start today's rehab",
			}
			: {
				title: completedCount < rehab.checklist.length ? "Continue today's rehab" : "Review episode history",
				description: completedCount < rehab.checklist.length ? "Finish the remaining checklist items and save today's session." : "Review what has changed across this episode and decide what to do next.",
				href: completedCount < rehab.checklist.length ? `/episodes/${episode.care_episode_id}/rehab` : `/episodes/${episode.care_episode_id}/history`,
				cta: completedCount < rehab.checklist.length ? "Continue rehab" : "Review history",
			};

	return (
		<div className="episode-overview">
			<section className="episode-overview-header">
				<div className="episode-overview-header-content">
					<div className="min-w-0">
						<h1>{episode.issue_title}</h1>
						{triageContext ? <TriageContextSummary text={triageContext} /> : null}
					</div>
					<div className="episode-overview-actions">
						<Link className="btn-secondary" href={`/episodes/${episode.care_episode_id}/history`}>History</Link>
					</div>
				</div>
			</section>

			<section className="episode-next-action" aria-live="polite">
				<div>
					<p className="episode-next-action-kicker">Next step</p>
					<h2>{nextAction.title}</h2>
					<p>{nextAction.description}</p>
				</div>
				<Link className="btn-primary shrink-0" href={nextAction.href}>{nextAction.cta}</Link>
			</section>

			<CareEpisodeBrief workspace={workspace} role="patient" showTitle={false} showContext={false} />

			<section className="episode-session-directory" aria-labelledby="episode-sessions-title">
				<div className="episode-session-directory-header">
					<h2 id="episode-sessions-title">Sessions in this episode</h2>
				</div>
				<div className="episode-summary-grid mt-4">
					<div className="card episode-summary-card episode-summary-card-triage">
						<div className="flex flex-wrap items-start justify-between gap-3">
							<div>
								<h3 className="section-title">AI Triage Intake</h3>
								<p className="mt-1 text-sm text-[var(--rf-ink-muted)]">Safety questions that decide which care path is right.</p>
							</div>
							<StatusBadge tone={triage ? "success" : "attention"}>{triage ? "Summary saved" : "Not started"}</StatusBadge>
						</div>
						{triage ? (
							<div className="mt-4 space-y-3 text-sm text-slate-700">
								<p className="max-w-[68ch]">{triage.recommendation}</p>
								<FullAiTriageHistory text={latestFullHistory} />
							</div>
						) : null}
						{unsavedTriageSummaries.length ? (
							<div className="mt-4 space-y-2 border-t border-line pt-4">
								<p className="text-sm font-semibold">Draft summaries you haven&apos;t saved</p>
								{statusMessage ? <p className="text-sm text-emerald-700">{statusMessage}</p> : null}
								{error ? <p className="text-sm text-rose-600">{error}</p> : null}
								{unsavedTriageSummaries.map((draft) => {
									const draftFullHistory = fullTriageHistory(draft);
									return (
										<div key={draft.triage_summary_draft_id} className="rounded-control border border-line bg-[var(--rf-surface-muted)] p-4">
											<p className="text-sm text-slate-700">{draft.recommendation}</p>
											<div className="mt-3">
												<FullAiTriageHistory text={draftFullHistory} />
											</div>
											<AppButton variant="ghost" className="mt-2 px-2 text-danger" onClick={() => deleteUnsavedSummary(draft.triage_summary_draft_id)} disabled={deletingDraftId === draft.triage_summary_draft_id}>
												{deletingDraftId === draft.triage_summary_draft_id ? "Deleting" : "Delete draft"}
											</AppButton>
										</div>
									);
								})}
							</div>
						) : null}
						<Link className="mt-4 inline-flex font-semibold text-action underline-offset-4 hover:underline" href={`/episodes/${episode.care_episode_id}/triage`}>
							{triage ? "Review or update triage" : "Open triage intake"}
						</Link>
					</div>

					<Link className="next-step-card" href={`/episodes/${episode.care_episode_id}/rehab`}>
						<div className="flex flex-wrap items-start justify-between gap-3">
							<h3 className="section-title">AI Daily Rehab</h3>
							<StatusBadge tone={episode.safety_gate_status === "needs_triage" ? "neutral" : "success"}>{safetyLabel(episode.safety_gate_status)}</StatusBadge>
						</div>
						{rehab ? (
							<div className="mt-3 space-y-2 text-sm text-slate-700">
								<p><span className="font-semibold text-slate-950">Last session:</span> {completedCount} of {rehab.checklist.length} exercises done</p>
								{rehab.session_summary ? <p>{rehab.session_summary}</p> : null}
							</div>
						) : (
							<p className="mt-3 text-sm text-[var(--rf-ink-muted)]">Guided exercise sessions with a checklist and a saved summary.</p>
						)}
					</Link>

					<Link className="next-step-card" href={`/episodes/${episode.care_episode_id}/professional-care`}>
						<div className="flex flex-wrap items-start justify-between gap-3">
							<h3 className="section-title">Professional Care</h3>
							<StatusBadge tone={professionalCare.has_active_relationship ? "success" : "neutral"}>{tierLabel(professionalCare.subscription_tier)}</StatusBadge>
						</div>
						<div className="mt-3 space-y-2 text-sm text-slate-700">
							<p><span className="font-semibold text-slate-950">Doctor:</span> {selectedDoctorLabel}</p>
							<p><span className="font-semibold text-slate-950">Requests:</span> {requestSummary(professionalCare.request_counts)}</p>
							{careSummary ? <p>{careSummary.plan_digest || careSummary.conversation_digest}</p> : null}
						</div>
					</Link>
				</div>
				<p className="mt-6 text-sm">
					<Link className="font-semibold text-action underline-offset-4 hover:underline" href="/episodes">All episodes</Link>
				</p>
			</section>
		</div>
	);
}
