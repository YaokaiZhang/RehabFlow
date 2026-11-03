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
					title="Professional Profile"
					description="What patients see when they choose a doctor."
				/>

				{error ? <p className="notice notice-danger mt-4">{error}</p> : null}
				{statusMessage ? <p className="notice mt-4" role="status">{statusMessage}</p> : null}
				{loading ? <p className="mt-4 text-sm text-slate-500">Loading professional profile...</p> : null}

				{!loading && profile ? (
					<form className="mt-5 grid gap-5" onSubmit={submit}>
						<div className="grid items-start gap-4 md:grid-cols-2">
							<label className="grid gap-1 text-sm font-semibold text-slate-800">
								<span>Primary specialty</span>
								<input
									className="input"
									value={draft.specialty}
									onChange={(event) => setDraft((previous) => ({ ...previous, specialty: event.target.value }))}
									placeholder="e.g. Sports physical therapy"
									maxLength={120}
								/>
							</label>

							<div className="rounded-control border border-line bg-[var(--rf-surface-muted)] p-4">
								<p className="text-sm font-semibold text-slate-800">Display name and verification status</p>
								<p className="mt-1 text-base font-bold text-slate-950">{profile.display_name}</p>
								<StatusBadge className="mt-2" tone={profile.verification_status === "verified" ? "success" : "neutral"}>
									{profile.verification_status === "verified" ? "Verified" : profile.verification_status === "pending" ? "Verification pending" : "Not verified"}
								</StatusBadge>
							</div>
						</div>

						<div className="grid gap-2">
							<label className="text-sm font-semibold text-slate-800" htmlFor="professional-profile-tag-input">Expertise</label>
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
								<AppButton type="button" variant="secondary" className="shrink-0 whitespace-nowrap" onClick={addTag} disabled={busy || !pendingTag.trim() || draft.expertise_tags.length >= maxExpertiseTags}>Add tag</AppButton>
							</div>
							<p className="text-xs text-slate-500">Add up to {maxExpertiseTags} tags. Patients may see these labels when choosing a doctor.</p>
							<div className="flex flex-wrap gap-2">
								{draft.expertise_tags.length ? draft.expertise_tags.map((tag) => (
									<span key={tag} className="inline-flex items-center gap-2 rounded-full border border-line bg-surface py-1 pl-3 pr-2 text-sm text-slate-800">
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
