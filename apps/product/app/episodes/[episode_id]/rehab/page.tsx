"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";

import { useAiDailyRehabWorkspace } from "../../../rehab/useAiDailyRehabWorkspace";
import { ArrowIcon } from "../../../../components/CareEpisodeBrief";
import { StatusBadge } from "../../../../components/ui";

type Params = { episode_id?: string };

function statusCopy(status: string) {
    const words = status.split("_").filter(Boolean).join(" ");
    return words ? words.charAt(0).toUpperCase() + words.slice(1) : "Not recorded";
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
        <div className="ep-exercise-tags">
            {tags.map((tag) => <span key={tag}>{tag}</span>)}
        </div>
    );
}

function FullText({ children }: { children: string }) {
    return <p>{children}</p>;
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
    if (loading) return <p>Loading full description...</p>;
    if (!details) return <p>Full exercise details unavailable.</p>;

    return (
        <>
            <FullText>{details.introduction}</FullText>
            <ExerciseMeta structures={details.structures_involved} conditions={details.related_conditions} />
            {details.source_url ? <a className="ep-exercise-source" href={details.source_url} target="_blank" rel="noreferrer">Source</a> : null}
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
        <article className="ep-exercise ep-exercise-suggested">
            <div className="ep-exercise-head">
                <h3>{item.title}</h3>
                <div className="ep-exercise-actions">
                    <button className={saved ? "btn ep-outline-on" : "btn ep-outline"} onClick={saved ? onRemove : onAdd} disabled={busy}>{saved ? "Remove" : "Add"}</button>
                    <button
                        type="button"
                        className="btn bg-white text-ink hover:bg-fill"
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
                <div id={detailsId} className="ep-exercise-info">
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
        <article className="ep-exercise">
            <div className="ep-exercise-head">
                <h3>{exercise.title}</h3>
                <div className="ep-exercise-actions">
                    <button className={saved ? "btn ep-outline-on" : "btn ep-outline"} onClick={saved ? onRemove : onAdd} disabled={busy}>{saved ? "Remove" : "Add"}</button>
                    <button
                        type="button"
                        className="btn bg-white text-ink hover:bg-fill"
                        aria-expanded={infoOpen}
                        aria-controls={detailsId}
                        aria-label={(infoOpen ? "Hide" : "Show") + " details for " + exercise.title}
                        onClick={() => setInfoOpen((current) => !current)}
                    >Info</button>
                </div>
            </div>
            {infoOpen ? (
                <div id={detailsId} className="ep-exercise-info">
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
        addExercise,
        removeExercise,
        loadExerciseDetails,
        createSession,
    } = useAiDailyRehabWorkspace(episodeId);

    if (loading) return <p className="ep-loading">Loading AI Daily Rehab...</p>;
    if (error && !episode) return <section className="page-column-narrow"><p className="notice notice-danger">{error}</p></section>;
    if (!episode) return null;

    return (
        <div className="page-column ep-page">
            <header className="page-header" data-tone="mint">
                <p className="page-header-kicker">AI Daily Rehab</p>
                <h1>{episode.issue_title}</h1>
                {!blocked ? <div className="ep-header-meta"><StatusBadge tone="success">{safetyCopy(episode.safety_gate_status)}</StatusBadge></div> : null}
            </header>

            {blocked ? (
                <section className="sticker tone-peach ep-gate" aria-labelledby="ep-gate-title">
                    <span className="tag"><span className="session-dot" />Rehab Safety Gate</span>
                    <h2 id="ep-gate-title">triage comes first</h2>
                    <p>This Care Episode has not cleared the Rehab Safety Gate. Daily rehab opens once you finish AI Triage Intake and save the summary, or a clinician reviews this episode.</p>
                    <div className="ep-gate-actions">
                        <Link className="btn-primary" href={"/episodes/" + episode.care_episode_id + "/triage"}>Start AI Triage Intake<ArrowIcon /></Link>
                        <Link className="btn-secondary bg-white/70" href={"/episodes/" + episode.care_episode_id}>Episode overview</Link>
                    </div>
                </section>
            ) : (
                <div className="ep-rehab-grid">
                    <div className="ep-rehab-main">
                        <section className="card" aria-labelledby="ep-rec-title">
                            <div className="ep-section-head">
                                <h2 id="ep-rec-title" className="section-title">Recommended from AI Triage</h2>
                                {recommendation ? <StatusBadge tone="neutral">{statusCopy(recommendation.status)}</StatusBadge> : null}
                            </div>
                            {recommendation ? (
                                <>
                                    {recommendation.empty_reason ? <p className="mt-2 text-sm text-[var(--rf-ink-muted)]">{recommendation.empty_reason}</p> : null}
                                    {recommendation.items.length ? (
                                        <div className="ep-exercise-grid">
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
                                    ) : <p className="ep-empty mt-3">{recommendation.empty_reason ? "nothing suggested yet" : "No recommendation items are active yet."}</p>}
                                </>
                            ) : <p className="ep-empty mt-3">no suggestions yet</p>}
                        </section>

                        <section className="card" aria-labelledby="ep-catalog-title">
                            <h2 id="ep-catalog-title" className="section-title">Exercise catalog</h2>
                            <label className="ep-search">
                                <span className="sr-only">Search exercises</span>
                                <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><circle cx="7" cy="7" r="5" stroke="currentColor" strokeWidth="1.6" /><path d="M11 11l3 3" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" /></svg>
                                <input className="input" value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} placeholder="Search by goal, symptom, movement, or name" />
                            </label>
                            {catalogLoading ? <p className="ep-loading mt-4">Loading catalog...</p> : null}
                            <div className="ep-exercise-grid">
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
                            {!catalogLoading && !catalogExercises.length ? <p className="ep-empty mt-4">no exercises match that search</p> : null}
                        </section>
                    </div>

                    <section className="sticker tone-mint ep-today" aria-labelledby="ep-today-title">
                        <div className="ep-section-head">
                            <h2 id="ep-today-title">today&apos;s list</h2>
                            <span className="tag">{savedListItems.length ? `${savedListItems.length} exercise${savedListItems.length === 1 ? "" : "s"} ready` : "Add at least one exercise to start"}</span>
                        </div>
                        {savedListItems.length ? (
                            <ul className="ep-today-list">
                                {savedListItems.map((item) => (
                                    <li key={item.exercise_id} className="ep-today-item">
                                        <label>
                                            <input type="checkbox" checked readOnly />
                                            <span>{item.label}</span>
                                        </label>
                                        <button className="btn ep-today-remove text-danger hover:bg-[var(--rf-danger-tint)]" onClick={() => removeExercise(item.exercise_id)} disabled={busy}>Remove</button>
                                    </li>
                                ))}
                            </ul>
                        ) : <p className="scribble text-[var(--rf-mint-deep)]">nothing on today&apos;s list yet</p>}
                        {error ? <p className="notice notice-danger">{error}</p> : null}
                        {statusMessage ? <p className="notice bg-white/70" role="status">{statusMessage}</p> : null}
                        <button className="btn-primary" onClick={createSession} disabled={busy || !savedListItems.length}>{busy ? "Saving" : "Start session"}{busy ? null : <ArrowIcon />}</button>
                    </section>
                </div>
            )}
        </div>
    );
}
