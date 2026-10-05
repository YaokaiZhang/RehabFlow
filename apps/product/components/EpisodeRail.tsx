"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";

import {
	deleteAiChatSession,
	listAiChatSessions,
	listCareEpisodes,
	type AiChatSessionSummary,
	type CareEpisode,
} from "@rehab/shared/api";
import type { AuthState } from "@rehab/shared/auth";

type EpisodeRailProps = {
	auth: AuthState | null | undefined;
};

function routeEpisodeId(pathname: string, queryEpisodeId: string) {
	const routeMatch = pathname.match(/^\/episodes\/([^/]+)/);
	return routeMatch?.[1] ? decodeURIComponent(routeMatch[1]) : queryEpisodeId;
}

function sessionIsActive(pathname: string, episodeId: string, session: string) {
	const root = `/episodes/${episodeId}`;
	if (session === "overview") return pathname === root;
	return pathname === `${root}/${session}` || pathname.startsWith(`${root}/${session}/`);
}

function chatIsActive(pathname: string, querySessionId: string, sessionId: string) {
	return pathname === "/" && querySessionId === sessionId;
}

function episodeLabel(episode: CareEpisode) {
	return episode.issue_title || episode.short_description || "Untitled episode";
}

function chatLabel(session: AiChatSessionSummary) {
	const title = (session.title || "").trim();
	if (title && title !== "New Rehab Session") return title;
	return session.preview.trim() || "New triage chat";
}

function formatChatDate(value: string) {
	const date = new Date(value);
	if (Number.isNaN(date.getTime())) return "Recent chat";
	return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(date);
}

function chatHref(session: AiChatSessionSummary) {
	const params = new URLSearchParams({ session_id: session.session_id });
	if (session.care_episode_id) params.set("episode_id", session.care_episode_id);
	return `/?${params.toString()}`;
}

const workflowSessions = [
	{ id: "triage", label: "AI Triage Intake" },
	{ id: "rehab", label: "AI Daily Rehab" },
	{ id: "professional-care", label: "Professional Care" },
	{ id: "history", label: "History" },
];

function ChatSessionRow({
	session,
	episodeNames,
	pathname,
	querySessionId,
	pendingDeleteId,
	deletingId,
	onRequestDelete,
	onCancelDelete,
	onConfirmDelete,
}: {
	session: AiChatSessionSummary;
	episodeNames: Map<string, string>;
	pathname: string;
	querySessionId: string;
	pendingDeleteId: string;
	deletingId: string;
	onRequestDelete: (event: React.MouseEvent<HTMLButtonElement>, sessionId: string) => void;
	onCancelDelete: () => void;
	onConfirmDelete: (sessionId: string) => void;
}) {
	const active = chatIsActive(pathname, querySessionId, session.session_id);
	const episodeName = session.care_episode_id
		? episodeNames.get(session.care_episode_id) || "Care episode"
		: "Not attached to an episode";
	const pending = pendingDeleteId === session.session_id;
	const deleting = deletingId === session.session_id;

	return (
		<div className={`episode-chat-row ${active ? "episode-chat-row-active" : ""}`}>
			<Link
				href={chatHref(session)}
				className="episode-chat-link"
				aria-current={active ? "page" : undefined}
			>
				<span className="episode-chat-title">{chatLabel(session)}</span>
				<span className="episode-chat-meta">{episodeName} - {formatChatDate(session.last_message_at || session.updated_at)}</span>
			</Link>
			<button
				type="button"
				className="episode-chat-delete"
				aria-label={`Delete ${chatLabel(session)}`}
				title="Delete chat"
				disabled={deleting}
				onClick={(event) => onRequestDelete(event, session.session_id)}
			>
				<svg aria-hidden="true" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
					<path d="M4 6h12M8 6V4.5h4V6m-6.5 0 .7 10h7.6l.7-10M8.5 9v4.5m3-4.5v4.5" strokeLinecap="round" strokeLinejoin="round" />
				</svg>
			</button>
			{pending ? (
				<div className="episode-chat-delete-confirmation" role="alertdialog" aria-label="Delete chat confirmation">
					<span>{deleting ? "Deleting chat..." : "Delete this chat?"}</span>
					{!deleting ? (
						<div className="episode-chat-delete-actions">
							<button type="button" className="episode-chat-delete-cancel" onClick={onCancelDelete}>Cancel</button>
							<button type="button" className="episode-chat-delete-confirm" onClick={() => onConfirmDelete(session.session_id)}>Delete</button>
						</div>
					) : null}
				</div>
			) : null}
		</div>
	);
}

function EpisodeRailContent({ auth }: EpisodeRailProps) {
	const pathname = usePathname();
	const router = useRouter();
	const searchParams = useSearchParams();
	const queryEpisodeId = searchParams.get("episode_id") || "";
	const querySessionId = searchParams.get("session_id") || "";
	const [episodes, setEpisodes] = useState<CareEpisode[]>([]);
	const [loading, setLoading] = useState(true);
	const [error, setError] = useState("");
	const [chatSessions, setChatSessions] = useState<AiChatSessionSummary[]>([]);
	const [chatLoading, setChatLoading] = useState(true);
	const [chatError, setChatError] = useState("");
	const [pendingDeleteId, setPendingDeleteId] = useState("");
	const [deletingId, setDeletingId] = useState("");
	const [reloadToken, setReloadToken] = useState(0);
	const activeEpisodeId = routeEpisodeId(pathname, queryEpisodeId);
	const episodeNames = new Map(episodes.map((episode) => [episode.care_episode_id, episodeLabel(episode)]));
	const unassignedChats = chatSessions.filter((session) => !session.care_episode_id);

	useEffect(() => {
		const refreshChatSessions = () => setReloadToken((current) => current + 1);
		window.addEventListener("rehab-ai-chat-changed", refreshChatSessions);
		return () => window.removeEventListener("rehab-ai-chat-changed", refreshChatSessions);
	}, []);

	useEffect(() => {
		if (!auth || auth.role !== "patient") {
			setEpisodes([]);
			setError("");
			setLoading(false);
			return;
		}

		let cancelled = false;
		setLoading(true);
		setError("");
		listCareEpisodes(auth.access_token)
			.then((items) => {
				if (!cancelled) setEpisodes(items);
			})
			.catch(() => {
				if (!cancelled) setError("Unable to load Care Episodes.");
			})
			.finally(() => {
				if (!cancelled) setLoading(false);
			});

		return () => {
			cancelled = true;
		};
	}, [auth?.access_token, pathname, reloadToken]);

	useEffect(() => {
		if (!auth || auth.role !== "patient") {
			setChatSessions([]);
			setChatError("");
			setChatLoading(false);
			return;
		}

		let cancelled = false;
		setChatLoading(true);
		setChatError("");
		listAiChatSessions(auth.access_token)
			.then((items) => {
				if (!cancelled) setChatSessions(items);
			})
			.catch(() => {
				if (!cancelled) setChatError("Unable to load AI triage chats.");
			})
			.finally(() => {
				if (!cancelled) setChatLoading(false);
			});

		return () => {
			cancelled = true;
		};
	}, [auth?.access_token, pathname, reloadToken]);

	const retry = () => setReloadToken((current) => current + 1);

	const requestDelete = (event: React.MouseEvent<HTMLButtonElement>, sessionId: string) => {
		event.preventDefault();
		event.stopPropagation();
		setChatError("");
		setPendingDeleteId(sessionId);
	};

	const confirmDelete = async (sessionId: string) => {
		if (!auth || auth.role !== "patient" || deletingId) return;
		setDeletingId(sessionId);
		setChatError("");
		try {
			await deleteAiChatSession(sessionId, auth.access_token);
			setChatSessions((current) => current.filter((session) => session.session_id !== sessionId));
			setPendingDeleteId("");
			window.dispatchEvent(new Event("rehab-ai-chat-changed"));
			if (querySessionId === sessionId && pathname === "/") router.push("/");
		} catch (deleteError) {
			setChatError(deleteError instanceof Error ? deleteError.message : "Unable to delete this chat.");
		} finally {
			setDeletingId("");
		}
	};

	return (
		<aside className="episode-rail" aria-label="Care episodes and AI triage chats">
			<div className="episode-rail-heading">
				<span className="episode-rail-heading-title">Episodes</span>
				<Link href="/episodes" className="episode-rail-all">View all</Link>
			</div>
			<p className="episode-rail-intro">Open an episode to work through its sessions.</p>
			<div className="episode-rail-actions">
				<Link href="/episodes/new" className="episode-rail-new">+ New episode</Link>
				<Link href="/" className="episode-rail-new episode-rail-new-chat">+ New chat</Link>
			</div>

			<div className="episode-rail-list">
				{error ? (
					<div className="episode-rail-feedback" role="alert">
						<p>{error}</p>
						<button type="button" className="episode-rail-retry" onClick={retry}>Try again</button>
					</div>
				) : null}
				{loading ? <p className="episode-rail-loading">Loading your episodes...</p> : null}
				{!loading && !error && episodes.length === 0 ? (
					<p className="episode-rail-empty">No episodes yet. Start with a new concern.</p>
				) : null}
				{episodes.map((episode) => {
					const isActive = episode.care_episode_id === activeEpisodeId;
					const episodeChatsForEpisode = chatSessions.filter((session) => session.care_episode_id === episode.care_episode_id);
					return (
						<div className="episode-folder-shell" key={episode.care_episode_id}>
							<Link
								href={`/episodes/${episode.care_episode_id}`}
								className={`episode-folder ${isActive ? "episode-folder-active" : ""}`}
								aria-current={isActive ? "page" : undefined}
							>
								<span className="episode-folder-marker" aria-hidden="true" />
								<span>
									<span className="episode-folder-title">{episodeLabel(episode)}</span>
									<span className="episode-folder-meta">{episode.body_area || "Care episode"}</span>
								</span>
								{isActive ? <span className="episode-folder-state">Open</span> : null}
							</Link>
							{isActive ? (
								<>
									<nav className="episode-sessions" aria-label={`${episodeLabel(episode)} sessions`}>
										<Link
											href={`/episodes/${episode.care_episode_id}`}
											className={`episode-session-link ${sessionIsActive(pathname, episode.care_episode_id, "overview") ? "episode-session-link-active" : ""}`}
										>
											Episode overview
										</Link>
										{workflowSessions.map((session) => (
											<Link
												key={session.id}
												href={`/episodes/${episode.care_episode_id}/${session.id}`}
												className={`episode-session-link ${sessionIsActive(pathname, episode.care_episode_id, session.id) ? "episode-session-link-active" : ""}`}
											>
												{session.label}
											</Link>
										))}
									</nav>
									<section
										className="episode-chat-group episode-chat-group-nested"
										aria-labelledby={`episode-chat-group-${episode.care_episode_id}`}
									>
										<div className="episode-chat-group-heading">
											<h3 id={`episode-chat-group-${episode.care_episode_id}`}>Episode chats</h3>
											<Link
												href={`/?episode_id=${episode.care_episode_id}`}
												className="episode-chat-new"
											>
												+ New chat
											</Link>
										</div>
										{chatLoading ? <p className="episode-chat-feedback">Loading chats...</p> : null}
										{chatError ? <p className="episode-chat-feedback">Chat history unavailable.</p> : null}
										{!chatLoading && !chatError && episodeChatsForEpisode.length ? episodeChatsForEpisode.map((session) => (
											<ChatSessionRow
												key={session.session_id}
												session={session}
												episodeNames={episodeNames}
												pathname={pathname}
												querySessionId={querySessionId}
												pendingDeleteId={pendingDeleteId}
												deletingId={deletingId}
												onRequestDelete={requestDelete}
												onCancelDelete={() => setPendingDeleteId("")}
												onConfirmDelete={confirmDelete}
											/>
										)) : null}
										{!chatLoading && !chatError && !episodeChatsForEpisode.length ? <p className="episode-chat-feedback">No chats in this episode yet.</p> : null}
									</section>
								</>
							) : null}
						</div>
					);
				})}
			</div>

			<section className="episode-chat-directory" aria-labelledby="episode-chat-directory-title">
				<div className="episode-chat-directory-heading">
					<h2 id="episode-chat-directory-title">Unassigned chats</h2>
					{unassignedChats.length ? <span>{unassignedChats.length}</span> : null}
				</div>
				{chatError ? (
					<div className="episode-rail-feedback" role="alert">
						<p>{chatError}</p>
						<button type="button" className="episode-rail-retry" onClick={retry}>Try again</button>
					</div>
				) : null}
			{chatLoading ? <p className="episode-chat-feedback">Loading chats...</p> : null}
			{!chatLoading && !chatError ? (
					unassignedChats.length ? (
						<div className="episode-chat-group">
							{unassignedChats.map((session) => (
								<ChatSessionRow
									key={session.session_id}
									session={session}
									episodeNames={episodeNames}
									pathname={pathname}
									querySessionId={querySessionId}
									pendingDeleteId={pendingDeleteId}
									deletingId={deletingId}
									onRequestDelete={requestDelete}
									onCancelDelete={() => setPendingDeleteId("")}
									onConfirmDelete={confirmDelete}
								/>
							))}
						</div>
					) : <p className="episode-chat-feedback">No chats started outside an episode yet.</p>
				) : null}
			</section>
		</aside>
	);
}

function EpisodeRailFallback() {
	return (
		<aside className="episode-rail" aria-label="Care episodes and AI triage chats">
			<p className="episode-rail-loading">Loading your episodes...</p>
		</aside>
	);
}

export default function EpisodeRail(props: EpisodeRailProps) {
	return (
		<Suspense fallback={<EpisodeRailFallback />}>
			<EpisodeRailContent {...props} />
		</Suspense>
	);
}
