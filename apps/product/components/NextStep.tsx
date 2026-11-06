import Link from "next/link";

import type { CareEpisode, CareEpisodeWorkspaceSummary, EpisodeRehabSession, TriageSummaryArtifact } from "@rehab/shared/api";

import { ArrowIcon } from "./CareEpisodeBrief";
import { needsClinicianReview, realSafetySignals } from "./safetyStatus";

export type NextStepTone = "rose" | "peach" | "mint" | "sky" | "lilac";

export type NextStepKind =
	| "risk"
	| "save-triage"
	| "start-triage"
	| "doctor-pending"
	| "doctor-replied"
	| "plan-rehab"
	| "resume-rehab"
	| "done-today";

type StepLink = { label: string; href: string };

export type NextStepInfo = {
	kind: NextStepKind;
	tone: NextStepTone;
	title: string;
	chips?: string[];
	action?: StepLink;
	secondary?: StepLink;
	quiet?: StepLink;
};

function isToday(value?: string | null) {
	if (!value) return false;
	const date = new Date(value);
	if (Number.isNaN(date.getTime())) return false;
	return date.toDateString() === new Date().toDateString();
}

export function rehabSessionHref(session: EpisodeRehabSession) {
	const exerciseIds = session.checklist.map((item) => item.exercise_id).filter(Boolean).join(",");
	const params = new URLSearchParams({ episode_id: session.care_episode_id, session_id: session.session_id });
	if (exerciseIds) params.set("exercise_ids", exerciseIds);
	return `/rehab/session?${params.toString()}`;
}

function riskStep(episode: CareEpisode, triage: TriageSummaryArtifact | null | undefined, base: string, doctorName?: string | null, hasDoctor = false): NextStepInfo | null {
	const signals = realSafetySignals(triage?.safety_signals);
	const flagged = Boolean(triage && (triage.clinician_review_needed || signals.length));
	if (!flagged && !needsClinicianReview(episode.safety_gate_status)) return null;
	return {
		kind: "risk",
		tone: "rose",
		title: "Get this checked first",
		chips: signals.slice(0, 4),
		action: hasDoctor
			? { label: doctorName ? `Message ${doctorName}` : "Message your doctor", href: `${base}/professional-care` }
			: { label: "Find a clinician", href: `${base}/professional-care` },
	};
}

function startTriageStep(episode: CareEpisode): NextStepInfo {
	return {
		kind: "start-triage",
		tone: "peach",
		title: "Start triage",
		action: { label: "Start", href: `/?episode_id=${encodeURIComponent(episode.care_episode_id)}` },
	};
}

/** First match wins. Order matters: safety, triage, doctor, then today's rehab. */
export function nextStepFor(workspace: CareEpisodeWorkspaceSummary): NextStepInfo {
	const { episode, latest_triage_summary: triage, unsaved_triage_summaries: drafts, latest_rehab_session: rehab, professional_care: care } = workspace;
	const base = `/episodes/${episode.care_episode_id}`;
	const doctorName = care.selected_doctor_name || null;

	const risk = riskStep(episode, triage, base, doctorName, care.has_active_relationship);
	if (risk) return risk;

	const triageOpen = !triage && episode.safety_gate_status !== "clinician_reviewed";
	if (triageOpen && drafts?.length) {
		return { kind: "save-triage", tone: "peach", title: "Save your triage summary", action: { label: "Review draft", href: `${base}/triage` } };
	}
	if (triageOpen) return startTriageStep(episode);

	const rehabToday = rehab && isToday(rehab.created_at) ? rehab : null;
	const startRehab: StepLink = rehabToday ? { label: "Resume rehab", href: rehabSessionHref(rehabToday) } : { label: "Start today's rehab", href: `${base}/rehab` };

	if (!care.has_active_relationship && (care.request_counts?.pending || 0) > 0) {
		return {
			kind: "doctor-pending",
			tone: "sky",
			title: doctorName ? `Waiting on ${doctorName}` : "Waiting on a doctor",
			secondary: rehabToday?.completion_status === "summarized" ? undefined : startRehab,
		};
	}

	if (!rehabToday) {
		return { kind: "plan-rehab", tone: "mint", title: "Pick today's exercises", action: { label: "Plan today", href: `${base}/rehab` } };
	}

	if (rehabToday.completion_status !== "summarized") {
		const total = rehabToday.checklist.length;
		const left = rehabToday.checklist.filter((item) => !item.completed).length;
		return {
			kind: "resume-rehab",
			tone: "mint",
			title: left > 0 ? `${left} of ${total} left` : "Finish today's session",
			action: { label: "Resume", href: rehabSessionHref(rehabToday) },
		};
	}

	return { kind: "done-today", tone: "lilac", title: "Done for today", quiet: { label: "See history", href: `${base}/history` } };
}

/** The part of nextStepFor that the episode list can answer without extra fetches. */
export function listStepFor(episode: CareEpisode): NextStepInfo | null {
	const triage = episode.latest_triage_summary || null;
	const risk = riskStep(episode, triage, `/episodes/${episode.care_episode_id}`);
	if (risk) return risk;
	if (!triage && episode.safety_gate_status === "needs_triage") return startTriageStep(episode);
	return null;
}

export function NextStep({ step }: { step: NextStepInfo }) {
	return (
		<section className={`ep-next sticker tone-${step.tone}`} aria-live="polite" aria-labelledby="ep-next-title" data-kind={step.kind}>
			<div className="ep-next-copy">
				<h2 id="ep-next-title" className="ep-next-title">{step.title}</h2>
				{step.chips?.length ? (
					<ul className="ep-next-chips" aria-label="Safety signals">
						{step.chips.map((chip) => <li key={chip}>{chip}</li>)}
					</ul>
				) : null}
			</div>
			{step.action || step.secondary || step.quiet ? (
				<div className="ep-next-action">
					{step.action ? <Link className="btn-primary ep-next-cta" href={step.action.href}>{step.action.label}<ArrowIcon /></Link> : null}
					{step.secondary ? <Link className="btn-secondary ep-next-secondary" href={step.secondary.href}>{step.secondary.label}</Link> : null}
					{step.quiet ? <Link className="ep-next-quiet" href={step.quiet.href}>{step.quiet.label}</Link> : null}
				</div>
			) : null}
		</section>
	);
}
