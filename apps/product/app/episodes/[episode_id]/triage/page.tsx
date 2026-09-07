"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import {
    createTriageSummaryDraft,
    deleteTriageSummaryDraft,
    getCareEpisode,
    listTriageSummaries,
    listTriageSummaryDrafts,
    selectTriageSummaryDraft,
    type CareEpisode,
    type TriageSummaryArtifact,
    type TriageSummaryDraft,
} from "@rehab/shared/api";
import { loadAuth, type AuthState } from "@rehab/shared/auth";

import { AppButton } from "../../../../components/ui";

type Params = { episode_id?: string };
type SummaryLike = TriageSummaryArtifact | TriageSummaryDraft;

function isSavedSummary(summary: SummaryLike): summary is TriageSummaryArtifact {
    return "triage_summary_id" in summary;
}

function SummaryPanel({
    summary,
    episodeIssueTitle,
    onSave,
    onDelete,
    busy,
}: {
    summary: SummaryLike;
    episodeIssueTitle: string;
    onSave?: () => void;
    onDelete?: () => void;
    busy?: boolean;
}) {
    const saved = isSavedSummary(summary);
    return (
        <section className={`card max-w-3xl ${saved ? "border-emerald-200" : "border-amber-200"}`}>
            <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                <div>
                    <p className={saved ? "text-xs font-semibold uppercase tracking-wide text-emerald-700" : "text-xs font-semibold uppercase tracking-wide text-amber-700"}>
                        {saved ? `Triage Summary v${summary.version}` : "Unsaved Triage Summary"}
                    </p>
                    <h2 className="mt-1 text-xl font-semibold text-slate-950">{summary.concern || episodeIssueTitle}</h2>
                </div>
                <div className="flex flex-wrap gap-2">
                    {!saved && onSave ? <AppButton onClick={onSave} disabled={busy}>{busy ? "Saving" : "Save Summary"}</AppButton> : null}
                    {!saved && onDelete ? <AppButton variant="secondary" onClick={onDelete} disabled={busy}>{busy ? "Working" : "Delete"}</AppButton> : null}
                </div>
            </div>
            <div className="mt-5">
                <h3 className="text-sm font-semibold text-slate-950">What we heard</h3>
                <p className="mt-2 text-sm leading-6 text-slate-700">{summary.relevant_context}</p>
            </div>
            <div className="mt-5 border-t border-slate-200 pt-4">
                <h3 className="text-sm font-semibold text-slate-950">Suggested next step</h3>
                <p className="mt-2 text-sm leading-6 text-slate-700">{summary.recommendation}</p>
            </div>
        </section>
    );
}

export default function EpisodeTriagePage() {
    const params = useParams<Params>();
    const router = useRouter();
    const episodeId = params?.episode_id || "";
    const [auth, setAuth] = useState<AuthState | null>(null);
    const [episode, setEpisode] = useState<CareEpisode | null>(null);
    const [summaries, setSummaries] = useState<TriageSummaryArtifact[]>([]);
    const [drafts, setDrafts] = useState<TriageSummaryDraft[]>([]);
    const [additionalContext, setAdditionalContext] = useState("");
    const [error, setError] = useState("");
    const [statusMessage, setStatusMessage] = useState("");
    const [loading, setLoading] = useState(true);
    const [generating, setGenerating] = useState(false);
    const [workingDraftId, setWorkingDraftId] = useState<string | null>(null);

    const loadEpisode = (nextAuth: AuthState) => {
        setLoading(true);
        return Promise.all([
            getCareEpisode(episodeId, nextAuth.access_token),
            listTriageSummaries(episodeId, nextAuth.access_token),
            listTriageSummaryDrafts(episodeId, nextAuth.access_token),
        ])
            .then(([nextEpisode, nextSummaries, nextDrafts]) => {
                setEpisode(nextEpisode);
                setSummaries(nextSummaries);
                setDrafts(nextDrafts);
            })
            .catch((err) => setError((err as Error).message))
            .finally(() => setLoading(false));
    };

    useEffect(() => {
        const nextAuth = loadAuth();
        setAuth(nextAuth);
        if (nextAuth && nextAuth.role === "doctor") {
            router.replace("/doctor/dashboard");
            setLoading(false);
            return;
        }
        if (!nextAuth || nextAuth.role !== "patient") {
            setError("Login as a patient to use episode triage.");
            setLoading(false);
            return;
        }
        void loadEpisode(nextAuth);
    }, [episodeId, router]);

    const generateSummary = async () => {
        if (!auth || !episode || generating) return;
        setError("");
        setStatusMessage("");
        setGenerating(true);
        try {
            const draft = await createTriageSummaryDraft(episode.care_episode_id, { additional_context: additionalContext }, auth.access_token);
            setDrafts((prev) => [draft, ...prev]);
            setAdditionalContext("");
            setStatusMessage("Unsaved Triage Summary generated.");
        } catch (err) {
            setError((err as Error).message);
        } finally {
            setGenerating(false);
        }
    };

    const saveDraft = async (draftId: string) => {
        if (!auth || !episode || workingDraftId) return;
        setError("");
        setStatusMessage("");
        setWorkingDraftId(draftId);
        try {
            const summary = await selectTriageSummaryDraft(episode.care_episode_id, draftId, auth.access_token);
            setDrafts((prev) => prev.filter((draft) => draft.triage_summary_draft_id !== draftId));
            setSummaries((prev) => [summary, ...prev]);
            setEpisode({ ...episode, latest_triage_summary: summary, safety_gate_status: "triage_complete" });
            setStatusMessage(`Triage Summary v${summary.version} saved.`);
        } catch (err) {
            setError((err as Error).message);
        } finally {
            setWorkingDraftId(null);
        }
    };

    const deleteDraft = async (draftId: string) => {
        if (!auth || !episode || workingDraftId) return;
        setError("");
        setStatusMessage("");
        setWorkingDraftId(draftId);
        try {
            await deleteTriageSummaryDraft(episode.care_episode_id, draftId, auth.access_token);
            setDrafts((prev) => prev.filter((draft) => draft.triage_summary_draft_id !== draftId));
            setStatusMessage("Unsaved Triage Summary deleted.");
        } catch (err) {
            setError((err as Error).message);
        } finally {
            setWorkingDraftId(null);
        }
    };

    if (loading) return <p className="text-sm text-slate-500">Loading episode triage...</p>;
    if (error && !episode) return <section className="card max-w-2xl text-sm text-rose-700">{error}</section>;
    if (!episode) return null;

    const latestSummary = summaries[0] || episode.latest_triage_summary || null;

    return (
        <div className="space-y-5">
            <section className="card max-w-3xl">
                <p className="text-xs font-semibold uppercase tracking-wide text-emerald-700">AI Triage Intake</p>
                <h1 className="mt-2 text-2xl font-semibold text-slate-950">{episode.issue_title}</h1>
                <p className="mt-2 text-sm leading-6 text-slate-600">Generate a structured Triage Summary, review it, then save the selected summary into this Care Episode.</p>
                <div className="mt-4 flex flex-wrap gap-2">
                    <Link className="btn-secondary" href={`/?episode_id=${episode.care_episode_id}`}>Continue AI Triage Chat</Link>
                    <Link className="btn-secondary" href={`/episodes/${episode.care_episode_id}`}>Episode Overview</Link>
                </div>
            </section>

            <section className="card max-w-3xl">
                <h2 className="section-title">Triage Summary Request</h2>
                <label className="block">
                    <span className="field-label">New context since the last summary</span>
                    <textarea
                        className="textarea min-h-28"
                        value={additionalContext}
                        onChange={(event) => setAdditionalContext(event.target.value)}
                        placeholder="Add answers from triage chat, symptom changes, red-flag negatives, or current limits."
                    />
                </label>
                {error ? <p className="mt-3 text-sm text-rose-600">{error}</p> : null}
                {statusMessage ? <p className="mt-3 text-sm text-emerald-700">{statusMessage}</p> : null}
                <AppButton className="mt-4" onClick={generateSummary} disabled={generating}>
                    {generating ? "Generating" : latestSummary ? "Generate New Unsaved Summary" : "Generate Unsaved Summary"}
                </AppButton>
            </section>

            {drafts.length ? (
                <section className="space-y-3">
                    {drafts.map((draft) => (
                        <SummaryPanel
                            key={draft.triage_summary_draft_id}
                            summary={draft}
                            episodeIssueTitle={episode.issue_title}
                            busy={workingDraftId === draft.triage_summary_draft_id}
                            onSave={() => saveDraft(draft.triage_summary_draft_id)}
                            onDelete={() => deleteDraft(draft.triage_summary_draft_id)}
                        />
                    ))}
                </section>
            ) : null}

            {latestSummary ? <SummaryPanel summary={latestSummary} episodeIssueTitle={episode.issue_title} /> : null}

            {summaries.length > 1 ? (
                <section className="card max-w-3xl">
                    <h2 className="section-title">Saved Summary History</h2>
                    <div className="mt-3 space-y-2">
                        {summaries.slice(1).map((summary) => (
                            <div key={summary.triage_summary_id} className="rounded-md border border-slate-200 p-3 text-sm text-slate-600">
                                <p className="font-semibold text-slate-950">Version {summary.version}</p>
                                <p className="mt-1">{summary.recommendation}</p>
                            </div>
                        ))}
                    </div>
                </section>
            ) : null}
        </div>
    );
}
