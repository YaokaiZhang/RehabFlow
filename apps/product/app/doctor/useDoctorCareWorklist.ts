"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import {
	acceptCareConnectionRequest,
	createCareConversationMessage,
	generateAICareSummary,
	getCareEpisodeWorkspaceSummary,
	getCareWorklist,
	listCareConversationMessages,
	rejectCareConnectionRequest,
	type AICareSummary,
	type CareConnectionRequest,
	type CareConversationMessage,
	type CareEpisodeWorkspaceSummary,
	type CareWorklist,
	type CareWorklistRelationship,
} from "@rehab/shared/api";
import { loadAuth, type AuthState } from "@rehab/shared/auth";

const EMPTY_WORKLIST: CareWorklist = { incoming_requests: [], accepted_requests: [], active_relationships: [] };

export function useDoctorCareWorklist() {
	const [auth, setAuth] = useState<AuthState | null>(null);
	const [worklist, setWorklist] = useState<CareWorklist>(EMPTY_WORKLIST);
	const [workspaceByEpisode, setWorkspaceByEpisode] = useState<Record<string, CareEpisodeWorkspaceSummary>>({});
	const [messagesByEpisode, setMessagesByEpisode] = useState<Record<string, CareConversationMessage[]>>({});
	const [summaryByEpisode, setSummaryByEpisode] = useState<Record<string, AICareSummary>>({});
	const [draftByEpisode, setDraftByEpisode] = useState<Record<string, string>>({});
	const [selectedRelationshipId, setSelectedRelationshipId] = useState("");
	const [errorMessage, setErrorMessage] = useState("");
	const [statusMessage, setStatusMessage] = useState("");
	const [loading, setLoading] = useState(true);
	const [busyRequestId, setBusyRequestId] = useState("");
	const [busyEpisodeId, setBusyEpisodeId] = useState("");

	const selectedRelationship = useMemo(
		() => worklist.active_relationships.find((relationship) => relationship.relationship_id === selectedRelationshipId) || null,
		[selectedRelationshipId, worklist.active_relationships]
	);

	const loadConversation = useCallback(async (careEpisodeId: string, token: string) => {
		const messages = await listCareConversationMessages(careEpisodeId, token);
		setMessagesByEpisode((prev) => ({ ...prev, [careEpisodeId]: messages }));
	}, []);

	const loadWorkspaceSummaries = useCallback(async (relationships: CareWorklistRelationship[], token: string) => {
		const entries = await Promise.all(
			relationships.map(async (relationship) => {
				const workspace = await getCareEpisodeWorkspaceSummary(relationship.care_episode_id, token);
				return [relationship.care_episode_id, workspace] as const;
			})
		);
		setWorkspaceByEpisode((prev) => {
			const next = { ...prev };
			for (const [careEpisodeId, workspace] of entries) next[careEpisodeId] = workspace;
			return next;
		});
		setSummaryByEpisode((prev) => {
			const next = { ...prev };
			for (const [careEpisodeId, workspace] of entries) {
				if (workspace.latest_ai_care_summary) next[careEpisodeId] = workspace.latest_ai_care_summary;
			}
			return next;
		});
	}, []);

	const loadWorklist = useCallback(async (nextAuth: AuthState) => {
		setLoading(true);
		try {
			const nextWorklist = await getCareWorklist(nextAuth.access_token);
			setWorklist(nextWorklist);
			setSelectedRelationshipId((current) => {
				if (nextWorklist.active_relationships.some((relationship) => relationship.relationship_id === current)) return current;
				return nextWorklist.active_relationships[0]?.relationship_id || "";
			});
			await Promise.all([
				Promise.all(nextWorklist.active_relationships.map((relationship) => loadConversation(relationship.care_episode_id, nextAuth.access_token))),
				loadWorkspaceSummaries(nextWorklist.active_relationships, nextAuth.access_token),
			]);
		} catch (err) {
			setErrorMessage((err as Error).message);
		} finally {
			setLoading(false);
		}
	}, [loadConversation, loadWorkspaceSummaries]);

	useEffect(() => {
		const nextAuth = loadAuth();
		setAuth(nextAuth);
		if (!nextAuth || nextAuth.role !== "doctor") {
			setLoading(false);
			return;
		}
		void loadWorklist(nextAuth);
	}, [loadWorklist]);

	const respond = async (request: CareConnectionRequest, action: "accept" | "reject") => {
		if (!auth || busyRequestId) return;
		setBusyRequestId(request.request_id);
		setErrorMessage("");
		setStatusMessage("");
		try {
			if (action === "accept") {
				await acceptCareConnectionRequest(request.request_id, auth.access_token);
				setStatusMessage("Care Connection Request accepted. The patient can now select you for this episode.");
			} else {
				await rejectCareConnectionRequest(request.request_id, auth.access_token);
				setStatusMessage("Care Connection Request rejected.");
			}
			await loadWorklist(auth);
		} catch (err) {
			setErrorMessage((err as Error).message);
		} finally {
			setBusyRequestId("");
		}
	};

	const sendMessage = async (careEpisodeId: string) => {
		if (!auth || busyEpisodeId) return;
		const content = (draftByEpisode[careEpisodeId] || "").trim();
		if (!content) return;
		setBusyEpisodeId(careEpisodeId);
		setErrorMessage("");
		try {
			const message = await createCareConversationMessage(careEpisodeId, content, auth.access_token);
			setMessagesByEpisode((prev) => ({ ...prev, [careEpisodeId]: [...(prev[careEpisodeId] || []), message] }));
			setDraftByEpisode((prev) => ({
				...prev,
				[careEpisodeId]: prev[careEpisodeId] === content ? "" : prev[careEpisodeId],
			}));
		} catch (err) {
			setDraftByEpisode((prev) => ({
				...prev,
				[careEpisodeId]: (prev[careEpisodeId] || "") === "" ? content : prev[careEpisodeId],
			}));
			setErrorMessage((err as Error).message);
		} finally {
			setBusyEpisodeId("");
		}
	};

	const summarize = async (careEpisodeId: string) => {
		if (!auth || busyEpisodeId) return;
		setBusyEpisodeId(careEpisodeId);
		setErrorMessage("");
		try {
			const summary = await generateAICareSummary(careEpisodeId, auth.access_token);
			setSummaryByEpisode((prev) => ({ ...prev, [careEpisodeId]: summary }));
			setStatusMessage("AI Care Summary generated for the selected episode.");
		} catch (err) {
			setErrorMessage((err as Error).message);
		} finally {
			setBusyEpisodeId("");
		}
	};

	const setDraftForEpisode = (careEpisodeId: string, value: string) => {
		setDraftByEpisode((prev) => ({ ...prev, [careEpisodeId]: value }));
	};

	return {
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
	};
}
