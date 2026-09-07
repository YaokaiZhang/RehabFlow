"use client";

import { type FormEvent, type KeyboardEvent } from "react";

import { AppButton, DashboardCard, SectionHeader, StatusBadge } from "../../components/ui";
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
			<DashboardCard className="max-w-2xl p-5 md:p-6">
				<SectionHeader
					eyebrow="Professional Profile"
					title="Sign in as a doctor to manage your professional profile"
					description="Your professional profile helps patients understand your clinical focus."
				/>
			</DashboardCard>
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

	return (
		<section className="space-y-5" id="professional-profile">
			<DashboardCard className="p-5 md:p-6">
				<SectionHeader
					eyebrow="Professional Profile"
					title="Professional Profile"
					description="Update the specialty and expertise patients see when choosing a doctor."
				/>

				{error ? <p className="mt-4 rounded-md border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">{error}</p> : null}
				{statusMessage ? <p className="mt-4 rounded-md border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-700">{statusMessage}</p> : null}
				{loading ? <p className="mt-4 text-sm text-slate-500">Loading professional profile...</p> : null}

				{!loading && profile ? (
					<form className="mt-5 grid gap-5" onSubmit={submit}>
						<div className="grid gap-4 md:grid-cols-2">
							<label className="grid gap-1 text-sm font-medium text-slate-700">
								<span>Primary specialty</span>
								<input
									className="input"
									value={draft.specialty}
									onChange={(event) => setDraft((previous) => ({ ...previous, specialty: event.target.value }))}
									placeholder="e.g. Sports physical therapy"
									maxLength={120}
								/>
							</label>

							<div className="rounded-md border border-slate-200 bg-slate-50 p-3">
								<p className="text-sm font-semibold text-slate-700">Display name and verification status</p>
								<p className="mt-2 text-sm text-slate-950">{profile.display_name}</p>
								<StatusBadge className="mt-2" tone={profile.verification_status === "verified" ? "success" : "neutral"}>
									{profile.verification_status}
								</StatusBadge>
							</div>
						</div>

						<div className="grid gap-2">
							<label className="text-sm font-medium text-slate-700" htmlFor="professional-profile-tag-input">Expertise</label>
							<div className="flex flex-col gap-2 sm:flex-row">
								<input
									id="professional-profile-tag-input"
									className="input"
									value={pendingTag}
									onChange={(event) => setPendingTag(event.target.value)}
									onKeyDown={handleTagKeyDown}
									placeholder="Add an expertise tag"
									maxLength={80}
									disabled={draft.expertise_tags.length >= maxExpertiseTags}
								/>
								<AppButton type="button" variant="secondary" onClick={addTag} disabled={busy || !pendingTag.trim() || draft.expertise_tags.length >= maxExpertiseTags}>Add tag</AppButton>
							</div>
							<p className="text-xs text-slate-500">Add up to {maxExpertiseTags} tags. Patients may see these labels when choosing a doctor.</p>
							<div className="flex flex-wrap gap-2">
								{draft.expertise_tags.length ? draft.expertise_tags.map((tag) => (
									<span key={tag} className="inline-flex items-center gap-2 rounded-md bg-slate-100 px-2.5 py-1.5 text-sm text-slate-700">
										{tag}
										<button type="button" className="font-semibold text-slate-500 hover:text-rose-700" onClick={() => removeTag(tag)} aria-label={"Remove " + tag}>
											<span aria-hidden="true">&times;</span>
											<span className="sr-only">Remove {tag}</span>
										</button>
									</span>
								)) : <p className="text-sm text-slate-500">No expertise tags added yet.</p>}
							</div>
						</div>

						<div className="flex flex-wrap gap-2">
							<AppButton type="submit" disabled={busy}>{busy ? "Saving" : "Save"}</AppButton>
							<AppButton type="button" variant="secondary" onClick={cancel} disabled={busy}>Cancel</AppButton>
						</div>
					</form>
				) : null}
			</DashboardCard>
		</section>
	);
}
