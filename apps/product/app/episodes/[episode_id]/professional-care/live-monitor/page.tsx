"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

import { getCareEpisodeWorkspaceSummary, issueDoctorMonitorTicket, type CareEpisodeWorkspaceSummary } from "@rehab/shared/api";
import { loadAuth, type AuthState } from "@rehab/shared/auth";
import { connectDoctorEpisodeMonitor } from "@rehab/shared/ws";

import { CareEpisodeBrief } from "../../../../../components/CareEpisodeBrief";
import { AppButton, DashboardCard, SectionHeader, StatusBadge } from "../../../../../components/ui";

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
	if (!timestamp) return "No movement received yet";
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

	const latestEvent = recentEvents[0] || null;
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
		return <section className="card max-w-2xl text-sm text-rose-700">Login as a doctor to use Optional Live Movement Monitoring.</section>;
	}

	if (loading) return <p className="text-sm text-slate-500">Loading Care Episode...</p>;

	return (
		<div className="space-y-5">
			<section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm md:p-6">
				<div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
					<div>
						<p className="text-xs font-semibold uppercase tracking-wide text-emerald-700">Professional Care</p>
						<h1 className="mt-2 text-2xl font-semibold text-slate-950 md:text-3xl">Optional Live Movement Monitoring</h1>
						<p className="mt-2 max-w-3xl text-sm leading-6 text-slate-600">
							Review live movement signals for this Care Episode while the patient is actively streaming.
						</p>
					</div>
					<Link className="btn-secondary" href="/doctor">Return to Care Worklist</Link>
				</div>
			</section>

			{errorMessage ? <p className="rounded-md bg-rose-50 p-3 text-sm text-rose-700">{errorMessage}</p> : null}

			{workspace ? <CareEpisodeBrief workspace={workspace} role="doctor" compact /> : null}

			<section className="grid gap-3 md:grid-cols-4">
				<DashboardCard className="rounded-md p-4 shadow-none hover:border-slate-200 hover:shadow-none">
					<p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Connection</p>
					<StatusBadge className="mt-2" tone={connectionStatus === "connected" ? "success" : connectionStatus === "error" ? "risk" : "neutral"}>{statusMessage}</StatusBadge>
				</DashboardCard>
				<DashboardCard className="rounded-md p-4 shadow-none hover:border-slate-200 hover:shadow-none">
					<p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Latest signal</p>
					<p className="mt-2 text-sm text-slate-700">{latestEvent ? `Score ${latestEvent.current_score}` : "Patient not streaming"}</p>
				</DashboardCard>
				<DashboardCard className="rounded-md p-4 shadow-none hover:border-slate-200 hover:shadow-none">
					<p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Landmark count</p>
					<p className="mt-2 text-sm text-slate-700">{latestEvent?.coordinates?.length ?? 0}</p>
				</DashboardCard>
				<DashboardCard className="rounded-md p-4 shadow-none hover:border-slate-200 hover:shadow-none">
					<p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Last received</p>
					<p className="mt-2 text-sm text-slate-700">{formatReceivedTime(latestEvent?.timestamp)}</p>
				</DashboardCard>
			</section>

			<section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm md:p-6">
				<SectionHeader
					title="Recent movement events"
					description="Use this stream as a secondary review signal during an active Professional Care session."
					action={<AppButton onClick={() => void connect()} disabled={!workspace}>Connect to Optional Live Movement Monitoring</AppButton>}
				/>

				<div className="mt-4 max-h-96 space-y-2 overflow-auto">
					{orderedEvents.length === 0 ? <p className="rounded-md border border-dashed border-slate-300 p-4 text-sm text-slate-500">Patient not streaming</p> : null}
					{orderedEvents.map((evt, idx) => (
						<DashboardCard key={`${evt.timestamp || "event"}-${idx}`} className="rounded-md p-3 text-sm text-slate-600 shadow-none hover:border-slate-200 hover:shadow-none">
							<p><span className="font-semibold text-slate-950">Time:</span> {formatReceivedTime(evt.timestamp)}</p>
							<p><span className="font-semibold text-slate-950">Latest signal:</span> Score {evt.current_score}</p>
							<p><span className="font-semibold text-slate-950">Landmark count:</span> {evt.coordinates?.length ?? 0}</p>
						</DashboardCard>
					))}
				</div>
			</section>
		</div>
	);
}
