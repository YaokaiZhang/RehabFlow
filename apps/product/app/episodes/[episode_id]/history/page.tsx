"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

import {
	getCareEpisodeHistory,
	type AICareSummary,
	type CareEpisodeHistory,
	type CareEpisode,
	type EpisodeRehabSession,
	type MemoryDocument,
	type TriageSummaryArtifact,
	type TriageSummaryDraft,
} from "@rehab/shared/api";
import { loadAuth } from "@rehab/shared/auth";

import { StatusBadge } from "../../../../components/ui";

type Params = { episode_id?: string };
type SummaryWithContext = TriageSummaryArtifact | TriageSummaryDraft;

function safetyCopy(status: CareEpisode["safety_gate_status"]) {
	if (status === "needs_triage") return "Needs AI Triage Intake before AI Daily Rehab";
	if (status === "clinician_reviewed") return "Clinician-reviewed context available";
	return "AI triage context available";
}

function safetyTone(status: CareEpisode["safety_gate_status"]): "attention" | "success" {
	if (status === "needs_triage") return "attention";
	return "success";
}

function formatDateTime(value?: string | null) {
	if (!value) return "Time unavailable";
	const date = new Date(value);
	if (Number.isNaN(date.getTime())) return value;
	return date.toLocaleString();
}

function checklistProgress(session: EpisodeRehabSession) {
	const total = Array.isArray(session.checklist) ? session.checklist.length : 0;
	const completed = Array.isArray(session.checklist) ? session.checklist.filter((item) => item.completed).length : 0;
	return total ? `${completed}/${total} complete` : "No checklist items saved";
}

function formatMemoryValue(value: unknown) {
	if (typeof value === "string") return value;
	try {
		return JSON.stringify(value, null, 2) || String(value);
	} catch {
		return String(value);
	}
}

function MemorySection({ title, document, emptyCopy }: { title: string; document?: MemoryDocument | null; emptyCopy: string }) {
	const fields = document ? Object.entries(document.editable_fields || {}) : [];
	return (
		<section className="card">
			<h2 className="section-title">{title}</h2>
			{document ? (
				<div className="mt-3 space-y-3 text-sm text-slate-700">
					<p className="whitespace-pre-line rounded-control border border-line bg-[var(--rf-surface-muted)] p-3">{document.compiled_text || emptyCopy}</p>
					{fields.length ? (
						<div className="space-y-2">
							{fields.map(([fieldName, value]) => (
								<article key={fieldName} className="rounded-md border border-slate-200 bg-white p-3">
									<p className="text-sm font-semibold text-slate-500">Memory field</p>
									<p className="mt-1 font-semibold text-slate-950">{fieldName}</p>
									<p className="mt-2 whitespace-pre-line text-sm leading-6 text-slate-700">{formatMemoryValue(value)}</p>
								</article>
							))}
						</div>
					) : (
						<p className="text-sm text-slate-500">{emptyCopy}</p>
					)}
				</div>
			) : (
				<p className="mt-3 text-sm text-slate-500">{emptyCopy}</p>
			)}
		</section>
	);
}

function TriageSummaryCard({ title, summary, badge }: { title: string; summary: SummaryWithContext; badge: string }) {
	const transcript = summary.source_conversation_transcript?.trim();
	return (
		<article className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
			<div className="flex flex-wrap items-start justify-between gap-3">
				<div>
					<p className="text-sm font-semibold text-slate-500">{badge}</p>
					<h3 className="mt-1 text-lg font-semibold text-slate-950">{title}</h3>
				</div>
				<p className="text-xs text-slate-500">{formatDateTime(summary.created_at)}</p>
			</div>
			<div className="mt-3 space-y-3 text-sm text-slate-700">
				<p><span className="font-semibold text-slate-950">Concern:</span> {summary.concern}</p>
				<p><span className="font-semibold text-slate-950">Recommendation:</span> {summary.recommendation}</p>
				{summary.relevant_context ? (
					<div className="rounded-control border border-line bg-[var(--rf-surface-muted)] p-3">
						<p className="text-sm font-semibold text-slate-500">Triage context summary</p>
						<p className="mt-2 whitespace-pre-line text-sm leading-6 text-slate-700">{summary.relevant_context}</p>
					</div>
				) : null}
				<div className="rounded-control border border-line bg-[var(--rf-surface-muted)] p-3">
					<p className="text-sm font-semibold text-slate-500">Source transcript snapshot</p>
					<p className="mt-2 whitespace-pre-line text-sm leading-6 text-slate-700">
						{transcript || "No source transcript snapshot saved for this older summary."}
					</p>
				</div>
			</div>
		</article>
	);
}

function RehabSessionsSection({ sessions }: { sessions: EpisodeRehabSession[] }) {
	return (
		<section className="card">
			<h2 className="section-title">AI Daily Rehab</h2>
			{sessions.length ? (
				<div className="mt-3 space-y-3">
					{sessions.map((session) => (
						<article key={session.session_id} className="rounded-md border border-slate-200 bg-white p-4 text-sm text-slate-700">
							<div className="flex flex-wrap items-start justify-between gap-3">
								<p className="font-semibold text-slate-950">{checklistProgress(session)}</p>
								<p className="text-xs text-slate-500">{formatDateTime(session.created_at)}</p>
							</div>
							<p className="mt-2"><span className="font-semibold text-slate-950">Notes:</span> {session.patient_notes || "No patient notes captured."}</p>
							<p className="mt-2 whitespace-pre-line">{session.session_summary || "No generated session summary yet."}</p>
						</article>
					))}
				</div>
			) : (
				<p className="mt-3 text-sm text-slate-500">No AI Daily Rehab sessions saved for this episode.</p>
			)}
		</section>
	);
}

function CareSummariesSection({ summaries }: { summaries: AICareSummary[] }) {
	return (
		<section className="card">
			<h2 className="section-title">Professional Care</h2>
			{summaries.length ? (
				<div className="mt-3 space-y-3">
					{summaries.map((summary) => (
						<article key={summary.care_summary_id} className="rounded-md border border-slate-200 bg-white p-4 text-sm text-slate-700">
							<div className="flex flex-wrap items-start justify-between gap-3">
								<p className="font-semibold text-slate-950">AI Care Summary</p>
								<p className="text-xs text-slate-500">{formatDateTime(summary.created_at)}</p>
							</div>
							<p className="mt-2 whitespace-pre-line">{summary.conversation_digest}</p>
							<p className="mt-2"><span className="font-semibold text-slate-950">Plan:</span> {summary.plan_digest}</p>
						</article>
					))}
				</div>
			) : (
				<p className="mt-3 text-sm text-slate-500">No Professional Care AI summaries saved for this episode.</p>
			)}
		</section>
	);
}

export default function CareEpisodeHistoryPage() {
	const params = useParams<Params>();
	const episodeId = params?.episode_id || "";
	const [history, setHistory] = useState<CareEpisodeHistory | null>(null);
	const [error, setError] = useState("");
	const [loading, setLoading] = useState(true);

	useEffect(() => {
		const auth = loadAuth();
		if (!auth || auth.role !== "patient") {
			setError("Login as a patient to open this episode history.");
			setLoading(false);
			return;
		}
		getCareEpisodeHistory(episodeId, auth.access_token)
			.then(setHistory)
			.catch((err) => setError((err instanceof Error ? err.message : String(err))))
			.finally(() => setLoading(false));
	}, [episodeId]);

	if (loading) return <p className="text-sm text-slate-500">Loading episode history...</p>;
	if (error && !history) return <section className="page-column-narrow"><p className="notice notice-danger">{error}</p></section>;
	if (!history) return null;

	const { episode, triage_summaries: summaries, triage_summary_drafts: drafts, patient_memory: patientMemory, episode_memory: episodeMemory, compiled_memory_context: compiledMemoryContext, rehab_sessions: rehabSessions, ai_care_summaries: careSummaries } = history;

	const episodeMemoryDetails = [
		episodeMemory?.level1_keywords?.length ? `Keywords: ${episodeMemory.level1_keywords.join(", ")}` : null,
		episodeMemory?.level1_description ? `Description: ${episodeMemory.level1_description}` : null,
		episodeMemory?.level2_summary ? `Summary: ${episodeMemory.level2_summary}` : null,
	].filter((detail): detail is string => Boolean(detail));

	return (
		<div className="page-column space-y-6">
			<section className="page-header">
				<p className="page-header-kicker">History</p>
				<h1>{episode.issue_title}</h1>
				<p className="page-header-lead">Everything saved for this episode: triage summaries with their chats, rehab sessions, doctor summaries, and what RehabFlow remembers.</p>
				<div className="page-header-actions">
					<Link className="btn-secondary" href={`/episodes/${episode.care_episode_id}`}>Episode Overview</Link>
					<Link className="btn-secondary" href="/episodes">All Episodes</Link>
				</div>
			</section>

			<section className="grid gap-4 lg:grid-cols-3">
				<div className="card">
					<h2 className="section-title">Care Episode</h2>
					<div className="mt-3 space-y-2 text-sm text-slate-700">
						<p><span className="font-semibold text-slate-950">Body area:</span> {episode.body_area}</p>
						<p><span className="font-semibold text-slate-950">Goal:</span> {episode.goal}</p>
						<p className="flex flex-wrap items-center gap-2"><span className="font-semibold text-slate-950">Safety gate:</span> <StatusBadge tone={safetyTone(episode.safety_gate_status)}>{safetyCopy(episode.safety_gate_status)}</StatusBadge></p>
						<p><span className="font-semibold text-slate-950">Description:</span> {episode.short_description}</p>
					</div>
				</div>
				<MemorySection title="Patient Memory" document={patientMemory} emptyCopy="Nothing saved about you yet. RehabFlow adds facts here as you chat." />
				<section className="card">
					<h2 className="section-title">Episode Memory</h2>
					<div className="mt-3 space-y-3 text-sm text-slate-700">
						{compiledMemoryContext ? <p className="whitespace-pre-line rounded-control border border-line bg-[var(--rf-surface-muted)] p-3">{compiledMemoryContext}</p> : null}
						{episodeMemoryDetails.length ? (
							<div className="space-y-2">
								{episodeMemoryDetails.map((detail) => (
									<p key={detail} className="whitespace-pre-line rounded-control border border-line bg-white p-3">{detail}</p>
								))}
							</div>
						) : null}
						{!compiledMemoryContext && !episodeMemoryDetails.length ? <p className="text-sm text-slate-500">Nothing remembered for this episode yet.</p> : null}
					</div>
				</section>
			</section>

			<section className="space-y-3">
				<div className="flex flex-wrap items-center justify-between gap-3">
					<h2 className="section-title">Triage Source History</h2>
					<p className="text-sm text-slate-500">Each summary keeps a copy of the chat it came from.</p>
				</div>
				{summaries.length === 0 && drafts.length === 0 ? (
					<section className="card text-sm text-slate-500">No triage summaries yet. They appear here after you save one from AI Triage Intake.</section>
				) : null}
				{summaries.map((summary, index) => (
					<TriageSummaryCard key={summary.triage_summary_id} title={`Triage Summary, version ${summary.version || index + 1}`} summary={summary} badge="Saved" />
				))}
				{drafts.map((draft, index) => (
					<TriageSummaryCard key={draft.triage_summary_draft_id} title={`Draft summary ${index + 1}`} summary={draft} badge="Draft" />
				))}
			</section>

			<section className="grid gap-4 lg:grid-cols-2">
				<RehabSessionsSection sessions={rehabSessions} />
				<CareSummariesSection summaries={careSummaries} />
			</section>
		</div>
	);
}
