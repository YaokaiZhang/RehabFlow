"use client";

import Link from "next/link";

import { useRouter } from "next/navigation";
import { FormEvent, useEffect, useState } from "react";

import { createCareEpisode } from "@rehab/shared/api";
import { loadAuth, type AuthState } from "@rehab/shared/auth";

import { ArrowIcon } from "../../../components/CareEpisodeBrief";

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
<div className="ep-new">
<header>
<h1 className="ep-new-title">New episode</h1>
</header>
<form onSubmit={onSubmit} className="card ep-new-form">
<label className="block"><span className="field-label">What&apos;s the problem?</span><input required className="input" placeholder="Right knee pain when running" value={issueTitle} onChange={(event) => setIssueTitle(event.target.value)} /></label>
<div className="ep-new-row">
<label className="block"><span className="field-label">Body area</span><input required className="input" placeholder="Right knee" value={bodyArea} onChange={(event) => setBodyArea(event.target.value)} /></label>
<label className="block"><span className="field-label">When did it start? (optional)</span><input type="date" className="input" value={symptomStartedOn} onChange={(event) => setSymptomStartedOn(event.target.value)} /></label>
</div>
<label className="block"><span className="field-label">Your goal</span><input required className="input" placeholder="Run 5 km without pain" value={goal} onChange={(event) => setGoal(event.target.value)} /></label>
<label className="block"><span className="field-label">Description</span><textarea required className="textarea mt-1.5 min-h-28" value={shortDescription} onChange={(event) => setShortDescription(event.target.value)} /></label>
{error ? <p className="notice notice-danger mt-4">{error}</p> : null}
<div className="ep-new-actions">
<button className="btn-primary" disabled={saving}>{saving ? "Creating" : "Create episode"}{saving ? null : <ArrowIcon />}</button>
<Link className="ep-new-alt" href="/">Use AI triage instead</Link>
</div>
</form>
</div>
);
}
