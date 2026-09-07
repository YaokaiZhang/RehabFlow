"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
	deletePatientMemoryField,
	getPatientMemoryDocument,
	patchPatientMemoryFields,
	replacePatientMemoryFields,
	type MemoryDocument,
	type MemoryDocumentFieldInput,
} from "@rehab/shared/api";
import { loadAuth, type AuthState } from "@rehab/shared/auth";

export function usePatientMemoryDocument() {
	const [auth, setAuth] = useState<AuthState | null>(null);
	const [memoryDocument, setMemoryDocument] = useState<MemoryDocument | null>(null);
	const [loading, setLoading] = useState(true);
	const [error, setError] = useState("");
	const [statusMessage, setStatusMessage] = useState("");
	const [busy, setBusy] = useState(false);
	const loadRequestIdRef = useRef(0);

	const loadDocument = useCallback(async (nextAuth: AuthState, options?: { quiet?: boolean }): Promise<boolean> => {
		const requestId = loadRequestIdRef.current += 1;
		if (!options?.quiet) setLoading(true);
		setError("");
		try {
			const nextDocument = await getPatientMemoryDocument(nextAuth.access_token);
			if (loadRequestIdRef.current !== requestId) return false;
			setMemoryDocument(nextDocument);
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
		if (!nextAuth || nextAuth.role !== "patient") {
			setError("Login as a patient to manage Patient Memory.");
			setLoading(false);
			return;
		}
		void loadDocument(nextAuth);
	}, [loadDocument]);

	const addItem = async (input: MemoryDocumentFieldInput): Promise<boolean> => {
		if (!auth || busy) return false;
		setBusy(true);
		setError("");
		setStatusMessage("");
		try {
			await patchPatientMemoryFields({ [input.field_name]: input.value }, auth.access_token);
			const reloaded = await loadDocument(auth, { quiet: true });
			if (!reloaded) return false;
			setStatusMessage("Patient Memory field added.");
			return true;
		} catch (err) {
			setError((err as Error).message);
			return false;
		} finally {
			setBusy(false);
		}
	};

	const editItem = async (fieldName: string, input: MemoryDocumentFieldInput): Promise<boolean> => {
		if (!auth || busy) return false;
		if (!memoryDocument) return false;
		setBusy(true);
		setError("");
		setStatusMessage("");
		try {
			const fields = { ...(memoryDocument.editable_fields || {}) };
			delete fields[fieldName];
			fields[input.field_name] = input.value;
			await replacePatientMemoryFields(fields, auth.access_token);
			const reloaded = await loadDocument(auth, { quiet: true });
			if (!reloaded) return false;
			setStatusMessage("Patient Memory field updated.");
			return true;
		} catch (err) {
			setError((err as Error).message);
			return false;
		} finally {
			setBusy(false);
		}
	};

	const deleteItem = async (fieldName: string): Promise<boolean> => {
		if (!auth || busy) return false;
		setBusy(true);
		setError("");
		setStatusMessage("");
		try {
			await deletePatientMemoryField(fieldName, auth.access_token);
			const reloaded = await loadDocument(auth, { quiet: true });
			if (!reloaded) return false;
			setStatusMessage("Patient Memory field deleted.");
			return true;
		} catch (err) {
			setError((err as Error).message);
			return false;
		} finally {
			setBusy(false);
		}
	};

	return {
		auth,
		memoryDocument,
		loading,
		error,
		statusMessage,
		busy,
		addItem,
		editItem,
		deleteItem,
	};
}
