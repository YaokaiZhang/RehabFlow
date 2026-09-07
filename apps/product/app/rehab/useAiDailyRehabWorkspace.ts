
"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
    createEpisodeRehabSession,
    getAIDailyRehabList,
    getAIDailyRehabRecommendation,
    getCareEpisode,
    getExerciseCatalogItem,
    searchExerciseCatalog,
    updateAIDailyRehabList,
    type AIDailyRehabList,
    type AIDailyRehabRecommendation,
    type CareEpisode,
    type ExerciseCatalogItem,
} from "@rehab/shared/api";
import { loadAuth, type AuthState } from "@rehab/shared/auth";

function isSafetyGateCleared(status: string) {
    return status === "triage_complete" || status === "clinician_reviewed";
}

export function useAiDailyRehabWorkspace(episodeId: string) {
    const router = useRouter();
    const [auth, setAuth] = useState<AuthState | null>(null);
    const [episode, setEpisode] = useState<CareEpisode | null>(null);
    const [recommendation, setRecommendation] = useState<AIDailyRehabRecommendation | null>(null);
    const [rehabList, setRehabList] = useState<AIDailyRehabList | null>(null);
    const [catalogExercises, setCatalogExercises] = useState<ExerciseCatalogItem[]>([]);
    const [exerciseDetails, setExerciseDetails] = useState<Record<string, ExerciseCatalogItem>>({});
    const [detailLoadingIds, setDetailLoadingIds] = useState<Record<string, boolean>>({});
    const [searchQuery, setSearchQuery] = useState("");
    const [error, setError] = useState("");
    const [statusMessage, setStatusMessage] = useState("");
    const [loading, setLoading] = useState(true);
    const [catalogLoading, setCatalogLoading] = useState(false);
    const [busy, setBusy] = useState(false);
    const loadRehabRequestRef = useRef(0);
    const catalogRequestRef = useRef(0);

    const blocked = episode ? !isSafetyGateCleared(episode.safety_gate_status) : false;
    const savedListItems = useMemo(() => rehabList?.items || [], [rehabList]);
    const savedExerciseIds = useMemo(() => new Set(savedListItems.map((item) => item.exercise_id)), [savedListItems]);
    const clinicianReviewNeeded = Boolean(episode?.latest_triage_summary?.clinician_review_needed);

    const loadCatalog = useCallback(async (nextAuth: AuthState) => {
        const requestId = catalogRequestRef.current += 1;
        setCatalogLoading(true);
        try {
            const response = await searchExerciseCatalog({
                query: searchQuery,
                limit: 12,
            }, nextAuth.access_token);
            if (requestId !== catalogRequestRef.current) return;
            setCatalogExercises(response.exercises);
        } catch (err) {
            if (requestId !== catalogRequestRef.current) return;
            setError((err as Error).message);
        } finally {
            if (requestId === catalogRequestRef.current) setCatalogLoading(false);
        }
    }, [searchQuery]);

    const loadRehab = useCallback(async (nextAuth: AuthState) => {
        const requestId = loadRehabRequestRef.current += 1;
        setLoading(true);
        setError("");
        try {
            const nextEpisode = await getCareEpisode(episodeId, nextAuth.access_token);
            if (requestId !== loadRehabRequestRef.current) return;
            setEpisode(nextEpisode);
            setRecommendation(null);
            setRehabList(null);
            setExerciseDetails({});
            setDetailLoadingIds({});
            if (isSafetyGateCleared(nextEpisode.safety_gate_status)) {
                const [nextRecommendation, nextList] = await Promise.all([
                    getAIDailyRehabRecommendation(episodeId, nextAuth.access_token),
                    getAIDailyRehabList(episodeId, nextAuth.access_token),
                ]);
                if (requestId !== loadRehabRequestRef.current) return;
                setRecommendation(nextRecommendation);
                setRehabList(nextList);
            }
        } catch (err) {
            if (requestId !== loadRehabRequestRef.current) return;
            setError((err as Error).message);
        } finally {
            if (requestId === loadRehabRequestRef.current) setLoading(false);
        }
    }, [episodeId]);

    useEffect(() => {
        const nextAuth = loadAuth();
        setAuth(nextAuth);
        if (!nextAuth || nextAuth.role !== "patient") {
            setError("Login as a patient to use AI Daily Rehab.");
            setLoading(false);
            return;
        }
        void loadRehab(nextAuth);
    }, [loadRehab]);

    useEffect(() => {
        if (!auth || !episode || blocked) return;
        const timer = window.setTimeout(() => {
            void loadCatalog(auth);
        }, 250);
        return () => window.clearTimeout(timer);
    }, [auth, episode, blocked, loadCatalog]);

    const loadExerciseDetails = async (exerciseId: string) => {
        if (!auth || exerciseDetails[exerciseId] || detailLoadingIds[exerciseId]) return;

        const catalogMatch = catalogExercises.find((exercise) => exercise.exercise_id === exerciseId);
        if (catalogMatch) {
            setExerciseDetails((current) => ({ ...current, [exerciseId]: catalogMatch }));
            return;
        }

        setDetailLoadingIds((current) => ({ ...current, [exerciseId]: true }));
        try {
            const detail = await getExerciseCatalogItem(exerciseId, auth.access_token);
            setExerciseDetails((current) => ({ ...current, [exerciseId]: detail }));
        } catch (err) {
            setError((err as Error).message);
        } finally {
            setDetailLoadingIds((current) => {
                const next = { ...current };
                delete next[exerciseId];
                return next;
            });
        }
    };

    const updateSavedList = async (exerciseIds: string[]) => {
        if (!auth || !episode || busy) return;
        setBusy(true);
        setError("");
        setStatusMessage("");
        try {
            const nextList = await updateAIDailyRehabList(episode.care_episode_id, exerciseIds, auth.access_token);
            setRehabList(nextList);
            setStatusMessage("AI Daily Rehab List updated.");
        } catch (err) {
            setError((err as Error).message);
        } finally {
            setBusy(false);
        }
    };

    const addExercise = async (exerciseId: string) => {
        if (savedExerciseIds.has(exerciseId)) return;
        await updateSavedList([...savedExerciseIds, exerciseId]);
    };

    const removeExercise = async (exerciseId: string) => {
        await updateSavedList([...savedExerciseIds].filter((item) => item !== exerciseId));
    };

    const createSession = async () => {
        if (!auth || !episode || busy || !savedListItems.length) return;
        setBusy(true);
        setError("");
        setStatusMessage("");
        try {
            const session = await createEpisodeRehabSession(episode.care_episode_id, { patient_notes: "" }, auth.access_token);
            const exerciseQuery = `exercise_ids=${encodeURIComponent(savedListItems.map((item) => item.exercise_id).join(","))}`;
            router.push(`/rehab/session?episode_id=${encodeURIComponent(episode.care_episode_id)}&session_id=${encodeURIComponent(session.session_id)}&${exerciseQuery}`);
        } catch (err) {
            setError((err as Error).message);
        } finally {
            setBusy(false);
        }
    };

    return {
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
    };
}
