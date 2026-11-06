"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useEffect, useState } from "react";

import { login } from "@rehab/shared/api";
import { saveAuth } from "@rehab/shared/auth";

import { AppButton } from "../../components/ui";

const LOGIN_ROLE_KEY = "rehab_login_role";

type LoginRole = "patient" | "doctor";

function safeNextPath(value: string | null) {
	if (!value || !value.startsWith("/") || value.startsWith("//")) return "";
	return value;
}

function safeInitialRole(value: string | null): LoginRole | "" {
	return value === "doctor" || value === "patient" ? value : "";
}

function readStoredRole(): LoginRole {
	if (typeof window === "undefined") return "patient";
	return safeInitialRole(window.sessionStorage.getItem(LOGIN_ROLE_KEY)) || "patient";
}

function friendlyAuthError(err: unknown) {
	const message = err instanceof Error ? err.message : "";
	if (!message || /failed to fetch|networkerror|load failed/i.test(message)) {
		return "RehabFlow can't reach its server right now. Check your connection and try again.";
	}
	if (/invalid credentials/i.test(message)) return "That username and password don't match. Check both and try again.";
	return message;
}

function doctorRedirectPath(nextPath: string) {
	return nextPath.startsWith("/doctor") ? nextPath : "/doctor";
}

export default function LoginPage() {
	const router = useRouter();
	const [nextPath, setNextPath] = useState("");
	const [role, setRole] = useState<LoginRole>(() => readStoredRole());
	const [error, setError] = useState("");
	const [loading, setLoading] = useState(false);
	useEffect(() => {
		const params = new URLSearchParams(window.location.search);
		const requestedRole = safeInitialRole(params.get("role"));
		setNextPath(safeNextPath(params.get("next")));
		if (requestedRole) {
			setRole(requestedRole);
			window.sessionStorage.setItem(LOGIN_ROLE_KEY, requestedRole);
		}
	}, []);

	const updateRole = (nextRole: LoginRole) => {
		setRole(nextRole);
		window.sessionStorage.setItem(LOGIN_ROLE_KEY, nextRole);
	};

	const onSubmit = async (e: FormEvent<HTMLFormElement>) => {
		e.preventDefault();
		const formData = new FormData(e.currentTarget);
		const username = String(formData.get("name") ?? "");
		const userPassword = String(formData.get("password") ?? "");
		const submittedRole = safeInitialRole(String(formData.get("role") ?? "")) || role;
		setError("");
		setLoading(true);
		try {
			const auth = await login({ role: submittedRole, username, password: userPassword });
			saveAuth(auth);
			router.push(auth.role === "doctor" ? doctorRedirectPath(nextPath) : nextPath || "/episodes");
		} catch (err) {
			setError(friendlyAuthError(err));
		} finally {
			setLoading(false);
		}
	};

	return (
		<section className="auth-page">
			<div className="auth-deco" aria-hidden="true">
				<span className="sticker tone-peach auth-deco-sticker auth-deco-a">triage</span>
				<span className="sticker tone-mint auth-deco-sticker auth-deco-b">daily rehab</span>
				<span className="sticker tone-sky auth-deco-sticker auth-deco-c">your doctor</span>
				<svg className="auth-deco-scribble" viewBox="0 0 120 60" fill="none"><path d="M6 40c18-26 34-30 40-14s-10 22-6 8 24-24 44-10 22 10 30 2" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" /></svg>
			</div>
			<div className="auth-card">
				<p className="scribble auth-note" aria-hidden="true">good to see you again</p>
				<h1>Welcome back</h1>
				<form onSubmit={onSubmit} className="auth-form">
					<fieldset className="auth-role">
						<legend className="sr-only">I am a</legend>
						<label className={role === "patient" ? "auth-role-option auth-role-option-active" : "auth-role-option"}>
							<input type="radio" name="role" value="patient" checked={role === "patient"} onChange={() => updateRole("patient")} />
							Patient
						</label>
						<label className={role === "doctor" ? "auth-role-option auth-role-option-active" : "auth-role-option"}>
							<input type="radio" name="role" value="doctor" checked={role === "doctor"} onChange={() => updateRole("doctor")} />
							Doctor
						</label>
					</fieldset>
					<div>
						<label className="field-label mt-0" htmlFor="login-name">Username</label>
						<input id="login-name" required name="name" autoComplete="username" className="input" />
					</div>
					<div>
						<label className="field-label mt-0" htmlFor="login-password">Password</label>
						<input
							id="login-password"
							type="password"
							required
							minLength={8}
							name="password"
							autoComplete="current-password"
							className="input"
						/>
					</div>
					{error ? <p className="notice notice-danger" role="alert">{error}</p> : null}
					<AppButton type="submit" className="w-full" disabled={loading}>
						{loading ? "Logging in" : "Log in"}<span className="btn-arrow"><svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8h9M8.5 4l4 4-4 4" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /></svg></span>
					</AppButton>
				</form>
				<p className="auth-switch">New to RehabFlow? <Link href={role === "doctor" ? "/register?role=doctor" : "/register"}>Create an account</Link></p>
			</div>
		</section>
	);
}
