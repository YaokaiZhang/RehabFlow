"use client";

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
<section className="card max-w-2xl">
<p className="text-xs font-semibold uppercase tracking-wide text-emerald-700">Manual Care Episode</p>
<h1 className="mt-2 text-2xl font-semibold text-slate-950">Create an episode without AI triage</h1>
<p className="mt-2 text-sm leading-6 text-slate-600">Manual episodes can open Professional Care now, but AI Daily Rehab remains gated until triage or clinician-reviewed context exists.</p>
<form onSubmit={onSubmit} className="mt-5 space-y-4">
<label className="block"><span className="field-label">Issue title</span><input required className="input" value={issueTitle} onChange={(event) => setIssueTitle(event.target.value)} /></label>
<label className="block"><span className="field-label">Body area</span><input required className="input" value={bodyArea} onChange={(event) => setBodyArea(event.target.value)} /></label>
<label className="block"><span className="field-label">Goal</span><input required className="input" value={goal} onChange={(event) => setGoal(event.target.value)} /></label>
<label className="block"><span className="field-label">Short description</span><textarea required className="textarea min-h-28" value={shortDescription} onChange={(event) => setShortDescription(event.target.value)} /></label>
<label className="block"><span className="field-label">Symptom start date</span><input type="date" className="input" value={symptomStartedOn} onChange={(event) => setSymptomStartedOn(event.target.value)} /></label>
{error ? <p className="text-sm text-rose-600">{error}</p> : null}
<button className="btn-primary" disabled={saving}>{saving ? "Creating" : "Create Episode"}</button>
</form>
</section>
);
}
