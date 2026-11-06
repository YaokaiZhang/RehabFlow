"use client";

import { useEffect, useState } from "react";

import { useParams } from "next/navigation";

import { usePatientProfessionalCareWorkspace } from "./usePatientProfessionalCareWorkspace";
import { ArrowIcon } from "../../../../components/CareEpisodeBrief";
import { AppButton, StatusBadge } from "../../../../components/ui";

type Params = { episode_id?: string };

const DOCTORS_PER_PAGE = 6;

export default function EpisodeProfessionalCarePage() {
    const params = useParams<Params>();
    const episodeId = params?.episode_id || "";
    const {
        episode,
        doctors,
        requests,
        messages,
        careSummary,
        requestReasonByDoctor,
        doctorSearchQuery,
        doctorSearchResults,
        messageText,
        error,
        statusMessage,
        loading,
        busy,
        isSubscriber,
        acceptedRequests,
        hasActiveRelationship,
        setRequestReasonByDoctor,
        setDoctorSearchQuery,
        setMessageText,
        subscribe,
        searchDoctors,
        sendRequest,
        selectDoctor,
        sendMessage,
        generateSummary,
    } = usePatientProfessionalCareWorkspace(episodeId);
    const [doctorPage, setDoctorPage] = useState(1);

    useEffect(() => {
        setDoctorPage(1);
    }, [doctorSearchQuery, doctorSearchResults]);

    if (loading) return <p className="ep-loading">Loading Professional Care...</p>;
    if (error && !episode) return <section className="page-column-narrow"><p className="notice notice-danger">{error}</p></section>;
    if (!episode) return null;

    const selectedDoctorRequest = requests.find((request) => request.doctor_id === episode.selected_doctor_id);
    const selectedDoctor = doctors.find((doctor) => doctor.doctor_id === episode.selected_doctor_id);
    const selectedDoctorLabel = selectedDoctorRequest?.doctor_name || selectedDoctor?.display_name || (episode.selected_doctor_id ? "Selected doctor" : "No doctor selected yet");
    const pendingRequests = requests.filter((request) => request.status === "pending");
    const requestedDoctorIds = new Set(
        requests
            .filter((request) => ["pending", "accepted"].includes(request.status))
            .map((request) => request.doctor_id)
    );
    if (episode.selected_doctor_id) requestedDoctorIds.add(episode.selected_doctor_id);
    const availableDoctors = doctors.filter((doctor) => !requestedDoctorIds.has(doctor.doctor_id));
    const rankedAvailableDoctorResults = doctorSearchResults.filter((result) => !requestedDoctorIds.has(result.doctor.doctor_id));
    const availableDoctorRows = rankedAvailableDoctorResults.length
        ? rankedAvailableDoctorResults
        : availableDoctors.map((doctor) => ({ doctor, score: 0, match_reason: "" }));
    const doctorPageCount = Math.max(1, Math.ceil(availableDoctorRows.length / DOCTORS_PER_PAGE));
    const currentDoctorPage = Math.min(doctorPage, doctorPageCount);
    const visibleDoctorRows = availableDoctorRows.slice(
        (currentDoctorPage - 1) * DOCTORS_PER_PAGE,
        currentDoctorPage * DOCTORS_PER_PAGE
    );

    const doctorSelection = (
        <>
            <section aria-labelledby="ep-queue-title" className="grid gap-4">
                <h2 id="ep-queue-title" className="section-title">Your doctors</h2>
                <div className="ep-care-grid">
                    <div className="card ep-care-tile" data-tone="sky">
                        <span className="ep-care-tile-label">Accepted</span>
                        {acceptedRequests.length === 0 ? <p className="ep-care-muted">None yet</p> : null}
                        {acceptedRequests.map((request) => {
                            const isSelected = episode.selected_doctor_id === request.doctor_id;
                            return (
                                <div key={request.request_id} className="ep-care-row">
                                    <p className="min-w-0 text-sm font-semibold">{request.doctor_name || "Doctor"}</p>
                                    <AppButton variant="secondary" className={isSelected ? "" : "ep-outline"} onClick={() => selectDoctor(request.doctor_id)} disabled={busy || isSelected}>{isSelected ? "Selected" : "Select"}</AppButton>
                                </div>
                            );
                        })}
                    </div>
                    <div className="card ep-care-tile" data-tone="sky">
                        <span className="ep-care-tile-label">Waiting</span>
                        {pendingRequests.length ? pendingRequests.map((request) => <p key={request.request_id} className="flex items-center gap-2 text-sm font-semibold"><span className="session-dot" />{request.doctor_name || "Doctor"}</p>) : <p className="ep-care-muted">None</p>}
                    </div>
                </div>
            </section>

            <section className="card" aria-labelledby="ep-find-title">
                <h2 id="ep-find-title" className="section-title">Find a doctor</h2>
                <div className="ep-search-row">
                    <label className="ep-search">
                        <span className="sr-only">Search doctors</span>
                        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><circle cx="7" cy="7" r="5" stroke="currentColor" strokeWidth="1.6" /><path d="M11 11l3 3" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" /></svg>
                        <input
                            className="input"
                            value={doctorSearchQuery}
                            onChange={(event) => setDoctorSearchQuery(event.target.value)}
                            onKeyDown={(event) => { if (event.key === "Enter") searchDoctors(); }}
                            placeholder="Body area, sport or expertise"
                        />
                    </label>
                    <AppButton variant="secondary" onClick={() => searchDoctors()} disabled={busy}>Search</AppButton>
                </div>
                {availableDoctorRows.length === 0 ? <p className="ep-empty mt-4">no matching doctors</p> : null}
                <div className="ep-doctor-list">
                    {visibleDoctorRows.map((result) => {
                        const doctor = result.doctor;
                        return (
                            <article key={doctor.doctor_id} className="ep-doctor">
                                <div className="ep-doctor-head">
                                    <div className="min-w-0">
                                        <p className="ep-doctor-name">{doctor.display_name}</p>
                                        <p className="ep-doctor-specialty">{doctor.specialty || (doctor.verification_status === "verified" ? "Verified" : doctor.verification_status.replace(/_/g, " "))}</p>
                                    </div>
                                    <AppButton variant="secondary" className="ep-outline" onClick={() => sendRequest(doctor.doctor_id)} disabled={busy}>Send request</AppButton>
                                </div>
                                {result.doctor.expertise_tags.length ? (
                                    <div className="ep-doctor-tags">
                                        {result.doctor.expertise_tags.map((tag) => <span key={tag}>{tag}</span>)}
                                    </div>
                                ) : null}
                                <textarea
                                    className="textarea"
                                    aria-label={`Note for ${doctor.display_name}`}
                                    value={requestReasonByDoctor[doctor.doctor_id] || ""}
                                    onChange={(event) => setRequestReasonByDoctor((prev) => ({ ...prev, [doctor.doctor_id]: event.target.value }))}
                                    placeholder="Note (optional)"
                                />
                            </article>
                        );
                    })}
                </div>
                {availableDoctorRows.length > DOCTORS_PER_PAGE ? (
                    <div className="ep-pager">
                        <p className="ep-count">Page {currentDoctorPage} of {doctorPageCount}</p>
                        <div className="flex gap-2">
                            <AppButton variant="secondary" onClick={() => setDoctorPage(Math.max(1, currentDoctorPage - 1))} disabled={currentDoctorPage === 1}>Previous</AppButton>
                            <AppButton variant="secondary" onClick={() => setDoctorPage(Math.min(doctorPageCount, currentDoctorPage + 1))} disabled={currentDoctorPage === doctorPageCount}>Next</AppButton>
                        </div>
                    </div>
                ) : null}
            </section>
        </>
    );

    return (
        <div className="page-column ep-page">
            <header className="page-header" data-tone="sky">
                <p className="page-header-kicker">Professional Care</p>
                <h1>{episode.issue_title}</h1>
                <div className="ep-header-meta">{episode.latest_triage_summary ? <StatusBadge tone="success">Triage summary shared</StatusBadge> : <StatusBadge tone="attention">No triage summary yet</StatusBadge>}</div>
            </header>

            {error ? <p className="notice notice-danger">{error}</p> : null}
            {statusMessage ? <p className="notice" role="status">{statusMessage}</p> : null}

            {!isSubscriber ? (
                <section className="sticker tone-sky ep-gate" aria-labelledby="ep-subscribe-title">
                    <h2 id="ep-subscribe-title">bring in a doctor</h2>
                    <div className="ep-gate-actions">
                        <AppButton onClick={subscribe} disabled={busy}>{busy ? "Subscribing" : "Subscribe"}{busy ? null : <ArrowIcon />}</AppButton>
                    </div>
                </section>
            ) : hasActiveRelationship ? (
                <>
                    <section className="ep-convo-grid">
                        <section className="card" aria-labelledby="ep-convo-title">
                            <h2 id="ep-convo-title" className="section-title">{selectedDoctorLabel}</h2>
                            <div className="ep-convo">
                                {messages.length === 0 ? <p className="ep-empty">no messages yet</p> : null}
                                {messages.map((message) => (
                                    <div key={message.message_id} className={`chat-card ${message.sender_role === "patient" ? "chat-card-user" : "chat-card-assistant"} text-sm`}>
                                        <span className="ep-convo-who">{message.sender_role === "patient" ? "You" : message.sender_role === "doctor" ? selectedDoctorLabel : message.sender_role}</span>
                                        {message.content}
                                    </div>
                                ))}
                            </div>
                            <form
                                className="ep-composer"
                                onSubmit={(event) => {
                                    event.preventDefault();
                                    if (!busy && messageText.trim()) sendMessage();
                                }}
                            >
                                <input className="input" aria-label="Message your doctor" value={messageText} onChange={(event) => setMessageText(event.target.value)} placeholder="Message" />
                                <AppButton type="submit" disabled={busy || !messageText.trim()}>Send</AppButton>
                            </form>
                        </section>
                        <div className="sticker tone-sky ep-care-summary grid gap-3">
                            <span className="tag"><span className="session-dot" />AI Care Summary</span>
                            {careSummary ? <><p className="text-sm leading-6">{careSummary.conversation_digest}</p><p className="text-sm font-semibold">Plan</p><p className="text-sm leading-6">{careSummary.plan_digest}</p></> : null}
                            <AppButton variant="secondary" className="justify-self-start bg-white/70" onClick={generateSummary} disabled={busy}>{careSummary ? "Refresh summary" : "Generate summary"}</AppButton>
                        </div>
                    </section>

                    <details className="ep-disclosure ep-care-more">
                        <summary>Other doctors</summary>
                        <div className="grid gap-6">{doctorSelection}</div>
                    </details>
                </>
            ) : (
                doctorSelection
            )}
        </div>
    );
}
