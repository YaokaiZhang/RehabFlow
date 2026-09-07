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
	if (status === "needs_triage") return "Rehab Safety Gate: AI triage needed";
	if (status === "clinician_reviewed") return "Rehab Safety Gate: clinician-reviewed";
	return "Rehab Safety Gate: AI triage complete";
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
		<details className="mt-3 max-w-3xl rounded-md border border-slate-200 bg-slate-50 p-3 text-sm text-slate-600">
			<summary className="cursor-pointer text-xs font-semibold uppercase tracking-wide text-slate-500">Triage context summary</summary>
			<p className="mt-2 whitespace-pre-line leading-6">{text}</p>
		</details>
	);
}

function FullAiTriageHistory({ text }: { text: string }) {
	return (
		<details className="rounded-md border border-slate-200 bg-slate-50 p-3 text-slate-600">
			<summary className="cursor-pointer text-xs font-semibold uppercase tracking-wide text-slate-500">Full AI triage history</summary>
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

	return (
		<div className="space-y-5">
			<section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm md:p-6">
				<div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
					<div>
						<p className="text-xs font-semibold uppercase tracking-wide text-emerald-700">Episode Workspace</p>
						<h1 className="mt-2 text-2xl font-semibold text-slate-950 md:text-3xl">{episode.issue_title}</h1>
						{triageContext ? <TriageContextSummary text={triageContext} /> : null}
					</div>
					<div className="flex flex-wrap gap-2">
						<Link className="btn-secondary" href={`/episodes/${episode.care_episode_id}/history`}>View full history</Link>
						<Link className="btn-secondary" href="/episodes">All Episodes</Link>
					</div>
				</div>
			</section>

			<CareEpisodeBrief workspace={workspace} role="patient" />

			<section className="grid gap-4 lg:grid-cols-3">
				<div className="card">
					<h2 className="section-title">Latest Triage Summary</h2>
					{triage ? (
						<div className="mt-3 space-y-3 text-sm text-slate-700">
							<p>{triage.recommendation}</p>
							<FullAiTriageHistory text={latestFullHistory} />
						</div>
					) : (
						<p className="mt-2 text-sm text-slate-500">No saved Triage Summary yet. Build safety context before AI Daily Rehab progression.</p>
					)}
					{unsavedTriageSummaries.length ? (
						<div className="mt-4 space-y-2 border-t border-slate-200 pt-4">
							<p className="text-xs font-semibold uppercase tracking-wide text-amber-700">Unsaved summaries</p>
							{statusMessage ? <p className="text-sm text-emerald-700">{statusMessage}</p> : null}
							{error ? <p className="text-sm text-rose-600">{error}</p> : null}
							{unsavedTriageSummaries.map((draft) => {
								const draftFullHistory = fullTriageHistory(draft);
								return (
									<div key={draft.triage_summary_draft_id} className="rounded-md border border-amber-200 bg-amber-50 p-3">
										<p className="text-sm text-slate-700">{draft.recommendation}</p>
										<div className="mt-3">
											<FullAiTriageHistory text={draftFullHistory} />
										</div>
										<AppButton variant="secondary" className="mt-3" onClick={() => deleteUnsavedSummary(draft.triage_summary_draft_id)} disabled={deletingDraftId === draft.triage_summary_draft_id}>
											{deletingDraftId === draft.triage_summary_draft_id ? "Deleting" : "Delete"}
										</AppButton>
									</div>
								);
							})}
						</div>
					) : null}
				</div>

				<div className="card">
					<h2 className="section-title">Latest Rehab Summary</h2>
					{rehab ? (
						<div className="mt-3 space-y-3 text-sm text-slate-700">
							<p><span className="font-semibold text-slate-950">Checklist:</span> {completedCount}/{rehab.checklist.length} complete</p>
							<p><span className="font-semibold text-slate-950">Notes:</span> {rehab.patient_notes || "No notes captured."}</p>
							<p>{rehab.session_summary || "No generated session summary yet."}</p>
						</div>
					) : (
						<p className="mt-2 text-sm text-slate-500">No rehab session yet for this episode.</p>
					)}
				</div>

				<div className="card">
					<h2 className="section-title">Professional Care Summary</h2>
					<div className="mt-3 space-y-3 text-sm text-slate-700">
						<p className="flex flex-wrap items-center gap-2"><span className="font-semibold text-slate-950">Subscription:</span> <StatusBadge tone="info">{professionalCare.subscription_tier}</StatusBadge></p>
						<p><span className="font-semibold text-slate-950">Selected doctor:</span> {selectedDoctorLabel}</p>
						<p><span className="font-semibold text-slate-950">Requests:</span> {Object.entries(professionalCare.request_counts).map(([status, count]) => `${status}: ${count}`).join(", ") || "None"}</p>
						{careSummary ? (
							<>
								<p>{careSummary.conversation_digest}</p>
								<p><span className="font-semibold text-slate-950">Plan:</span> {careSummary.plan_digest}</p>
							</>
						) : (
							<p>No AI Care Summary yet.</p>
						)}
					</div>
				</div>
			</section>

			<section className="grid gap-4 md:grid-cols-3">
				<Link className="next-step-card next-step-primary" href={`/episodes/${episode.care_episode_id}/triage`}>
					<p className="text-xs font-semibold uppercase tracking-wide text-emerald-700">AI Triage Intake</p>
					<h2 className="mt-2 text-lg font-semibold text-slate-950">Build safety context</h2>
					<p className="mt-2 text-sm leading-6 text-slate-600">Ask follow-up questions and request or regenerate a Triage Summary for this concern.</p>
				</Link>
				<Link className="next-step-card" href={`/episodes/${episode.care_episode_id}/rehab`}>
					<p className="text-xs font-semibold uppercase tracking-wide text-amber-700">{safetyLabel(episode.safety_gate_status)}</p>
					<h2 className="mt-2 text-lg font-semibold text-slate-950">AI Daily Rehab</h2>
					<p className="mt-2 text-sm leading-6 text-slate-600">Start today session, update the checklist, and save a session summary into episode memory inputs.</p>
				</Link>
				<Link className="next-step-card" href={`/episodes/${episode.care_episode_id}/professional-care`}>
					<p className="text-xs font-semibold uppercase tracking-wide text-emerald-700">Professional Care</p>
					<h2 className="mt-2 text-lg font-semibold text-slate-950">Work with a doctor</h2>
					<p className="mt-2 text-sm leading-6 text-slate-600">Manage requests, Care Conversation, and AI Care Summary for this episode.</p>
				</Link>
			</section>
		</div>
	);
}
