"use client";

import Link from "next/link";

import { useRouter } from "next/navigation";
import { FormEvent, useEffect, useState } from "react";

import { createCareEpisode } from "@rehab/shared/api";
import { loadAuth, type AuthState } from "@rehab/shared/auth";

export default function NewCareEpisodePage() {
const router = useRouter();
const [auth, setAuth] = useState<AuthState | null>(null);
const [issueTitle, setIssueTitle] = useState("");
const [bodyArea, setBodyArea] = useState("");
const [goal, setGoal] = useState("");
const [shortDescription, setShortDescription] = useState("");
const [symptomStartedOn, setSymptomStartedOn] = useState("");
const [error, setError] = useState("");
const [saving, setSaving] = useState(false);

useEffect(() => {
setAuth(loadAuth());
}, []);

const onSubmit = async (event: FormEvent) => {
event.preventDefault();
setError("");
if (!auth || auth.role !== "patient") {
setError("Login as a patient to create a Care Episode.");
return;
}
setSaving(true);
try {
const episode = await createCareEpisode(
{
issue_title: issueTitle.trim(),
body_area: bodyArea.trim(),
goal: goal.trim(),
short_description: shortDescription.trim(),
symptom_started_on: symptomStartedOn || null,
},
auth.access_token
);
router.push(`/episodes/${episode.care_episode_id}`);
} catch (err) {
setError((err as Error).message);
} finally {
setSaving(false);
}
};

return (
<div className="page-column-narrow space-y-6">
<section className="page-header">
<p className="page-header-kicker">New episode</p>
<h1>Start an episode yourself</h1>
<p className="page-header-lead">Use this when you already know what you want to track. You can ask for Professional Care right away. AI Daily Rehab opens after AI triage or a clinician review.</p>
</section>
<form onSubmit={onSubmit} className="card space-y-1">
<label className="block"><span className="field-label mt-0">What&apos;s the problem?</span><input required className="input" placeholder="For example: Right knee pain when running" value={issueTitle} onChange={(event) => setIssueTitle(event.target.value)} /></label>
<label className="block"><span className="field-label">Body area</span><input required className="input" placeholder="For example: Right knee" value={bodyArea} onChange={(event) => setBodyArea(event.target.value)} /></label>
<label className="block"><span className="field-label">Your goal</span><input required className="input" placeholder="For example: Run 5 km without pain" value={goal} onChange={(event) => setGoal(event.target.value)} /></label>
<label className="block"><span className="field-label">Describe it in a few sentences</span><textarea required className="textarea min-h-28" value={shortDescription} onChange={(event) => setShortDescription(event.target.value)} /></label>
<label className="block"><span className="field-label">When did it start? (optional)</span><input type="date" className="input max-w-xs" value={symptomStartedOn} onChange={(event) => setSymptomStartedOn(event.target.value)} /></label>
{error ? <p className="notice notice-danger mt-4">{error}</p> : null}
<div className="flex flex-wrap items-center gap-3 pt-5">
<button className="btn-primary" disabled={saving}>{saving ? "Creating" : "Create Episode"}</button>
<Link className="btn-secondary" href="/">Use AI triage instead</Link>
</div>
</form>
</div>
);
}
