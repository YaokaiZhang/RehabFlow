import { clearAuth, type AuthState } from "./auth";
import { fetchWithTimeout, getApiBase } from "./runtime";

type AuthResponse = {
	access_token: string;
	token_type: "bearer";
	user: {
		user_id: string;
		role: "patient" | "doctor";
		username: string;
	};
};

function formatApiError(detail: unknown, fallback: string): string {
	if (typeof detail === "string") return detail;
	if (Array.isArray(detail)) {
		const messages = detail
			.map((item) => {
				if (typeof item === "string") return item;
				if (item && typeof item === "object") {
					const record = item as { loc?: Array<string | number>; msg?: string };
					const field = record.loc?.filter((part) => part !== "body").join(".");
					return [field, record.msg].filter(Boolean).join(": " );
				}
				return "";
			})
			.filter(Boolean);
		if (messages.length) return messages.join("; " );
	}
	return fallback;
}

async function requestUrl<T>(url: string, init?: RequestInit, timeoutMs: number | null = 30000): Promise<T> {
	const response = await fetchWithTimeout(url, {
		...init,
		headers: {
			"Content-Type": "application/json",
			...(init?.headers || {}),
		},
	}, timeoutMs);

	if (!response.ok) {
		let detail = "Request failed (" + response.status + ")";
		try {
			const data = (await response.json()) as { detail?: unknown };
			detail = formatApiError(data.detail, detail);
		} catch {
			// ignore parse errors
		}
		if (response.status === 401 && /invalid or expired token|missing bearer token/i.test(detail)) {
			clearAuth();
			detail = "Your session expired. Please sign in again.";
		}
		throw new Error(detail);
	}

	return (await response.json()) as T;
}

async function request<T>(path: string, init?: RequestInit, timeoutMs: number | null = 30000): Promise<T> {
	return requestUrl<T>(getApiBase() + path, init, timeoutMs);
}

async function requestNoContent(path: string, init?: RequestInit): Promise<void> {
	const response = await fetchWithTimeout(getApiBase() + path, {
		...init,
		headers: {
			"Content-Type": "application/json",
			...(init?.headers || {}),
		},
	});

	if (!response.ok) {
		let detail = "Request failed (" + response.status + ")";
		try {
			const data = (await response.json()) as { detail?: unknown };
			detail = formatApiError(data.detail, detail);
		} catch {
			// ignore parse errors
		}
		if (response.status === 401 && /invalid or expired token|missing bearer token/i.test(detail)) {
			clearAuth();
			detail = "Your session expired. Please sign in again.";
		}
		throw new Error(detail);
	}
}



type StreamTicketResponse = {
  ticket?: unknown;
};

function parseStreamTicket(data: StreamTicketResponse): string {
  if (typeof data.ticket !== "string" || !data.ticket.trim()) {
    throw new Error("Stream ticket response was invalid.");
  }
  return data.ticket;
}

async function issueStreamTicket(path: string, accessToken: string): Promise<string> {
  if (!accessToken.trim()) {
    throw new Error("Authentication is required to issue a stream ticket.");
  }
  const data = await request<StreamTicketResponse>(path, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${accessToken}`,
    },
  });
  return parseStreamTicket(data);
}

export async function issuePatientStreamTicket(accessToken: string): Promise<string> {
  return issueStreamTicket("/stream-tickets/patient-rehab", accessToken);
}

export async function issueDoctorMonitorTicket(careEpisodeId: string, accessToken: string): Promise<string> {
  if (!careEpisodeId.trim()) {
    throw new Error("Care Episode is required to issue a doctor monitor ticket.");
  }
  return issueStreamTicket(
    `/stream-tickets/doctor-monitor/${encodeURIComponent(careEpisodeId)}`,
    accessToken,
  );
}

export type AiChatTurnInput = {
	message: string;
	care_episode_id?: string;
	idempotency_key: string;
};

export type AiChatTurnResponse = {
	event: "response" | "error";
	session_id?: string | null;
	response?: string;
	timestamp?: string;
	routing_decision?: string;
	needs_more_info?: boolean;
	safety_passed?: boolean | null;
	detail?: string;
	debug_trace?: Array<Record<string, unknown>>;
	retrieved_docs?: Array<Record<string, unknown>>;
	tool_calling_used?: boolean | null;
	tool_calling_error?: string;
	originating_event_id?: string | null;
	sources?: Array<{
		label: string;
		title: string;
		url: string;
	}>;
};

export async function sendAiChatTurn(
	sessionId: string,
	input: AiChatTurnInput,
	accessToken: string,
): Promise<AiChatTurnResponse> {
	if (!accessToken.trim()) {
		throw new Error("Authentication is required to send an AI chat turn.");
	}

	const encodedSessionId = encodeURIComponent(sessionId || "new");
	const url = typeof window !== "undefined"
		? "/ai-chat/" + encodedSessionId
		: getApiBase() + "/ai/chat/" + encodedSessionId;
	const payload = {
		message: input.message,
		care_episode_id: input.care_episode_id,
		idempotency_key: input.idempotency_key,
	};

	return requestUrl<AiChatTurnResponse>(url, {
		method: "POST",
		headers: {
			Authorization: `Bearer ${accessToken}`,
		},
		body: JSON.stringify(payload),
	}, null);
}

export async function registerPatient(input: {
	patient_name: string;
	password: string;
	subscription_tier?: string;
	real_info?: Record<string, unknown>;
}): Promise<AuthState> {
	const data = await request<AuthResponse>("/auth/register/patient", {
		method: "POST",
		body: JSON.stringify({
			patient_name: input.patient_name,
			password: input.password,
			subscription_tier: input.subscription_tier || "self_serve",
			real_info: input.real_info || {},
		}),
	});

	return {
		access_token: data.access_token,
		role: data.user.role,
		user_id: data.user.user_id,
		username: data.user.username,
	};
}

export async function registerDoctor(input: {
	doctor_name: string;
	password: string;
	verification_status?: string;
	real_info?: Record<string, unknown>;
}): Promise<AuthState> {
	const data = await request<AuthResponse>("/auth/register/doctor", {
		method: "POST",
		body: JSON.stringify({
			doctor_name: input.doctor_name,
			password: input.password,
			verification_status: input.verification_status || "pending",
			real_info: input.real_info || {},
		}),
	});

	return {
		access_token: data.access_token,
		role: data.user.role,
		user_id: data.user.user_id,
		username: data.user.username,
	};
}

export async function login(input: {
	role: "patient" | "doctor";
	username: string;
	password: string;
}): Promise<AuthState> {
	const data = await request<AuthResponse>("/auth/login", {
		method: "POST",
		body: JSON.stringify(input),
	});

	return {
		access_token: data.access_token,
		role: data.user.role,
		user_id: data.user.user_id,
		username: data.user.username,
	};
}

export async function linkPatient(patientId: string, token: string): Promise<void> {
	await request("/bindings/link", {
		method: "POST",
		headers: {
			Authorization: `Bearer ${token}`,
		},
		body: JSON.stringify({ patient_id: patientId, status: "active" }),
	});
}

export type DemoVideo = {
	exercise_id: string;
	slug: string;
	title: string;
	source_url: string;
	video_url: string;
	local_video_path?: string | null;
	public_video_url?: string | null;
	demo_profile: string;
	relevance_rank: number;
	relevance_score: number;
	relevance_notes: string;
	download_status: string;
	pose_status: string;
	metadata: Record<string, unknown>;
	pose_summary?: {
		frame_count: number;
		detected_frame_count: number;
		fps: number;
		duration_seconds: number;
		metrics: Record<string, unknown>;
	} | null;
};

export type DemoPoseFrame = {
	frame_index: number;
	timestamp_seconds?: number | null;
	detected: boolean;
	landmarks: Array<{ name: string; x: number; y: number; z: number; visibility?: number }>;
	metrics: Record<string, unknown>;
};

export type DemoPoseExtraction = {
	extraction_id: string;
	exercise_id: string;
	source_video_path: string;
	frame_count: number;
	detected_frame_count: number;
	fps: number;
	duration_seconds: number;
	detector_name: string;
	detector_version: string;
	metrics: Record<string, unknown>;
	pose_data: {
		sample_fps?: number;
		sample_every_frames?: number;
		landmark_names?: string[];
		frames?: DemoPoseFrame[];
	};
};

export async function listAclKneeStiffnessVideos(): Promise<{
	profile: string;
	title: string;
	description: string;
	videos: DemoVideo[];
}> {
	return request("/demo/acl-knee-stiffness/videos");
}

export async function getAclPoseExtraction(exerciseId: string): Promise<{
	video: DemoVideo;
	extraction: DemoPoseExtraction;
}> {
	return request(`/demo/acl-knee-stiffness/poses/${exerciseId}`);
}



export type TriageSummaryArtifact = {
	triage_summary_id: string;
	care_episode_id: string;
	patient_id?: string;
	version: number;
	concern: string;
	relevant_context: string;
	safety_signals: string[];
	limitations: string[];
	recommendation: string;
	missing_information: string[];
	unresolved_questions: string[];
	clinician_review_needed: boolean;
	additional_context?: string;
	source_ai_session_id?: string | null;
	source_conversation_transcript?: string;
	created_at?: string | null;
};

export type TriageSummaryDraft = {
	triage_summary_draft_id: string;
	care_episode_id: string;
	patient_id?: string;
	saved: false;
	concern: string;
	relevant_context: string;
	safety_signals: string[];
	limitations: string[];
	recommendation: string;
	missing_information: string[];
	unresolved_questions: string[];
	clinician_review_needed: boolean;
	additional_context?: string;
	source_ai_session_id?: string | null;
	source_conversation_transcript?: string;
	created_at?: string | null;
};

export type CareEpisode = {
	care_episode_id: string;
	patient_id: string;
	issue_title: string;
	body_area: string;
	goal: string;
	short_description: string;
	symptom_started_on?: string | null;
	origin: "manual" | "triage";
	safety_gate_status: "needs_triage" | "triage_complete" | "clinician_reviewed";
	status: string;
	selected_doctor_id?: string | null;
	latest_triage_summary?: TriageSummaryArtifact | null;
	created_at: string;
	updated_at: string;
};

export type CreateCareEpisodeInput = {
	issue_title: string;
	body_area: string;
	goal: string;
	short_description: string;
	symptom_started_on?: string | null;
};

function authHeaders(token: string): Record<string, string> {
	return { Authorization: `Bearer ${token}` };
}

export type MemoryDocument = {
	document_id: string;
	scope: string;
	patient_id: string;
	care_episode_id?: string | null;
	compiled_text: string;
	status: string;
	last_compiled_at?: string | null;
	editable_fields: Record<string, unknown>;
	summary_text: string;
	level1_keywords: string[];
	level1_description: string;
	level1_session_ids: string[];
	level2_summary: string;
	last_memory_error: string;
	created_at: string;
	updated_at: string;
};

export type MemoryDocumentFieldInput = {
	field_name: string;
	value: string;
};

export async function getPatientMemoryDocument(token: string): Promise<MemoryDocument> {
	return request<MemoryDocument>("/memory/patient-document", {
		headers: authHeaders(token),
	});
}

export async function patchPatientMemoryFields(fields: Record<string, unknown>, token: string): Promise<MemoryDocument> {
	return request<MemoryDocument>("/memory/patient-document/fields", {
		method: "PATCH",
		headers: authHeaders(token),
		body: JSON.stringify({ fields }),
	});
}

export async function replacePatientMemoryFields(fields: Record<string, unknown>, token: string): Promise<MemoryDocument> {
	return request<MemoryDocument>("/memory/patient-document/fields", {
		method: "PUT",
		headers: authHeaders(token),
		body: JSON.stringify({ fields }),
	});
}

export async function deletePatientMemoryField(fieldName: string, token: string): Promise<MemoryDocument> {
	return request<MemoryDocument>(`/memory/patient-document/fields/${encodeURIComponent(fieldName)}`, {
		method: "DELETE",
		headers: authHeaders(token),
	});
}

export type CareAppointment = {
	appointment_id: string;
	relationship_id: string;
	care_episode_id?: string | null;
	patient_id: string;
	doctor_id: string;
	scheduled_start: string;
	scheduled_end?: string | null;
	status: string;
	purpose: string;
	created_at: string;
	updated_at: string;
};

export type AttentionMapBucket = {
	bucket: string;
	label: string;
	items: Array<Record<string, unknown>>;
};

export type AttentionMap = {
	summary: string;
	buckets: AttentionMapBucket[];
};

export type DoctorIntelligenceArtifact = {
	artifact_id: string;
	doctor_id: string;
	artifact_type: string;
	title: string;
	content: string;
	sections: Array<Record<string, unknown>>;
	attention_map: Record<string, unknown>;
	input_refs: Record<string, unknown>;
	visibility: string;
	version: number;
	status: string;
	created_at: string;
	updated_at: string;
};

export type DoctorDashboardRelationship = {
	relationship_id: string;
	care_episode_id: string;
	patient_id: string;
	patient_name?: string | null;
	doctor_id: string;
	status: string;
	issue_title?: string | null;
	body_area?: string | null;
	goal?: string | null;
	episode_status?: string | null;
	safety_gate_status?: string | null;
	latest_triage_summary?: TriageSummaryArtifact | Record<string, unknown> | null;
	memory_context?: string | null;
	appointment_signal?: Record<string, unknown> | null;
	started_at?: string | null;
};

export type DoctorDashboard = {
	doctor_id: string;
	care_worklist_href: string;
	patient_panel_briefing: DoctorIntelligenceArtifact;
	attention_map: AttentionMap;
	upcoming_appointments: CareAppointment[];
	care_relationships: DoctorDashboardRelationship[];
};

export type DoctorProfessionalProfile = {
	doctor_id: string;
	display_name: string;
	verification_status: string;
	specialty: string | null;
	expertise_tags: string[];
};

export type DoctorProfessionalProfileUpdateInput = {
	specialty?: string | null;
	expertise_tags?: string[];
};

export type DoctorIntelligenceArtifactInput = {
	artifact_type: string;
	title: string;
	content: string;
	sections?: Array<Record<string, unknown>>;
	attention_map?: Record<string, unknown>;
	input_refs?: Record<string, unknown>;
};

export async function getDoctorDashboard(token: string): Promise<DoctorDashboard> {
	return request<DoctorDashboard>("/doctor/dashboard", {
		headers: authHeaders(token),
	});
}

export async function refreshPatientPanelBriefing(token: string): Promise<DoctorIntelligenceArtifact> {
	return request<DoctorIntelligenceArtifact>("/doctor/dashboard/briefing/refresh", {
		method: "POST",
		headers: authHeaders(token),
	});
}

export async function createDoctorIntelligenceArtifact(
	input: DoctorIntelligenceArtifactInput,
	token: string
): Promise<DoctorIntelligenceArtifact> {
	return request<DoctorIntelligenceArtifact>("/doctor/dashboard/artifacts", {
		method: "POST",
		headers: authHeaders(token),
		body: JSON.stringify({
			artifact_type: input.artifact_type,
			title: input.title,
			content: input.content,
			sections: input.sections || [],
			attention_map: input.attention_map || {},
			input_refs: input.input_refs || {},
		}),
	});
}

export async function getDoctorProfessionalProfile(token: string): Promise<DoctorProfessionalProfile> {
	return request<DoctorProfessionalProfile>("/doctor/profile", {
		headers: authHeaders(token),
	});
}

export async function updateDoctorProfessionalProfile(
	input: DoctorProfessionalProfileUpdateInput,
	token: string
): Promise<DoctorProfessionalProfile> {
	return request<DoctorProfessionalProfile>("/doctor/profile", {
		method: "PATCH",
		headers: authHeaders(token),
		body: JSON.stringify(input),
	});
}

export async function listCareEpisodes(token: string): Promise<CareEpisode[]> {
	const data = await request<{ episodes: CareEpisode[] }>("/care-episodes", {
		headers: authHeaders(token),
	});
	return data.episodes;
}

export async function createCareEpisode(input: CreateCareEpisodeInput, token: string): Promise<CareEpisode> {
	return request<CareEpisode>("/care-episodes", {
		method: "POST",
		headers: authHeaders(token),
		body: JSON.stringify(input),
	});
}

export async function getCareEpisode(careEpisodeId: string, token: string): Promise<CareEpisode> {
	return request<CareEpisode>(`/care-episodes/${encodeURIComponent(careEpisodeId)}`, {
		headers: authHeaders(token),
	});
}

export async function deleteCareEpisode(careEpisodeId: string, token: string): Promise<void> {
	await requestNoContent(`/care-episodes/${encodeURIComponent(careEpisodeId)}`, {
		method: "DELETE",
		headers: authHeaders(token),
	});
}

export type TriageSummaryRequestInput = {
	additional_context?: string;
	ai_session_id?: string | null;
	originating_event_id?: string | null;
};

export async function requestTriageSummary(
	careEpisodeId: string,
	input: TriageSummaryRequestInput,
	token: string
): Promise<TriageSummaryArtifact> {
	return request<TriageSummaryArtifact>(`/care-episodes/${encodeURIComponent(careEpisodeId)}/triage-summary`, {
		method: "POST",
		headers: authHeaders(token),
		body: JSON.stringify({ additional_context: input.additional_context || "", ai_session_id: input.ai_session_id || null, originating_event_id: input.originating_event_id || null }),
	});
}


export type TriageSummaryEpisodeDraft = {
	episode: CareEpisode;
	draft: TriageSummaryDraft;
};

export async function createEpisodeFromTriageSummaryRequest(
	input: TriageSummaryRequestInput,
	token: string
): Promise<TriageSummaryEpisodeDraft> {
	return request<TriageSummaryEpisodeDraft>("/care-episodes/triage-summary-request", {
		method: "POST",
		headers: authHeaders(token),
		body: JSON.stringify({ additional_context: input.additional_context || "", ai_session_id: input.ai_session_id || null, originating_event_id: input.originating_event_id || null }),
	});
}

export async function listTriageSummaries(careEpisodeId: string, token: string): Promise<TriageSummaryArtifact[]> {
	const data = await request<{ summaries: TriageSummaryArtifact[] }>(
		`/care-episodes/${encodeURIComponent(careEpisodeId)}/triage-summaries`,
		{ headers: authHeaders(token) }
	);
	return data.summaries;
}

export async function createTriageSummaryDraft(
	careEpisodeId: string,
	input: TriageSummaryRequestInput,
	token: string
): Promise<TriageSummaryDraft> {
	return request<TriageSummaryDraft>(`/care-episodes/${encodeURIComponent(careEpisodeId)}/triage-summary-drafts`, {
		method: "POST",
		headers: authHeaders(token),
		body: JSON.stringify({ additional_context: input.additional_context || "", ai_session_id: input.ai_session_id || null, originating_event_id: input.originating_event_id || null }),
	});
}

export async function listTriageSummaryDrafts(careEpisodeId: string, token: string): Promise<TriageSummaryDraft[]> {
	const data = await request<{ drafts: TriageSummaryDraft[] }>(
		`/care-episodes/${encodeURIComponent(careEpisodeId)}/triage-summary-drafts`,
		{ headers: authHeaders(token) }
	);
	return data.drafts;
}

export async function selectTriageSummaryDraft(
	careEpisodeId: string,
	triageSummaryDraftId: string,
	token: string
): Promise<TriageSummaryArtifact> {
	return request<TriageSummaryArtifact>(
		`/care-episodes/${encodeURIComponent(careEpisodeId)}/triage-summary-drafts/${encodeURIComponent(triageSummaryDraftId)}/select`,
		{ method: "POST", headers: authHeaders(token) }
	);
}

export async function deleteTriageSummaryDraft(careEpisodeId: string, triageSummaryDraftId: string, token: string): Promise<void> {
	await requestNoContent(
		`/care-episodes/${encodeURIComponent(careEpisodeId)}/triage-summary-drafts/${encodeURIComponent(triageSummaryDraftId)}`,
		{ method: "DELETE", headers: authHeaders(token) }
	);
}


export type DoctorDirectoryEntry = {
	doctor_id: string;
	display_name: string;
	verification_status: string;
	specialty?: string | null;
	expertise_tags: string[];
};

export type DoctorSearchResult = {
	doctor: DoctorDirectoryEntry;
	score: number;
	match_reason: string;
};

export type CareConnectionRequest = {
	request_id: string;
	care_episode_id: string;
	patient_id: string;
	doctor_id: string;
	request_reason: string;
	status: "pending" | "accepted" | "rejected" | string;
	created_at: string;
	updated_at: string;
	responded_at?: string | null;
	doctor_name?: string | null;
	patient_name?: string | null;
	issue_title?: string | null;
	latest_triage_summary?: TriageSummaryArtifact | null;
};

export type CareRelationship = {
	relationship_id: string;
	care_episode_id: string;
	patient_id: string;
	doctor_id: string;
	source_request_id: string;
	status: string;
	created_at: string;
	updated_at: string;
};

export type CareWorklistRelationship = {
	relationship_id: string;
	care_episode_id: string;
	patient_id: string;
	patient_name?: string | null;
	doctor_id: string;
	status: string;
	issue_title?: string | null;
	latest_triage_summary?: TriageSummaryArtifact | null;
	started_at: string;
	last_activity_at: string;
	last_activity_label: string;
};

export type CareWorklist = {
	incoming_requests: CareConnectionRequest[];
	accepted_requests: CareConnectionRequest[];
	active_relationships: CareWorklistRelationship[];
};

export async function subscribeToProfessionalCare(token: string): Promise<{ patient_id: string; subscription_tier: string }> {
	return request("/professional-care/subscribe", {
		method: "POST",
		headers: authHeaders(token),
	});
}

export async function listProfessionalCareDoctors(token: string): Promise<DoctorDirectoryEntry[]> {
	const data = await request<{ doctors: DoctorDirectoryEntry[] }>("/professional-care/doctors", {
		headers: authHeaders(token),
	});
	return data.doctors;
}

export async function searchProfessionalCareDoctors(
	careEpisodeId: string,
	query: string,
	token: string
): Promise<DoctorSearchResult[]> {
	const params = new URLSearchParams();
	if (query.trim()) params.set("query", query.trim());
	const suffix = params.toString() ? `?${params.toString()}` : "";
	const data = await request<{ results: DoctorSearchResult[] }>(
		`/care-episodes/${encodeURIComponent(careEpisodeId)}/doctor-search${suffix}`,
		{ headers: authHeaders(token) }
	);
	return data.results;
}

export async function createCareConnectionRequest(
	careEpisodeId: string,
	input: { doctor_id: string; request_reason?: string },
	token: string
): Promise<CareConnectionRequest> {
	return request<CareConnectionRequest>(`/care-episodes/${encodeURIComponent(careEpisodeId)}/connection-requests`, {
		method: "POST",
		headers: authHeaders(token),
		body: JSON.stringify({ doctor_id: input.doctor_id, request_reason: input.request_reason || "" }),
	});
}

export async function listCareConnectionRequests(careEpisodeId: string, token: string): Promise<CareConnectionRequest[]> {
	const data = await request<{ requests: CareConnectionRequest[] }>(
		`/care-episodes/${encodeURIComponent(careEpisodeId)}/connection-requests`,
		{ headers: authHeaders(token) }
	);
	return data.requests;
}

export async function selectCareRelationshipDoctor(careEpisodeId: string, doctorId: string, token: string): Promise<CareRelationship> {
	return request<CareRelationship>(`/care-episodes/${encodeURIComponent(careEpisodeId)}/care-relationship/select`, {
		method: "POST",
		headers: authHeaders(token),
		body: JSON.stringify({ doctor_id: doctorId }),
	});
}

export async function getCareWorklist(token: string): Promise<CareWorklist> {
	return request<CareWorklist>("/doctor/care-worklist", {
		headers: authHeaders(token),
	});
}

export async function acceptCareConnectionRequest(requestId: string, token: string): Promise<CareConnectionRequest> {
	return request<CareConnectionRequest>(`/doctor/care-requests/${encodeURIComponent(requestId)}/accept`, {
		method: "POST",
		headers: authHeaders(token),
	});
}

export async function rejectCareConnectionRequest(requestId: string, token: string): Promise<CareConnectionRequest> {
	return request<CareConnectionRequest>(`/doctor/care-requests/${encodeURIComponent(requestId)}/reject`, {
		method: "POST",
		headers: authHeaders(token),
	});
}


export type CareConversationMessage = {
	message_id: string;
	care_episode_id: string;
	relationship_id: string;
	sender_id: string;
	sender_role: "patient" | "doctor" | string;
	content: string;
	created_at: string;
};

export type AICareSummary = {
	care_summary_id: string;
	care_episode_id: string;
	relationship_id: string;
	conversation_digest: string;
	plan_digest: string;
	unresolved_questions: string[];
	raw_score_trend: Record<string, unknown> | null;
	created_at: string;
};

export type ExerciseCatalogItem = {
	exercise_id: string;
	slug: string;
	title: string;
	introduction: string;
	structures_involved: string[];
	related_conditions: string[];
	source_url: string;
	video_url: string;
	video_provider: string;
	video_id: string;
	video_available: boolean;
};

export type ExerciseCatalogListResponse = {
	total: number;
	limit: number;
	offset: number;
	exercises: ExerciseCatalogItem[];
};

export type ExerciseCatalogFacets = {
	structures: string[];
	conditions: string[];
};

export type AIDailyRehabRecommendationItem = {
	exercise_id: string;
	title: string;
	reason: string;
	dosage: string;
	structures_involved: string[];
	related_conditions: string[];
	video_url: string;
	video_provider: string;
	source_url: string;
	source: string;
	score: number | null;
};

export type AIDailyRehabRecommendation = {
	recommendation_id: string;
	care_episode_id: string;
	patient_id: string;
	source_triage_summary_id?: string | null;
	source_triage_summary_version: number;
	status: string;
	empty_reason: string;
	query_text: string;
	items: AIDailyRehabRecommendationItem[];
	active: boolean;
	created_at: string;
	updated_at: string;
};

export type AIDailyRehabListItem = {
	exercise_id: string;
	label: string;
	snapshot: ExerciseCatalogItem;
};

export type AIDailyRehabList = {
	list_id: string;
	care_episode_id: string;
	patient_id: string;
	source_recommendation_id?: string | null;
	items: AIDailyRehabListItem[];
	created_at: string;
	updated_at: string;
};

export type EpisodeRehabRecommendedExercise = string | AIDailyRehabListItem;

export type EpisodeRehabChecklistItem = {
	label: string;
	completed: boolean;
	exercise_id?: string | null;
	snapshot?: ExerciseCatalogItem | Record<string, unknown>;
};

export type EpisodeRehabSession = {
	session_id: string;
	care_episode_id: string;
	patient_id: string;
	recommended_exercises: EpisodeRehabRecommendedExercise[];
	checklist: EpisodeRehabChecklistItem[];
	patient_notes: string;
	session_summary: string;
	completion_status: string;
	created_at: string;
	updated_at: string;
};

export async function listCareConversationMessages(careEpisodeId: string, token: string): Promise<CareConversationMessage[]> {
	const data = await request<{ messages: CareConversationMessage[] }>(
		`/care-episodes/${encodeURIComponent(careEpisodeId)}/care-conversation/messages`,
		{ headers: authHeaders(token) }
	);
	return data.messages;
}

export async function createCareConversationMessage(careEpisodeId: string, content: string, token: string): Promise<CareConversationMessage> {
	return request<CareConversationMessage>(`/care-episodes/${encodeURIComponent(careEpisodeId)}/care-conversation/messages`, {
		method: "POST",
		headers: authHeaders(token),
		body: JSON.stringify({ content }),
	});
}

export async function generateAICareSummary(careEpisodeId: string, token: string): Promise<AICareSummary> {
	return request<AICareSummary>(`/care-episodes/${encodeURIComponent(careEpisodeId)}/ai-care-summary`, {
		method: "POST",
		headers: authHeaders(token),
	});
}



export type ProfessionalCareWorkspaceSummary = {
	subscription_tier: string;
	selected_doctor_id?: string | null;
	selected_doctor_name?: string | null;
	has_active_relationship: boolean;
	relationship_id?: string | null;
	request_counts: Record<string, number>;
	show_live_monitor: boolean;
};

export type CareEpisodeWorkspaceSummary = {
	episode: CareEpisode;
	latest_triage_summary?: TriageSummaryArtifact | null;
	unsaved_triage_summaries: TriageSummaryDraft[];
	latest_rehab_session?: EpisodeRehabSession | null;
	latest_ai_care_summary?: AICareSummary | null;
	professional_care: ProfessionalCareWorkspaceSummary;
};

export async function getCareEpisodeWorkspaceSummary(careEpisodeId: string, token: string): Promise<CareEpisodeWorkspaceSummary> {
	return request<CareEpisodeWorkspaceSummary>(`/care-episodes/${encodeURIComponent(careEpisodeId)}/workspace-summary`, {
		headers: authHeaders(token),
	});
}

export type CareEpisodeHistory = {
	episode: CareEpisode;
	triage_summaries: TriageSummaryArtifact[];
	triage_summary_drafts: TriageSummaryDraft[];
	patient_memory?: MemoryDocument | null;
	episode_memory?: MemoryDocument | null;
	compiled_memory_context: string;
	rehab_sessions: EpisodeRehabSession[];
	ai_care_summaries: AICareSummary[];
};

export async function getCareEpisodeHistory(careEpisodeId: string, token: string): Promise<CareEpisodeHistory> {
	return request<CareEpisodeHistory>(`/care-episodes/${encodeURIComponent(careEpisodeId)}/history`, {
		headers: authHeaders(token),
	});
}

export type ExerciseCatalogSearchInput = {
	query?: string;
	structure?: string;
	condition?: string;
	limit?: number;
	offset?: number;
};

function appendOptionalParam(params: URLSearchParams, key: string, value: string | number | undefined): void {
	if (value === undefined || value === "") return;
	params.set(key, String(value));
}

export async function searchExerciseCatalog(
	input: ExerciseCatalogSearchInput,
	token: string
): Promise<ExerciseCatalogListResponse> {
	const params = new URLSearchParams();
	appendOptionalParam(params, "query", input.query);
	appendOptionalParam(params, "structure", input.structure);
	appendOptionalParam(params, "condition", input.condition);
	appendOptionalParam(params, "limit", input.limit);
	appendOptionalParam(params, "offset", input.offset);
	const queryString = params.toString();
	return request<ExerciseCatalogListResponse>(`/exercise-catalog${queryString ? `?${queryString}` : ""}`, {
		headers: authHeaders(token),
	});
}

export async function getExerciseCatalogFacets(token: string): Promise<ExerciseCatalogFacets> {
	return request<ExerciseCatalogFacets>("/exercise-catalog/facets", {
		headers: authHeaders(token),
	});
}

export async function getExerciseCatalogItem(exerciseId: string, token: string): Promise<ExerciseCatalogItem> {
	return request<ExerciseCatalogItem>(`/exercise-catalog/${encodeURIComponent(exerciseId)}`, {
		headers: authHeaders(token),
	});
}

export async function getAIDailyRehabRecommendation(
	careEpisodeId: string,
	token: string
): Promise<AIDailyRehabRecommendation | null> {
	return request<AIDailyRehabRecommendation | null>(
		`/care-episodes/${encodeURIComponent(careEpisodeId)}/rehab-recommendation`,
		{ headers: authHeaders(token) }
	);
}

export async function getAIDailyRehabList(careEpisodeId: string, token: string): Promise<AIDailyRehabList> {
	return request<AIDailyRehabList>(`/care-episodes/${encodeURIComponent(careEpisodeId)}/rehab-list`, {
		headers: authHeaders(token),
	});
}

export async function updateAIDailyRehabList(
	careEpisodeId: string,
	exerciseIds: string[],
	token: string
): Promise<AIDailyRehabList> {
	return request<AIDailyRehabList>(`/care-episodes/${encodeURIComponent(careEpisodeId)}/rehab-list`, {
		method: "PUT",
		headers: authHeaders(token),
		body: JSON.stringify({ exercise_ids: exerciseIds }),
	});
}

export async function createEpisodeRehabSession(
	careEpisodeId: string,
	input: { patient_notes?: string },
	token: string
): Promise<EpisodeRehabSession> {
	return request<EpisodeRehabSession>(`/care-episodes/${encodeURIComponent(careEpisodeId)}/rehab-sessions`, {
		method: "POST",
		headers: authHeaders(token),
		body: JSON.stringify({ patient_notes: input.patient_notes }),
	});
}

export async function listEpisodeRehabSessions(careEpisodeId: string, token: string): Promise<EpisodeRehabSession[]> {
	const data = await request<{ sessions: EpisodeRehabSession[] }>(`/care-episodes/${encodeURIComponent(careEpisodeId)}/rehab-sessions`, {
		headers: authHeaders(token),
	});
	return data.sessions;
}

export async function updateEpisodeRehabSession(
	sessionId: string,
	input: { completed_items: string[]; patient_notes?: string },
	token: string
): Promise<EpisodeRehabSession> {
	return request<EpisodeRehabSession>(`/rehab-sessions/${encodeURIComponent(sessionId)}`, {
		method: "PATCH",
		headers: authHeaders(token),
		body: JSON.stringify(input),
	});
}

export async function summarizeEpisodeRehabSession(sessionId: string, token: string): Promise<EpisodeRehabSession> {
	return request<EpisodeRehabSession>(`/rehab-sessions/${encodeURIComponent(sessionId)}/summary`, {
		method: "POST",
		headers: authHeaders(token),
	});
}
