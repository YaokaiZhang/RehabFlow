"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { deleteCareEpisode, listCareEpisodes, type CareEpisode } from "@rehab/shared/api";
import { loadAuth, type AuthState } from "@rehab/shared/auth";

import { AppButton, DashboardCard, SectionHeader, StatusBadge } from "../../components/ui";

function safetyCopy(status: CareEpisode["safety_gate_status"]) {
	if (status === "needs_triage") return "Needs AI Triage Intake before AI Daily Rehab";
	if (status === "clinician_reviewed") return "Clinician-reviewed context available";
	return "AI triage context available";
}

function safetyTone(status: CareEpisode["safety_gate_status"]) {
	if (status === "needs_triage") return "attention";
	if (status === "clinician_reviewed") return "success";
	return "info";
}

function statusCopy(status: CareEpisode["status"]) {
	return status
		.split("_")
		.filter(Boolean)
		.map((part) => part.charAt(0).toUpperCase() + part.slice(1))
		.join(" ") || "Active";
}

function formatDate(value?: string | null) {
	if (!value) return "Not recorded";
	const date = new Date(value);
	if (Number.isNaN(date.getTime())) return "Not recorded";
	return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" }).format(date);
}

export default function PatientDashboardPage() {
	const [auth, setAuth] = useState<AuthState | null>(null);
	const [episodes, setEpisodes] = useState<CareEpisode[]>([]);
	const [loading, setLoading] = useState(true);
	const [episodesLoaded, setEpisodesLoaded] = useState(false);
	const [error, setError] = useState("");
	const [statusMessage, setStatusMessage] = useState("");
	const [deletingEpisodeId, setDeletingEpisodeId] = useState<string | null>(null);

	useEffect(() => {
		const nextAuth = loadAuth();
		setAuth(nextAuth);
		if (!nextAuth || nextAuth.role !== "patient") {
			setLoading(false);
			return;
		}
		listCareEpisodes(nextAuth.access_token)
			.then((nextEpisodes) => {
				setEpisodes(nextEpisodes);
				setEpisodesLoaded(true);
			})
			.catch((err) => setError((err as Error).message))
			.finally(() => setLoading(false));
	}, []);

	const deleteEpisode = async (episode: CareEpisode) => {
		if (!auth || deletingEpisodeId) return;
		const confirmed = window.confirm(`Delete ${episode.issue_title}? This removes the episode workspace and saved context for this concern.`);
		if (!confirmed) return;
		setError("");
		setStatusMessage("");
		setDeletingEpisodeId(episode.care_episode_id);
		try {
			await deleteCareEpisode(episode.care_episode_id, auth.access_token);
			setEpisodes((current) => current.filter((item) => item.care_episode_id !== episode.care_episode_id));
			setStatusMessage("Care Episode deleted.");
		} catch (err) {
			setError((err as Error).message);
		} finally {
			setDeletingEpisodeId(null);
		}
	};

	const metricValue = (value: number) => {
		if (episodesLoaded) return value;
		return loading ? "Loading..." : "Unavailable";
	};

	if (!auth || auth.role !== "patient") {
		return (
			<DashboardCard className="max-w-2xl p-5 md:p-6">
				<p className="text-xs font-semibold uppercase tracking-wide text-emerald-700">Patient Dashboard</p>
				<h1 className="mt-2 text-2xl font-semibold text-slate-950">Sign in to organize rehab by concern</h1>
				<p className="mt-2 text-sm leading-6 text-slate-600">Care Episodes keep shoulder, back, ankle, and other recovery concerns separate so triage, rehab, and Professional Care do not blend together.</p>
				<div className="mt-4 flex flex-wrap gap-2">
					<Link className="btn-primary" href="/login">Login</Link>
					<Link className="btn-secondary" href="/register">Register</Link>
				</div>
			</DashboardCard>
		);
	}

	return (
		<div className="space-y-5">
			<section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm md:p-6">
				<div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
					<div>
						<p className="text-xs font-semibold uppercase tracking-wide text-emerald-700">Patient Dashboard</p>
						<h1 className="mt-2 text-2xl font-semibold text-slate-950 md:text-3xl">Your rehab concerns, organized by episode</h1>
						<p className="mt-2 max-w-2xl text-sm leading-6 text-slate-600">See where each concern stands, then open the right Care Episode before starting triage, AI Daily Rehab, Professional Care, or history work.</p>
					</div>
					<div className="flex flex-wrap gap-2">
						<Link className="btn-primary" href="/">Create Episode With AI Triage</Link>
						<Link className="btn-secondary" href="/episodes/new">New Manual Episode</Link>
					</div>
				</div>
			</section>

			<section className="grid gap-4 md:grid-cols-3" aria-label="Patient dashboard status">
				<DashboardCard>
					<p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Care Episodes</p>
					<p className="mt-2 text-2xl font-semibold text-slate-950">{metricValue(episodes.length)}</p>
					<p className="mt-1 text-sm text-slate-600">Active and past rehab concerns</p>
				</DashboardCard>
				<DashboardCard>
					<p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Needs context</p>
					<p className="mt-2 text-2xl font-semibold text-slate-950">{metricValue(episodes.filter((episode) => episode.safety_gate_status === "needs_triage").length)}</p>
					<p className="mt-1 text-sm text-slate-600">Episodes waiting for triage context</p>
				</DashboardCard>
				<DashboardCard>
					<p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Professional Care</p>
					<p className="mt-2 text-2xl font-semibold text-slate-950">{metricValue(episodes.filter((episode) => episode.selected_doctor_id).length)}</p>
					<p className="mt-1 text-sm text-slate-600">Episodes with selected clinician support</p>
				</DashboardCard>
			</section>

			<section className="space-y-4" aria-label="Care Episode List">
				<SectionHeader
					eyebrow="Patient Dashboard"
					title="Care Episode List"
					description="Open the episode that matches your current rehab concern. Deeper triage, rehab, Professional Care, and history actions stay inside each episode."
					action={<Link className="btn-secondary" href="/episodes/new">New Manual Episode</Link>}
				/>

				{error ? <p className="rounded-md border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">{error}</p> : null}
				{statusMessage ? <p className="rounded-md border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-700">{statusMessage}</p> : null}
				{loading ? <p className="text-sm text-slate-500">Loading Care Episodes...</p> : null}

				{episodesLoaded && episodes.length === 0 ? (
					<DashboardCard className="max-w-2xl p-5">
						<h2 className="text-lg font-semibold text-slate-950">No Care Episodes yet</h2>
						<p className="mt-2 text-sm leading-6 text-slate-600">Start with AI Triage Intake to create an episode with safety context, or create a Manual Episode if you already know what you want to track.</p>
						<div className="mt-4 flex flex-wrap gap-2">
							<Link className="btn-primary" href="/">Create Episode With AI Triage</Link>
							<Link className="btn-secondary" href="/episodes/new">Create Manual Episode</Link>
						</div>
					</DashboardCard>
				) : null}

				<div className="grid gap-4 md:grid-cols-2">
					{episodes.map((episode) => (
						<DashboardCard key={episode.care_episode_id} className="flex h-full flex-col gap-4">
							<div className="flex flex-wrap items-start justify-between gap-3">
								<div>
									<p className="text-xs font-semibold uppercase tracking-wide text-emerald-700">{episode.body_area}</p>
									<h2 className="mt-1 text-lg font-semibold text-slate-950">{episode.issue_title}</h2>
								</div>
								<StatusBadge tone="neutral">{statusCopy(episode.status)}</StatusBadge>
							</div>
							<p className="text-sm leading-6 text-slate-600">{episode.goal}</p>
							<div className="flex flex-wrap gap-2">
								<StatusBadge tone={safetyTone(episode.safety_gate_status)}>{safetyCopy(episode.safety_gate_status)}</StatusBadge>
								{episode.selected_doctor_id ? <StatusBadge tone="info">Professional Care selected</StatusBadge> : null}
							</div>
							<dl className="grid gap-3 border-t border-slate-100 pt-3 text-xs text-slate-500 sm:grid-cols-2">
								<div>
									<dt className="font-semibold uppercase tracking-wide text-slate-400">Latest activity</dt>
									<dd className="mt-1 text-slate-700">{formatDate(episode.updated_at)}</dd>
								</div>
								<div>
									<dt className="font-semibold uppercase tracking-wide text-slate-400">Created</dt>
									<dd className="mt-1 text-slate-700">{formatDate(episode.created_at)}</dd>
								</div>
							</dl>
							<div className="mt-auto flex flex-wrap gap-2">
								<Link className="btn-primary" href={`/episodes/${episode.care_episode_id}`}>Open</Link>
								<AppButton variant="secondary" onClick={() => deleteEpisode(episode)} disabled={deletingEpisodeId === episode.care_episode_id}>
									{deletingEpisodeId === episode.care_episode_id ? "Deleting" : "Delete"}
								</AppButton>
							</div>
						</DashboardCard>
					))}
				</div>
			</section>
		</div>
	);
}
