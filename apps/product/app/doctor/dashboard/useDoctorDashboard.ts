"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
	createDoctorIntelligenceArtifact,
	getDoctorDashboard,
	refreshPatientPanelBriefing,
	type DoctorDashboard,
	type DoctorIntelligenceArtifact,
	type DoctorIntelligenceArtifactInput,
} from "@rehab/shared/api";
import { loadAuth, type AuthState } from "@rehab/shared/auth";

export function useDoctorDashboard() {
	const [auth, setAuth] = useState<AuthState | null | undefined>(undefined);
	const [dashboard, setDashboard] = useState<DoctorDashboard | null>(null);
	const [savedArtifact, setSavedArtifact] = useState<DoctorIntelligenceArtifact | null>(null);
	const [loading, setLoading] = useState(true);
	const [error, setError] = useState("");
	const [statusMessage, setStatusMessage] = useState("");
	const [busyAction, setBusyAction] = useState<"refresh" | "save" | null>(null);
	const loadRequestIdRef = useRef(0);

	const loadDashboard = useCallback(async (nextAuth: AuthState, options?: { quiet?: boolean }): Promise<boolean> => {
		const requestId = loadRequestIdRef.current += 1;
		if (!options?.quiet) setLoading(true);
		setError("");
		try {
			const nextDashboard = await getDoctorDashboard(nextAuth.access_token);
			if (loadRequestIdRef.current !== requestId) return false;
			setDashboard(nextDashboard);
			return true;
		} catch (err) {
			if (loadRequestIdRef.current !== requestId) return false;
			setError((err as Error).message);
			return false;
		} finally {
			if (loadRequestIdRef.current === requestId && !options?.quiet) setLoading(false);
		}
	}, []);

	useEffect(() => {
		const nextAuth = loadAuth();
		setAuth(nextAuth);
		if (!nextAuth || nextAuth.role !== "doctor") {
			setError("Login as a doctor to use Doctor Dashboard.");
			setLoading(false);
			return;
		}
		void loadDashboard(nextAuth);
	}, [loadDashboard]);

	const refreshBriefing = async (): Promise<boolean> => {
		if (!auth || loading || busyAction) return false;
		setBusyAction("refresh");
		setError("");
		setStatusMessage("");
		try {
			const briefing = await refreshPatientPanelBriefing(auth.access_token);
			setDashboard((prev) => prev ? { ...prev, patient_panel_briefing: briefing } : prev);
			setStatusMessage("Patient Panel Briefing refreshed.");
			void loadDashboard(auth, { quiet: true });
			return true;
		} catch (err) {
			setError((err as Error).message);
			return false;
		} finally {
			setBusyAction(null);
		}
	};

	const saveArtifact = async (input: DoctorIntelligenceArtifactInput): Promise<boolean> => {
		if (!auth || busyAction) return false;
		setBusyAction("save");
		setError("");
		setStatusMessage("");
		try {
			const artifact = await createDoctorIntelligenceArtifact(input, auth.access_token);
			setSavedArtifact(artifact);
			setStatusMessage("Doctor Intelligence Artifact saved.");
			return true;
		} catch (err) {
			setError((err as Error).message);
			return false;
		} finally {
			setBusyAction(null);
		}
	};

	return {
		auth,
		dashboard,
		savedArtifact,
		loading,
		error,
		statusMessage,
		busyAction,
		refreshBriefing,
		saveArtifact,
	};
}
