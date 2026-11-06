"use client";

import { type FormEvent, type KeyboardEvent } from "react";

import { AppButton } from "../../components/ui";
import { useDoctorProfessionalProfile } from "./useDoctorProfessionalProfile";

export default function DoctorProfessionalProfileSection() {
	const {
		auth,
		profile,
		draft,
		setDraft,
		pendingTag,
		setPendingTag,
		loading,
		busy,
		error,
		statusMessage,
		addTag,
		removeTag,
		cancel,
		save,
		maxExpertiseTags,
	} = useDoctorProfessionalProfile();

	if (!auth || auth.role !== "doctor") {
		return (
			<div className="card settings-card">
				<p className="settings-empty">Sign in as a doctor.</p>
			</div>
		);
	}

	const submit = async (event: FormEvent<HTMLFormElement>) => {
		event.preventDefault();
		await save();
	};

	const handleTagKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
		if (event.key === "Enter") {
			event.preventDefault();
			addTag();
		}
	};

	const verification = profile?.verification_status === "verified" ? "Verified" : profile?.verification_status === "pending" ? "Verification pending" : "Not verified";

	return (
		<section className="card settings-card" id="professional-profile" aria-labelledby="professional-profile-title" data-tone="sky">
			<h2 className="settings-card-title" id="professional-profile-title"><span className="session-dot" aria-hidden="true" />Professional profile</h2>

			{error ? <p className="notice notice-danger mt-4">{error}</p> : null}
			{statusMessage ? <p className="notice mt-4" role="status">{statusMessage}</p> : null}
			{loading ? <p className="dashboard-loading mt-4">Loading…</p> : null}

			{!loading && profile ? (
				<form className="settings-form" onSubmit={submit}>
					<div className="sticker tone-sky settings-profile-identity">
						<p className="settings-profile-name">{profile.display_name}</p>
						<span className="tag" data-tone={profile.verification_status === "verified" ? "mint" : undefined}>{verification}</span>
					</div>

					<label className="field-label" htmlFor="professional-profile-specialty">Primary specialty</label>
					<input
						id="professional-profile-specialty"
						className="input"
						value={draft.specialty}
						onChange={(event) => setDraft((previous) => ({ ...previous, specialty: event.target.value }))}
						placeholder="e.g. Sports physical therapy"
						maxLength={120}
					/>

					<label className="field-label" htmlFor="professional-profile-tag-input">Expertise</label>
					<div className="settings-tag-row mt-1.5">
						<input
							id="professional-profile-tag-input"
							className="input"
							value={pendingTag}
							onChange={(event) => setPendingTag(event.target.value)}
							onKeyDown={handleTagKeyDown}
							maxLength={80}
							aria-describedby="professional-profile-tag-hint"
							disabled={draft.expertise_tags.length >= maxExpertiseTags}
						/>
						<AppButton type="button" variant="secondary" className="shrink-0 whitespace-nowrap" onClick={addTag} disabled={busy || !pendingTag.trim() || draft.expertise_tags.length >= maxExpertiseTags}>Add tag</AppButton>
					</div>
					<p className="settings-hint" id="professional-profile-tag-hint">Up to {maxExpertiseTags} tags.</p>
					<div className="settings-tags">
						{draft.expertise_tags.length ? draft.expertise_tags.map((tag) => (
							<span key={tag} className="tag">
								{tag}
								<button type="button" className="settings-tag-remove" onClick={() => removeTag(tag)} aria-label={"Remove " + tag}>
									<span aria-hidden="true">&times;</span>
								</button>
							</span>
						)) : <p className="settings-empty mt-0">No tags yet</p>}
					</div>

					<div className="settings-form-actions">
						<AppButton type="submit" disabled={busy}>{busy ? "Saving" : "Save"}</AppButton>
						<AppButton type="button" variant="secondary" onClick={cancel} disabled={busy}>Cancel</AppButton>
					</div>
				</form>
			) : null}
		</section>
	);
}
