"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";

import { useAiDailyRehabWorkspace } from "../../../rehab/useAiDailyRehabWorkspace";

type Params = { episode_id?: string };

function statusCopy(status: string) {
    return status
        .split("_")
        .filter(Boolean)
        .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
        .join(" ") || "Not recorded";
}

function safetyCopy(status: string) {
    if (status === "needs_triage") return "Needs AI Triage Intake before AI Daily Rehab";
    if (status === "triage_complete") return "AI triage context available";
    if (status === "clinician_reviewed") return "Clinician-reviewed context available";
    return "Not recorded";
}

function ExerciseMeta({ structures, conditions, limit = 3 }: { structures: string[]; conditions: string[]; limit?: number }) {
    const tags = [...structures, ...conditions].filter(Boolean).slice(0, limit);
    if (!tags.length) return null;
    return (
        <div className="mt-3 flex flex-wrap gap-2 text-xs text-slate-600">
            {tags.map((tag) => <span key={tag} className="rounded-full bg-slate-100 px-2 py-1">{tag}</span>)}
        </div>
    );
}

function FullText({ children }: { children: string }) {
    return <p className="text-sm leading-6 text-slate-600">{children}</p>;
}


type ExerciseDetails = {
    exercise_id: string;
    title: string;
    introduction: string;
    structures_involved: string[];
    related_conditions: string[];
    source_url: string;
};

type RecommendationCardItem = {
    exercise_id: string;
    title: string;
};

function ExerciseInfoDetails({ details, loading = false }: { details?: ExerciseDetails; loading?: boolean }) {
    if (loading) return <p className="text-sm text-slate-500">Loading full description...</p>;
    if (!details) return <p className="text-sm text-slate-600">Full exercise details unavailable.</p>;

    return (
        <>
            <FullText>{details.introduction}</FullText>
            <ExerciseMeta structures={details.structures_involved} conditions={details.related_conditions} />
            {details.source_url ? <a className="btn-secondary mt-4" href={details.source_url} target="_blank" rel="noreferrer">Source</a> : null}
        </>
    );
}

function ExerciseRecommendationCard({
    item,
    details,
    detailLoading,
    saved,
    busy,
    onLoadDetails,
    onAdd,
    onRemove,
}: {
    item: RecommendationCardItem;
    details?: ExerciseDetails;
    detailLoading: boolean;
    saved: boolean;
    busy: boolean;
    onLoadDetails: () => void;
    onAdd: () => void;
    onRemove: () => void;
}) {
    const [infoOpen, setInfoOpen] = useState(false);
    const detailsId = "recommendation-info-" + item.exercise_id;

    return (
        <article className="rounded-md border border-emerald-100 bg-white p-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
                <h3 className="font-semibold text-slate-950">{item.title}</h3>
                <div className="flex flex-wrap gap-2">
                    <button className={saved ? "btn-secondary" : "btn-primary"} onClick={saved ? onRemove : onAdd} disabled={busy}>{saved ? "Remove" : "Add"}</button>
                    <button
                        type="button"
                        className="btn-secondary"
                        aria-expanded={infoOpen}
                        aria-controls={detailsId}
                        aria-label={(infoOpen ? "Hide" : "Show") + " details for " + item.title}
                        onClick={() => {
                            const nextOpen = !infoOpen;
                            setInfoOpen(nextOpen);
                            if (nextOpen) onLoadDetails();
                        }}
                    >Info</button>
                </div>
            </div>
            {infoOpen ? (
                <div id={detailsId} className="mt-4 border-t border-slate-100 pt-3">
                    <ExerciseInfoDetails details={details} loading={detailLoading} />
                </div>
            ) : null}
        </article>
    );
}

function ExerciseCatalogCard({
    exercise,
    saved,
    busy,
    onAdd,
    onRemove,
}: {
    exercise: ExerciseDetails;
    saved: boolean;
    busy: boolean;
    onAdd: () => void;
    onRemove: () => void;
}) {
    const [infoOpen, setInfoOpen] = useState(false);
    const detailsId = "exercise-info-" + exercise.exercise_id;

    return (
        <article className="rounded-md border border-slate-200 bg-white p-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
                <h3 className="font-semibold text-slate-950">{exercise.title}</h3>
                <div className="flex flex-wrap gap-2">
                    <button className={saved ? "btn-secondary" : "btn-primary"} onClick={saved ? onRemove : onAdd} disabled={busy}>{saved ? "Remove" : "Add"}</button>
                    <button
                        type="button"
                        className="btn-secondary"
                        aria-expanded={infoOpen}
                        aria-controls={detailsId}
                        aria-label={(infoOpen ? "Hide" : "Show") + " details for " + exercise.title}
                        onClick={() => setInfoOpen((current) => !current)}
                    >Info</button>
                </div>
            </div>
            {infoOpen ? (
                <div id={detailsId} className="mt-4 border-t border-slate-100 pt-3">
                    <ExerciseInfoDetails details={exercise} />
                </div>
            ) : null}
        </article>
    );
}

export default function EpisodeRehabPage() {
    const params = useParams<Params>();
    const episodeId = params?.episode_id || "";
    const {
        episode,
        recommendation,
        catalogExercises,
        exerciseDetails,
        detailLoadingIds,
        searchQuery,
        setSearchQuery,
        error,
        statusMessage,
        loading,
        catalogLoading,
        busy,
        blocked,
        savedListItems,
        savedExerciseIds,
        clinicianReviewNeeded,
        addExercise,
        removeExercise,
        loadExerciseDetails,
        createSession,
    } = useAiDailyRehabWorkspace(episodeId);

    if (loading) return <p className="text-sm text-slate-500">Loading AI Daily Rehab...</p>;
    if (error && !episode) return <section className="card max-w-2xl text-sm text-rose-700">{error}</section>;
    if (!episode) return null;

    return (
        <div className="space-y-5">
            <section className="card max-w-4xl">
                <p className="text-xs font-semibold uppercase tracking-wide text-amber-700">AI Daily Rehab</p>
                <h1 className="mt-2 text-2xl font-semibold text-slate-950">{episode.issue_title}</h1>
                {blocked ? (
                    <>
                        <p className="mt-2 text-sm leading-6 text-slate-600">This Care Episode has not cleared the Rehab Safety Gate. Complete AI Triage Intake or get clinician-reviewed context before AI-assisted self-rehab starts.</p>
                        <div className="mt-4 flex flex-wrap gap-2"><Link className="btn-primary" href={"/episodes/" + episode.care_episode_id + "/triage"}>Start AI Triage Intake</Link><Link className="btn-secondary" href={"/episodes/" + episode.care_episode_id}>Episode Overview</Link></div>
                    </>
                ) : (
                    <div className="mt-3 space-y-2 text-sm leading-6 text-slate-600">
                        <p>Safety context is available. Build a simple daily list from AI triage recommendations and the exercise catalog, then launch today&apos;s guided video session.</p>
                        <p className="rounded-md border border-emerald-200 bg-emerald-50 p-3 text-emerald-800">Safety gate: {safetyCopy(episode.safety_gate_status)}</p>
                    </div>
                )}
            </section>

            {!blocked ? (
                <>
                    <section className="card max-w-4xl border-emerald-200 bg-emerald-50">
                        <h2 className="section-title">Recommended from AI Triage</h2>
                        {clinicianReviewNeeded ? <p className="mt-3 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">Clinician review is recommended for this episode. Keep today&apos;s list conservative.</p> : null}
                        {recommendation ? (
                            <>
                                <p className="mt-3 text-sm text-slate-700">Recommendation status: <span className="font-semibold text-slate-950">{statusCopy(recommendation.status)}</span>{recommendation.empty_reason ? " - " + recommendation.empty_reason : ""}</p>
                                {recommendation.items.length ? (
                                    <div className="mt-4 grid gap-3 md:grid-cols-2">
                                        {recommendation.items.map((item) => {
                                            const saved = savedExerciseIds.has(item.exercise_id);
                                            return (
                                                <ExerciseRecommendationCard
                                                    key={item.exercise_id}
                                                    item={item}
                                                    details={exerciseDetails[item.exercise_id]}
                                                    detailLoading={Boolean(detailLoadingIds[item.exercise_id])}
                                                    saved={saved}
                                                    busy={busy}
                                                    onLoadDetails={() => loadExerciseDetails(item.exercise_id)}
                                                    onAdd={() => addExercise(item.exercise_id)}
                                                    onRemove={() => removeExercise(item.exercise_id)}
                                                />
                                            );
                                        })}
                                    </div>
                                ) : <p className="mt-3 text-sm text-slate-600">{recommendation.empty_reason || "No recommendation items are active yet."}</p>}
                            </>
                        ) : <p className="mt-3 text-sm text-slate-600">No AI triage recommendation is active yet.</p>}
                    </section>

                    <section className="card max-w-4xl">
                        <div className="flex flex-wrap items-center justify-between gap-3">
                            <h2 className="section-title">AI Daily Rehab List</h2>
                            <button className="btn-primary" onClick={createSession} disabled={busy || !savedListItems.length}>{busy ? "Saving" : "Start Session"}</button>
                        </div>
                        {savedListItems.length ? (
                            <div className="mt-4 space-y-2">
                                {savedListItems.map((item) => (
                                    <div key={item.exercise_id} className="flex items-start justify-between gap-3 rounded-md border border-slate-200 bg-white p-3">
                                        <label className="flex min-w-0 items-start gap-3 text-sm text-slate-700">
                                            <input className="mt-1" type="checkbox" checked readOnly />
                                            <span className="block font-semibold text-slate-950">{item.label}</span>
                                        </label>
                                        <button className="btn-secondary shrink-0" onClick={() => removeExercise(item.exercise_id)} disabled={busy}>Remove</button>
                                    </div>
                                ))}
                            </div>
                        ) : <p className="mt-3 text-sm text-slate-600">No saved exercises yet. Add exercises from recommendations or the catalog.</p>}
                        {error ? <p className="mt-3 text-sm text-rose-600">{error}</p> : null}
                        {statusMessage ? <p className="mt-3 text-sm text-emerald-700">{statusMessage}</p> : null}
                    </section>

                    <section className="card max-w-4xl">
                        <h2 className="section-title">Exercise Catalog</h2>
                        <label className="mt-4 block max-w-2xl">
                            <span className="field-label">Search exercises</span>
                            <input className="input" value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} placeholder="Search by goal, symptom, movement, or name" />
                        </label>
                        {catalogLoading ? <p className="mt-4 text-sm text-slate-500">Loading catalog...</p> : null}
                        <div className="mt-4 grid gap-3 md:grid-cols-2">
                            {catalogExercises.map((exercise) => {
                                const saved = savedExerciseIds.has(exercise.exercise_id);
                                return (
                                    <ExerciseCatalogCard
                                        key={exercise.exercise_id}
                                        exercise={exercise}
                                        saved={saved}
                                        busy={busy}
                                        onAdd={() => addExercise(exercise.exercise_id)}
                                        onRemove={() => removeExercise(exercise.exercise_id)}
                                    />
                                );
                            })}
                        </div>
                        {!catalogLoading && !catalogExercises.length ? <p className="mt-4 text-sm text-slate-600">No catalog exercises match the current search.</p> : null}
                    </section>
                </>
            ) : null}
        </div>
    );
}
