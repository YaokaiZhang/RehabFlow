"use client";

import Link from "next/link";
import type { ReactNode } from "react";

import { AccordionSection } from "../../components/AccordionSection";
import { CareEpisodeBrief } from "../../components/CareEpisodeBrief";
import { AppButton } from "../../components/ui";
import { useDoctorCareWorklist } from "./useDoctorCareWorklist";

function listText(items: string[] | undefined, fallback: string) {
	return items && items.length ? items.join(" ") : fallback;
}

function displayPatientName(name: string | null | undefined) {
	return name?.trim() || "Patient";
}

function QueueGroup({ title, tone, count, children }: { title: string; tone: "peach" | "sky"; count: number; children: ReactNode }) {
	return (
		<section className="doctor-queue-group" data-tone={tone}>
			<h2 className="doctor-queue-heading">
				<span>{title}</span>
				<span className="doctor-queue-count">{count}</span>
			</h2>
			{children}
		</section>
	);
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
			<section className="page-column-narrow page-header" data-tone="sky">
				<h1>Sign in as a doctor</h1>
				<div className="page-header-actions">
					<Link className="btn-primary" href="/login?role=doctor&next=/doctor">Log in</Link>
					<Link className="btn-secondary" href="/register">Register</Link>
				</div>
			</section>
		);
	}

	const { incoming_requests: incoming, accepted_requests: accepted, active_relationships: active } = worklist;

	return (
		<div className="page-column">
			<section className="page-header doctor-page-header" data-tone="sky">
				<h1>Care Worklist</h1>
			</section>

			<div className="doctor-messages">
				{loading ? <p className="scribble" role="status">loading worklist…</p> : null}
				{statusMessage ? <p className="notice" role="status">{statusMessage}</p> : null}
				{errorMessage ? <p className="notice notice-danger">{errorMessage}</p> : null}
			</div>

			<div className="doctor-worklist-grid">
				<div className="doctor-queue">
					<QueueGroup title="New requests" tone="peach" count={incoming.length}>
						{incoming.length ? (
							<ul className="doctor-queue-list">
								{incoming.map((request) => (
									<li key={request.request_id} className="doctor-request tone-peach">
										<p className="doctor-row-title">{request.issue_title || "Care Episode"}</p>
										<p className="doctor-row-meta">{displayPatientName(request.patient_name)}</p>
										<p className="doctor-request-reason">{request.latest_triage_summary ? request.latest_triage_summary.recommendation : request.request_reason}</p>
										{request.latest_triage_summary?.clinician_review_needed ? (
											<p className="doctor-request-source">
												<span className="tag" data-tone="rose">Clinician review</span>
											</p>
										) : null}
										<div className="doctor-request-actions">
											<button type="button" className="doctor-pill doctor-pill-accept" onClick={() => respond(request, "accept")} disabled={busyRequestId === request.request_id}>Accept</button>
											<button type="button" className="doctor-pill doctor-pill-decline" onClick={() => respond(request, "reject")} disabled={busyRequestId === request.request_id}>Decline</button>
										</div>
									</li>
								))}
							</ul>
						) : <p className="doctor-empty-note">no requests yet</p>}
					</QueueGroup>

					{accepted.length ? (
						<QueueGroup title="Waiting for patient" tone="peach" count={accepted.length}>
							<ul className="doctor-queue-list">
								{accepted.map((request) => (
									<li key={request.request_id} className="doctor-waiting-row">
										<div className="min-w-0">
											<p className="doctor-row-title">{request.issue_title || "Care Episode"}</p>
											<p className="doctor-row-meta">{displayPatientName(request.patient_name)}</p>
										</div>
									</li>
								))}
							</ul>
						</QueueGroup>
					) : null}

					<QueueGroup title="Your patients" tone="sky" count={active.length}>
						{active.length ? (
							<div className="doctor-queue-list" role="list">
								{active.map((relationship) => {
									const isActive = selectedRelationshipId === relationship.relationship_id;
									return (
										<button
											key={relationship.relationship_id}
											type="button"
											role="listitem"
											aria-current={isActive ? "true" : undefined}
											className={`doctor-patient-row ${isActive ? "doctor-patient-row-active" : ""}`}
											onClick={() => setSelectedRelationshipId(relationship.relationship_id)}
										>
											<span className="doctor-patient-row-top">
												<span className="doctor-row-title">{displayPatientName(relationship.patient_name)}</span>
												{relationship.latest_triage_summary?.clinician_review_needed ? <span className="tag" data-tone="rose">Review</span> : null}
											</span>
											<span className="doctor-row-meta">{relationship.issue_title || "Care Episode"}</span>
											<span className="doctor-row-time">{relationship.last_activity_label} · {new Date(relationship.last_activity_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}</span>
										</button>
									);
								})}
							</div>
						) : <p className="doctor-empty-note">no patients yet</p>}
					</QueueGroup>
				</div>

				<div className="doctor-detail">
					{!selectedRelationship ? (
						<div className="doctor-detail-empty">
							<p className="font-hand">{active.length ? "pick a patient" : "no patient selected"}</p>
						</div>
					) : (
						<section className="card doctor-detail-card" aria-label={`${displayPatientName(selectedRelationship.patient_name)}, ${selectedRelationship.issue_title || "Care Episode"}`}>
							<header className="doctor-detail-header">
								<div className="min-w-0">
									<p className="doctor-detail-name">{displayPatientName(selectedRelationship.patient_name)}</p>
									<p className="doctor-detail-since">With you since {new Date(selectedRelationship.started_at).toLocaleDateString(undefined, { dateStyle: "medium" })}</p>
								</div>
								<div className="doctor-detail-tags">
									{selectedTriage?.clinician_review_needed ? <span className="tag" data-tone="rose">Clinician review</span> : null}
								</div>
							</header>

							{selectedWorkspace ? <CareEpisodeBrief workspace={selectedWorkspace} role="doctor" /> : <p className="scribble" role="status">loading brief…</p>}

							{selectedWorkspace ? (
								<div className="doctor-sections">
									<AccordionSection title="Full Triage Summary" summary={selectedTriage ? selectedTriage.recommendation : undefined} defaultOpen={Boolean(selectedTriage)}>
										{selectedTriage ? (
											<dl className="doctor-facts">
												<div><dt>Concern</dt><dd>{selectedTriage.concern}</dd></div>
												<div><dt>Relevant context</dt><dd>{selectedTriage.relevant_context || "None captured."}</dd></div>
												<div><dt>Safety signals</dt><dd>{listText(selectedTriage.safety_signals, "None captured.")}</dd></div>
												<div><dt>Limitations</dt><dd>{listText(selectedTriage.limitations, "None captured.")}</dd></div>
												<div><dt>Missing information</dt><dd>{listText(selectedTriage.missing_information, "None captured.")}</dd></div>
												<div><dt>Unresolved questions</dt><dd>{listText(selectedTriage.unresolved_questions, "None captured.")}</dd></div>
											</dl>
										) : <p className="doctor-empty-note">no triage summary yet</p>}
									</AccordionSection>

									<AccordionSection title="Rehab History" summary={selectedRehab ? `${completedChecklistCount} of ${selectedChecklist.length} checklist items done` : undefined}>
										{selectedRehab ? (
											<dl className="doctor-facts">
												<div><dt>Patient notes</dt><dd>{selectedRehab.patient_notes || "None captured."}</dd></div>
												<div><dt>Session summary</dt><dd>{selectedRehab.session_summary || "None captured."}</dd></div>
											</dl>
										) : <p className="doctor-empty-note">no rehab session yet</p>}
									</AccordionSection>

									<AccordionSection title="Care Conversation" summary={`${selectedMessages.length} message${selectedMessages.length === 1 ? "" : "s"}`}>
										<div className="space-y-3">
											<div className="doctor-thread">
												{selectedMessages.length === 0 ? <p className="doctor-empty-note">no messages yet</p> : null}
												{selectedMessages.map((message) => (
													<div key={message.message_id} className={`chat-card ${message.sender_role === "doctor" ? "chat-card-user" : "chat-card-assistant doctor-chat-patient"}`}>
														<p className="doctor-chat-sender">{message.sender_role === "doctor" ? "You" : displayPatientName(selectedRelationship.patient_name)}</p>
														<p>{message.content}</p>
													</div>
												))}
											</div>
											<div className="doctor-reply">
												<input className="input" aria-label="Reply to patient" value={selectedDraft} onChange={(event) => setDraftForEpisode(selectedRelationship.care_episode_id, event.target.value)} placeholder="Reply to patient" />
												<AppButton onClick={() => sendMessage(selectedRelationship.care_episode_id)} disabled={selectedBusy || !selectedDraft.trim()}>Send</AppButton>
											</div>
										</div>
									</AccordionSection>

									<AccordionSection title="AI Care Summary">
										{selectedSummary ? (
											<dl className="doctor-facts">
												<div><dt>Conversation</dt><dd>{selectedSummary.conversation_digest}</dd></div>
												<div><dt>Plan</dt><dd>{selectedSummary.plan_digest}</dd></div>
											</dl>
										) : <p className="doctor-empty-note">no summary yet</p>}
										<AppButton variant="secondary" className="mt-4" onClick={() => summarize(selectedRelationship.care_episode_id)} disabled={selectedBusy}>{selectedSummary ? "Regenerate summary" : "Generate summary"}</AppButton>
									</AccordionSection>

									<AccordionSection title="Live Movement Monitoring">
										<div className="doctor-monitor">
											<Link className="btn-secondary" href={`/episodes/${selectedRelationship.care_episode_id}/professional-care/live-monitor`}>Open live monitor</Link>
										</div>
									</AccordionSection>
								</div>
							) : null}
						</section>
					)}
				</div>
			</div>
		</div>
	);
}
