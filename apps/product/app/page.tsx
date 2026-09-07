"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

import { loadAuth } from "@rehab/shared/auth";
import { createEpisodeFromTriageSummaryRequest, createTriageSummaryDraft, getCareEpisode, sendAiChatTurn } from "@rehab/shared/api";

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
	afterMs: number;
	label: string;
	detail: string;
};

const PENDING_TRIAGE_SUMMARY_KEY = "rehab_pending_triage_summary";
const starterPrompt = "My shoulder feels stiff after rotator cuff rehab. What should I do today?";

const suggestedPrompts = [
	"My shoulder feels stiff after rotator cuff rehab. What should I do today?",
	"My lower back flared up after yesterday exercise. Should I rest or move gently?",
	"I rolled my ankle last week and still have swelling. Can I exercise today?",
	"I am recovering from surgery and feel new pain during stairs. Should I contact my clinician?",
];

const agentProgressSteps: AgentProgressStep[] = [
	{
		afterMs: 0,
		label: "Reading symptoms",
		detail: "Reviewing your message and recent triage context.",
	},
	{
		afterMs: 6000,
		label: "Checking safety",
		detail: "Screening for red flags and deciding whether more details are needed.",
	},
	{
		afterMs: 14000,
		label: "Searching context",
		detail: "Looking up relevant rehab guidance and exercise catalog context.",
	},
	{
		afterMs: 24000,
		label: "Reviewing plan",
		detail: "Running the safety review before showing the recommendation.",
	},
	{
		afterMs: 36000,
		label: "Writing answer",
		detail: "Finalizing the response and saving the session state.",
	},
];
const summaryRequestFallback = "Patient requested a Triage Summary from the AI intake chat.";

function makeId() {
	return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function prettyRoute(route: string) {
	if (!route) return "Awaiting triage";
	return route.replaceAll("_", " ");
}

function agentProgressForElapsed(elapsedMs: number) {
	return agentProgressSteps.reduce((current, step) => (elapsedMs >= step.afterMs ? step : current), agentProgressSteps[0]);
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

export default function HomePage() {
	const router = useRouter();
	const agentProgressIntervalRef = useRef<number | null>(null);
	const turnStartedAtRef = useRef(0);
	const activeTurnRef = useRef<string | null>(null);
	const sessionIdRef = useRef("new");
	const originatingEventIdRef = useRef<string | null>(null);
	const pendingSubmissionRef = useRef<PendingAiChatSubmission | null>(null);
	const [sessionId, setSessionId] = useState("new");
	const [episodeId, setEpisodeId] = useState("");
	const [episodeTitle, setEpisodeTitle] = useState("");
	const [episodeTitleLoading, setEpisodeTitleLoading] = useState(false);
	const [patientId, setPatientId] = useState("");
	const [patientLabel, setPatientLabel] = useState("Sign in to begin");
	const [status, setStatus] = useState("Ready");
	const [prompt, setPrompt] = useState(starterPrompt);
	const [isWaiting, setIsWaiting] = useState(false);
	const [agentProgress, setAgentProgress] = useState(agentProgressSteps[0]);
	const [isGeneratingSummary, setIsGeneratingSummary] = useState(false);
	const [summaryError, setSummaryError] = useState("");
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
		const params = new URLSearchParams(window.location.search);
		const urlEpisodeId = params.get("episode_id") || "";
		setEpisodeId(urlEpisodeId);
		const auth = loadAuth();
		if (auth?.role === "patient") {
			setPatientId(auth.user_id);
			setPatientLabel(auth.username);
			if (params.get("resume_triage") === "summary") {
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
					setSummaryError("Signed in. Your triage history is ready for a summary.");

					const cleanParams = new URLSearchParams(window.location.search);
					cleanParams.delete("resume_triage");
					const nextSearch = cleanParams.toString();
					router.replace(`${window.location.pathname}${nextSearch ? `?${nextSearch}` : ""}`);
				}
			}
		}
		return () => {
			clearAgentProgress();
		};
	}, [router]);

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
		if (agentProgressIntervalRef.current) {
			window.clearInterval(agentProgressIntervalRef.current);
			agentProgressIntervalRef.current = null;
		}
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
		setStatus(agentProgressSteps[0].label);
		const auth = loadAuth();
		if (!auth || auth.role !== "patient") {
			finishTurn(turnId);
			setPrompt(submission.message);
			setSummaryError("Please sign in as a patient before starting AI triage.");
			router.push("/login?role=patient&next=" + encodeURIComponent("/"));
			return;
		}

		try {
			const data = await sendAiChatTurn(
				sessionIdRef.current || "new",
				{
					message: submission.message,
					care_episode_id: submission.careEpisodeId || undefined,
					idempotency_key: submission.idempotencyKey,
				},
				auth.access_token,
			);
			if (activeTurnRef.current !== turnId) return;
			if (data.session_id) {
				sessionIdRef.current = data.session_id;
				setSessionId(data.session_id);
			}
			if (data.originating_event_id) {
				originatingEventIdRef.current = data.originating_event_id;
			}
			applyRouteState(data);
			finishTurn(turnId);
			if (data.event === "error") {
				pendingSubmissionRef.current = submission;
				setPrompt(submission.message);
				setStatus("Connection issue");
				setMessages((prev) => [...prev, { id: makeId(), role: "system", text: friendlyError(data.detail) }]);
			} else {
				pendingSubmissionRef.current = null;
				setStatus("Ready");
				setMessages((prev) => [
					...prev,
					{ id: makeId(), role: "assistant", text: data.response || "I could not generate a response for that turn.", sources: data.sources },
				]);
			}
		} catch (error) {
			if (!finishTurn(turnId)) return;
			setPrompt(submission.message);
			setStatus("Connection issue");
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
			router.push(`/episodes/${targetEpisodeId}/triage`);
		} catch (error) {
			setSummaryError(error instanceof Error ? error.message : "Could not generate the triage summary draft.");
		} finally {
			setIsGeneratingSummary(false);
		}
	};

	const sendPrompt = (nextPrompt = prompt) => {
		const text = nextPrompt.trim();
		if (!text || isWaiting) return;
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
		const firstProgressStep = agentProgressSteps[0];
		setMessages((prev) => [...prev, { id: makeId(), role: "user", text }]);
		setPrompt("");
		setIsWaiting(true);
		setAgentProgress(firstProgressStep);
		setStatus(firstProgressStep.label);
		activeTurnRef.current = turnId;
		turnStartedAtRef.current = Date.now();
		clearAgentProgress();
		agentProgressIntervalRef.current = window.setInterval(() => {
			if (activeTurnRef.current !== turnId) return;
			const nextStep = agentProgressForElapsed(Date.now() - turnStartedAtRef.current);
			setAgentProgress(nextStep);
			setStatus(nextStep.label);
		}, 1000);

		setSummaryError("");
		void sendChatTurn(turnId, submission);
	};

	const routeSummary = useMemo(() => {
		if (!routeState.routingDecision) return "Answer a few triage questions to receive today’s recommended path.";
		if (routeState.needsMoreInfo) return "The assistant needs one or two more details before recommending today’s plan.";
		if (routeState.safetyPassed === false || routeState.routingDecision.includes("urgent")) {
			return "This response may need Professional Care guidance or urgent care if symptoms are severe.";
		}
		return "You have enough triage context to move into a guided self-serve rehab session.";
	}, [routeState]);

	const tone = routeTone(routeState);
	const statusClass = status === "Connected" ? "status-ok" : status === "Connection issue" ? "status-alert" : "status-neutral";

	return (
		<div className="space-y-6">
			<section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm md:p-6">
				<div className="grid gap-6 lg:grid-cols-[minmax(0,1.15fr)_360px] lg:items-start">
					<div>
						<div className="flex flex-wrap items-center gap-2 text-xs font-semibold uppercase tracking-wide text-emerald-700">
							<span>Patient rehab copilot</span>
							<span className={`status-pill ${statusClass}`}>{status}</span>
						</div>
						<h1 className="mt-3 max-w-3xl text-3xl font-semibold text-slate-950 md:text-4xl">Start with AI triage, leave with today’s rehab route.</h1>
						<p className="mt-3 max-w-2xl text-sm leading-6 text-slate-600">
							Tell RehabFlow what hurts, where you are in recovery, and what changed recently. The assistant will ask clarifying questions, flag risk, and guide you toward AI Daily Rehab or Professional Care. Optional Live Movement Monitoring can support a specific Care Episode when a clinician relationship is active.
						</p>
						<div className="mt-5 grid gap-3 sm:grid-cols-3">
							<div className="metric-tile"><span>1</span><p>Describe symptoms</p></div>
							<div className="metric-tile"><span>2</span><p>Get triaged</p></div>
							<div className="metric-tile"><span>3</span><p>Choose your next step</p></div>
						</div>
					</div>
					<aside className={`rounded-lg border p-4 shadow-sm ${patientId ? "border-emerald-200 bg-emerald-50" : "border-emerald-100 bg-emerald-50"}`}>
						<p className={`text-xs font-semibold uppercase tracking-wide ${patientId ? "text-emerald-800" : "text-emerald-700"}`}>{patientId ? "Your rehab" : "Ready when you are"}</p>
						<p className="mt-2 text-lg font-semibold text-slate-950">{patientLabel}</p>
						<p className="mt-1 text-xs leading-5 text-slate-600">
							{patientId ? "Signed in. Continue to your Patient Dashboard whenever you are ready." : "Sign in as a patient to start AI triage and connect your care context."}
						</p>
						{patientId ? (
							<Link className="btn-primary mt-4 w-full" href="/episodes">Patient Dashboard</Link>
						) : (
							<div className="mt-4 flex gap-2">
								<Link className="btn-secondary flex-1" href="/login">Login</Link>
								<Link className="btn-primary flex-1" href="/register">Register</Link>
							</div>
						)}
					</aside>
				</div>
			</section>

			<section className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_380px]">
				<div className="rounded-lg border border-slate-200 bg-white shadow-sm">
					<div className="border-b border-slate-200 p-4 md:p-5">
						<div className="flex flex-wrap items-start justify-between gap-3">
							<div>
								<p className="text-xs font-semibold uppercase tracking-wide text-slate-500">AI triage intake</p>
								<h2 className="mt-1 text-xl font-semibold text-slate-950">{episodeTitleLoading ? "Loading rehab episode..." : episodeTitle || "What does your body need today?"}</h2>
							</div>
							<span className={`route-badge route-${tone}`}>{prettyRoute(routeState.routingDecision)}</span>
						</div>
					</div>

					<div className="max-h-[520px] space-y-3 overflow-auto bg-slate-50 p-4 md:p-5">
						{messages.map((message) => (
							<div
								key={message.id}
								className={`chat-card ${
									message.role === "user" ? "chat-card-user" : message.role === "system" ? "chat-card-system" : "chat-card-assistant"
								}`}
							>
								<p className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">{message.role === "user" ? "You" : message.role === "system" ? "System" : "RehabFlow"}</p>
								<p className="mt-1 whitespace-pre-wrap text-sm leading-6">{message.text}</p>
								{message.sources?.length ? (
									<div className="mt-2 flex flex-wrap gap-2">
										{message.sources.map((source) => (
											<a key={source.url} className="inline-flex max-w-full items-center gap-1 text-xs font-medium text-emerald-700 underline decoration-emerald-300 underline-offset-2" href={source.url} target="_blank" rel="noreferrer" title={source.title}>
												<svg aria-hidden="true" className="h-3 w-3 shrink-0" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M9.5 2.5h4v4M13.25 2.75 7.5 8.5" strokeLinecap="round" strokeLinejoin="round" /><path d="M13 9v3.25A1.25 1.25 0 0 1 11.75 13.5h-8A1.25 1.25 0 0 1 2.5 12.25v-8A1.25 1.25 0 0 1 3.75 3H7" strokeLinecap="round" /></svg>
												<span className="truncate">{source.title || source.label}</span>
											</a>
										))}
									</div>
								) : null}
							</div>
						))}
						{isWaiting ? (
							<div className="chat-card chat-card-assistant agent-progress-card">
								<div className="flex items-center gap-2">
									<span className="agent-progress-dot" />
									<p className="text-[11px] font-semibold uppercase tracking-wide text-emerald-700">RehabFlow is working</p>
								</div>
								<p className="mt-2 text-sm font-semibold text-slate-800">{agentProgress.label}</p>
								<p className="mt-1 text-sm leading-6 text-slate-500">{agentProgress.detail}</p>
							</div>
						) : null}
					</div>

					<div className="border-t border-slate-200 p-4 md:p-5">
						<div className="flex flex-wrap gap-2 pb-3">
							{suggestedPrompts.map((item) => (
								<button key={item} className="suggestion-chip" onClick={() => setPrompt(item)} disabled={isWaiting}>
									{item}
								</button>
							))}
						</div>
						<div className="flex flex-col gap-2 sm:flex-row">
							<textarea
								className="textarea min-h-24 flex-1 resize-none"
								value={prompt}
								onChange={(event) => setPrompt(event.target.value)}
								onKeyDown={(event) => {
									if ((event.metaKey || event.ctrlKey) && event.key === "Enter") sendPrompt();
								}}
							/>
							<button className="btn-primary sm:w-32" onClick={() => sendPrompt()} disabled={isWaiting || !prompt.trim()}>
								{isWaiting ? "Sending" : "Send"}
							</button>
						</div>
					</div>
				</div>

				<aside className="space-y-4">
					<div className="rounded-lg border border-emerald-100 bg-emerald-50 p-4 shadow-sm">
						<p className="text-xs font-semibold uppercase tracking-wide text-emerald-700">Triage Summary Request</p>
						<p className="mt-2 text-sm leading-6 text-slate-700">When you are done answering triage questions, request a summary. If this triage is not already inside an episode, RehabFlow will create one from the summary request.</p>
						<button className="btn-primary mt-4 w-full" onClick={finishTriage} disabled={isWaiting || isGeneratingSummary}>
							{isGeneratingSummary ? "Requesting" : "Request Summary"}
						</button>
						{summaryError ? <p className="mt-3 text-sm leading-6 text-rose-600">{summaryError}</p> : null}
					</div>

					<div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
						<p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Recommended route</p>
						<p className="mt-2 text-lg font-semibold capitalize text-slate-950">{prettyRoute(routeState.routingDecision)}</p>
						<p className="mt-2 text-sm leading-6 text-slate-600">{routeSummary}</p>
					</div>

					<Link className="next-step-card next-step-primary" href="/rehab/session">
						<p className="text-sm font-semibold text-slate-950">Try guided rehab demo</p>
						<p className="mt-1 text-sm text-slate-600">Open pose-guided exercise feedback for today’s rehab session.</p>
					</Link>

					<Link className="next-step-card" href="/episodes">
						<p className="text-sm font-semibold text-slate-950">Professional Care</p>
						<p className="mt-1 text-sm text-slate-600">Continue to your Care Episodes to connect with a clinician when recovery needs shared support.</p>
					</Link>

					<div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
						<p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Safety notes</p>
						<ul className="mt-3 space-y-2 text-sm leading-6 text-slate-600">
							<li>Stop exercise for sharp pain, new instability, fever, or sudden swelling.</li>
							<li>Consider Professional Care for worsening symptoms or post-op uncertainty.</li>
							<li>Call emergency services for severe trauma, chest pain, or neurologic symptoms.</li>
						</ul>
					</div>
				</aside>
			</section>
		</div>
	);
}
