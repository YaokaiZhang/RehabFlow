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
	if (!sections.length) return null;
	return (
		<div className="mt-4 grid gap-3 lg:grid-cols-2">
			{sections.map((section, index) => {
				const typedSection = section as BriefingSection;
				return (
					<DashboardCard key={typedSection.key || index} className="rounded-md bg-slate-50 p-4 shadow-none hover:border-slate-200 hover:shadow-none">
						<h3 className="text-sm font-semibold text-slate-950">{textValue(typedSection.title, "Briefing section")}</h3>
						<div className="mt-3 space-y-2">
							{sectionItems(typedSection).length ? sectionItems(typedSection).map((item, itemIndex) => (
								<div key={String(item.relationship_id || item.care_episode_id || item.appointment_id || itemIndex)} className="rounded-md border border-slate-200 bg-white p-3 text-sm text-slate-700">
									<p className="font-medium text-slate-950">{textValue(item.patient_name || item.issue_title || item.label || item.title, "Panel item")}</p>
									{item.goal ? <p className="mt-1">Goal: {String(item.goal)}</p> : null}
									{item.memory_context ? <p className="mt-1">Memory: {String(item.memory_context)}</p> : null}
								</div>
							)) : <p className="text-sm text-slate-500">No items in this section.</p>}
						</div>
					</DashboardCard>
				);
			})}
		</div>
	);
}

function RelationshipSummary({ relationship }: { relationship: DoctorDashboardRelationship }) {
	const triage = relationship.latest_triage_summary as { recommendation?: string; concern?: string; clinician_review_needed?: boolean } | null | undefined;
	return (
		<DashboardCard className="rounded-md p-4 shadow-none hover:border-slate-200 hover:shadow-none">
			<div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
				<div>
					<p className="text-xs font-semibold uppercase tracking-wide text-emerald-700">{textValue(relationship.patient_name, "Patient")}</p>
					<h3 className="mt-1 text-base font-semibold text-slate-950">{relationship.issue_title || "Care Episode"}</h3>
				</div>
				<StatusBadge tone={triage?.clinician_review_needed ? "risk" : "success"}>
					{triage?.clinician_review_needed ? "Review signal" : "Briefing signal"}
				</StatusBadge>
			</div>
			<div className="mt-3 grid gap-2 text-sm text-slate-700 md:grid-cols-2">
				<p><span className="font-medium text-slate-950">Goal:</span> {relationship.goal || "Not recorded"}</p>
				<p><span className="font-medium text-slate-950">Body area:</span> {relationship.body_area || "Not recorded"}</p>
				<p><span className="font-medium text-slate-950">Safety gate:</span> {safetyCopy(relationship.safety_gate_status)}</p>
				<p><span className="font-medium text-slate-950">Started:</span> {formatDateTime(relationship.started_at)}</p>
			</div>
			{relationship.memory_context ? <p className="mt-3 text-sm leading-6 text-slate-700">{relationship.memory_context}</p> : null}
			{triage?.recommendation || triage?.concern ? (
				<div className="mt-3 rounded-md border border-emerald-100 bg-emerald-50 p-3 text-sm text-slate-700">
					<p className="font-medium text-slate-950">Latest triage summary</p>
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
			<section className="card max-w-2xl">
				<p className="text-xs font-semibold uppercase tracking-wide text-emerald-700">Doctor Dashboard</p>
				<h1 className="mt-2 text-2xl font-semibold text-slate-950">Loading Doctor Dashboard...</h1>
				<p className="mt-2 text-sm leading-6 text-slate-600">Checking your session.</p>
			</section>
		);
	}

	if (!auth || auth.role !== "doctor") {
		return (
			<section className="card max-w-2xl">
				<p className="text-xs font-semibold uppercase tracking-wide text-emerald-700">Doctor Dashboard</p>
				<h1 className="mt-2 text-2xl font-semibold text-slate-950">Sign in to open Doctor Dashboard</h1>
				<p className="mt-2 text-sm leading-6 text-slate-600">Doctor Dashboard prepares your panel briefing and attention map. Care Worklist actions stay in the doctor worklist.</p>
				<div className="mt-4 flex flex-wrap gap-2">
					<Link className="btn-primary" href="/login">Login</Link>
					<Link className="btn-secondary" href="/doctor">Care Worklist</Link>
				</div>
			</section>
		);
	}

	return (
		<div className="space-y-5">
			<section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm md:p-6">
				<div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
					<div>
						<p className="text-xs font-semibold uppercase tracking-wide text-emerald-700">Panel intelligence</p>
						<h1 className="mt-2 text-2xl font-semibold text-slate-950 md:text-3xl">Doctor Dashboard</h1>
						<p className="mt-2 max-w-2xl text-sm leading-6 text-slate-600">Review Patient Panel Briefing, Attention Map, appointments, and relationship intelligence before deciding what to do next.</p>
					</div>
					<div className="flex flex-wrap gap-2">
						<AppButton variant="secondary" onClick={refreshBriefing} disabled={loading || busyAction !== null}>{busyAction === "refresh" ? "Refreshing" : "Refresh Briefing"}</AppButton>
						<Link className="btn-primary" href={dashboard?.care_worklist_href || "/doctor"}>Care Worklist</Link>
					</div>
				</div>
			</section>

			{error ? <p className="rounded-md border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">{error}</p> : null}
			{statusMessage ? <p className="rounded-md border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-700">{statusMessage}</p> : null}
			{loading ? <p className="text-sm text-slate-500">Loading Doctor Dashboard...</p> : null}

			<section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm md:p-6">
				<div className="flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
					<div>
						<p className="text-xs font-semibold uppercase tracking-wide text-emerald-700">Patient Panel Briefing</p>
						<SectionHeader title={dashboard?.patient_panel_briefing.title || "Patient Panel Briefing"} />
					</div>
					<p className="text-xs font-medium text-slate-500">Version {dashboard?.patient_panel_briefing.version || 0}</p>
				</div>
				<div className="mt-4 whitespace-pre-wrap rounded-md border border-slate-200 bg-slate-50 p-4 text-sm leading-6 text-slate-700">
					{dashboard?.patient_panel_briefing.content || "No Patient Panel Briefing is available yet."}
				</div>
				<BriefingSections sections={briefingSections} />
			</section>

			<section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm md:p-6">
				<p className="text-xs font-semibold uppercase tracking-wide text-emerald-700">Attention Map</p>
				<SectionHeader title="Attention Map" description={dashboard?.attention_map.summary || "No attention signals yet."} />
				<div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
					{(dashboard?.attention_map.buckets || []).map((bucket) => (
						<article key={bucket.bucket} className="rounded-md border border-slate-200 bg-slate-50 p-4">
							<h3 className="text-sm font-semibold text-slate-950">{bucket.label}</h3>
							<div className="mt-3 space-y-2">
								{bucket.items.length ? bucket.items.map((item, index) => (
									<p key={String(item.relationship_id || item.appointment_id || index)} className="rounded-md border border-slate-200 bg-white p-2 text-sm text-slate-700">
										{textValue(item.label || item.issue_title || item.purpose, "Attention item")}
									</p>
								)) : <p className="text-sm text-slate-500">No items.</p>}
							</div>
						</article>
					))}
				</div>
			</section>

			<section className="grid gap-5 xl:grid-cols-[minmax(0,1.25fr)_minmax(320px,0.75fr)]">
				<div className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm md:p-6">
					<SectionHeader title="Relationship intelligence summaries" description="Cross-episode relationship context for panel preparation." />
					<div className="mt-4 grid gap-3">
						{dashboard?.care_relationships.length ? dashboard.care_relationships.map((relationship) => (
							<RelationshipSummary key={relationship.relationship_id} relationship={relationship} />
						)) : <p className="rounded-md border border-slate-200 bg-slate-50 p-4 text-sm text-slate-600">No active Care Relationships are available for briefing.</p>}
					</div>
				</div>

				<div className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm md:p-6">
					<SectionHeader title="Upcoming appointments" description="Care Appointments across active Care Relationships." />
					<div className="mt-4 space-y-3">
						{dashboard?.upcoming_appointments.length ? dashboard.upcoming_appointments.map((appointment) => (
							<DashboardCard key={appointment.appointment_id} className="rounded-md bg-slate-50 p-3 text-sm text-slate-700 shadow-none hover:border-slate-200 hover:shadow-none">
								<p className="font-semibold text-slate-950">{appointment.purpose || "Care appointment"}</p>
								<p className="mt-1">{formatDateTime(appointment.scheduled_start)}</p>
								<p className="mt-1 text-xs font-medium text-slate-500">{statusCopy(appointment.status)}</p>
							</DashboardCard>
						)) : <p className="rounded-md border border-slate-200 bg-slate-50 p-4 text-sm text-slate-600">No upcoming appointments.</p>}
					</div>
				</div>
			</section>

			<section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm md:p-6">
				<div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
					<div>
						<p className="text-xs font-semibold uppercase tracking-wide text-emerald-700">Doctor Intelligence Artifact</p>
						<SectionHeader title="Save prep note" />
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
						<textarea className="textarea min-h-32" value={artifactDraft.content} onChange={(event) => setArtifactDraft((prev) => ({ ...prev, content: event.target.value }))} placeholder="Write the prep artifact you explicitly want to save." />
					</label>
					<div>
						<AppButton type="submit" disabled={busyAction !== null || !artifactDraft.title.trim() || !artifactDraft.content.trim()}>{busyAction === "save" ? "Saving" : "Save Doctor Intelligence Artifact"}</AppButton>
					</div>
				</form>
			</section>
		</div>
	);
}
