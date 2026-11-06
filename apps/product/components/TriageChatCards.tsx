"use client";

import Link from "next/link";

import type { AiChatExerciseCard, AiChatNextStep, TriageSummaryDraft } from "@rehab/shared/api";

export type WrapUp =
	| { stage: "offer" }
	| { stage: "writing" }
	| { stage: "draft"; episodeId: string; draft: TriageSummaryDraft }
	| { stage: "saving"; episodeId: string; draft: TriageSummaryDraft };

const tilts = [-1.5, 1, -0.5];

function Arrow() {
	return (
		<span className="btn-arrow" aria-hidden="true">
			<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12h14m-6-6 6 6-6 6" /></svg>
		</span>
	);
}

// Exercises the agent suggested, as small mint stickers under its reply.
export function ExerciseCards({ cards }: { cards: AiChatExerciseCard[] }) {
	return (
		<ul className="chat-exercises" aria-label="Suggested exercises">
			{cards.map((card, index) => (
				<li key={card.exercise_id} className="sticker tone-mint chat-exercise" style={{ transform: `rotate(${tilts[index % tilts.length]}deg)` }}>
					<p className="chat-exercise-title">{card.title}</p>
					{card.summary ? <p className="chat-exercise-summary">{card.summary}</p> : null}
					{card.targets.length ? (
						<div className="chat-exercise-tags">
							{card.targets.map((target) => <span key={target} className="tag">{target}</span>)}
						</div>
					) : null}
					<div className="chat-exercise-links">
						{card.video_url ? (
							<a href={card.video_url} target="_blank" rel="noreferrer">
								<svg aria-hidden="true" viewBox="0 0 16 16" fill="currentColor"><path d="M5 3.5v9l7-4.5z" /></svg>
								Watch
							</a>
						) : null}
						{card.source_url ? <a href={card.source_url} target="_blank" rel="noreferrer">Source</a> : null}
					</div>
				</li>
			))}
		</ul>
	);
}

// The end of the chat: one clear next step, chosen by what the agent concluded.
export function WrapUpCard({
	step,
	wrapUp,
	error,
	episodeId,
	onWrite,
	onSave,
	onDismiss,
}: {
	step: AiChatNextStep;
	wrapUp: WrapUp;
	error: string;
	episodeId: string;
	onWrite: () => void;
	onSave: () => void;
	onDismiss: () => void;
}) {
	if (wrapUp.stage === "draft" || wrapUp.stage === "saving") {
		const { draft } = wrapUp;
		const saving = wrapUp.stage === "saving";
		const toClinician = draft.clinician_review_needed;
		return (
			<section className="sticker tone-peach wrapup-summary" aria-label="Your summary">
				<span className="tag">Summary</span>
				<h2 className="wrapup-title">{draft.concern}</h2>
				{draft.safety_signals.length ? (
					<div className="wrapup-signals">
						{draft.safety_signals.map((signal) => <span key={signal} className="tag" data-tone="rose">{signal}</span>)}
					</div>
				) : null}
				<dl className="wrapup-blocks">
					<div><dt>What we heard</dt><dd>{draft.relevant_context}</dd></div>
					<div><dt>Next step</dt><dd>{draft.recommendation}</dd></div>
				</dl>
				{error ? <p className="notice notice-danger mt-3 text-sm">{error}</p> : null}
				<div className="wrapup-actions">
					<button type="button" className="btn-primary" onClick={onSave} disabled={saving}>
						{saving ? "Saving" : toClinician ? "Save & find a clinician" : "Save & start rehab"}
						<Arrow />
					</button>
					<button type="button" className="btn bg-transparent text-ink hover:bg-fill" onClick={onDismiss} disabled={saving}>Not quite</button>
				</div>
			</section>
		);
	}

	if (step === "urgent") {
		return (
			<section className="sticker tone-rose wrapup-urgent" role="alert">
				<h2 className="wrapup-title">Get checked in person today</h2>
				<p>If it&apos;s severe or getting worse fast, call emergency services.</p>
				{episodeId ? (
					<Link className="btn-primary mt-3" href={`/episodes/${episodeId}/professional-care`}>
						Find a clinician
						<Arrow />
					</Link>
				) : null}
			</section>
		);
	}

	const writing = wrapUp.stage === "writing";
	return (
		<div className="wrapup-offer">
			<button type="button" className="sticker tone-peach wrapup-offer-button" onClick={onWrite} disabled={writing}>
				{writing ? (
					<>
						<span className="agent-progress-dots" aria-hidden="true"><span /><span /><span /></span>
						Writing your plan
					</>
				) : (
					<>
						Make my plan
						<Arrow />
					</>
				)}
			</button>
			{error ? <p className="notice notice-danger text-sm">{error}</p> : null}
		</div>
	);
}
