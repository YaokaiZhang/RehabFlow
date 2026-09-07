"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
    createCareConnectionRequest,
    createCareConversationMessage,
    generateAICareSummary,
    getCareEpisode,
    listCareConnectionRequests,
    listCareConversationMessages,
    listProfessionalCareDoctors,
    searchProfessionalCareDoctors,
    selectCareRelationshipDoctor,
    subscribeToProfessionalCare,
    type AICareSummary,
    type CareConnectionRequest,
    type CareConversationMessage,
    type CareEpisode,
    type DoctorDirectoryEntry,
    type DoctorSearchResult,
} from "@rehab/shared/api";
import { loadAuth, type AuthState } from "@rehab/shared/auth";

export function usePatientProfessionalCareWorkspace(episodeId: string) {
    const [auth, setAuth] = useState<AuthState | null>(null);
    const [episode, setEpisode] = useState<CareEpisode | null>(null);
    const [doctors, setDoctors] = useState<DoctorDirectoryEntry[]>([]);
    const [requests, setRequests] = useState<CareConnectionRequest[]>([]);
    const [messages, setMessages] = useState<CareConversationMessage[]>([]);
    const [careSummary, setCareSummary] = useState<AICareSummary | null>(null);
    const [requestReasonByDoctor, setRequestReasonByDoctor] = useState<Record<string, string>>({});
    const [doctorSearchQuery, setDoctorSearchQuery] = useState("");
    const [doctorSearchResults, setDoctorSearchResults] = useState<DoctorSearchResult[]>([]);
    const [messageText, setMessageText] = useState("");
    const [error, setError] = useState("");
    const [statusMessage, setStatusMessage] = useState("");
    const [loading, setLoading] = useState(true);
    const [busy, setBusy] = useState(false);
    const [isSubscriber, setIsSubscriber] = useState(false);
    const loadRequestIdRef = useRef(0);
    const doctorSearchRequestIdRef = useRef(0);
    const doctorSearchQueryRef = useRef("");

    const acceptedRequests = useMemo(() => requests.filter((request) => request.status === "accepted"), [requests]);
    const hasActiveRelationship = Boolean(episode?.selected_doctor_id);

    const updateDoctorSearchQuery = useCallback((query: string) => {
        doctorSearchQueryRef.current = query;
        setDoctorSearchQuery(query);
        setDoctorSearchResults([]);
    }, []);

    const loadWorkspace = useCallback(async (nextAuth: AuthState) => {
        const requestId = loadRequestIdRef.current + 1;
        loadRequestIdRef.current = requestId;
        setLoading(true);
        setError("");
        try {
            const nextEpisode = await getCareEpisode(episodeId, nextAuth.access_token);
            if (loadRequestIdRef.current !== requestId) return;
            setEpisode(nextEpisode);
            const searchQuery = "";
            const searchRequestId = doctorSearchRequestIdRef.current + 1;
            doctorSearchRequestIdRef.current = searchRequestId;
            const [nextDoctors, nextRequests, nextDoctorSearchResults] = await Promise.all([
                listProfessionalCareDoctors(nextAuth.access_token),
                listCareConnectionRequests(episodeId, nextAuth.access_token),
                searchProfessionalCareDoctors(nextEpisode.care_episode_id, searchQuery, nextAuth.access_token),
            ]);
            const nextMessages = nextEpisode.selected_doctor_id
                ? await listCareConversationMessages(episodeId, nextAuth.access_token)
                : [];
            if (loadRequestIdRef.current !== requestId) return;
            setDoctors(nextDoctors);
            setRequests(nextRequests);
            setIsSubscriber(true);
            setMessages(nextMessages);
            if (doctorSearchRequestIdRef.current !== searchRequestId || doctorSearchQueryRef.current !== searchQuery) return;
            setDoctorSearchResults(nextDoctorSearchResults);
        } catch (err) {
            if (loadRequestIdRef.current !== requestId) return;
            const message = (err as Error).message;
            if (message.includes("Professional Care Subscription required")) {
                setIsSubscriber(false);
            } else {
                setError(message);
            }
        } finally {
            if (loadRequestIdRef.current !== requestId) return;
            setLoading(false);
        }
    }, [episodeId]);

    useEffect(() => {
        const nextAuth = loadAuth();
        setAuth(nextAuth);
        if (!nextAuth || nextAuth.role !== "patient") {
            setError("Login as a patient to open Professional Care.");
            setLoading(false);
            return;
        }
        void loadWorkspace(nextAuth);
    }, [episodeId, loadWorkspace]);

    const subscribe = async () => {
        if (!auth || busy) return;
        setBusy(true);
        setError("");
        try {
            await subscribeToProfessionalCare(auth.access_token);
            setStatusMessage("Professional Care Subscription is active.");
            await loadWorkspace(auth);
        } catch (err) {
            setError((err as Error).message);
        } finally {
            setBusy(false);
        }
    };

    const searchDoctors = async (query = doctorSearchQuery) => {
        if (!auth || !episode || busy) return;
        const searchQuery = query;
        const searchRequestId = doctorSearchRequestIdRef.current + 1;
        doctorSearchRequestIdRef.current = searchRequestId;
        doctorSearchQueryRef.current = searchQuery;
        setBusy(true);
        setError("");
        setStatusMessage("");
        try {
            const nextDoctorSearchResults = await searchProfessionalCareDoctors(episode.care_episode_id, searchQuery, auth.access_token);
            if (doctorSearchRequestIdRef.current !== searchRequestId || doctorSearchQueryRef.current !== searchQuery) return;
            setDoctorSearchResults(nextDoctorSearchResults);
        } catch (err) {
            if (doctorSearchRequestIdRef.current !== searchRequestId || doctorSearchQueryRef.current !== searchQuery) return;
            setError((err as Error).message);
        } finally {
            if (doctorSearchRequestIdRef.current !== searchRequestId) return;
            setBusy(false);
        }
    };

    const sendRequest = async (doctorId: string) => {
        if (!auth || !episode || busy) return;
        setBusy(true);
        setError("");
        setStatusMessage("");
        try {
            const request = await createCareConnectionRequest(
                episode.care_episode_id,
                { doctor_id: doctorId, request_reason: requestReasonByDoctor[doctorId] || "" },
                auth.access_token
            );
            setRequests((prev) => [request, ...prev]);
            setRequestReasonByDoctor((prev) => ({ ...prev, [doctorId]: "" }));
            setStatusMessage("Care Connection Request sent.");
        } catch (err) {
            setError((err as Error).message);
        } finally {
            setBusy(false);
        }
    };

    const selectDoctor = async (doctorId: string) => {
        if (!auth || !episode || busy) return;
        setBusy(true);
        setError("");
        try {
            await selectCareRelationshipDoctor(episode.care_episode_id, doctorId, auth.access_token);
            setEpisode({ ...episode, selected_doctor_id: doctorId });
            setMessages(await listCareConversationMessages(episode.care_episode_id, auth.access_token));
            setStatusMessage("Active Care Relationship created.");
        } catch (err) {
            setError((err as Error).message);
        } finally {
            setBusy(false);
        }
    };

    const sendMessage = async () => {
        if (!auth || !episode || !messageText.trim() || busy) return;
        const content = messageText.trim();
        const clientMessageId = `optimistic-${Date.now()}`;
        const optimisticMessage: CareConversationMessage & { client_message_id: string; optimistic: boolean } = {
            message_id: clientMessageId,
            care_episode_id: episode.care_episode_id,
            relationship_id: `pending-${episode.care_episode_id}`,
            sender_id: auth.user_id,
            sender_role: "patient",
            content,
            created_at: new Date().toISOString(),
            client_message_id: clientMessageId,
            optimistic: true,
        };
        setBusy(true);
        setError("");
        setMessages((prev) => [...prev, optimisticMessage]);
        setMessageText("");
        try {
            const message = await createCareConversationMessage(episode.care_episode_id, content, auth.access_token);
            setMessages((prev) => prev.map((item) => item.message_id === optimisticMessage.message_id ? message : item));
        } catch (err) {
            setMessages((prev) => prev.filter((item) => item.message_id !== optimisticMessage.message_id));
            setMessageText((current) => current === "" ? content : current);
            setError((err as Error).message);
        } finally {
            setBusy(false);
        }
    };

    const generateSummary = async () => {
        if (!auth || !episode || busy) return;
        setBusy(true);
        setError("");
        try {
            setCareSummary(await generateAICareSummary(episode.care_episode_id, auth.access_token));
            setStatusMessage("AI Care Summary generated.");
        } catch (err) {
            setError((err as Error).message);
        } finally {
            setBusy(false);
        }
    };

    return {
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
        setDoctorSearchQuery: updateDoctorSearchQuery,
        setMessageText,
        subscribe,
        searchDoctors,
        sendRequest,
        selectDoctor,
        sendMessage,
        generateSummary,
    };
}
