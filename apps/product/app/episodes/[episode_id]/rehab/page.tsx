"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";

import { useAiDailyRehabWorkspace } from "../../../rehab/useAiDailyRehabWorkspace";
import { ArrowIcon } from "../../../../components/CareEpisodeBrief";
import { StatusBadge } from "../../../../components/ui";
import { safetyStatus } from "../../../../components/safetyStatus";

type Params = { episode_id?: string };


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

    const safety = safetyStatus(episode.safety_gate_status);
    const suggestions = recommendation?.items || [];

    return (
        <div className="page-column ep-page">
            <header className="page-header" data-tone="mint">
                <p className="page-header-kicker">AI Daily Rehab</p>
                <h1>{episode.issue_title}</h1>
                {!blocked ? <div className="ep-header-meta"><StatusBadge tone={safety.tone}>{safety.label}</StatusBadge></div> : null}
            </header>

            {blocked ? (
                <section className="sticker tone-peach ep-gate" aria-labelledby="ep-gate-title">
                    <h2 id="ep-gate-title">triage comes first</h2>
                    <div className="ep-gate-actions">
                        <Link className="btn-primary" href={"/?episode_id=" + encodeURIComponent(episode.care_episode_id)}>Start triage<ArrowIcon /></Link>
                    </div>
                </section>
            ) : (
                <div className="ep-rehab-grid">
                    <section className="sticker tone-mint ep-today" aria-labelledby="ep-today-title">
                        <div className="ep-section-head">
                            <h2 id="ep-today-title">today&apos;s list</h2>
                            {savedListItems.length ? <span className="tag">{savedListItems.length} ready</span> : null}
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
                        ) : <p className="ep-today-empty">Nothing added yet</p>}
                        {error ? <p className="notice notice-danger">{error}</p> : null}
                        {statusMessage ? <p className="notice bg-white/70" role="status">{statusMessage}</p> : null}
                        <button className="btn-primary" onClick={createSession} disabled={busy || !savedListItems.length}>{busy ? "Saving" : "Start session"}{busy ? null : <ArrowIcon />}</button>
                    </section>

                    <div className="ep-rehab-main">
                        <section className="card" aria-labelledby="ep-rec-title">
                            <h2 id="ep-rec-title" className="section-title">Suggested for you</h2>
                            {suggestions.length ? (
                                <div className="ep-exercise-grid">
                                    {suggestions.map((item) => (
                                        <ExerciseRecommendationCard
                                            key={item.exercise_id}
                                            item={item}
                                            details={exerciseDetails[item.exercise_id]}
                                            detailLoading={Boolean(detailLoadingIds[item.exercise_id])}
                                            saved={savedExerciseIds.has(item.exercise_id)}
                                            busy={busy}
                                            onLoadDetails={() => loadExerciseDetails(item.exercise_id)}
                                            onAdd={() => addExercise(item.exercise_id)}
                                            onRemove={() => removeExercise(item.exercise_id)}
                                        />
                                    ))}
                                </div>
                            ) : <p className="ep-empty mt-3">no suggestions yet</p>}
                        </section>

                        <details className="card ep-catalog" open={!suggestions.length || undefined}>
                            <summary className="ep-catalog-summary">
                                <span className="section-title">Add more</span>
                            </summary>
                            <label className="ep-search">
                                <span className="sr-only">Search exercises</span>
                                <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><circle cx="7" cy="7" r="5" stroke="currentColor" strokeWidth="1.6" /><path d="M11 11l3 3" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" /></svg>
                                <input className="input" value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} placeholder="Search exercises" />
                            </label>
                            {catalogLoading ? <p className="ep-loading mt-4">Loading catalog...</p> : null}
                            <div className="ep-exercise-grid">
                                {catalogExercises.map((exercise) => (
                                    <ExerciseCatalogCard
                                        key={exercise.exercise_id}
                                        exercise={exercise}
                                        saved={savedExerciseIds.has(exercise.exercise_id)}
                                        busy={busy}
                                        onAdd={() => addExercise(exercise.exercise_id)}
                                        onRemove={() => removeExercise(exercise.exercise_id)}
                                    />
                                ))}
                            </div>
                            {!catalogLoading && !catalogExercises.length ? <p className="ep-empty mt-4">no matches</p> : null}
                        </details>
                    </div>
                </div>
            )}
        </div>
    );
}
