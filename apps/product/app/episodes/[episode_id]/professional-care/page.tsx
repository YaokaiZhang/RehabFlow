"use client";

import { useEffect, useState } from "react";

import Link from "next/link";
import { useParams } from "next/navigation";

import { usePatientProfessionalCareWorkspace } from "./usePatientProfessionalCareWorkspace";
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

    if (loading) return <p className="text-sm text-slate-500">Loading Professional Care...</p>;
    if (error && !episode) return <section className="card max-w-2xl text-sm text-rose-700">{error}</section>;
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

    return (
        <div className="space-y-5">
            <section className="card max-w-4xl">
                <p className="text-xs font-semibold uppercase tracking-wide text-emerald-700">Professional Care</p>
                <h1 className="mt-2 text-2xl font-semibold text-slate-950">{episode.issue_title}</h1>
                <p className="mt-2 text-sm leading-6 text-slate-600">Professional Care is scoped to this Care Episode. Subscribe, request doctors, then use Care Conversation and AI Care Summary after one accepted doctor is selected.</p>
                {episode.latest_triage_summary ? <p className="mt-3"><StatusBadge tone="success">A Triage Summary is available for this episode.</StatusBadge></p> : <p className="mt-3"><StatusBadge tone="attention">No AI Triage Summary exists yet. A doctor can still help clarify this Manual Care Episode.</StatusBadge></p>}
                <div className="mt-4 flex flex-wrap gap-2">
                    <Link className="btn-secondary" href={`/episodes/${episode.care_episode_id}`}>Episode Overview</Link>
                    <Link className="btn-secondary" href={`/episodes/${episode.care_episode_id}/triage`}>Triage Summary</Link>
                </div>
            </section>

            {!isSubscriber ? (
                <section className="card max-w-3xl border-emerald-200 bg-emerald-50">
                    <h2 className="section-title">Professional Care Subscription</h2>
                    <p className="mt-2 text-sm leading-6 text-slate-600">The MVP uses a one-click subscription state so doctor requests are tied to a durable patient identity.</p>
                    <AppButton className="mt-4" onClick={subscribe} disabled={busy}>{busy ? "Subscribing" : "Subscribe to Professional Care"}</AppButton>
                </section>
            ) : (
                <>
                    <section className="card max-w-4xl">
                        <h2 className="section-title">Doctor Selection Queue</h2>
                        {error ? <p className="mt-3 text-sm text-rose-600">{error}</p> : null}
                        {statusMessage ? <p className="mt-3 text-sm text-emerald-700">{statusMessage}</p> : null}
                        <div className="mt-4 grid gap-3">
                            <div className="rounded-md border border-slate-200 p-3">
                                <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Selected doctor</p>
                                <p className="mt-1 text-sm font-semibold text-slate-950">{selectedDoctorLabel}</p>
                            </div>
                            <div className="rounded-md border border-slate-200 p-3">
                                <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Accepted doctors</p>
                                {acceptedRequests.length === 0 ? <p className="mt-2 text-sm text-slate-500">No accepted requests yet.</p> : null}
                                {acceptedRequests.map((request) => (
                                    <div key={request.request_id} className="mt-2 flex flex-col gap-2 border-t border-slate-100 pt-2 sm:flex-row sm:items-center sm:justify-between">
                                        <div>
                                            <p className="text-sm font-semibold text-slate-950">{request.doctor_name || "Accepted doctor"}</p>
                                            <p className="text-xs text-slate-500">{episode.selected_doctor_id === request.doctor_id ? "Selected for this episode" : "Accepted your request"}</p>
                                        </div>
                                        <AppButton onClick={() => selectDoctor(request.doctor_id)} disabled={busy || episode.selected_doctor_id === request.doctor_id}>{episode.selected_doctor_id === request.doctor_id ? "Selected" : "Select"}</AppButton>
                                    </div>
                                ))}
                            </div>
                            <div className="rounded-md border border-slate-200 p-3">
                                <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Pending requests</p>
                                {pendingRequests.length ? pendingRequests.map((request) => <p key={request.request_id} className="mt-2 text-sm text-slate-700">{request.doctor_name || "Requested doctor"}</p>) : <p className="mt-2 text-sm text-slate-500">No pending requests.</p>}
                            </div>
                        </div>
                    </section>

                    <section className="card max-w-4xl">
                        <h2 className="section-title">Find another doctor</h2>
                        <div className="mt-3 flex flex-col gap-2 sm:flex-row">
                            <input
                                className="input"
                                value={doctorSearchQuery}
                                onChange={(event) => setDoctorSearchQuery(event.target.value)}
                                placeholder="Search by goal, body area, sport, or expertise"
                            />
                            <AppButton variant="secondary" onClick={() => searchDoctors()} disabled={busy}>Search</AppButton>
                        </div>
                        <div className="mt-3 space-y-2">
                            {availableDoctorRows.length === 0 ? <p className="text-sm text-slate-500">No matching doctors are available for this Care Episode.</p> : null}
                            {visibleDoctorRows.map((result) => {
                                const doctor = result.doctor;
                                return (
                                    <div key={doctor.doctor_id} className="rounded-md border border-slate-200 p-3">
                                        <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
                                            <div className="min-w-0">
                                                <p className="text-sm font-semibold text-slate-950">{doctor.display_name}</p>
                                                <p className="mt-1 text-xs text-slate-500">{doctor.specialty || doctor.verification_status}</p>
                                                <div className="mt-2 flex flex-wrap gap-1">
                                                    {result.doctor.expertise_tags.map((tag) => <span key={tag} className="rounded-md bg-slate-100 px-2 py-1 text-xs text-slate-600">{tag}</span>)}
                                                </div>
                                            </div>
                                            <AppButton onClick={() => sendRequest(doctor.doctor_id)} disabled={busy}>Send Request</AppButton>
                                        </div>
                                        <textarea
                                            className="textarea mt-3 min-h-16"
                                            value={requestReasonByDoctor[doctor.doctor_id] || ""}
                                            onChange={(event) => setRequestReasonByDoctor((prev) => ({ ...prev, [doctor.doctor_id]: event.target.value }))}
                                            placeholder="Optional note for this doctor. Leave blank to use the latest Triage Summary."
                                        />
                                    </div>
                                );
                            })}
                        </div>
                        {availableDoctorRows.length > DOCTORS_PER_PAGE ? (
                            <div className="mt-4 flex items-center justify-between gap-3 border-t border-slate-100 pt-3">
                                <p className="text-xs text-slate-500">Page {currentDoctorPage} of {doctorPageCount}</p>
                                <div className="flex gap-2">
                                    <AppButton
                                        variant="secondary"
                                        onClick={() => setDoctorPage(Math.max(1, currentDoctorPage - 1))}
                                        disabled={currentDoctorPage === 1}
                                    >
                                        Previous
                                    </AppButton>
                                    <AppButton
                                        variant="secondary"
                                        onClick={() => setDoctorPage(Math.min(doctorPageCount, currentDoctorPage + 1))}
                                        disabled={currentDoctorPage === doctorPageCount}
                                    >
                                        Next
                                    </AppButton>
                                </div>
                            </div>
                        ) : null}
                    </section>

                    {hasActiveRelationship ? (
                        <section className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_360px]">
                            <div className="card">
                                <h2 className="section-title">Care Conversation</h2>
                                <div className="mt-3 max-h-80 space-y-2 overflow-auto rounded-md border border-slate-200 bg-slate-50 p-3">
                                    {messages.length === 0 ? <p className="text-sm text-slate-500">No messages yet.</p> : null}
                                    {messages.map((message) => <div key={message.message_id} className="rounded-md border border-slate-200 bg-white p-3 text-sm"><p className="text-xs font-semibold uppercase tracking-wide text-slate-400">{message.sender_role}</p><p className="mt-1 text-slate-700">{message.content}</p></div>)}
                                </div>
                                <div className="mt-3 flex flex-col gap-2 sm:flex-row"><input className="input" value={messageText} onChange={(event) => setMessageText(event.target.value)} placeholder="Message your doctor" /><AppButton onClick={sendMessage} disabled={busy || !messageText.trim()}>Send</AppButton></div>
                            </div>
                            <div className="card border-emerald-200 bg-emerald-50">
                                <h2 className="section-title">AI Care Summary</h2>
                                {careSummary ? <><p className="mt-3 text-sm leading-6 text-slate-700">{careSummary.conversation_digest}</p><p className="mt-3 text-sm font-semibold text-slate-950">Plan</p><p className="mt-1 text-sm leading-6 text-slate-700">{careSummary.plan_digest}</p></> : <p className="mt-2 text-sm text-slate-600">Generate a digest from Care Conversation and care plan guidance. Optional Live Movement Monitoring remains a separate care tool.</p>}
                                <AppButton className="mt-4" onClick={generateSummary} disabled={busy}>Generate Summary</AppButton>
                            </div>
                        </section>
                    ) : null}
                </>
            )}
        </div>
    );
}
