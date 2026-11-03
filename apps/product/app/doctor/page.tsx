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
				<p className="text-sm font-semibold text-emerald-700">Care Worklist</p>
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
		<div className="page-column space-y-6">
			<section className="page-header">
				<p className="page-header-kicker"><Link href="/doctor">Professional Care</Link></p>
				<h1>Care Worklist</h1>
				<p className="page-header-lead">New requests, patients waiting to choose you, and the patients you are working with.</p>
			</section>

			{loading ? <p className="text-sm text-slate-500">Loading Care Worklist...</p> : null}
			{statusMessage ? <p className="notice" role="status">{statusMessage}</p> : null}
			{errorMessage ? <p className="notice notice-danger">{errorMessage}</p> : null}

			<div className="doctor-worklist-grid">
			<div className="doctor-queue">
			<section className="doctor-queue-section">
				<SectionHeader title="Incoming requests" description={worklist.incoming_requests.length ? undefined : "No new requests."} />
				<div className="mt-3 space-y-3">
					{worklist.incoming_requests.map((request) => (
						<DashboardCard key={request.request_id} className="p-4">
							<p className="font-bold text-slate-950">{request.issue_title || "Care Episode"}</p>
							<p className="mt-0.5 text-sm text-slate-600">{displayPatientName(request.patient_name)}</p>
							<p className="mt-2 line-clamp-3 text-sm leading-relaxed text-slate-700">{request.latest_triage_summary ? request.latest_triage_summary.recommendation : request.request_reason}</p>
							{request.latest_triage_summary ? <p className="mt-1 text-xs text-slate-500">From the patient&apos;s Triage Summary</p> : null}
							<div className="mt-3 flex gap-2">
								<AppButton className="flex-1" onClick={() => respond(request, "accept")} disabled={busyRequestId === request.request_id}>Accept</AppButton>
								<AppButton variant="secondary" className="flex-1" onClick={() => respond(request, "reject")} disabled={busyRequestId === request.request_id}>Decline</AppButton>
							</div>
						</DashboardCard>
					))}
				</div>
			</section>

			{worklist.accepted_requests.length ? (
				<section className="doctor-queue-section">
					<SectionHeader title="Awaiting patient selection" description="You accepted. The patient now chooses a doctor." />
					<div className="mt-3 space-y-2">
						{worklist.accepted_requests.map((request) => (
							<DashboardCard key={request.request_id} className="p-4">
								<div className="flex items-start justify-between gap-3">
									<div className="min-w-0">
										<p className="font-bold text-slate-950">{request.issue_title || "Care Episode"}</p>
										<p className="mt-0.5 text-sm text-slate-600">{displayPatientName(request.patient_name)}</p>
									</div>
									<StatusBadge tone="neutral">Accepted</StatusBadge>
								</div>
							</DashboardCard>
						))}
					</div>
				</section>
			) : null}

			<section className="doctor-queue-section">
				<SectionHeader title="Your patients" description={worklist.active_relationships.length ? undefined : "Patients appear here after they choose you."} />
				<div className="mt-3 space-y-2" role="list">
					{worklist.active_relationships.map((relationship) => (
						<button
							key={relationship.relationship_id}
							type="button"
							role="listitem"
							aria-current={selectedRelationshipId === relationship.relationship_id ? "true" : undefined}
							className={`doctor-patient-row ${selectedRelationshipId === relationship.relationship_id ? "doctor-patient-row-active" : ""}`}
							onClick={() => setSelectedRelationshipId(relationship.relationship_id)}
						>
							<span className="block font-bold text-slate-950">{relationship.issue_title || "Care Episode"}</span>
							<span className="mt-0.5 block text-sm text-slate-600">{displayPatientName(relationship.patient_name)}</span>
							{relationship.latest_triage_summary ? <span className="mt-1.5 line-clamp-2 block text-sm leading-snug text-slate-600">{relationship.latest_triage_summary.recommendation}</span> : null}
							<span className="mt-1.5 block text-xs text-slate-500">{relationship.last_activity_label}: {new Date(relationship.last_activity_at).toLocaleString()}</span>
						</button>
					))}
				</div>
			</section>
			</div>

			<div className="doctor-detail">
			{!selectedRelationship ? (
				<div className="doctor-detail-empty">
					<p className="font-bold text-ink">No patient selected</p>
					<p className="mt-1 text-sm text-slate-600">{worklist.active_relationships.length ? "Choose a patient from Your patients to see their episode." : "When a patient chooses you, their episode opens here."}</p>
				</div>
			) : null}
			{selectedRelationship ? (
				<section className="space-y-4">
					{selectedWorkspace ? <CareEpisodeBrief workspace={selectedWorkspace} role="doctor" /> : <p className="notice">Loading Care Episode Brief...</p>}
					{selectedWorkspace ? (
						<div className="space-y-3">
							<AccordionSection title="Full Triage Summary" summary={selectedTriage ? selectedTriage.recommendation : "No saved Triage Summary yet."} defaultOpen={Boolean(selectedTriage)}>
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
									<div className="max-h-64 space-y-2 overflow-auto rounded-control border border-line bg-[var(--rf-surface-muted)] p-3">
										{selectedMessages.length === 0 ? <p className="text-sm text-slate-500">No messages yet.</p> : null}
										{selectedMessages.map((message) => <div key={message.message_id} className="rounded-control border border-line bg-white p-3"><p className="text-xs font-bold text-action">{message.sender_role === "doctor" ? "You" : displayPatientName(selectedRelationship.patient_name)}</p><p className="mt-1 text-slate-700">{message.content}</p></div>)}
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

							<AccordionSection title="Optional Live Movement Monitoring" summary="Watch the patient's movement live during a session.">
								<div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
									<p className="leading-6 text-slate-600">Useful when you need to see how the patient moves. The patient starts the session from AI Daily Rehab.</p>
									<Link className="btn-secondary" href={`/episodes/${selectedRelationship.care_episode_id}/professional-care/live-monitor`}>Open Optional Live Movement Monitoring</Link>
								</div>
							</AccordionSection>
						</div>
					) : null}
				</section>
			) : null}
			</div>
			</div>
		</div>
	);
}
