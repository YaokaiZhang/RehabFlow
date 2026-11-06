"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Fragment, Suspense, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";

import { loadAuth } from "@rehab/shared/auth";

import { ExerciseCards, WrapUpCard, type WrapUp } from "../components/TriageChatCards";
import {
	createEpisodeFromTriageSummaryRequest,
	createTriageSummaryDraft,
	deleteTriageSummaryDraft,
	getAiChatSession,
	getCareEpisode,
	selectTriageSummaryDraft,
	streamAiChatTurn,
	type AiChatExerciseCard,
	type AiChatNextStep,
} from "@rehab/shared/api";

type ChatMessage = {
	id: string;
	role: "assistant" | "user" | "system";
	text: string;
	sources?: Array<{ label: string; title: string; url: string }>;
	exerciseCards?: AiChatExerciseCard[];
	nextStep?: AiChatNextStep;
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

type PendingAiChatSubmission = {
	message: string;
	careEpisodeId: string;
	idempotencyKey: string;
};

const suggestedPrompts = [
	"Six weeks after my rotator cuff repair, my shoulder is still really stiff in the mornings. Is it okay to push through it?",
	"I spent Saturday gardening and my lower back has been tight since. Getting out of bed is the worst part. Rest or keep moving?",
	"Rolled my ankle at basketball five days ago. It's still a bit puffy, but I can walk on it fine. When can I start running again?",
	"I had my knee replaced a month ago. Stairs hurt more this week than last, and the knee feels warm. Is that normal?",
];

// How each suggestion sticker looks: its pastel and its resting angle.
const promptStickers = [
	{ tone: "sky", tilt: -3, depth: 18 },
	{ tone: "peach", tilt: 2.5, depth: 30 },
	{ tone: "mint", tilt: 3, depth: 24 },
	{ tone: "lilac", tilt: -2, depth: 36 },
] as const;

// Stage label reported live by the backend while a turn runs.
const initialAgentProgress = "Reading your message";
const summaryRequestFallback = "Patient requested a Triage Summary from the AI intake chat.";


function makeId() {
	return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
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
	const [wrapUp, setWrapUp] = useState<WrapUp>({ stage: "offer" });
	const [wrapUpError, setWrapUpError] = useState("");
	// Set when this page itself wrote session_id into the URL, so the history loader can skip it.
	const ownUrlSessionRef = useRef<string | null>(null);
	const [historyLoading, setHistoryLoading] = useState(false);
	const [historyError, setHistoryError] = useState("");
	const [messages, setMessages] = useState<ChatMessage[]>([]);
	const [routeState, setRouteState] = useState<RouteState>({
		routingDecision: "",
		needsMoreInfo: null,
		safetyPassed: null,
	});

	useEffect(() => {
		// This page wrote the session into the URL itself; the chat on screen is already current.
		if (urlSessionId && ownUrlSessionRef.current === urlSessionId) return;
		ownUrlSessionRef.current = null;
		setEpisodeId(urlEpisodeId);
		setHistoryError("");
		sessionIdRef.current = urlSessionId || "new";
		setSessionId(sessionIdRef.current);
		const auth = loadAuth();
		if (auth?.role === "patient") {
			setPatientId(auth.user_id);
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
							exerciseCards: message.exercise_cards,
							nextStep: message.next_step ?? null,
						})));
						setWrapUp({ stage: "offer" });
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
			setMessages([]);
			setPrompt("");
			setRouteState({ routingDecision: "", needsMoreInfo: null, safetyPassed: null });
			setWrapUp({ stage: "offer" });
			setWrapUpError("");
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
					if (activeTurnRef.current === turnId) setAgentProgress(progress.label);
				},
			);
			if (activeTurnRef.current !== turnId) return;
			if (data.session_id) {
				const isNewSession = sessionIdRef.current !== data.session_id;
				sessionIdRef.current = data.session_id;
				setSessionId(data.session_id);
				window.dispatchEvent(new Event("rehab-ai-chat-changed"));
				// Keep the chat on refresh: put the session in the URL without reloading it.
				if (isNewSession) {
					ownUrlSessionRef.current = data.session_id;
					const params = new URLSearchParams(window.location.search);
					params.set("session_id", data.session_id);
					window.history.replaceState(null, "", `${window.location.pathname}?${params.toString()}`);
				}
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
					{
						id: makeId(),
						role: "assistant",
						text: data.response || "I could not generate a response for that turn.",
						sources: data.sources,
						exerciseCards: data.exercise_cards,
						nextStep: data.next_step ?? null,
					},
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
			.filter((message) => message.role !== "system")
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

	const writeSummary = async () => {
		if (wrapUp.stage !== "offer") return;
		const auth = loadAuth();
		if (!auth || auth.role !== "patient") {
			router.push("/login?role=patient&next=" + encodeURIComponent(window.location.pathname + window.location.search));
			return;
		}
		const request = buildTriageSummaryRequest();
		setWrapUpError("");
		setWrapUp({ stage: "writing" });
		try {
			if (episodeId) {
				const draft = await createTriageSummaryDraft(episodeId, request, auth.access_token);
				setWrapUp({ stage: "draft", episodeId, draft });
			} else {
				const created = await createEpisodeFromTriageSummaryRequest(request, auth.access_token);
				setEpisodeId(created.episode.care_episode_id);
				setEpisodeTitle(created.episode.issue_title);
				setWrapUp({ stage: "draft", episodeId: created.episode.care_episode_id, draft: created.draft });
			}
			window.dispatchEvent(new Event("rehab-ai-chat-changed"));
		} catch (error) {
			setWrapUp({ stage: "offer" });
			setWrapUpError(error instanceof Error ? error.message : "Could not write the summary.");
		}
	};

	const saveSummary = async () => {
		if (wrapUp.stage !== "draft") return;
		const auth = loadAuth();
		if (!auth || auth.role !== "patient") return;
		const { episodeId: targetEpisodeId, draft } = wrapUp;
		setWrapUpError("");
		setWrapUp({ stage: "saving", episodeId: targetEpisodeId, draft });
		try {
			await selectTriageSummaryDraft(targetEpisodeId, draft.triage_summary_draft_id, auth.access_token);
			window.dispatchEvent(new Event("rehab-ai-chat-changed"));
			router.push(draft.clinician_review_needed
				? `/episodes/${targetEpisodeId}/professional-care`
				: `/episodes/${targetEpisodeId}/rehab`);
		} catch (error) {
			setWrapUp({ stage: "draft", episodeId: targetEpisodeId, draft });
			setWrapUpError(error instanceof Error ? error.message : "Could not save the summary.");
		}
	};

	// "Not quite": drop the draft and let the patient keep talking.
	const dismissDraft = async () => {
		if (wrapUp.stage !== "draft") return;
		const auth = loadAuth();
		const { episodeId: targetEpisodeId, draft } = wrapUp;
		setWrapUp({ stage: "offer" });
		setWrapUpError("");
		document.getElementById("triage-message")?.focus();
		if (auth?.role === "patient") {
			void deleteTriageSummaryDraft(targetEpisodeId, draft.triage_summary_draft_id, auth.access_token).catch(() => undefined);
		}
	};

	const sendPrompt = (nextPrompt = prompt) => {
		const text = nextPrompt.trim();
		if (!text || isWaiting || historyLoading || historyError) return;
		const auth = loadAuth();
		if (!auth || auth.role !== "patient") {
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

		setWrapUp({ stage: "offer" });
		setWrapUpError("");
		void sendChatTurn(turnId, submission);
	};

	const conversationStarted = messages.some((message) => message.role === "user");

	const showHero = !conversationStarted && !historyLoading && !historyError;

	// What the latest reply allows: wrap up into a plan, get checked in person, or keep talking.
	// Older turns without next_step fall back to the route the chat reported.
	const lastAssistant = [...messages].reverse().find((message) => message.role !== "user");
	const latestStep: AiChatNextStep = useMemo(() => {
		if (!lastAssistant || lastAssistant.role !== "assistant") return null;
		if (lastAssistant.nextStep !== undefined) return lastAssistant.nextStep;
		const route = routeState.routingDecision;
		if (!route) return null;
		if (routeState.safetyPassed === false || route.includes("urgent")) return "urgent";
		if (routeState.needsMoreInfo || route === "ask_clarification" || route === "unsupported") return null;
		return "ready";
	}, [lastAssistant, routeState]);
	const showWrapUp = !isWaiting && !historyLoading && !historyError && (latestStep !== null || wrapUp.stage !== "offer");

	// Keep the newest message in view, like any chat.
	useEffect(() => {
		const log = chatLogRef.current;
		if (!log || showHero) return;
		if (log.scrollHeight > log.clientHeight) log.scrollTop = log.scrollHeight;
		// On phones the page scrolls, not the log.
		else (log.lastElementChild as HTMLElement | null)?.scrollIntoView({ block: "end" });
	}, [messages, isWaiting, historyLoading, wrapUp.stage, wrapUpError, showHero]);

	// The suggestion stickers drift a little with the pointer (CSS reads --px/--py).
	const heroRef = useRef<HTMLElement | null>(null);
	useEffect(() => {
		const hero = heroRef.current;
		if (!showHero || !hero) return;
		const calm = window.matchMedia("(prefers-reduced-motion: reduce)");
		const finePointer = window.matchMedia("(pointer: fine)");
		if (calm.matches || !finePointer.matches) return;
		let frame = 0;
		const onMove = (event: PointerEvent) => {
			cancelAnimationFrame(frame);
			frame = requestAnimationFrame(() => {
				hero.style.setProperty("--px", (event.clientX / window.innerWidth - 0.5).toFixed(3));
				hero.style.setProperty("--py", (event.clientY / window.innerHeight - 0.5).toFixed(3));
			});
		};
		window.addEventListener("pointermove", onMove);
		return () => {
			cancelAnimationFrame(frame);
			window.removeEventListener("pointermove", onMove);
			hero.style.removeProperty("--px");
			hero.style.removeProperty("--py");
		};
	}, [showHero]);

	const composerDisabled = historyLoading || Boolean(historyError);
	const sendDisabled = isWaiting || historyLoading || Boolean(historyError) || !prompt.trim();
	return (
		<div className="triage-layout" data-mode={showHero ? "hero" : "chat"}>
			<section className="triage-chat" aria-labelledby="triage-chat-title" ref={heroRef}>
				<header className="triage-chat-header">
					<div className="min-w-0">
						<h1 id="triage-chat-title" className="triage-title">{episodeTitleLoading ? "opening your episode..." : episodeTitle || "what's bothering you today?"}</h1>
					</div>
				</header>

				<div className="triage-chat-log" aria-live="polite" ref={chatLogRef}>
					{historyLoading ? <p className="chat-history-state">Loading previous chat...</p> : null}
					{historyError ? <p className="chat-history-state chat-history-state-error">{historyError}</p> : null}
					{!showHero && !historyLoading && !historyError && !messages.length ? <p className="chat-history-state chat-history-empty">nothing here yet</p> : null}
					{!historyLoading && !historyError ? messages.map((message) => (
						<Fragment key={message.id}>
						<div
							className={`chat-card ${
								message.role === "user" ? "chat-card-user" : message.role === "system" ? "chat-card-system" : "chat-card-assistant"
							}`}
						>
							<p className="whitespace-pre-wrap text-[0.9875rem] leading-relaxed">{message.text}</p>
							{message.sources?.length ? (
								<div className="chat-card-sources">
									{message.sources.map((source) => (
										<a key={source.url} className="chat-card-source" href={source.url} target="_blank" rel="noreferrer" title={source.title}>
											<svg aria-hidden="true" className="h-3 w-3 shrink-0" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M9.5 2.5h4v4M13.25 2.75 7.5 8.5" strokeLinecap="round" strokeLinejoin="round" /><path d="M13 9v3.25A1.25 1.25 0 0 1 11.75 13.5h-8A1.25 1.25 0 0 1 2.5 12.25v-8A1.25 1.25 0 0 1 3.75 3H7" strokeLinecap="round" /></svg>
											<span className="truncate">{source.title || source.label}</span>
										</a>
									))}
								</div>
							) : null}
						</div>
						{message.exerciseCards?.length ? <ExerciseCards cards={message.exerciseCards} /> : null}
						</Fragment>
					)) : null}
					{showWrapUp ? (
						<WrapUpCard
							step={latestStep}
							wrapUp={wrapUp}
							error={wrapUpError}
							onWrite={writeSummary}
							onSave={saveSummary}
							onDismiss={dismissDraft}
							episodeId={episodeId}
						/>
					) : null}
					{isWaiting ? (
						<div className="chat-card chat-card-assistant agent-progress-card" role="status">
							<div className="agent-progress-head">
								<span className="agent-progress-dots" aria-hidden="true"><span /><span /><span /></span>
								<p className="agent-progress-label">{agentProgress}</p>
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
					<div className="triage-composer-field">
						<label htmlFor="triage-message" className="sr-only">Your message</label>
						<textarea
							id="triage-message"
							className="triage-composer-input"
							rows={1}
							placeholder="What hurts, since when, and what makes it worse?"
							aria-describedby="triage-message-hint"
							value={prompt}
							onChange={(event) => setPrompt(event.target.value)}
							disabled={composerDisabled}
							onKeyDown={(event) => {
								if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
									event.preventDefault();
									sendPrompt();
								}
							}}
						/>
						<button type="submit" className="triage-composer-send" disabled={sendDisabled}>
							<span className="sr-only">{isWaiting ? "Sending" : "Send"}</span>
							<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round"><path d="M12 19V5M5.5 11.5 12 5l6.5 6.5" /></svg>
						</button>
					</div>
					<p id="triage-message-hint" className="sr-only">Enter to send, Shift + Enter for a new line.</p>
				</form>

				{showHero ? (
					<div className="triage-prompts" role="group" aria-label="Example messages">
						{suggestedPrompts.map((item, index) => {
							const look = promptStickers[index % promptStickers.length];
							return (
								<button
									key={item}
									type="button"
									className={`sticker tone-${look.tone} triage-prompt triage-prompt-${index + 1}`}
									style={{ "--tilt": `${look.tilt}deg`, "--depth": look.depth } as CSSProperties}
									onClick={() => setPrompt(item)}
									disabled={isWaiting}
								>
									<span className="triage-prompt-text">{item}</span>
								</button>
							);
						})}
					</div>
				) : null}
			</section>

			<aside className="triage-side" aria-label="Triage progress and safety">
				<div className="sticker tone-rose triage-safety">
					<h2 className="triage-safety-title">Stop and get help if</h2>
					<ul className="triage-safety-list">
						<li>pain is sharp, the joint gives way, or swelling comes on suddenly</li>
						<li>you have a fever, numbness, or weakness that is spreading</li>
						<li>there was a serious fall or injury: call emergency services</li>
					</ul>
				</div>

				{viewerRole === null ? (
					<p className="triage-guest">
						<Link href="/rehab/session">Try a demo session</Link>
					</p>
				) : null}
			</aside>
		</div>
	);
}

export default function HomePage() {
	return (
		<Suspense fallback={<p className="text-sm text-slate-500">Loading...</p>}>
			<HomePageContent />
		</Suspense>
	);
}
