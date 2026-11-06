"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

import { getCareEpisodeWorkspaceSummary, issueDoctorMonitorTicket, type CareEpisodeWorkspaceSummary } from "@rehab/shared/api";
import { loadAuth, type AuthState } from "@rehab/shared/auth";
import { connectDoctorEpisodeMonitor } from "@rehab/shared/ws";

import { CareEpisodeBrief } from "../../../../../components/CareEpisodeBrief";
import { AppButton, StatusBadge } from "../../../../../components/ui";

type Params = { episode_id?: string };

type MonitorPayload = {
	event?: string;
	patient_id?: string;
	timestamp?: string;
	coordinates?: Array<{ x: number; y: number; z: number; visibility?: number }>;
	current_score: number;
};

type StatusPayload = { event?: string; detail?: string; channel?: string };
type ConnectionStatus = "idle" | "connecting" | "connected" | "error" | "disconnected";

function formatReceivedTime(timestamp?: string) {
	if (!timestamp) return "—";
	return new Date(timestamp).toLocaleString();
}

export default function ProfessionalCareLiveMonitorPage() {
	const params = useParams<Params>();
	const episodeId = params?.episode_id || "";
	const wsRef = useRef<WebSocket | null>(null);
	const terminalStatusRef = useRef("");
	const monitorAttemptRef = useRef(0);

	const [auth, setAuth] = useState<AuthState | null>(null);
	const [workspace, setWorkspace] = useState<CareEpisodeWorkspaceSummary | null>(null);
	const [loading, setLoading] = useState(true);
	const [errorMessage, setErrorMessage] = useState("");
	const [statusMessage, setStatusMessage] = useState("Not connected");
	const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>("idle");
	const [recentEvents, setRecentEvents] = useState<MonitorPayload[]>([]);

	useEffect(() => {
		const nextAuth = loadAuth();
		setAuth(nextAuth);
		if (!nextAuth || nextAuth.role !== "doctor") {
			setLoading(false);
			return;
		}
		if (!episodeId) {
			setErrorMessage("Missing Care Episode.");
			setLoading(false);
			return;
		}
		getCareEpisodeWorkspaceSummary(episodeId, nextAuth.access_token)
			.then(setWorkspace)
			.catch((err) => setErrorMessage((err as Error).message))
			.finally(() => setLoading(false));
	}, [episodeId]);

	useEffect(() => {
		return () => {
			monitorAttemptRef.current += 1;
			if (wsRef.current && wsRef.current.readyState <= 1) wsRef.current.close();
			wsRef.current = null;
		};
	}, []);

	const orderedEvents = useMemo(() => recentEvents.slice(0, 12), [recentEvents]);

	const handleMonitorMessage = (ws: WebSocket, event: MessageEvent<string>) => {
		if (wsRef.current !== ws) return;
		try {
			const data = JSON.parse(event.data) as MonitorPayload | StatusPayload;
			if ("current_score" in data && typeof data.current_score === "number") {
				setRecentEvents((prev) => [data, ...prev].slice(0, 50));
				setConnectionStatus("connected");
				setStatusMessage("Receiving patient movement stream");
				return;
			}
			if (data.event === "subscribed") {
				setConnectionStatus("connected");
				setStatusMessage("Connected to Care Episode monitor");
				return;
			}
			if ("detail" in data && data.detail) {
				terminalStatusRef.current = data.detail;
				setStatusMessage(data.detail);
			}
		} catch {
			// Ignore non-JSON websocket keepalive payloads.
		}
	};

	const connect = async () => {
		const attempt = monitorAttemptRef.current + 1;
		monitorAttemptRef.current = attempt;
		if (!episodeId) return;
		const nextAuth = loadAuth();
		if (!nextAuth || nextAuth.role !== "doctor" || !nextAuth.access_token) {
			setErrorMessage("Login as a doctor to use Optional Live Movement Monitoring.");
			setConnectionStatus("error");
			return;
		}
		if (wsRef.current && wsRef.current.readyState <= 1) {
			wsRef.current.onopen = null;
			wsRef.current.onerror = null;
			wsRef.current.onclose = null;
			wsRef.current.onmessage = null;
			wsRef.current.close();
		}
		wsRef.current = null;
		setErrorMessage("");
		setStatusMessage("Connecting to Care Episode monitor...");
		setConnectionStatus("connecting");
		terminalStatusRef.current = "";

		try {
			const ticket = await issueDoctorMonitorTicket(episodeId, nextAuth.access_token);
			if (attempt !== monitorAttemptRef.current || !ticket) return;
			const ws = connectDoctorEpisodeMonitor(episodeId, ticket);
			wsRef.current = ws;
			ws.onopen = () => {
				if (wsRef.current !== ws) return;
				setConnectionStatus("connected");
				setStatusMessage("Connected to Care Episode monitor");
			};
			ws.onerror = () => {
				if (wsRef.current !== ws) return;
				setConnectionStatus("error");
				setStatusMessage("Live movement monitor socket error");
			};
			ws.onclose = () => {
				if (wsRef.current !== ws) return;
				setConnectionStatus("disconnected");
				if (!terminalStatusRef.current) setStatusMessage("Disconnected from Care Episode monitor");
			};
			ws.onmessage = (event) => handleMonitorMessage(ws, event);
		} catch (err) {
			if (attempt !== monitorAttemptRef.current) return;
			setConnectionStatus("error");
			setStatusMessage("Unable to connect to Care Episode monitor");
			setErrorMessage((err as Error).message);
		}
	};

	if (!auth || auth.role !== "doctor") {
		return <section className="page-column-narrow"><p className="notice">Log in as a doctor to use Optional Live Movement Monitoring.</p></section>;
	}

	if (loading) return <p className="ep-loading">Loading Care Episode...</p>;

	const connectionTone = connectionStatus === "connected" ? "success" : connectionStatus === "error" ? "risk" : "neutral";

	return (
		<div className="page-column ep-page">
			<header className="page-header" data-tone="sky">
				<h1>Optional Live Movement Monitoring</h1>
				<div className="ep-header-links">
					<Link className="btn-secondary" href="/doctor">Back to Care Worklist</Link>
				</div>
			</header>

			{errorMessage ? <p className="notice notice-danger">{errorMessage}</p> : null}

			{workspace ? <CareEpisodeBrief workspace={workspace} role="doctor" compact /> : null}

			<section className="ep-monitor-stats" aria-label="Monitor status">
				<div className="ep-stat ep-stat-sky">
					<span className="ep-stat-label">Connection</span>
					<StatusBadge tone={connectionTone}>{statusMessage}</StatusBadge>
				</div>
			</section>

			<section className="card" aria-labelledby="ep-events-title">
				<div className="ep-section-head">
					<div className="min-w-0">
						<h2 id="ep-events-title" className="section-title">Recent movement events</h2>
						<p className="mt-1 max-w-[62ch] text-sm text-[var(--rf-ink-muted)]">Scores support your judgement; they don&apos;t replace watching the movement.</p>
					</div>
					<AppButton onClick={() => void connect()} disabled={!workspace}>Connect</AppButton>
				</div>

				{orderedEvents.length === 0 ? <p className="ep-empty mt-4">the patient isn&apos;t streaming yet</p> : null}
				{orderedEvents.length ? (
					<ul className="ep-events">
						{orderedEvents.map((evt, idx) => (
							<li key={`${evt.timestamp || "event"}-${idx}`} className="ep-event">
								<span className="ep-event-score" aria-label={`Score ${evt.current_score}`}>{evt.current_score}</span>
								<span className="ep-event-time">{formatReceivedTime(evt.timestamp)}</span>
								<span className="ep-event-points">{evt.coordinates?.length ?? 0} pose points</span>
							</li>
						))}
					</ul>
				) : null}
			</section>
		</div>
	);
}
