"use client";

import Link from "next/link";

import { AccordionSection } from "../../components/AccordionSection";
import { CareEpisodeBrief } from "../../components/CareEpisodeBrief";
import { AppButton, DashboardCard, ProgressPill, SectionHeader, StatusBadge } from "../../components/ui";
import { useDoctorCareWorklist } from "./useDoctorCareWorklist";

function listText(items: string[] | undefined, fallback: string) {
	return items && items.length ? items.join(" ") : fallback;
}

function displayPatientName(name: string | null | undefined) {
	return name?.trim() || "Patient";
}

export default function DoctorPage() {
	const {
		auth,
		worklist,
		workspaceByEpisode,
		messagesByEpisode,
		summaryByEpisode,
		draftByEpisode,
		selectedRelationship,
		selectedRelationshipId,
		errorMessage,
		statusMessage,
		loading,
		busyRequestId,
		busyEpisodeId,
		setSelectedRelationshipId,
		setDraftForEpisode,
		respond,
		sendMessage,
		summarize,
	} = useDoctorCareWorklist();

	const selectedWorkspace = selectedRelationship ? workspaceByEpisode[selectedRelationship.care_episode_id] : null;
	const selectedTriage = selectedWorkspace?.latest_triage_summary;
	const selectedRehab = selectedWorkspace?.latest_rehab_session;
	const selectedMessages = selectedRelationship ? messagesByEpisode[selectedRelationship.care_episode_id] || [] : [];
	const selectedSummary = selectedRelationship ? summaryByEpisode[selectedRelationship.care_episode_id] || selectedWorkspace?.latest_ai_care_summary : null;
	const selectedDraft = selectedRelationship ? draftByEpisode[selectedRelationship.care_episode_id] || "" : "";
	const selectedBusy = selectedRelationship ? busyEpisodeId === selectedRelationship.care_episode_id : false;
	const selectedChecklist = Array.isArray(selectedRehab?.checklist) ? selectedRehab.checklist : [];
	const completedChecklistCount = selectedChecklist.filter((item) => item.completed).length;

	if (!auth || auth.role !== "doctor") {
		return (
			<section className="card max-w-2xl">
				<p className="text-xs font-semibold uppercase tracking-wide text-emerald-700">Care Worklist</p>
				<h1 className="mt-2 text-2xl font-semibold text-slate-950">Sign in as a doctor</h1>
				<p className="mt-2 text-sm leading-6 text-slate-600">Doctors review incoming Care Connection Requests and active Care Relationships here.</p>
				<div className="mt-4 flex flex-wrap gap-2">
					<Link className="btn-primary" href="/login?role=doctor&next=/doctor">Login</Link>
					<Link className="btn-secondary" href="/register">Register</Link>
				</div>
			</section>
		);
	}

	return (
		<div className="space-y-5">
			<section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm md:p-6">
				<div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
					<div>
						<p className="text-xs font-semibold uppercase tracking-wide text-emerald-700">Professional Care</p>
						<h1 className="mt-2 text-2xl font-semibold text-slate-950 md:text-3xl">Care Worklist</h1>
						<p className="mt-2 max-w-2xl text-sm leading-6 text-slate-600">Review incoming Care Connection Requests, continue Care Conversation, and generate AI Care Summary for the selected Care Relationship.</p>
					</div>
					<Link className="btn-secondary" href="/doctor">Care Worklist</Link>
				</div>
			</section>

			{loading ? <p className="text-sm text-slate-500">Loading Care Worklist...</p> : null}
			{statusMessage ? <p className="rounded-md bg-emerald-50 p-3 text-sm text-emerald-700">{statusMessage}</p> : null}
			{errorMessage ? <p className="rounded-md bg-rose-50 p-3 text-sm text-rose-700">{errorMessage}</p> : null}

			<section className="card">
				<SectionHeader eyebrow="Care Worklist" title="Incoming requests" description="Review new Care Connection Requests before they become active Care Relationships." />
				{worklist.incoming_requests.length === 0 ? <p className="mt-2 text-sm text-slate-500">No pending Care Connection Requests.</p> : null}
				<div className="mt-4 space-y-3">
					{worklist.incoming_requests.map((request) => (
						<DashboardCard key={request.request_id} className="rounded-md p-4 shadow-none hover:border-slate-200 hover:shadow-none">
							<div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
								<div>
									<p className="text-sm font-semibold text-slate-950">{request.issue_title || "Care Episode"}</p>
									<p className="mt-1 text-xs text-slate-500">Patient: {displayPatientName(request.patient_name)}</p>
									<p className="mt-2 text-sm leading-6 text-slate-600">{request.request_reason}</p>
								</div>
								<div className="flex gap-2">
									<AppButton onClick={() => respond(request, "accept")} disabled={busyRequestId === request.request_id}>Accept</AppButton>
									<AppButton variant="secondary" onClick={() => respond(request, "reject")} disabled={busyRequestId === request.request_id}>Reject</AppButton>
								</div>
							</div>
							{request.latest_triage_summary ? (
								<div className="mt-3 rounded-md border border-emerald-100 bg-emerald-50 p-3 text-sm text-slate-700">
									<p className="font-semibold text-slate-950">Latest Triage Summary</p>
									<p className="mt-1">{request.latest_triage_summary.recommendation}</p>
								</div>
							) : null}
						</DashboardCard>
					))}
				</div>
			</section>

			<section className="card">
				<SectionHeader eyebrow="Care Worklist" title="Accepted requests" description="These doctors have accepted a request and are waiting for patient selection." />
				{worklist.accepted_requests.length === 0 ? <p className="mt-2 text-sm text-slate-500">No accepted Care Connection Requests are waiting for patient selection.</p> : null}
				<div className="mt-4 space-y-3">
					{worklist.accepted_requests.map((request) => (
						<DashboardCard key={request.request_id} className="rounded-md border-emerald-200 bg-emerald-50 p-4 shadow-none hover:border-emerald-200 hover:shadow-none">
							<div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
								<div>
									<p className="text-xs font-semibold uppercase tracking-wide text-emerald-700">Awaiting patient selection</p>
									<p className="mt-1 text-sm font-semibold text-slate-950">{request.issue_title || "Care Episode"}</p>
									<p className="mt-1 text-xs text-slate-500">Patient: {displayPatientName(request.patient_name)}</p>
									<p className="mt-2 text-sm leading-6 text-slate-600">{request.request_reason}</p>
								</div>
								<StatusBadge tone="success">Accepted</StatusBadge>
							</div>
							{request.latest_triage_summary ? (
								<div className="mt-3 rounded-md border border-emerald-100 bg-white p-3 text-sm text-slate-700">
									<p className="font-semibold text-slate-950">Latest Triage Summary</p>
									<p className="mt-1">{request.latest_triage_summary.recommendation}</p>
								</div>
							) : null}
						</DashboardCard>
					))}
				</div>
			</section>

			<section className="card">
				<SectionHeader
					eyebrow="Professional Care"
					title="Active Care Relationships"
					description="Choose one active relationship to review the Care Episode Brief and deeper Professional Care details."
					action={selectedRelationship ? <StatusBadge tone="info">Selected relationship</StatusBadge> : undefined}
				/>
				{worklist.active_relationships.length === 0 ? <p className="mt-3 text-sm text-slate-500">Patients appear here after they select you for an accepted request.</p> : null}
				<div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
					{worklist.active_relationships.map((relationship) => (
						<button
							key={relationship.relationship_id}
							type="button"
							className={`rounded-md border p-4 text-left transition ${selectedRelationshipId === relationship.relationship_id ? "border-emerald-500 bg-emerald-50 shadow-sm" : "border-slate-200 bg-white hover:border-emerald-300"}`}
							onClick={() => setSelectedRelationshipId(relationship.relationship_id)}
						>
							<StatusBadge tone="info">Active Care Relationship</StatusBadge>
							<h3 className="mt-1 text-base font-semibold text-slate-950">{relationship.issue_title || "Care Episode"}</h3>
							<p className="mt-1 text-sm text-slate-600">Patient: {displayPatientName(relationship.patient_name)}</p>
							<p className="mt-2 text-xs text-slate-500">{relationship.last_activity_label}: {new Date(relationship.last_activity_at).toLocaleString()}</p>
							{relationship.latest_triage_summary ? <p className="mt-2 line-clamp-2 text-sm leading-5 text-slate-600">{relationship.latest_triage_summary.recommendation}</p> : null}
						</button>
					))}
				</div>
			</section>

			{selectedRelationship ? (
				<section className="space-y-4">
					{selectedWorkspace ? <CareEpisodeBrief workspace={selectedWorkspace} role="doctor" /> : <p className="rounded-md border border-slate-200 bg-white p-4 text-sm text-slate-500">Loading Care Episode Brief...</p>}
					{selectedWorkspace ? (
						<div className="space-y-3">
							<AccordionSection title="Full Triage Summary" summary={selectedTriage ? selectedTriage.recommendation : "No saved Triage Summary yet."} defaultOpen>
								{selectedTriage ? (
									<div className="space-y-3 leading-6">
										<p><span className="font-semibold text-slate-950">Concern:</span> {selectedTriage.concern}</p>
										<p><span className="font-semibold text-slate-950">Relevant context:</span> {selectedTriage.relevant_context || "None captured."}</p>
										<p><span className="font-semibold text-slate-950">Safety signals:</span> {listText(selectedTriage.safety_signals, "None captured.")}</p>
										<p><span className="font-semibold text-slate-950">Limitations:</span> {listText(selectedTriage.limitations, "None captured.")}</p>
										<p><span className="font-semibold text-slate-950">Missing information:</span> {listText(selectedTriage.missing_information, "None captured.")}</p>
										<p><span className="font-semibold text-slate-950">Unresolved questions:</span> {listText(selectedTriage.unresolved_questions, "None captured.")}</p>
									</div>
								) : <p>No saved Triage Summary yet.</p>}
							</AccordionSection>

							<AccordionSection title="Rehab History" summary={selectedRehab ? `${completedChecklistCount}/${selectedChecklist.length} checklist items complete` : "No rehab session yet."}>
								{selectedRehab ? (
									<div className="space-y-3 leading-6">
										<ProgressPill value={completedChecklistCount} max={selectedChecklist.length} label={`${completedChecklistCount}/${selectedChecklist.length} complete`} />
										<p><span className="font-semibold text-slate-950">Latest rehab checklist count:</span> {completedChecklistCount}/{selectedChecklist.length} complete</p>
										<p><span className="font-semibold text-slate-950">Patient notes:</span> {selectedRehab.patient_notes || "None captured."}</p>
										<p><span className="font-semibold text-slate-950">Session summary:</span> {selectedRehab.session_summary || "None captured."}</p>
									</div>
								) : <p>No rehab session has been recorded for this Care Episode yet.</p>}
							</AccordionSection>

							<AccordionSection title="Care Conversation" summary={`${selectedMessages.length} message${selectedMessages.length === 1 ? "" : "s"}`}>
								<div className="space-y-3">
									<div className="max-h-64 space-y-2 overflow-auto rounded-md border border-slate-200 bg-slate-50 p-3">
										{selectedMessages.length === 0 ? <p className="text-sm text-slate-500">No messages yet.</p> : null}
										{selectedMessages.map((message) => <div key={message.message_id} className="rounded-md border border-slate-200 bg-white p-3"><p className="text-xs font-semibold uppercase tracking-wide text-slate-400">{message.sender_role}</p><p className="mt-1 text-slate-700">{message.content}</p></div>)}
									</div>
									<div className="flex flex-col gap-2 sm:flex-row">
										<input className="input" value={selectedDraft} onChange={(event) => setDraftForEpisode(selectedRelationship.care_episode_id, event.target.value)} placeholder="Reply to patient" />
										<AppButton onClick={() => sendMessage(selectedRelationship.care_episode_id)} disabled={selectedBusy || !selectedDraft.trim()}>Send</AppButton>
									</div>
								</div>
							</AccordionSection>

							<AccordionSection title="AI Care Summary" summary={selectedSummary ? "Generated summary available" : "Generate a care digest from the active conversation."}>
								{selectedSummary ? <><p className="leading-6 text-slate-700">{selectedSummary.conversation_digest}</p><p className="mt-3 font-semibold text-slate-950">Plan</p><p className="mt-1 leading-6 text-slate-700">{selectedSummary.plan_digest}</p></> : <p className="leading-6 text-slate-600">Generate a care digest from the active conversation. Optional Live Movement Monitoring stays secondary to the episode narrative.</p>}
								<AppButton className="mt-3" onClick={() => summarize(selectedRelationship.care_episode_id)} disabled={selectedBusy}>Generate Summary</AppButton>
							</AccordionSection>

							<AccordionSection title="Optional Live Movement Monitoring" summary="Open the episode-scoped live movement review console.">
								<div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
									<p className="leading-6 text-slate-600">Use this only when the active Care Relationship needs live movement context for this Care Episode.</p>
									<Link className="btn-secondary" href={`/episodes/${selectedRelationship.care_episode_id}/professional-care/live-monitor`}>Open Optional Live Movement Monitoring</Link>
								</div>
							</AccordionSection>
						</div>
					) : null}
				</section>
			) : null}
		</div>
	);
}
