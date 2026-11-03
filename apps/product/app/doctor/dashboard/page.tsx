"use client";

import Link from "next/link";
import { type FormEvent, useMemo, useState } from "react";

import { type DoctorDashboardRelationship } from "@rehab/shared/api";

import { AppButton, DashboardCard, SectionHeader, StatusBadge } from "../../../components/ui";
import { useDoctorDashboard } from "./useDoctorDashboard";

type BriefingSection = {
	key?: string;
	title?: string;
	items?: Array<Record<string, unknown>>;
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
	return status
		.split("_")
		.filter(Boolean)
		.map((part) => part.charAt(0).toUpperCase() + part.slice(1))
		.join(" ") || "Not recorded";
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
	if (!filled.length) return <p className="mt-3 text-sm text-slate-600">Nothing in today&apos;s briefing yet.</p>;
	return (
		<div className="mt-4 grid gap-6 lg:grid-cols-2">
			{filled.map((typedSection, index) => (
				<div key={typedSection.key || index}>
					<h3 className="text-base font-bold text-slate-950">{textValue(typedSection.title, "Briefing section")}</h3>
					<ul className="mt-2 divide-y divide-[var(--rf-line)] border-y border-line">
						{sectionItems(typedSection).map((item, itemIndex) => (
							<li key={String(item.relationship_id || item.care_episode_id || item.appointment_id || itemIndex)} className="py-2.5 text-sm text-slate-700">
								<p className="font-semibold text-slate-950">{textValue(item.patient_name || item.issue_title || item.label || item.title, "Panel item")}</p>
								{item.goal ? <p className="mt-0.5">Goal: {String(item.goal)}</p> : null}
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
		<DashboardCard className="p-5">
			<div className="flex flex-wrap items-start justify-between gap-2">
				<div className="min-w-0">
					<h3 className="text-base font-bold text-slate-950">{relationship.issue_title || "Care Episode"}</h3>
					<p className="mt-0.5 text-sm text-slate-600">{textValue(relationship.patient_name, "Patient")}</p>
				</div>
				{triage?.clinician_review_needed ? <StatusBadge tone="risk">Needs your review</StatusBadge> : null}
			</div>
			<div className="mt-3 grid gap-2 text-sm text-slate-700 md:grid-cols-2">
				<p><span className="font-medium text-slate-950">Goal:</span> {relationship.goal || "Not recorded"}</p>
				<p><span className="font-medium text-slate-950">Body area:</span> {relationship.body_area || "Not recorded"}</p>
				<p><span className="font-medium text-slate-950">Safety gate:</span> {safetyCopy(relationship.safety_gate_status)}</p>
				<p><span className="font-medium text-slate-950">Started:</span> {formatDateTime(relationship.started_at)}</p>
			</div>
			{triage?.recommendation || triage?.concern ? (
				<div className="mt-3 border-t border-line pt-3 text-sm text-slate-700">
					<p className="font-semibold text-slate-950">Latest triage summary</p>
					<p className="mt-1">{triage.recommendation || triage.concern}</p>
				</div>
			) : null}
		</DashboardCard>
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
			<section className="page-column-narrow page-header">
				<p className="page-header-kicker">Doctor Dashboard</p>
				<h1>Loading Doctor Dashboard...</h1>
				<p className="page-header-lead">Checking your session.</p>
			</section>
		);
	}

	if (!auth || auth.role !== "doctor") {
		return (
			<section className="page-column-narrow page-header">
				<p className="page-header-kicker">Doctor Dashboard</p>
				<h1>Sign in to open Doctor Dashboard</h1>
				<p className="page-header-lead">Log in with a doctor account to see your patient briefing.</p>
				<div className="page-header-actions">
					<Link className="btn-primary" href="/login?role=doctor">Log in</Link>
					<Link className="btn-secondary" href="/doctor">Care Worklist</Link>
				</div>
			</section>
		);
	}

	const attentionBuckets = (dashboard?.attention_map.buckets || []).filter((bucket) => bucket.items.length);

	return (
		<div className="page-column space-y-8">
			<section className="page-header">
				<p className="page-header-kicker">Professional Care</p>
				<h1>Doctor Dashboard</h1>
				<p className="page-header-lead">A short briefing on your patients and appointments. Requests and conversations live in the <Link className="font-semibold text-action underline underline-offset-4" href={dashboard?.care_worklist_href || "/doctor"}>Care Worklist</Link>.</p>
				<div className="page-header-actions">
					<AppButton onClick={refreshBriefing} disabled={loading || busyAction !== null}>{busyAction === "refresh" ? "Refreshing" : "Refresh Briefing"}</AppButton>
				</div>
			</section>

			{error ? <p className="notice notice-danger">{error}</p> : null}
			{statusMessage ? <p className="notice" role="status">{statusMessage}</p> : null}
			{loading ? <p className="text-sm text-slate-500">Loading Doctor Dashboard...</p> : null}

			<section className="card">
				<SectionHeader title={dashboard?.patient_panel_briefing.title || "Patient Panel Briefing"} />
				<BriefingSections sections={briefingSections} />
				{dashboard?.patient_panel_briefing.content ? (
					<details className="mt-5 text-sm">
						<summary className="cursor-pointer font-semibold text-action">Read the full briefing text</summary>
						<div className="mt-3 max-h-96 overflow-auto whitespace-pre-wrap rounded-control border border-line bg-[var(--rf-surface-muted)] p-4 leading-6 text-slate-700">
							{dashboard.patient_panel_briefing.content}
						</div>
					</details>
				) : null}
			</section>

			<section>
				<SectionHeader title="Attention Map" description={dashboard?.attention_map.summary || "Nothing needs your attention right now."} />
				{attentionBuckets.length ? (
					<div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
						{attentionBuckets.map((bucket) => (
							<article key={bucket.bucket} className="card p-5">
								<h3 className="flex items-center justify-between gap-2 text-base font-bold text-slate-950">{bucket.label}<StatusBadge tone="neutral">{bucket.items.length}</StatusBadge></h3>
								<ul className="mt-3 space-y-1.5 text-sm text-slate-700">
									{bucket.items.map((item, index) => (
										<li key={String(item.relationship_id || item.appointment_id || index)}>{textValue(item.label || item.issue_title || item.purpose, "Attention item")}</li>
									))}
								</ul>
							</article>
						))}
					</div>
				) : null}
			</section>

			<section className="grid gap-6 xl:grid-cols-[minmax(0,1.4fr)_minmax(300px,0.6fr)]">
				<div>
					<SectionHeader title="Your patients" />
					<div className="mt-4 grid gap-3">
						{dashboard?.care_relationships.length ? dashboard.care_relationships.map((relationship) => (
							<RelationshipSummary key={relationship.relationship_id} relationship={relationship} />
						)) : <p className="rounded-control border border-dashed border-line p-4 text-sm text-slate-600">No patients yet. They appear after choosing you in the Care Worklist.</p>}
					</div>
				</div>

				<div>
					<SectionHeader title="Upcoming appointments" />
					<div className="mt-4 space-y-2">
						{dashboard?.upcoming_appointments.length ? dashboard.upcoming_appointments.map((appointment) => (
							<DashboardCard key={appointment.appointment_id} className="p-4 text-sm text-slate-700">
								<p className="font-semibold text-slate-950">{appointment.purpose || "Care appointment"}</p>
								<p className="mt-1">{formatDateTime(appointment.scheduled_start)}</p>
								<p className="mt-1 text-xs font-medium text-slate-500">{statusCopy(appointment.status)}</p>
							</DashboardCard>
						)) : <p className="rounded-control border border-dashed border-line p-4 text-sm text-slate-600">No upcoming appointments.</p>}
					</div>
				</div>
			</section>

			<section className="card">
				<div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
					<div>
						<SectionHeader eyebrow="Doctor Intelligence Artifact" title="Save a prep note" description="Notes you save here are kept with your briefing history." />
					</div>
					{savedArtifact ? <p className="text-xs font-medium text-slate-500">Saved v{savedArtifact.version}</p> : null}
				</div>
				<form className="mt-4 grid gap-3" onSubmit={submitArtifact}>
					<label className="grid gap-1 text-sm font-medium text-slate-700">
						<span>Title</span>
						<input className="input" value={artifactDraft.title} onChange={(event) => setArtifactDraft((prev) => ({ ...prev, title: event.target.value }))} placeholder="Prep note title" />
					</label>
					<label className="grid gap-1 text-sm font-medium text-slate-700">
						<span>Content</span>
						<textarea className="textarea min-h-32" value={artifactDraft.content} onChange={(event) => setArtifactDraft((prev) => ({ ...prev, content: event.target.value }))} placeholder="What you want to remember before your next sessions" />
					</label>
					<div>
						<AppButton type="submit" disabled={busyAction !== null || !artifactDraft.title.trim() || !artifactDraft.content.trim()}>{busyAction === "save" ? "Saving" : "Save Doctor Intelligence Artifact"}</AppButton>
					</div>
				</form>
			</section>
		</div>
	);
}
