"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

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

function fieldLabel(fieldName: string) {
	const words = fieldName.replace(/[_-]+/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2").trim().toLowerCase();
	return words ? words.charAt(0).toUpperCase() + words.slice(1) : "Memory field";
}

function timeValue(value?: string | null) {
	if (!value) return Number.NEGATIVE_INFINITY;
	const time = new Date(value).getTime();
	return Number.isNaN(time) ? Number.NEGATIVE_INFINITY : time;
}

function MemorySection({ title, document, emptyCopy }: { title: string; document?: MemoryDocument | null; emptyCopy: string }) {
	const fields = document ? Object.entries(document.editable_fields || {}) : [];
	return (
		<section className="sticker tone-lilac ep-memory">
			<h2>{title}</h2>
			{document ? (
				<>
					<p className="ep-memory-text">{document.compiled_text || emptyCopy}</p>
					{fields.length ? (
						fields.map(([fieldName, value]) => (
							<div key={fieldName} className="ep-memory-field">
								<span className="ep-memory-field-name">{fieldLabel(fieldName)}</span>
								<p className="ep-memory-text">{formatMemoryValue(value)}</p>
							</div>
						))
					) : (
						<p className="ep-care-muted">{emptyCopy}</p>
					)}
				</>
			) : (
				<p className="ep-care-muted">{emptyCopy}</p>
			)}
		</section>
	);
}

type TimelineEvent = { key: string; at?: string | null; tone: "peach" | "mint" | "sky"; node: ReactNode };

function TimelineItem({ event }: { event: TimelineEvent }) {
	return (
		<li className="ep-tl-item" data-tone={event.tone}>
			<span className="ep-tl-dot" aria-hidden="true" />
			{event.node}
		</li>
	);
}

function TriageSummaryCard({ title, summary, badge }: { title: string; summary: SummaryWithContext; badge: string }) {
	const transcript = summary.source_conversation_transcript?.trim();
	return (
		<article className="ep-tl-card">
			<div className="ep-tl-head">
				<div className="ep-tl-head-left">
					<span className="tag">AI Triage Intake · {badge}</span>
					<h3 className="ep-tl-title">{title}</h3>
				</div>
				<span className="ep-tl-time">{formatDateTime(summary.created_at)}</span>
			</div>
			<p><span className="ep-tl-label">Concern:</span> {summary.concern}</p>
			<p><span className="ep-tl-label">Recommendation:</span> {summary.recommendation}</p>
			{summary.relevant_context ? (
				<details className="ep-disclosure">
					<summary>Triage context summary</summary>
					<p className="whitespace-pre-line">{summary.relevant_context}</p>
				</details>
			) : null}
			<details className="ep-disclosure">
				<summary>Source transcript snapshot</summary>
				<p className="whitespace-pre-line">{transcript || "No source transcript snapshot saved for this older summary."}</p>
			</details>
		</article>
	);
}

function RehabSessionCard({ session }: { session: EpisodeRehabSession }) {
	return (
		<article className="ep-tl-card">
			<div className="ep-tl-head">
				<div className="ep-tl-head-left">
					<span className="tag">AI Daily Rehab</span>
					<h3 className="ep-tl-title">{checklistProgress(session)}</h3>
				</div>
				<span className="ep-tl-time">{formatDateTime(session.created_at)}</span>
			</div>
			<p><span className="ep-tl-label">Notes:</span> {session.patient_notes || "No patient notes captured."}</p>
			<p className="whitespace-pre-line">{session.session_summary || "No generated session summary yet."}</p>
		</article>
	);
}

function CareSummaryCard({ summary }: { summary: AICareSummary }) {
	return (
		<article className="ep-tl-card">
			<div className="ep-tl-head">
				<div className="ep-tl-head-left">
					<span className="tag">Professional Care</span>
					<h3 className="ep-tl-title">AI Care Summary</h3>
				</div>
				<span className="ep-tl-time">{formatDateTime(summary.created_at)}</span>
			</div>
			<p className="whitespace-pre-line">{summary.conversation_digest}</p>
			<p><span className="ep-tl-label">Plan:</span> {summary.plan_digest}</p>
		</article>
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

	if (loading) return <p className="ep-loading">Loading episode history...</p>;
	if (error && !history) return <section className="page-column-narrow"><p className="notice notice-danger">{error}</p></section>;
	if (!history) return null;

	const { episode, triage_summaries: summaries, triage_summary_drafts: drafts, patient_memory: patientMemory, episode_memory: episodeMemory, compiled_memory_context: compiledMemoryContext, rehab_sessions: rehabSessions, ai_care_summaries: careSummaries } = history;

	const episodeMemoryDetails = [
		episodeMemory?.level1_keywords?.length ? `Keywords: ${episodeMemory.level1_keywords.join(", ")}` : null,
		episodeMemory?.level1_description ? `Description: ${episodeMemory.level1_description}` : null,
		episodeMemory?.level2_summary ? `Summary: ${episodeMemory.level2_summary}` : null,
	].filter((detail): detail is string => Boolean(detail));

	const timeline: TimelineEvent[] = [
		...summaries.map((summary, index) => ({
			key: `summary-${summary.triage_summary_id}`,
			at: summary.created_at,
			tone: "peach" as const,
			node: <TriageSummaryCard title={`Triage Summary, version ${summary.version || index + 1}`} summary={summary} badge="Saved" />,
		})),
		...drafts.map((draft, index) => ({
			key: `draft-${draft.triage_summary_draft_id}`,
			at: draft.created_at,
			tone: "peach" as const,
			node: <TriageSummaryCard title={`Draft summary ${index + 1}`} summary={draft} badge="Draft" />,
		})),
		...rehabSessions.map((session) => ({
			key: `rehab-${session.session_id}`,
			at: session.created_at,
			tone: "mint" as const,
			node: <RehabSessionCard session={session} />,
		})),
		...careSummaries.map((summary) => ({
			key: `care-${summary.care_summary_id}`,
			at: summary.created_at,
			tone: "sky" as const,
			node: <CareSummaryCard summary={summary} />,
		})),
	].sort((a, b) => timeValue(b.at) - timeValue(a.at));

	return (
		<div className="page-column ep-page">
			<header className="page-header" data-tone="lilac">
				<p className="page-header-kicker">History</p>
				<h1>{episode.issue_title}</h1>
				<div className="ep-header-links">
					<Link className="btn-secondary" href={`/episodes/${episode.care_episode_id}`}>Episode overview</Link>
				</div>
			</header>

			<section className="ep-history-top">
				<section className="card" aria-labelledby="ep-history-episode">
					<h2 id="ep-history-episode" className="section-title">Care Episode</h2>
					<dl className="ep-facts">
						<div><dt>Body area</dt><dd>{episode.body_area}</dd></div>
						<div><dt>Goal</dt><dd>{episode.goal}</dd></div>
						<div><dt>Safety gate</dt><dd><StatusBadge tone={safetyTone(episode.safety_gate_status)}>{safetyCopy(episode.safety_gate_status)}</StatusBadge></dd></div>
						<div><dt>Description</dt><dd>{episode.short_description}</dd></div>
					</dl>
				</section>
				<MemorySection title="Patient memory" document={patientMemory} emptyCopy="Nothing saved about you yet. RehabFlow adds facts here as you chat." />
				<section className="sticker tone-lilac ep-memory" aria-labelledby="ep-history-memory">
					<h2 id="ep-history-memory">Episode memory</h2>
					{compiledMemoryContext ? <p className="ep-memory-text">{compiledMemoryContext}</p> : null}
					{episodeMemoryDetails.map((detail) => (
						<p key={detail} className="ep-memory-text">{detail}</p>
					))}
					{!compiledMemoryContext && !episodeMemoryDetails.length ? <p className="ep-care-muted">Nothing remembered for this episode yet.</p> : null}
				</section>
			</section>

			<section aria-labelledby="ep-timeline-title" className="grid gap-5">
				<div className="ep-section-head">
					<h2 id="ep-timeline-title" className="section-title flex items-center gap-2"><span className="session-dot" data-tone="lilac" />Timeline</h2>
					{timeline.length ? <ul className="ep-legend" aria-label="Legend">
						<li data-tone="peach"><span className="session-dot" />AI Triage Intake</li>
						<li data-tone="mint"><span className="session-dot" />AI Daily Rehab</li>
						<li data-tone="sky"><span className="session-dot" />Professional Care</li>
					</ul> : null}
				</div>
				{timeline.length ? (
					<ol className="ep-timeline">
						{timeline.map((event) => <TimelineItem key={event.key} event={event} />)}
					</ol>
				) : (
					<div className="card">
						<p className="ep-empty">nothing saved yet</p>
						<p className="mt-2 text-sm text-[var(--rf-ink-muted)]">Triage summaries, rehab sessions and doctor summaries appear here as you save them.</p>
					</div>
				)}
			</section>
		</div>
	);
}
