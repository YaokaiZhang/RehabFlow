"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { deleteCareEpisode, listCareEpisodes, type CareEpisode } from "@rehab/shared/api";
import { loadAuth, type AuthState } from "@rehab/shared/auth";



type EpisodeTone = "peach" | "mint" | "sky";

// The sticker colour follows the session the episode is in right now.
function episodeStage(episode: CareEpisode): { tone: EpisodeTone; label: string } {
	if (episode.safety_gate_status === "needs_triage") return { tone: "peach", label: "Needs triage" };
	if (episode.selected_doctor_id) return { tone: "sky", label: "With a doctor" };
	return { tone: "mint", label: "In rehab" };
}

function statusCopy(status: CareEpisode["status"]) {
	const words = status.split("_").filter(Boolean).join(" ");
	return words ? words.charAt(0).toUpperCase() + words.slice(1) : "Active";
}

function formatDate(value?: string | null) {
	if (!value) return "Not recorded";
	const date = new Date(value);
	if (Number.isNaN(date.getTime())) return "Not recorded";
	return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(date);
}

const tilts = ["tilt-l", "tilt-r", "tilt-r", "tilt-l"];

function ArrowIcon() {
	return (
		<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true">
			<path d="M3 8h9M8.5 4l4 4-4 4" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
		</svg>
	);
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

	const needsTriageCount = episodes.filter((episode) => episode.safety_gate_status === "needs_triage").length;
	const withDoctorCount = episodes.filter((episode) => episode.selected_doctor_id).length;

	if (!auth || auth.role !== "patient") {
		return (
			<section className="dashboard-guest">
				<div className="card dashboard-guest-card">
					<h1 className="font-hand">Sign in to see your episodes</h1>
					<Link className="btn-primary" href="/login">Log in<span className="btn-arrow"><ArrowIcon /></span></Link>
				</div>
			</section>
		);
	}

	return (
		<div className="dashboard-page">
			<header className="page-header dashboard-header">
				<h1>My episodes</h1>
				<p className="scribble dashboard-header-note">one concern each, so nothing gets mixed up</p>
				{episodesLoaded && episodes.length > 0 ? (
					<ul className="dashboard-counts" aria-label="Episode counts">
						<li className="tag">{episodes.length} {episodes.length === 1 ? "episode" : "episodes"}</li>
						{needsTriageCount ? <li className="tag" data-tone="peach"><span className="session-dot" aria-hidden="true" />{needsTriageCount} need triage</li> : null}
						{withDoctorCount ? <li className="tag" data-tone="sky"><span className="session-dot" aria-hidden="true" />{withDoctorCount} with a doctor</li> : null}
					</ul>
				) : null}
			</header>

			{error ? <p className="notice notice-danger">{error}</p> : null}
			{statusMessage ? <p className="notice" role="status">{statusMessage}</p> : null}
			{loading ? <p className="dashboard-loading">Loading Care Episodes...</p> : null}

			<section className="dashboard-episode-section" aria-label="Care Episode List">
				<ul className="dashboard-episode-grid">
					{!loading ? (
						<li className={`sticker tone-plain dashboard-new-card tilt-l`}>
							<span className="dashboard-new-plus" aria-hidden="true">+</span>
							<h2 className="font-hand dashboard-new-title">{episodesLoaded && episodes.length === 0 ? "No Care Episodes yet" : "New episode"}</h2>
							<Link className="btn-primary" href="/">Start with AI triage<span className="btn-arrow"><ArrowIcon /></span></Link>
							<Link className="dashboard-new-manual" href="/episodes/new">or add one by hand</Link>
						</li>
					) : null}
					{episodes.map((episode, index) => {
						const stage = episodeStage(episode);
						const triageDone = episode.safety_gate_status !== "needs_triage";
						const steps = [
							{ tone: "peach", label: "AI Triage Intake", done: triageDone },
							{ tone: "mint", label: "AI Daily Rehab", done: triageDone },
							{ tone: "sky", label: "Professional Care", done: Boolean(episode.selected_doctor_id) },
						];
						const doneCount = steps.filter((step) => step.done).length;
						const deleting = deletingEpisodeId === episode.care_episode_id;
						return (
							<li key={episode.care_episode_id} className={`sticker tone-${stage.tone} ${tilts[(index + 1) % tilts.length]} dashboard-episode-card`}>
								<div className="dashboard-episode-top">
									<div className="dashboard-episode-tags">
										<span className="tag">{stage.label}</span>
										{episode.status && episode.status !== "active" ? <span className="tag dashboard-tag-plain">{statusCopy(episode.status)}</span> : null}
									</div>
									<button
										type="button"
										className="dashboard-delete-button"
										onClick={() => deleteEpisode(episode)}
										disabled={deleting}
										aria-label={deleting ? `Deleting ${episode.issue_title}` : `Delete ${episode.issue_title}`}
										title="Delete episode"
									>
										{deleting ? (
											<span className="dashboard-delete-busy" aria-hidden="true" />
										) : (
											<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
												<path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 8.5h5.8l.6-8.5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
											</svg>
										)}
									</button>
								</div>
								<p className="dashboard-episode-area">{episode.body_area}</p>
								<h2 className="dashboard-episode-title">
									<Link className="dashboard-episode-link" href={`/episodes/${episode.care_episode_id}`}>{episode.issue_title}</Link>
								</h2>
								{episode.goal ? <p className="dashboard-episode-goal">{episode.goal}</p> : null}
								<div className="dashboard-episode-progress" role="img" aria-label={`${doneCount} of 3 steps: ${steps.map((step) => `${step.label} ${step.done ? "done" : "not yet"}`).join(", ")}`}>
									{steps.map((step) => (
										<span key={step.label} data-tone={step.tone} className={step.done ? "dashboard-step dashboard-step-done" : "dashboard-step"} />
									))}
								</div>
								<div className="dashboard-episode-foot">
									<span>Updated {formatDate(episode.updated_at)}</span>
									<span className="dashboard-episode-go" aria-hidden="true"><ArrowIcon /></span>
								</div>
							</li>
						);
					})}

				</ul>
			</section>
		</div>
	);
}
