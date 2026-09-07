"use client";

import { useEffect, useState } from "react";

import {
	getDoctorProfessionalProfile,
	updateDoctorProfessionalProfile,
	type DoctorProfessionalProfile,
	type DoctorProfessionalProfileUpdateInput,
} from "@rehab/shared/api";
import { loadAuth, type AuthState } from "@rehab/shared/auth";

const MAX_EXPERTISE_TAGS = 8;

type ProfessionalProfileDraft = {
	specialty: string;
	expertise_tags: string[];
};

function draftFromProfile(profile: DoctorProfessionalProfile): ProfessionalProfileDraft {
	return {
		specialty: profile.specialty || "",
		expertise_tags: profile.expertise_tags || [],
	};
}

export function useDoctorProfessionalProfile() {
	const [auth, setAuth] = useState<AuthState | null | undefined>(undefined);
	const [profile, setProfile] = useState<DoctorProfessionalProfile | null>(null);
	const [draft, setDraft] = useState<ProfessionalProfileDraft>({ specialty: "", expertise_tags: [] });
	const [pendingTag, setPendingTag] = useState("");
	const [loading, setLoading] = useState(true);
	const [busy, setBusy] = useState(false);
	const [error, setError] = useState<string | null>(null);
	const [statusMessage, setStatusMessage] = useState<string | null>(null);

	useEffect(() => {
		const currentAuth = loadAuth();
		setAuth(currentAuth);
		if (!currentAuth || currentAuth.role !== "doctor") {
			setLoading(false);
			return;
		}

		let active = true;
		setLoading(true);
		getDoctorProfessionalProfile(currentAuth.access_token)
			.then((nextProfile) => {
				if (!active) return;
				setProfile(nextProfile);
				setDraft(draftFromProfile(nextProfile));
				setError(null);
			})
			.catch((reason: unknown) => {
				if (!active) return;
				setError(reason instanceof Error ? reason.message : "Unable to load professional profile.");
			})
			.finally(() => {
				if (active) setLoading(false);
			});

		return () => {
			active = false;
		};
	}, []);

	const addTag = () => {
		const value = pendingTag.trim();
		if (!value || draft.expertise_tags.some((tag) => tag.toLocaleLowerCase() === value.toLocaleLowerCase()) || draft.expertise_tags.length >= MAX_EXPERTISE_TAGS) return;
		setDraft((previous) => ({ ...previous, expertise_tags: [...previous.expertise_tags, value] }));
		setPendingTag("");
	};

	const removeTag = (tagToRemove: string) => {
		setDraft((previous) => ({
			...previous,
			expertise_tags: previous.expertise_tags.filter((tag) => tag !== tagToRemove),
		}));
	};

	const cancel = () => {
		if (profile) setDraft(draftFromProfile(profile));
		setPendingTag("");
		setError(null);
		setStatusMessage(null);
	};

	const save = async (): Promise<boolean> => {
		if (!auth || auth.role !== "doctor") return false;
		setBusy(true);
		setError(null);
		setStatusMessage(null);
		const input: DoctorProfessionalProfileUpdateInput = {
			specialty: draft.specialty.trim() || null,
			expertise_tags: draft.expertise_tags,
		};
		try {
			const nextProfile = await updateDoctorProfessionalProfile(input, auth.access_token);
			setProfile(nextProfile);
			setDraft(draftFromProfile(nextProfile));
			setPendingTag("");
			setStatusMessage("Professional profile saved.");
			return true;
		} catch (reason: unknown) {
			setError(reason instanceof Error ? reason.message : "Unable to save professional profile.");
			return false;
		} finally {
			setBusy(false);
		}
	};

	return {
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
		maxExpertiseTags: MAX_EXPERTISE_TAGS,
	};
}
