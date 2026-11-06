"use client";

import { useParams } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import {
	getCareEpisodeHistory,
	type AICareSummary,
	type CareEpisodeHistory,
	type EpisodeRehabSession,
	type MemoryDocument,
	type TriageSummaryArtifact,
	type TriageSummaryDraft,
} from "@rehab/shared/api";
import { loadAuth } from "@rehab/shared/auth";

import { StatusBadge } from "../../../../components/ui";
import { safetyStatus } from "../../../../components/safetyStatus";

type Params = { episode_id?: string };
type SummaryWithContext = TriageSummaryArtifact | TriageSummaryDraft;

function formatDateTime(value?: string | null) {
	if (!value) return "Time unavailable";
	const date = new Date(value);
	if (Number.isNaN(date.getTime())) return value;
	return date.toLocaleString();
}

function checklistProgress(session: EpisodeRehabSession) {
	const total = Array.isArray(session.checklist) ? session.checklist.length : 0;
	const completed = Array.isArray(session.checklist) ? session.checklist.filter((item) => item.completed).length : 0;
	return total ? `${completed} of ${total} done` : "Session";
}

function isEmptyMemoryValue(value: unknown): boolean {
	if (value === null || value === undefined || value === "") return true;
	if (Array.isArray(value)) return value.every(isEmptyMemoryValue);
	if (typeof value === "object") return Object.values(value as Record<string, unknown>).every(isEmptyMemoryValue);
	return false;
}

/** Readable rendering of a memory field: no raw JSON, no snake_case keys. */
function MemoryValue({ value }: { value: unknown }): ReactNode {
	if (typeof value === "boolean") return <p className="ep-memory-text">{value ? "Yes" : "No"}</p>;
	if (typeof value === "string" || typeof value === "number") return <p className="ep-memory-text">{String(value)}</p>;
	if (Array.isArray(value)) {
		const items = value.filter((item) => !isEmptyMemoryValue(item));
		if (items.every((item) => typeof item !== "object")) return <p className="ep-memory-text">{items.map(String).join(", ")}</p>;
		return (
			<ul className="ep-memory-list">
				{items.map((item, index) => <li key={index}><MemoryValue value={item} /></li>)}
			</ul>
		);
	}
	if (value && typeof value === "object") {
		const entries = Object.entries(value as Record<string, unknown>).filter(([, item]) => !isEmptyMemoryValue(item));
		return (
			<>
				{entries.map(([key, item]) => (
					<div key={key} className="ep-memory-field">
						<span className="ep-memory-field-name">{fieldLabel(key)}</span>
						<MemoryValue value={item} />
					</div>
				))}
			</>
		);
	}
	return null;
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
	const fields = document ? Object.entries(document.editable_fields || {}).filter(([, value]) => !isEmptyMemoryValue(value)) : [];
	return (
		<section className="sticker tone-lilac ep-memory">
			<h2>{title}</h2>
			{document?.compiled_text ? <p className="ep-memory-text">{document.compiled_text}</p> : null}
			{fields.map(([fieldName, value]) => (
				<div key={fieldName} className="ep-memory-field">
					<span className="ep-memory-field-name">{fieldLabel(fieldName)}</span>
					<MemoryValue value={value} />
				</div>
			))}
			{!document?.compiled_text && !fields.length ? <p className="ep-care-muted">{emptyCopy}</p> : null}
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

function TriageSummaryCard({ title, summary }: { title: string; summary: SummaryWithContext }) {
	const transcript = summary.source_conversation_transcript?.trim();
	return (
		<article className="ep-tl-card">
			<div className="ep-tl-head">
				<div className="ep-tl-head-left">
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
			{transcript ? (
				<details className="ep-disclosure">
					<summary>Source transcript</summary>
					<p className="whitespace-pre-line">{transcript}</p>
				</details>
			) : null}
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
			{session.patient_notes ? <p><span className="ep-tl-label">Notes:</span> {session.patient_notes}</p> : null}
			{session.session_summary ? <p className="whitespace-pre-line">{session.session_summary}</p> : null}
		</article>
	);
}

function CareSummaryCard({ summary }: { summary: AICareSummary }) {
	return (
		<article className="ep-tl-card">
			<div className="ep-tl-head">
				<div className="ep-tl-head-left">
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
			node: <TriageSummaryCard title={`Triage Summary, version ${summary.version || index + 1}`} summary={summary} />,
		})),
		...drafts.map((draft, index) => ({
			key: `draft-${draft.triage_summary_draft_id}`,
			at: draft.created_at,
			tone: "peach" as const,
			node: <TriageSummaryCard title={`Draft summary ${index + 1}`} summary={draft} />,
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

	const safety = safetyStatus(episode.safety_gate_status);

	return (
		<div className="page-column ep-page">
			<header className="page-header" data-tone="lilac">
				<p className="page-header-kicker">History</p>
				<h1>{episode.issue_title}</h1>
			</header>

			<section aria-labelledby="ep-timeline-title" className="grid gap-5">
				<div className="ep-section-head">
					<h2 id="ep-timeline-title" className="sr-only">Timeline</h2>
					{timeline.length ? <ul className="ep-legend" aria-label="Legend">
						<li data-tone="peach"><span className="session-dot" />Triage</li>
						<li data-tone="mint"><span className="session-dot" />Rehab</li>
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
					</div>
				)}
			</section>

			<details className="ep-disclosure ep-history-more">
				<summary>Episode details and memory</summary>
				<div>
					<section className="ep-history-top">
						<section className="card" aria-labelledby="ep-history-episode">
							<h2 id="ep-history-episode" className="section-title">Care Episode</h2>
							<dl className="ep-facts">
								<div><dt>Body area</dt><dd>{episode.body_area}</dd></div>
								<div><dt>Goal</dt><dd>{episode.goal}</dd></div>
								<div><dt>Safety</dt><dd><StatusBadge tone={safety.tone}>{safety.label}</StatusBadge></dd></div>
								<div><dt>Description</dt><dd>{episode.short_description}</dd></div>
							</dl>
						</section>
						<MemorySection title="Patient memory" document={patientMemory} emptyCopy="Nothing saved yet" />
						<section className="sticker tone-lilac ep-memory" aria-labelledby="ep-history-memory">
							<h2 id="ep-history-memory">Episode memory</h2>
							{compiledMemoryContext ? <p className="ep-memory-text">{compiledMemoryContext}</p> : null}
							{episodeMemoryDetails.map((detail) => (
								<p key={detail} className="ep-memory-text">{detail}</p>
							))}
							{!compiledMemoryContext && !episodeMemoryDetails.length ? <p className="ep-care-muted">Nothing saved yet</p> : null}
						</section>
					</section>
				</div>
			</details>
		</div>
	);
}
