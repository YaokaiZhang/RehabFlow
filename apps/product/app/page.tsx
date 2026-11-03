"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useRef, useState } from "react";

import { loadAuth } from "@rehab/shared/auth";
import { createEpisodeFromTriageSummaryRequest, createTriageSummaryDraft, getAiChatSession, getCareEpisode, streamAiChatTurn, type AiChatProgress } from "@rehab/shared/api";

type ChatMessage = {
	id: string;
	role: "assistant" | "user" | "system";
	text: string;
	sources?: Array<{ label: string; title: string; url: string }>;
};

type AiEvent = {
	event?: string;
	response?: string;
	session_id?: string | null;
	originating_event_id?: string | null;
	needs_more_info?: boolean;
	routing_decision?: string;
	safety_passed?: boolean | null;
	detail?: string;
	sources?: Array<{ label: string; title: string; url: string }>;
};

type RouteState = {
	routingDecision: string;
	needsMoreInfo: boolean | null;
	safetyPassed: boolean | null;
};

type PendingTriageSummary = {
	sessionId: string;
	episodeId: string;
	messages: ChatMessage[];
	prompt: string;
	routeState: RouteState;
	request: { ai_session_id: string | null; additional_context: string; originating_event_id?: string | null };
};

type PendingAiChatSubmission = {
	message: string;
	careEpisodeId: string;
	idempotencyKey: string;
};

type AgentProgressStep = {
	label: string;
	detail: string;
};

const PENDING_TRIAGE_SUMMARY_KEY = "rehab_pending_triage_summary";

const suggestedPrompts = [
	"My shoulder feels stiff after rotator cuff rehab. What should I do today?",
	"My lower back flared up after yesterday exercise. Should I rest or move gently?",
	"I rolled my ankle last week and still have swelling. Can I exercise today?",
	"I am recovering from surgery and feel new pain during stairs. Should I contact my clinician?",
];

// Stages reported live by the backend while a turn runs; the label comes from the server.
const agentProgressDetails: Record<string, string> = {
	reading: "Reviewing your message and screening for red flags.",
	planning: "Deciding which guidance and exercises to look up.",
	searching_catalog: "Finding exercises that match your situation.",
	searching_web: "Looking up current clinical guidance for your question.",
	searching_history: "Checking what you discussed in earlier sessions.",
	drafting: "Putting together a reply for you.",
	reviewing: "Running the safety and accuracy review before showing it.",
	revising: "Adjusting the reply after the review.",
};
const initialAgentProgress: AgentProgressStep = {
	label: "Reading your message",
	detail: agentProgressDetails.reading,
};
const summaryRequestFallback = "Patient requested a Triage Summary from the AI intake chat.";

function makeId() {
	return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

// routing_decision values from backend/app/ai/router.py, in patient language.
function prettyRoute(route: string) {
	if (!route) return "Not assessed yet";
	if (route.includes("urgent")) return "Seek care now";
	if (route === "ask_clarification") return "Needs more detail";
	if (route === "unsupported") return "Outside RehabFlow's scope";
	return "Assessment in progress";
}

function agentProgressForStage(progress: AiChatProgress): AgentProgressStep {
	return { label: progress.label, detail: agentProgressDetails[progress.stage] || "" };
}

function friendlyError(detail?: string) {
	if (!detail) return "The triage service could not complete that turn. Please try again.";
	if (detail.includes("Storage folder") && detail.includes("Qdrant")) {
		return "The rehab knowledge service is busy. Please retry in a moment, or ask your clinician if symptoms are changing quickly.";
	}
	if (detail.includes("local variable") || detail.includes("Traceback")) {
		return "The triage service hit an internal issue. Please try again or start a new session.";
	}
	return detail;
}

function routeTone(routeState: RouteState) {
	if (routeState.safetyPassed === false || routeState.routingDecision.includes("urgent")) return "rose";
	if (routeState.needsMoreInfo) return "amber";
	if (routeState.routingDecision) return "emerald";
	return "slate";
}

function buildLoginReturnPath(episodeId: string) {
	const params = new URLSearchParams({ resume_triage: "summary" });
	if (episodeId) params.set("episode_id", episodeId);
	return `/?${params.toString()}`;
}

function readPendingTriageSummary() {
	try {
		const raw = sessionStorage.getItem(PENDING_TRIAGE_SUMMARY_KEY);
		if (!raw) return null;
		const pending = JSON.parse(raw) as PendingTriageSummary;
		if (!Array.isArray(pending.messages) || typeof pending.request?.additional_context !== "string") return null;
		return pending;
	} catch {
		return null;
	}
}

function clearPendingTriageSummary() {
	sessionStorage.removeItem(PENDING_TRIAGE_SUMMARY_KEY);
}

function HomePageContent() {
	const router = useRouter();
	const searchParams = useSearchParams();
	const urlSessionId = searchParams.get("session_id") || "";
	const urlEpisodeId = searchParams.get("episode_id") || "";
	const activeTurnRef = useRef<string | null>(null);
	const chatLogRef = useRef<HTMLDivElement | null>(null);

	useEffect(() => {
		const auth = loadAuth();
		// Doctors have their own workspace; triage intake is for patients and visitors.
		if (auth?.role === "doctor") {
			router.replace("/doctor");
			return;
		}
		setViewerRole(auth ? auth.role : null);
	}, [router]);
	const sessionIdRef = useRef("new");
	const originatingEventIdRef = useRef<string | null>(null);
	const pendingSubmissionRef = useRef<PendingAiChatSubmission | null>(null);
	const [viewerRole, setViewerRole] = useState<string | null | undefined>(undefined);
	const [sessionId, setSessionId] = useState("new");
	const [episodeId, setEpisodeId] = useState("");
	const [episodeTitle, setEpisodeTitle] = useState("");
	const [episodeTitleLoading, setEpisodeTitleLoading] = useState(false);
	const [patientId, setPatientId] = useState("");
	const [prompt, setPrompt] = useState("");
	const [isWaiting, setIsWaiting] = useState(false);
	const [agentProgress, setAgentProgress] = useState(initialAgentProgress);
	const [isGeneratingSummary, setIsGeneratingSummary] = useState(false);
	const [summaryError, setSummaryError] = useState("");
	const [historyLoading, setHistoryLoading] = useState(false);
	const [historyError, setHistoryError] = useState("");
	const [messages, setMessages] = useState<ChatMessage[]>([
		{
			id: "intro",
			role: "assistant",
			text: "Welcome. Describe your current symptoms, rehab stage, and what changes the discomfort. I will triage your situation and suggest a safe next step for today.",
		},
	]);
	const [routeState, setRouteState] = useState<RouteState>({
		routingDecision: "",
		needsMoreInfo: null,
		safetyPassed: null,
	});

	useEffect(() => {
		setEpisodeId(urlEpisodeId);
		setHistoryError("");
		sessionIdRef.current = urlSessionId || "new";
		setSessionId(sessionIdRef.current);
		const auth = loadAuth();
		if (auth?.role === "patient") {
			setPatientId(auth.user_id);
			if (searchParams.get("resume_triage") === "summary") {
				const pending = readPendingTriageSummary();
				if (pending) {
					const restoredEpisodeId = pending.episodeId || urlEpisodeId;
					sessionIdRef.current = pending.sessionId || "new";
					originatingEventIdRef.current = pending.request.originating_event_id || null;
					setSessionId(sessionIdRef.current);
					setEpisodeId(restoredEpisodeId);
					setMessages(pending.messages);
					setPrompt(pending.prompt || "");
					setRouteState(pending.routeState);
					setHistoryLoading(false);
					setSummaryError("Signed in. Your triage history is ready for a summary.");

					const cleanParams = new URLSearchParams(searchParams.toString());
					cleanParams.delete("resume_triage");
					const nextSearch = cleanParams.toString();
					router.replace(`${window.location.pathname}${nextSearch ? `?${nextSearch}` : ""}`);
					return () => clearAgentProgress();
				}
			}

			if (urlSessionId) {
				let active = true;
				setHistoryLoading(true);
				setMessages([]);
				setPrompt("");
				void getAiChatSession(urlSessionId, auth.access_token)
					.then((session) => {
						if (!active) return;
						sessionIdRef.current = session.session_id;
						setSessionId(session.session_id);
						setEpisodeId(urlEpisodeId || session.care_episode_id || "");
						setMessages(session.messages.map((message) => ({
							id: message.message_id,
							role: message.sender_role,
							text: message.content,
							sources: message.sources,
						})));
						setPrompt("");
					})
					.catch((error) => {
						if (active) setHistoryError(error instanceof Error ? error.message : "Unable to load this triage chat.");
					})
					.finally(() => {
						if (active) setHistoryLoading(false);
					});
				return () => {
					active = false;
					clearAgentProgress();
				};
			}
		} else {
			setPatientId("");
		}

		if (!urlSessionId) {
			originatingEventIdRef.current = null;
			pendingSubmissionRef.current = null;
			setMessages([{
				id: "intro",
				role: "assistant",
				text: "Welcome. Describe your current symptoms, rehab stage, and what changes the discomfort. I will triage your situation and suggest a safe next step for today.",
			}]);
			setPrompt("");
			setRouteState({ routingDecision: "", needsMoreInfo: null, safetyPassed: null });
			setHistoryLoading(false);
		}
		return () => {
			clearAgentProgress();
		};
	}, [router, searchParams, urlEpisodeId, urlSessionId]);

	useEffect(() => {
		if (!episodeId) {
			setEpisodeTitle("");
			setEpisodeTitleLoading(false);
			return;
		}

		const auth = loadAuth();
		if (!auth || auth.role !== "patient") {
			setEpisodeTitle("");
			setEpisodeTitleLoading(false);
			return;
		}

		let active = true;
		setEpisodeTitle("");
		setEpisodeTitleLoading(true);
		void getCareEpisode(episodeId, auth.access_token)
			.then((episode) => {
				if (active) setEpisodeTitle(episode.issue_title);
			})
			.catch(() => {
				if (active) setEpisodeTitle("");
			})
			.finally(() => {
				if (active) setEpisodeTitleLoading(false);
			});

		return () => {
			active = false;
		};
	}, [episodeId]);

	const clearAgentProgress = () => {
		setAgentProgress(initialAgentProgress);
	};

	const finishTurn = (turnId: string) => {
		if (activeTurnRef.current !== turnId) return false;
		activeTurnRef.current = null;
		clearAgentProgress();
		setIsWaiting(false);
		return true;
	};

	const applyRouteState = (data: AiEvent) => {
		if (data.routing_decision || typeof data.needs_more_info === "boolean" || typeof data.safety_passed === "boolean") {
			setRouteState((prev) => ({
				routingDecision: data.routing_decision ? String(data.routing_decision) : prev.routingDecision,
				needsMoreInfo: typeof data.needs_more_info === "boolean" ? data.needs_more_info : prev.needsMoreInfo,
				safetyPassed: typeof data.safety_passed === "boolean" ? data.safety_passed : prev.safetyPassed,
			}));
		}
	};

	const sendChatTurn = async (turnId: string, submission: PendingAiChatSubmission) => {
		const auth = loadAuth();
		if (!auth || auth.role !== "patient") {
			finishTurn(turnId);
			setPrompt(submission.message);
			setSummaryError("Please sign in as a patient before starting AI triage.");
			router.push("/login?role=patient&next=" + encodeURIComponent("/"));
			return;
		}

		try {
			const data = await streamAiChatTurn(
				sessionIdRef.current || "new",
				{
					message: submission.message,
					care_episode_id: submission.careEpisodeId || undefined,
					idempotency_key: submission.idempotencyKey,
				},
				auth.access_token,
				(progress) => {
					if (activeTurnRef.current === turnId) setAgentProgress(agentProgressForStage(progress));
				},
			);
			if (activeTurnRef.current !== turnId) return;
			if (data.session_id) {
				sessionIdRef.current = data.session_id;
				setSessionId(data.session_id);
				window.dispatchEvent(new Event("rehab-ai-chat-changed"));
			}
			if (data.originating_event_id) {
				originatingEventIdRef.current = data.originating_event_id;
			}
			applyRouteState(data);
			finishTurn(turnId);
			if (data.event === "error") {
				pendingSubmissionRef.current = submission;
				setPrompt(submission.message);
				setMessages((prev) => [...prev, { id: makeId(), role: "system", text: friendlyError(data.detail) }]);
			} else {
				pendingSubmissionRef.current = null;
				setMessages((prev) => [
					...prev,
					{ id: makeId(), role: "assistant", text: data.response || "I could not generate a response for that turn.", sources: data.sources },
				]);
			}
		} catch (error) {
			if (!finishTurn(turnId)) return;
			setPrompt(submission.message);
			setMessages((prev) => [
				...prev,
				{ id: makeId(), role: "system", text: friendlyError(error instanceof Error ? error.message : undefined) },
			]);
		}
	};

	const buildTriageTranscript = () => {
		const transcript = messages
			.filter((message) => message.role !== "system" && message.id !== "intro")
			.map((message) => `${message.role === "user" ? "Patient" : "RehabFlow"}: ${message.text}`)
			.join("\n\n");
		return transcript.length > 3500 ? `${transcript.slice(0, 3500).trim()}...` : transcript;
	};

	const buildTriageSummaryRequest = () => {
		const aiSessionId = sessionIdRef.current && sessionIdRef.current !== "new" ? sessionIdRef.current : null;
		return {
			ai_session_id: aiSessionId,
			additional_context: aiSessionId ? "" : buildTriageTranscript() || summaryRequestFallback,
			originating_event_id: originatingEventIdRef.current,
		};
	};

	const finishTriage = async () => {
		if (isGeneratingSummary) return;
		const request = buildTriageSummaryRequest();
		const auth = loadAuth();
		if (!auth || auth.role !== "patient") {
			sessionStorage.setItem(PENDING_TRIAGE_SUMMARY_KEY, JSON.stringify({
				sessionId: sessionIdRef.current,
				episodeId,
				messages,
				prompt,
				routeState,
				request,
			}));
			setSummaryError("Please sign in as the patient before generating a triage summary. Your triage history will stay here after login.");
			router.push(`/login?next=${encodeURIComponent(buildLoginReturnPath(episodeId))}`);
			return;
		}
		setSummaryError("");
		setIsGeneratingSummary(true);
		try {
			let targetEpisodeId = episodeId;
			if (!targetEpisodeId) {
				const created = await createEpisodeFromTriageSummaryRequest(request, auth.access_token);
				targetEpisodeId = created.episode.care_episode_id;
			} else {
				await createTriageSummaryDraft(targetEpisodeId, request, auth.access_token);
			}
			clearPendingTriageSummary();
			window.dispatchEvent(new Event("rehab-ai-chat-changed"));
			router.push(`/episodes/${targetEpisodeId}/triage`);
		} catch (error) {
			setSummaryError(error instanceof Error ? error.message : "Could not generate the triage summary draft.");
		} finally {
			setIsGeneratingSummary(false);
		}
	};

	const sendPrompt = (nextPrompt = prompt) => {
		const text = nextPrompt.trim();
		if (!text || isWaiting || historyLoading || historyError) return;
		const auth = loadAuth();
		if (!auth || auth.role !== "patient") {
			setSummaryError("Please sign in as a patient before starting AI triage.");
			router.push("/login?role=patient&next=" + encodeURIComponent("/"));
			return;
		}

		const turnId = makeId();
		const previousSubmission = pendingSubmissionRef.current;
		const submission = previousSubmission?.message === text && previousSubmission.careEpisodeId === episodeId
			? previousSubmission
			: {
				message: text,
				careEpisodeId: episodeId,
				idempotencyKey: crypto.randomUUID(),
			};
		pendingSubmissionRef.current = submission;
		setMessages((prev) => [...prev, { id: makeId(), role: "user", text }]);
		setPrompt("");
		setIsWaiting(true);
		setAgentProgress(initialAgentProgress);
		activeTurnRef.current = turnId;

		setSummaryError("");
		void sendChatTurn(turnId, submission);
	};

	const conversationStarted = messages.some((message) => message.role === "user");

	const routeSummary = useMemo(() => {
		if (!routeState.routingDecision) {
			return conversationStarted
				? "When you've answered the follow-up questions, request a Triage Summary to save this chat to an episode."
				: "Describe what hurts. RehabFlow asks follow-up questions, then you can request a Triage Summary.";
		}
		if (routeState.safetyPassed === false || routeState.routingDecision.includes("urgent")) {
			return "Your answers suggest getting checked in person. If symptoms are severe, contact emergency services.";
		}
		if (routeState.needsMoreInfo || routeState.routingDecision === "ask_clarification") return "Answer the follow-up question so the advice fits your situation.";
		if (routeState.routingDecision === "unsupported") return "This question is outside rehab triage. A clinician is the right person to ask.";
		return "Keep answering until the advice feels complete, then request a Triage Summary to save it to an episode.";
	}, [routeState, conversationStarted]);

	// Keep the newest message in view, like any chat.
	useEffect(() => {
		const log = chatLogRef.current;
		if (log) log.scrollTop = log.scrollHeight;
	}, [messages, isWaiting, historyLoading]);

	const tone = routeTone(routeState);
	return (
		<div className="triage-layout">
			<section className="triage-chat" aria-labelledby="triage-chat-title">
				<header className="triage-chat-header">
					<div className="min-w-0">
						<p className="text-sm font-semibold text-action">AI Triage Intake</p>
						<h1 id="triage-chat-title" className="mt-0.5 text-2xl font-bold leading-tight">{episodeTitleLoading ? "Loading rehab episode..." : episodeTitle || "What does your body need today?"}</h1>
					</div>
					{routeState.routingDecision ? <span className={`route-badge route-${tone}`}>{prettyRoute(routeState.routingDecision)}</span> : null}
				</header>

				<div className="triage-chat-log" aria-live="polite" ref={chatLogRef}>
					{historyLoading ? <p className="chat-history-state">Loading previous chat...</p> : null}
					{historyError ? <p className="chat-history-state chat-history-state-error">{historyError}</p> : null}
					{!historyLoading && !historyError && !messages.length ? <p className="chat-history-state">This chat has no messages yet.</p> : null}
					{!historyLoading && !historyError ? messages.map((message) => (
						<div
							key={message.id}
							className={`chat-card ${
								message.role === "user" ? "chat-card-user" : message.role === "system" ? "chat-card-system" : "chat-card-assistant"
							}`}
						>
							{message.role === "user" ? null : <p className="chat-card-author">{message.role === "system" ? "Something went wrong" : "RehabFlow"}</p>}
							<p className="whitespace-pre-wrap text-[0.9875rem] leading-relaxed">{message.text}</p>
							{message.sources?.length ? (
								<div className="mt-3 flex flex-wrap gap-x-3 gap-y-1 border-t border-line pt-2">
									{message.sources.map((source) => (
										<a key={source.url} className="inline-flex max-w-full items-center gap-1 text-xs font-medium text-action underline decoration-[rgba(82,97,78,0.35)] underline-offset-2" href={source.url} target="_blank" rel="noreferrer" title={source.title}>
											<svg aria-hidden="true" className="h-3 w-3 shrink-0" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M9.5 2.5h4v4M13.25 2.75 7.5 8.5" strokeLinecap="round" strokeLinejoin="round" /><path d="M13 9v3.25A1.25 1.25 0 0 1 11.75 13.5h-8A1.25 1.25 0 0 1 2.5 12.25v-8A1.25 1.25 0 0 1 3.75 3H7" strokeLinecap="round" /></svg>
											<span className="truncate">{source.title || source.label}</span>
										</a>
									))}
								</div>
							) : null}
						</div>
					)) : null}
					{isWaiting ? (
						<div className="chat-card chat-card-assistant agent-progress-card" role="status">
							<div className="flex items-center gap-2">
								<span className="agent-progress-dot" />
								<p className="text-sm font-semibold">{agentProgress.label}</p>
							</div>
							{agentProgress.detail ? <p className="mt-1 text-sm text-[var(--rf-ink-muted)]">{agentProgress.detail}</p> : null}
						</div>
					) : null}
					{!conversationStarted && !historyLoading && !historyError ? (
						<div className="triage-examples">
							<p className="text-sm font-semibold text-[var(--rf-ink-muted)]">Not sure how to start? Try one of these.</p>
							<div className="mt-2 flex flex-wrap gap-2">
								{suggestedPrompts.map((item) => (
									<button key={item} className="suggestion-chip" onClick={() => setPrompt(item)} disabled={isWaiting}>
										{item}
									</button>
								))}
							</div>
						</div>
					) : null}
				</div>

				<form
					className="triage-composer"
					onSubmit={(event) => {
						event.preventDefault();
						sendPrompt();
					}}
				>
					<label htmlFor="triage-message" className="sr-only">Your message</label>
					<textarea
						id="triage-message"
						className="textarea triage-composer-input"
						rows={2}
						placeholder="Describe what hurts, since when, and what makes it better or worse"
						value={prompt}
						onChange={(event) => setPrompt(event.target.value)}
						disabled={historyLoading || Boolean(historyError)}
						onKeyDown={(event) => {
							if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
								event.preventDefault();
								sendPrompt();
							}
						}}
					/>
					<button type="submit" className="btn-primary triage-composer-send" disabled={isWaiting || historyLoading || Boolean(historyError) || !prompt.trim()}>
						{isWaiting ? "Sending" : "Send"}
					</button>
					<p className="triage-composer-hint">Enter to send, Shift + Enter for a new line.</p>
				</form>
			</section>

			<aside className="triage-side" aria-label="Triage progress and safety">
				<div className="card summary-request-card">
					<h2 className="section-title">Save this triage</h2>
					<p className="mt-1.5 text-sm text-[var(--rf-ink-muted)]">{routeSummary}</p>
					<button className="btn-secondary mt-4 w-full" onClick={finishTriage} disabled={isWaiting || isGeneratingSummary || !conversationStarted}>
						{isGeneratingSummary ? "Requesting" : "Request Summary"}
					</button>
					{summaryError ? <p className="notice notice-danger mt-3 text-sm">{summaryError}</p> : null}
					<p className="mt-3 text-xs text-[var(--rf-ink-soft)]">The summary is saved to a Care Episode, where you can start AI Daily Rehab or ask for Professional Care.</p>
				</div>

				<div className="triage-safety">
					<h2 className="text-base font-bold">Stop and get help if</h2>
					<ul className="mt-2 space-y-1.5 text-sm">
						<li>pain is sharp, the joint gives way, or swelling comes on suddenly</li>
						<li>you have a fever, numbness, or weakness that is spreading</li>
						<li>there was a serious fall or injury: call emergency services</li>
					</ul>
				</div>

				{viewerRole === null ? (
					<p className="px-1 text-sm text-[var(--rf-ink-muted)]">
						Just looking? <Link className="font-semibold text-action underline underline-offset-4" href="/rehab/session">Try a demo exercise session</Link>
					</p>
				) : null}
			</aside>
		</div>
	);
}

export default function HomePage() {
	return (
		<Suspense fallback={<p className="text-sm text-slate-500">Loading AI triage...</p>}>
			<HomePageContent />
		</Suspense>
	);
}
