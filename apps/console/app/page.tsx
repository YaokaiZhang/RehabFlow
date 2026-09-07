"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import { issueDoctorMonitorTicket, issuePatientStreamTicket, linkPatient, login, registerDoctor, registerPatient, sendAiChatTurn } from "@rehab/shared/api";
import { fetchWithTimeout, getApiBase, getWsBase } from "@rehab/shared/runtime";
import type { AuthState } from "@rehab/shared/auth";
import { loadAuth, saveAuth } from "@rehab/shared/auth";
import { connectDoctorEpisodeMonitor, connectPatientStream } from "@rehab/shared/ws";
import { resolveAiChatSubmission, type AiChatSubmission } from "./ai-chat-submission";

type TabKey = "system" | "identity" | "ai" | "streams" | "knowledge";
type LogLevel = "info" | "ok" | "error";
type StreamRole = "patient" | "doctor";

type LogEntry = {
	id: string;
	time: string;
	level: LogLevel;
	message: string;
	data?: unknown;
};

type ChatMessage = {
	id: string;
	role: "user" | "assistant" | "system";
	text: string;
	latencyMs?: number;
	sources?: Array<{ label: string; title: string; url: string }>;
};

type DiagnosticResult = {
	status?: string;
	latency_ms?: number;
	[key: string]: unknown;
};


const tabs: Array<{ key: TabKey; label: string }> = [
	{ key: "system", label: "System" },
	{ key: "identity", label: "Identity" },
	{ key: "ai", label: "AI Chat" },
	{ key: "streams", label: "Streams" },
	{ key: "knowledge", label: "Knowledge" },
];

function nowLabel(): string {
	return new Date().toLocaleTimeString();
}

function makeId(prefix: string): string {
	return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function formatJson(value: unknown): string {
	try {
		return JSON.stringify(value, null, 2);
	} catch {
		return String(value);
	}
}

function statusClass(level: LogLevel): string {
	if (level === "ok") return "border-emerald-200 bg-emerald-50 text-emerald-800";
	if (level === "error") return "border-rose-200 bg-rose-50 text-rose-800";
	return "border-slate-200 bg-slate-50 text-slate-700";
}

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
	const response = await fetchWithTimeout(`${getApiBase()}${path}`, {
		...init,
		headers: {
			"Content-Type": "application/json",
			...(init?.headers || {}),
		},
	});

	const text = await response.text();
	const data = text ? JSON.parse(text) : null;
	if (!response.ok) {
		const detail = typeof data?.detail === "string" ? data.detail : `Request failed (${response.status})`;
		throw new Error(detail);
	}
	return data as T;
}

function makeCoordinates(frame: number) {
	return Array.from({ length: 33 }, (_, index) => {
		const phase = frame / 8 + index * 0.17;
		return {
			x: Number((0.5 + Math.sin(phase) * 0.18).toFixed(4)),
			y: Number((0.5 + Math.cos(phase * 0.8) * 0.22).toFixed(4)),
			z: Number((Math.sin(phase * 0.5) * 0.08).toFixed(4)),
		};
	});
}

export default function TestConsolePage() {
	const patientSocketRef = useRef<WebSocket | null>(null);
	const patientStreamAttemptRef = useRef(0);
	const doctorSocketRef = useRef<WebSocket | null>(null);
	const doctorStreamAttemptRef = useRef(0);
	const frameTimerRef = useRef<number | null>(null);
	const frameRef = useRef(0);
	const pendingSubmissionRef = useRef<AiChatSubmission | null>(null);

	const [activeTab, setActiveTab] = useState<TabKey>("system");
	const [logs, setLogs] = useState<LogEntry[]>([]);
	const [systemResults, setSystemResults] = useState<Record<string, unknown>>({});
	const [patientAuth, setPatientAuth] = useState<AuthState | null>(null);
	const [doctorAuth, setDoctorAuth] = useState<AuthState | null>(null);
	const [savedAuth, setSavedAuth] = useState<AuthState | null>(null);
	const [password, setPassword] = useState("rehab-demo-123");
	const [patientName, setPatientName] = useState(() => `patient_${Date.now()}`);
	const [doctorName, setDoctorName] = useState(() => `doctor_${Date.now()}`);
	const [loginRole, setLoginRole] = useState<"patient" | "doctor">("patient");
	const [loginUsername, setLoginUsername] = useState("");
	const [loginPassword, setLoginPassword] = useState("rehab-demo-123");
	const [bindingStatus, setBindingStatus] = useState("Idle");
	const [aiSessionId, setAiSessionId] = useState("new");
	const [aiPrompt, setAiPrompt] = useState("I have mild knee stiffness after ACL rehab. What should I do today?");
	const [aiStatus, setAiStatus] = useState("Ready");
	const [aiSending, setAiSending] = useState(false);
	const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
	const [patientStreamId, setPatientStreamId] = useState("");
	const [doctorStreamId, setDoctorStreamId] = useState("");
	const [careEpisodeId, setCareEpisodeId] = useState("");
	const careEpisodeIdRef = useRef("");
	careEpisodeIdRef.current = careEpisodeId;
	const [patientStreamStatus, setPatientStreamStatus] = useState("Disconnected");
	const [doctorStreamStatus, setDoctorStreamStatus] = useState("Disconnected");
	const [lastScore, setLastScore] = useState<number | null>(null);
	const [streamEvents, setStreamEvents] = useState<Array<{ role: StreamRole; data: unknown }>>([]);
	const [knowledgeQuery, setKnowledgeQuery] = useState("knee stiffness ACL rehab squat");
	const [embeddingText, setEmbeddingText] = useState("knee stiffness ACL rehab");
	const [knowledgeResult, setKnowledgeResult] = useState<unknown>(null);
	const [runtimeApiBase, setRuntimeApiBase] = useState("Resolving backend target...");
	const [runtimeWsBase, setRuntimeWsBase] = useState("Resolving WebSocket target...");

	const syncSavedAuthStatus = () => {
		const currentAuth = loadAuth();
		setSavedAuth(currentAuth);
		setAiStatus(currentAuth?.role === "patient" ? "Ready" : "Sign in as patient");
	};

	const appendLog = (level: LogLevel, message: string, data?: unknown) => {
		setLogs((prev) => [{ id: makeId("log"), time: nowLabel(), level, message, data }, ...prev].slice(0, 120));
	};

	useEffect(() => {
		setRuntimeApiBase(getApiBase());
		setRuntimeWsBase(getWsBase());
		syncSavedAuthStatus();
		const auth = loadAuth();
		if (auth?.role === "patient") {
			setPatientAuth(auth);
			setPatientStreamId(auth.user_id);
		}
		if (auth?.role === "doctor") {
			setDoctorAuth(auth);
			setDoctorStreamId(auth.user_id);
		}
	}, []);

	useEffect(() => {
		if (patientAuth?.user_id) {
			setPatientStreamId(patientAuth.user_id);
			const currentAuth = loadAuth();
			setAiStatus(currentAuth?.role === "patient" ? "Ready" : "Sign in as patient");
		}
	}, [patientAuth]);

	useEffect(() => {
		if (doctorAuth?.user_id) setDoctorStreamId(doctorAuth.user_id);
	}, [doctorAuth]);

	useEffect(() => {
		return () => {
			stopSyntheticFrames();
			patientStreamAttemptRef.current += 1;
			doctorStreamAttemptRef.current += 1;
			closeSocket(patientSocketRef.current);
			closeSocket(doctorSocketRef.current);
			patientSocketRef.current = null;
			doctorSocketRef.current = null;
		};
	}, []);

	const latestIds = useMemo(
		() => [
			{ label: "Patient", value: patientAuth?.user_id || patientStreamId || "Not set" },
			{ label: "Doctor", value: doctorAuth?.user_id || doctorStreamId || "Not set" },
			{ label: "AI session", value: aiSessionId || "new" },
		],
		[aiSessionId, doctorAuth, doctorStreamId, patientAuth, patientStreamId]
	);

	const runRequest = async (label: string, fn: () => Promise<unknown>) => {
		appendLog("info", `${label} started`);
		try {
			const started = performance.now();
			const result = await fn();
			appendLog("ok", `${label} completed in ${Math.round(performance.now() - started)} ms`, result);
			return result;
		} catch (error) {
			appendLog("error", `${label} failed`, (error as Error).message);
			throw error;
		}
	};

	const runSystemChecks = async () => {
		const entries = await Promise.allSettled([
			runRequest("API health", () => fetchJson<DiagnosticResult>("/health")),
			runRequest("AI chat health", () => fetchJson<DiagnosticResult>("/ai/chat/health")),
			runRequest("Diagnostics config", () => fetchJson<DiagnosticResult>("/diagnostics/config")),
			runRequest("Database check", () => fetchJson<DiagnosticResult>("/diagnostics/db")),
		]);
		setSystemResults({
			health: entries[0].status === "fulfilled" ? entries[0].value : entries[0].reason,
			ai_chat: entries[1].status === "fulfilled" ? entries[1].value : entries[1].reason,
			config: entries[2].status === "fulfilled" ? entries[2].value : entries[2].reason,
			database: entries[3].status === "fulfilled" ? entries[3].value : entries[3].reason,
		});
	};

	const createPatient = async () => {
		const auth = (await runRequest("Register patient", () =>
			registerPatient({ patient_name: patientName, password, real_info: { source: "test-console" } })
		)) as AuthState;
		setPatientAuth(auth);
		saveAuth(auth);
		syncSavedAuthStatus();
	};

	const createDoctor = async () => {
		const auth = (await runRequest("Register doctor", () =>
			registerDoctor({ doctor_name: doctorName, password, real_info: { source: "test-console" } })
		)) as AuthState;
		setDoctorAuth(auth);
		saveAuth(auth);
		syncSavedAuthStatus();
	};

	const runLogin = async () => {
		const auth = (await runRequest("Login", () => login({ role: loginRole, username: loginUsername, password: loginPassword }))) as AuthState;
		if (auth.role === "patient") setPatientAuth(auth);
		if (auth.role === "doctor") setDoctorAuth(auth);
		saveAuth(auth);
		syncSavedAuthStatus();
	};

	const bindDoctorPatient = async () => {
		if (!doctorAuth?.access_token || !patientAuth?.user_id) {
			appendLog("error", "Binding needs a registered/logged-in doctor and patient");
			return;
		}
		setBindingStatus("Binding...");
		await runRequest("Bind doctor to patient", () => linkPatient(patientAuth.user_id, doctorAuth.access_token));
		setBindingStatus("Bound");
	};


	const sendAiMessage = async () => {
		const text = aiPrompt.trim();
		if (!text || aiSending) return;

		const auth = loadAuth();
		if (!auth || auth.role !== "patient" || !auth.access_token) {
			const detail = "Please sign in as a patient before sending AI chat messages.";
			setAiStatus("Sign in as patient");
			setChatMessages((prev) => [...prev, { id: makeId("chat"), role: "system", text: detail }]);
			appendLog("error", detail);
			return;
		}

		const { submission, isRetry } = resolveAiChatSubmission(
			pendingSubmissionRef.current,
			text,
			aiSessionId,
			() => crypto.randomUUID(),
		);
		pendingSubmissionRef.current = submission;

		const started = performance.now();
		setAiSending(true);
		setAiStatus("Sending...");
		if (!isRetry) {
			setChatMessages((prev) => [...prev, { id: makeId("chat"), role: "user", text: submission.message }]);
		}
		setAiPrompt((current) => (current.trim() === "" || current.trim() === submission.message ? "" : current));

		try {
			const result = await sendAiChatTurn(
				submission.sessionId,
				{
					message: submission.message,
					idempotency_key: submission.idempotencyKey,
				},
				auth.access_token,
			);
			const latencyMs = Math.round(performance.now() - started);
			const sessionId = result.session_id ? String(result.session_id) : submission.sessionId;
			if (result.session_id) setAiSessionId(sessionId);

			if (result.event === "error" || !result.response) {
				throw new Error(result.detail || "No response generated.");
			}

			pendingSubmissionRef.current = null;
			setChatMessages((prev) => [
				...prev,
				{ id: makeId("chat"), role: "assistant", text: result.response || "No response generated.", latencyMs, sources: result.sources },
			]);
			setAiStatus("Ready");
			appendLog("ok", "AI response received in " + latencyMs + " ms", {
				session_id: sessionId,
				response_preview: result.response.slice(0, 700),
			});
		} catch (error) {
			const detail = error instanceof Error ? error.message : "AI chat request failed.";
			setAiStatus("Error");
			setAiPrompt((current) => (current.trim() ? current : submission.message));
			setChatMessages((prev) => [...prev, { id: makeId("chat"), role: "system", text: detail }]);
			appendLog("error", "AI chat request failed", detail);
		} finally {
			setAiSending(false);
		}
	};

	const closePatientStream = () => {
		patientStreamAttemptRef.current += 1;
		closeSocket(patientSocketRef.current);
		patientSocketRef.current = null;
		setPatientStreamStatus("Disconnected");
	};

	const closeDoctorStream = () => {
		doctorStreamAttemptRef.current += 1;
		closeSocket(doctorSocketRef.current);
		doctorSocketRef.current = null;
		setDoctorStreamStatus("Disconnected");
	};

	const startPatientStream = async () => {
		const attempt = patientStreamAttemptRef.current + 1;
		patientStreamAttemptRef.current = attempt;
		const auth = loadAuth();
		const authSnapshot = auth && auth.role === "patient" && auth.access_token ? {
			role: auth.role,
			userId: auth.user_id,
			accessToken: auth.access_token,
		} : null;
		const isCurrentAttempt = () => {
			const currentAuth = loadAuth();
			return patientStreamAttemptRef.current === attempt
				&& authSnapshot !== null
				&& currentAuth?.role === authSnapshot.role
				&& currentAuth.user_id === authSnapshot.userId
				&& currentAuth.access_token === authSnapshot.accessToken;
		};
		if (!authSnapshot) {
			closeSocket(patientSocketRef.current);
			patientSocketRef.current = null;
			appendLog("error", "Patient movement needs patient authentication");
			setPatientStreamStatus("Authentication required");
			return;
		}
		closeSocket(patientSocketRef.current);
		patientSocketRef.current = null;
		setPatientStreamStatus("Issuing ticket...");
		try {
			const ticket = await issuePatientStreamTicket(authSnapshot.accessToken);
			if (!ticket || !isCurrentAttempt()) return;
			const socket = connectPatientStream(ticket);
			if (!isCurrentAttempt()) {
				closeSocket(socket);
				return;
			}
			patientSocketRef.current = socket;
			setPatientStreamStatus("Connecting...");
			socket.onopen = () => {
				if (!isCurrentAttempt() || patientSocketRef.current !== socket) return;
				setPatientStreamStatus("Connected");
				appendLog("ok", "Patient movement socket connected");
			};
			socket.onerror = () => {
				if (!isCurrentAttempt() || patientSocketRef.current !== socket) return;
				setPatientStreamStatus("Socket error");
				appendLog("error", "Patient movement socket error");
			};
			socket.onclose = () => {
				if (!isCurrentAttempt() || patientSocketRef.current !== socket) return;
				setPatientStreamStatus("Disconnected");
			};
			socket.onmessage = (event) => {
				if (!isCurrentAttempt() || patientSocketRef.current !== socket) return;
				const data = parseSocketData(event.data);
				if (data && typeof data === "object" && "current_score" in data && typeof data.current_score === "number") {
					setLastScore(data.current_score);
				}
				setStreamEvents((prev) => [{ role: "patient" as const, data }, ...prev].slice(0, 60));
			};
		} catch (error) {
			if (!isCurrentAttempt()) return;
			setPatientStreamStatus("Ticket error");
			appendLog("error", "Patient movement ticket failed", (error as Error).message);
		}
	};

	const startDoctorStream = async () => {
		const attempt = doctorStreamAttemptRef.current + 1;
		doctorStreamAttemptRef.current = attempt;
		const auth = loadAuth();
		const careEpisodeSnapshot = careEpisodeId;
		const authSnapshot = auth && auth.role === "doctor" && auth.access_token && careEpisodeSnapshot.trim() ? {
			role: auth.role,
			userId: auth.user_id,
			accessToken: auth.access_token,
		} : null;
		const isCurrentAttempt = () => {
			const currentAuth = loadAuth();
			return doctorStreamAttemptRef.current === attempt
				&& careEpisodeIdRef.current === careEpisodeSnapshot
				&& authSnapshot !== null
				&& currentAuth?.role === authSnapshot.role
				&& currentAuth.user_id === authSnapshot.userId
				&& currentAuth.access_token === authSnapshot.accessToken;
		};
		if (!authSnapshot) {
			closeSocket(doctorSocketRef.current);
			doctorSocketRef.current = null;
			if (!auth || auth.role !== "doctor" || !auth.access_token) {
				appendLog("error", "Doctor monitor needs doctor authentication");
				setDoctorStreamStatus("Authentication required");
				return;
			}
			appendLog("error", "Doctor monitor needs a Care Episode ID");
			return;
		}
		closeSocket(doctorSocketRef.current);
		doctorSocketRef.current = null;
		setDoctorStreamStatus("Issuing ticket...");
		try {
			const ticket = await issueDoctorMonitorTicket(careEpisodeSnapshot, authSnapshot.accessToken);
			if (!ticket || !isCurrentAttempt()) return;
			const socket = connectDoctorEpisodeMonitor(careEpisodeSnapshot, ticket);
			if (!isCurrentAttempt()) {
				closeSocket(socket);
				return;
			}
			doctorSocketRef.current = socket;
			setDoctorStreamStatus("Connecting...");
			socket.onopen = () => {
				if (!isCurrentAttempt() || doctorSocketRef.current !== socket) return;
				setDoctorStreamStatus("Connected");
				appendLog("ok", "Doctor monitor socket connected");
			};
			socket.onerror = () => {
				if (!isCurrentAttempt() || doctorSocketRef.current !== socket) return;
				setDoctorStreamStatus("Socket error");
				appendLog("error", "Doctor monitor socket error");
			};
			socket.onclose = () => {
				if (!isCurrentAttempt() || doctorSocketRef.current !== socket) return;
				setDoctorStreamStatus("Disconnected");
			};
			socket.onmessage = (event) => {
				if (!isCurrentAttempt() || doctorSocketRef.current !== socket) return;
				const data = parseSocketData(event.data);
				setStreamEvents((prev) => [{ role: "doctor" as const, data }, ...prev].slice(0, 60));
				appendLog("info", "Doctor monitor event", data);
			};
		} catch (error) {
			if (!isCurrentAttempt()) return;
			setDoctorStreamStatus("Ticket error");
			appendLog("error", "Doctor monitor ticket failed", (error as Error).message);
		}
	};

	const sendSyntheticFrame = () => {
		const socket = patientSocketRef.current;
		if (!socket || socket.readyState !== WebSocket.OPEN) {
			appendLog("error", "Patient movement socket is not connected");
			return;
		}
		frameRef.current += 1;
		socket.send(JSON.stringify({ coordinates: makeCoordinates(frameRef.current) }));
	};

	const startSyntheticFrames = () => {
		stopSyntheticFrames();
		sendSyntheticFrame();
		frameTimerRef.current = window.setInterval(sendSyntheticFrame, 900);
		appendLog("info", "Synthetic movement frames started");
	};

	function stopSyntheticFrames() {
		if (frameTimerRef.current) {
			window.clearInterval(frameTimerRef.current);
			frameTimerRef.current = null;
		}
	}

	const runEmbeddingCheck = async () => {
		const result = await runRequest("Embedding check", () =>
			fetchJson<DiagnosticResult>("/diagnostics/embedding", {
				method: "POST",
				body: JSON.stringify({ text: embeddingText }),
			})
		);
		setKnowledgeResult(result);
	};

	const runQdrantSearch = async () => {
		const result = await runRequest("Qdrant search", () =>
			fetchJson<DiagnosticResult>("/diagnostics/qdrant/search", {
				method: "POST",
				body: JSON.stringify({ query: knowledgeQuery, limit: 5 }),
			})
		);
		setKnowledgeResult(result);
	};

	return (
		<div className="space-y-4">
			<section className="panel">
				<div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
					<div>
						<h1 className="text-xl font-semibold">Interactive Test Console</h1>
						<p className="mt-1 text-sm text-slate-600">API: {runtimeApiBase} | WS: {runtimeWsBase}</p>
					</div>
					<div className="grid gap-2 text-xs sm:grid-cols-3">
						{latestIds.map((item) => (
							<div key={item.label} className="rounded-md border border-slate-200 bg-white px-3 py-2">
								<p className="font-medium text-slate-500">{item.label}</p>
								<p className="mt-1 max-w-52 truncate text-slate-900">{item.value}</p>
							</div>
						))}
					</div>
				</div>
			</section>

			<div className="flex flex-wrap gap-2 border-b border-slate-200 pb-2">
				{tabs.map((tab) => (
					<button
						key={tab.key}
						onClick={() => setActiveTab(tab.key)}
						className={`tab-button ${activeTab === tab.key ? "tab-button-active" : ""}`}
					>
						{tab.label}
					</button>
				))}
			</div>

			{activeTab === "system" ? (
				<div className="grid gap-4 xl:grid-cols-[1fr_420px]">
					<section className="panel">
						<div className="flex items-center justify-between gap-3">
							<h2 className="section-title">Backend Checks</h2>
							<button className="btn-primary" onClick={runSystemChecks}>Run All</button>
						</div>
						<pre className="result-box mt-4">{formatJson(systemResults)}</pre>
					</section>
					<LogPanel logs={logs} />
				</div>
			) : null}

			{activeTab === "identity" ? (
				<div className="grid gap-4 xl:grid-cols-[1fr_420px]">
					<section className="grid gap-4 lg:grid-cols-2">
						<div className="panel">
							<h2 className="section-title">Create Users</h2>
							<label className="field-label">Shared password</label>
							<input className="input" value={password} onChange={(e) => setPassword(e.target.value)} />
							<label className="field-label">Patient username</label>
							<input className="input" value={patientName} onChange={(e) => setPatientName(e.target.value)} />
							<button className="btn-primary mt-3" onClick={createPatient}>Register Patient</button>
							<label className="field-label mt-5">Doctor username</label>
							<input className="input" value={doctorName} onChange={(e) => setDoctorName(e.target.value)} />
							<button className="btn-secondary mt-3" onClick={createDoctor}>Register Doctor</button>
						</div>
						<div className="panel">
							<h2 className="section-title">Login And Bind</h2>
							<div className="segmented mt-3">
								<button className={loginRole === "patient" ? "selected" : ""} onClick={() => setLoginRole("patient")}>Patient</button>
								<button className={loginRole === "doctor" ? "selected" : ""} onClick={() => setLoginRole("doctor")}>Doctor</button>
							</div>
							<label className="field-label">Username</label>
							<input className="input" value={loginUsername} onChange={(e) => setLoginUsername(e.target.value)} />
							<label className="field-label">Password</label>
							<input className="input" value={loginPassword} onChange={(e) => setLoginPassword(e.target.value)} />
							<div className="mt-3 flex flex-wrap gap-2">
								<button className="btn-primary" onClick={runLogin}>Login</button>
								<button className="btn-secondary" onClick={bindDoctorPatient}>Bind Doctor To Patient</button>
							</div>
							<p className="mt-3 text-sm text-slate-600">Binding status: {bindingStatus}</p>
							<pre className="result-box mt-4">{formatJson({ savedAuth, patientAuth, doctorAuth })}</pre>
						</div>
					</section>
					<LogPanel logs={logs} />
				</div>
			) : null}

			{activeTab === "ai" ? (
				<div className="grid gap-4 xl:grid-cols-[1fr_420px]">
					<section className="panel">
						<div className="grid gap-3 lg:grid-cols-[1fr_auto]">
							<div>
								<label className="field-label">Session</label>
								<input className="input" value={aiSessionId} onChange={(e) => setAiSessionId(e.target.value)} />
							</div>
							<p className="flex items-end text-sm text-slate-600">Authenticated patient HTTP transport</p>
						</div>
						<p className="mt-3 text-sm text-slate-600">Status: {aiStatus}</p>
						<div className="mt-4 min-h-80 rounded-md border border-slate-200 bg-slate-50 p-3">
							{chatMessages.length === 0 ? <p className="text-sm text-slate-500">No messages yet.</p> : null}
							<div className="space-y-3">
								{chatMessages.map((message) => (
									<div key={message.id} className={`chat-bubble ${message.role === "user" ? "chat-user" : "chat-assistant"}`}>
										<p className="text-xs font-semibold uppercase tracking-wide text-slate-500">{message.role}{message.latencyMs ? ` | ${message.latencyMs} ms` : ""}</p>
										<p className="mt-1 whitespace-pre-wrap text-sm">{message.text}</p>
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
							</div>
						</div>
						<div className="mt-3 flex gap-2">
							<textarea className="textarea" value={aiPrompt} onChange={(e) => setAiPrompt(e.target.value)} />
							<button className="btn-primary min-w-24" onClick={sendAiMessage} disabled={aiSending}>{aiSending ? "Sending..." : "Send"}</button>
						</div>
					</section>
					<LogPanel logs={logs} />
				</div>
			) : null}
			{activeTab === "streams" ? (
				<div className="grid gap-4 xl:grid-cols-[1fr_420px]">
					<section className="grid gap-4 lg:grid-cols-2">
						<div className="panel">
							<h2 className="section-title">Patient Movement Stream</h2>
							<p className="text-sm text-slate-600">Uses the currently signed-in patient account: {patientAuth?.username || "Not signed in"}</p>
							<p className="mt-3 text-sm text-slate-600">Status: {patientStreamStatus}</p>
							<p className="mt-1 text-sm text-slate-600">Last score: {lastScore ?? "None"}</p>
							<div className="mt-3 flex flex-wrap gap-2">
								<button className="btn-primary" onClick={() => void startPatientStream()}>Connect</button>
								<button className="btn-secondary" onClick={sendSyntheticFrame}>Send Frame</button>
								<button className="btn-secondary" onClick={startSyntheticFrames}>Start Loop</button>
								<button className="btn-secondary" onClick={stopSyntheticFrames}>Stop Loop</button>
								<button className="btn-secondary" onClick={closePatientStream}>Close</button>
							</div>
						</div>
						<div className="panel">
							<h2 className="section-title">Doctor Monitor Stream</h2>
							<label className="field-label">Doctor UUID</label>
							<input className="input" value={doctorStreamId} onChange={(e) => setDoctorStreamId(e.target.value)} />
							<label className="field-label">Care Episode ID</label>
							<input className="input" value={careEpisodeId} onChange={(e) => setCareEpisodeId(e.target.value)} />
							<p className="mt-3 text-sm text-slate-600">Status: {doctorStreamStatus}</p>
							<div className="mt-3 flex flex-wrap gap-2">
								<button className="btn-primary" onClick={() => void startDoctorStream()}>Connect</button>
								<button className="btn-secondary" onClick={closeDoctorStream}>Close</button>
							</div>
						</div>
						<div className="panel lg:col-span-2">
							<h2 className="section-title">Stream Events</h2>
							<div className="mt-3 max-h-96 space-y-2 overflow-auto">
								{streamEvents.map((event, index) => (
									<div key={`${event.role}-${index}`} className="rounded-md border border-slate-200 bg-slate-50 p-2 text-xs">
										<p className="font-semibold uppercase text-slate-500">{event.role}</p>
										<pre className="mt-1 whitespace-pre-wrap break-words">{formatJson(event.data)}</pre>
									</div>
								))}
							</div>
						</div>
					</section>
					<LogPanel logs={logs} />
				</div>
			) : null}

			{activeTab === "knowledge" ? (
				<div className="grid gap-4 xl:grid-cols-[1fr_420px]">
					<section className="grid gap-4 lg:grid-cols-2">
						<div className="panel">
							<h2 className="section-title">Embedding</h2>
							<label className="field-label">Text</label>
							<textarea className="textarea min-h-28" value={embeddingText} onChange={(e) => setEmbeddingText(e.target.value)} />
							<button className="btn-primary mt-3" onClick={runEmbeddingCheck}>Run Embedding Check</button>
						</div>
						<div className="panel">
							<h2 className="section-title">Qdrant Search</h2>
							<label className="field-label">Query</label>
							<textarea className="textarea min-h-28" value={knowledgeQuery} onChange={(e) => setKnowledgeQuery(e.target.value)} />
							<button className="btn-primary mt-3" onClick={runQdrantSearch}>Search Knowledge</button>
						</div>
						<div className="panel lg:col-span-2">
							<h2 className="section-title">Knowledge Result</h2>
							<pre className="result-box mt-3">{formatJson(knowledgeResult)}</pre>
						</div>
					</section>
					<LogPanel logs={logs} />
				</div>
			) : null}
		</div>
	);
}

function parseSocketData(raw: string): Record<string, unknown> | string {
	try {
		return JSON.parse(raw) as Record<string, unknown>;
	} catch {
		return raw;
	}
}

function closeSocket(socket: WebSocket | null) {
	if (socket && socket.readyState <= WebSocket.OPEN) socket.close();
}

function LogPanel({ logs }: { logs: LogEntry[] }) {
	return (
		<section className="panel">
			<h2 className="section-title">Event Log</h2>
			<div className="mt-3 max-h-[640px] space-y-2 overflow-auto">
				{logs.length === 0 ? <p className="text-sm text-slate-500">No events yet.</p> : null}
				{logs.map((log) => (
					<div key={log.id} className={`rounded-md border p-2 text-xs ${statusClass(log.level)}`}>
						<div className="flex items-center justify-between gap-2">
							<p className="font-semibold uppercase">{log.level}</p>
							<p>{log.time}</p>
						</div>
						<p className="mt-1 text-sm">{log.message}</p>
						{log.data !== undefined ? <pre className="mt-2 whitespace-pre-wrap break-words">{formatJson(log.data)}</pre> : null}
					</div>
				))}
			</div>
		</section>
	);
}
