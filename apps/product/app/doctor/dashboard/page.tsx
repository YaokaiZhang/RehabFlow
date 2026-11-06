"use client";

import Link from "next/link";
import { type FormEvent, useMemo, useState } from "react";

import { type DoctorDashboardRelationship } from "@rehab/shared/api";

import { AppButton } from "../../../components/ui";
import { useDoctorDashboard } from "./useDoctorDashboard";

type BriefingSection = {
	key?: string;
	title?: string;
	items?: Array<Record<string, unknown>>;
};

const bucketTone: Record<string, string> = {
	clinician_review: "rose",
	upcoming_appointments: "sky",
};

const artifactInitialState = {
	title: "",
	content: "",
};

function formatDateTime(value?: string | null) {
	if (!value) return "Not scheduled";
	return new Date(value).toLocaleString();
}

function textValue(value: unknown, fallback = "Not provided") {
	return typeof value === "string" && value.trim() ? value : fallback;
}

function statusCopy(status: string) {
	const words = status.split("_").filter(Boolean).join(" ");
	return words ? words.charAt(0).toUpperCase() + words.slice(1) : "Not recorded";
}

function safetyCopy(status: string | null | undefined) {
	if (!status) return "Not recorded";
	if (status === "needs_triage") return "Needs AI Triage Intake before AI Daily Rehab";
	if (status === "triage_complete") return "AI triage context available";
	if (status === "clinician_reviewed") return "Clinician-reviewed context available";
	return "Not recorded";
}

function sectionItems(section: BriefingSection) {
	return Array.isArray(section.items) ? section.items : [];
}

function BriefingSections({ sections }: { sections: Array<Record<string, unknown>> }) {
	// Only sections with content; empty ones repeat what the rest of the page already says.
	const filled = sections.map((section) => section as BriefingSection).filter((section) => sectionItems(section).length);
	if (!filled.length) return <p className="doctor-empty-note">nothing in today&apos;s briefing yet</p>;
	return (
		<div className="doctor-briefing-sections">
			{filled.map((typedSection, index) => (
				<div key={typedSection.key || index}>
					<h3 className="doctor-subheading">{textValue(typedSection.title, "Briefing section")}</h3>
					<ul className="doctor-briefing-list">
						{sectionItems(typedSection).map((item, itemIndex) => (
							<li key={String(item.relationship_id || item.care_episode_id || item.appointment_id || itemIndex)}>
								<p className="doctor-row-title">{textValue(item.patient_name || item.issue_title || item.label || item.title, "Panel item")}</p>
								{item.goal ? <p className="doctor-row-meta">Goal: {String(item.goal)}</p> : null}
							</li>
						))}
					</ul>
				</div>
			))}
		</div>
	);
}

function RelationshipSummary({ relationship }: { relationship: DoctorDashboardRelationship }) {
	const triage = relationship.latest_triage_summary as { recommendation?: string; concern?: string; clinician_review_needed?: boolean } | null | undefined;
	return (
		<article className="doctor-relationship">
			<div className="doctor-relationship-head">
				<div className="min-w-0">
					<h3 className="doctor-row-title">{textValue(relationship.patient_name, "Patient")}</h3>
					<p className="doctor-row-meta">{relationship.issue_title || "Care Episode"}</p>
				</div>
				{triage?.clinician_review_needed ? <span className="tag" data-tone="rose">Needs your review</span> : null}
			</div>
			<dl className="doctor-relationship-facts">
				<div><dt>Goal</dt><dd>{relationship.goal || "Not recorded"}</dd></div>
				<div><dt>Body area</dt><dd>{relationship.body_area || "Not recorded"}</dd></div>
				<div><dt>Safety gate</dt><dd>{safetyCopy(relationship.safety_gate_status)}</dd></div>
				<div><dt>Started</dt><dd>{formatDateTime(relationship.started_at)}</dd></div>
			</dl>
			{triage?.recommendation || triage?.concern ? (
				<div className="doctor-relationship-triage" data-tone="peach">
					<span className="tag">Latest triage summary</span>
					<p>{triage.recommendation || triage.concern}</p>
				</div>
			) : null}
		</article>
	);
}

export default function DoctorDashboardPage() {
	const { auth, dashboard, loading, error, statusMessage, busyAction, refreshBriefing, saveArtifact, savedArtifact } = useDoctorDashboard();
	const [artifactDraft, setArtifactDraft] = useState(artifactInitialState);
	const briefingSections = useMemo(() => dashboard?.patient_panel_briefing.sections || [], [dashboard]);

	const submitArtifact = async (event: FormEvent<HTMLFormElement>) => {
		event.preventDefault();
		if (!artifactDraft.title.trim() || !artifactDraft.content.trim()) return;
		const saved = await saveArtifact({
			artifact_type: "doctor_prep_note",
			title: artifactDraft.title.trim(),
			content: artifactDraft.content.trim(),
			input_refs: {
				doctor_dashboard_id: dashboard?.doctor_id,
				briefing_artifact_id: dashboard?.patient_panel_briefing.artifact_id,
			},
		});
		if (saved) setArtifactDraft(artifactInitialState);
	};

	if (auth === undefined) {
		return (
			<section className="page-column-narrow page-header" data-tone="sky">
				<p className="page-header-kicker">Professional Care</p>
				<h1>Doctor Dashboard</h1>
				<p className="scribble mt-4" role="status">checking your session…</p>
			</section>
		);
	}

	if (!auth || auth.role !== "doctor") {
		return (
			<section className="page-column-narrow page-header" data-tone="sky">
				<p className="page-header-kicker">Professional Care</p>
				<h1>Sign in to open Doctor Dashboard</h1>
				<div className="page-header-actions">
					<Link className="btn-primary" href="/login?role=doctor">Log in</Link>
				</div>
			</section>
		);
	}

	const attentionBuckets = (dashboard?.attention_map.buckets || []).filter((bucket) => bucket.items.length);
	const relationships = dashboard?.care_relationships || [];
	const appointments = dashboard?.upcoming_appointments || [];

	return (
		<div className="page-column">
			<section className="page-header doctor-page-header doctor-dashboard-header" data-tone="sky">
				<div>
					<p className="page-header-kicker">Professional Care</p>
					<h1>Doctor Dashboard</h1>
				</div>
				<div className="doctor-header-actions">
					<Link className="btn-secondary" href={dashboard?.care_worklist_href || "/doctor"}>Care Worklist</Link>
					<AppButton variant="secondary" onClick={refreshBriefing} disabled={loading || busyAction !== null}>{busyAction === "refresh" ? "Refreshing…" : "Refresh briefing"}</AppButton>
				</div>
			</section>

			<div className="doctor-messages">
				{error ? <p className="notice notice-danger">{error}</p> : null}
				{statusMessage ? <p className="notice" role="status">{statusMessage}</p> : null}
				{loading ? <p className="scribble" role="status">loading dashboard…</p> : null}
			</div>

			<div className="doctor-dashboard">
				<section className="doctor-attention" aria-labelledby="doctor-attention-title">
					<div className="doctor-section-head">
						<h2 id="doctor-attention-title" className="section-title">Attention map</h2>
						{dashboard?.attention_map.summary ? <p className="doctor-section-meta">{dashboard.attention_map.summary}</p> : null}
					</div>
					{attentionBuckets.length ? (
						<div className="doctor-attention-grid">
							{attentionBuckets.map((bucket) => (
								<article key={bucket.bucket} className={`doctor-bucket tone-${bucketTone[bucket.bucket] || "plain"}`}>
									<h3 className="doctor-bucket-head">
										<span>{bucket.label}</span>
										<span className="doctor-bucket-count">{bucket.items.length}</span>
									</h3>
									<ul className="doctor-bucket-list">
										{bucket.items.map((item, index) => (
											<li key={String(item.relationship_id || item.appointment_id || index)}>{textValue(item.label || item.issue_title || item.purpose, "Attention item")}</li>
										))}
									</ul>
								</article>
							))}
						</div>
					) : !loading ? <p className="doctor-empty-note">nothing needs you right now</p> : null}
				</section>

				<section className="card doctor-briefing" aria-labelledby="doctor-briefing-title">
					<h2 id="doctor-briefing-title" className="section-title">{dashboard?.patient_panel_briefing.title || "Patient Panel Briefing"}</h2>
					<BriefingSections sections={briefingSections} />
					{dashboard?.patient_panel_briefing.content ? (
						<details className="doctor-briefing-full">
							<summary>Read the full briefing</summary>
							<div className="doctor-briefing-text">{dashboard.patient_panel_briefing.content}</div>
						</details>
					) : null}
				</section>

				<div className="doctor-dashboard-columns">
					<section aria-labelledby="doctor-patients-title" data-tone="sky">
						<h2 id="doctor-patients-title" className="doctor-queue-heading">
							<span className="session-dot" aria-hidden="true" />
							<span>Your patients</span>
							<span className="doctor-queue-count">{relationships.length}</span>
						</h2>
						<div className="doctor-stack">
							{relationships.length ? relationships.map((relationship) => (
								<RelationshipSummary key={relationship.relationship_id} relationship={relationship} />
							)) : <p className="doctor-empty-note">no patients yet</p>}
						</div>
					</section>

					<section aria-labelledby="doctor-appointments-title" data-tone="sky">
						<h2 id="doctor-appointments-title" className="doctor-queue-heading">
							<span className="session-dot" aria-hidden="true" />
							<span>Upcoming appointments</span>
							<span className="doctor-queue-count">{appointments.length}</span>
						</h2>
						<div className="doctor-stack">
							{appointments.length ? appointments.map((appointment) => (
								<article key={appointment.appointment_id} className="doctor-appointment">
									<p className="doctor-row-title">{appointment.purpose || "Care appointment"}</p>
									<p className="doctor-row-meta">{formatDateTime(appointment.scheduled_start)}</p>
									<span className="tag">{statusCopy(appointment.status)}</span>
								</article>
							)) : <p className="doctor-empty-note">no appointments yet</p>}
						</div>
					</section>
				</div>

				<section className="card doctor-prep" aria-labelledby="doctor-prep-title">
					<div className="doctor-section-head">
						<h2 id="doctor-prep-title" className="section-title">Prep note</h2>
						{savedArtifact ? <span className="tag" data-tone="sky">Saved v{savedArtifact.version}</span> : null}
					</div>
					<form className="doctor-prep-form" onSubmit={submitArtifact}>
						<label className="doctor-field">
							<span>Title</span>
							<input className="input" value={artifactDraft.title} onChange={(event) => setArtifactDraft((prev) => ({ ...prev, title: event.target.value }))} placeholder="Prep note title" />
						</label>
						<label className="doctor-field">
							<span>Note</span>
							<textarea className="textarea min-h-32" value={artifactDraft.content} onChange={(event) => setArtifactDraft((prev) => ({ ...prev, content: event.target.value }))} placeholder="What you want to remember before your next sessions" />
						</label>
						<div>
							<AppButton type="submit" disabled={busyAction !== null || !artifactDraft.title.trim() || !artifactDraft.content.trim()}>{busyAction === "save" ? "Saving…" : "Save prep note"}</AppButton>
						</div>
					</form>
				</section>
			</div>
		</div>
	);
}
