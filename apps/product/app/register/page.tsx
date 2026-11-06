"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useEffect, useState } from "react";

import { registerDoctor, registerPatient } from "@rehab/shared/api";
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

function friendlyAuthError(err: unknown) {
	const message = err instanceof Error ? err.message : "";
	if (!message || /failed to fetch|networkerror|load failed/i.test(message)) {
		return "RehabFlow can't reach its server right now. Check your connection and try again.";
	}
	return message;
}

function doctorRedirectPath(nextPath: string) {
	return nextPath.startsWith("/doctor") ? nextPath : "/doctor";
}

export default function RegisterPage() {
	const router = useRouter();
	const [nextPath, setNextPath] = useState("");
	const [role, setRole] = useState<LoginRole>("patient");
	const [name, setName] = useState("");
	const [password, setPassword] = useState("");
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

	const onSubmit = async (e: FormEvent) => {
		e.preventDefault();
		setError("");
		setLoading(true);
		try {
			const auth = role === "patient"
				? await registerPatient({
					patient_name: name,
					password,
					real_info: { source: "mvp-register-page" },
				})
				: await registerDoctor({
					doctor_name: name,
					password,
					real_info: { source: "mvp-register-page" },
				});
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
			<div className="auth-card">
				<h1>Let&apos;s get you set up</h1>
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
						<label className="field-label mt-0" htmlFor="register-name">Username</label>
						<input
							id="register-name"
							required
							autoComplete="username"
							className="input"
							value={name}
							onChange={(e) => setName(e.target.value)}
						/>
					</div>

					<div>
						<label className="field-label mt-0" htmlFor="register-password">Password</label>
						<input
							id="register-password"
							type="password"
							required
							minLength={8}
							autoComplete="new-password"
							aria-describedby="register-password-hint"
							className="input"
							value={password}
							onChange={(e) => setPassword(e.target.value)}
						/>
						<p id="register-password-hint" className="auth-hint">At least 8 characters.</p>
					</div>

					{error ? <p className="notice notice-danger" role="alert">{error}</p> : null}

					<AppButton type="submit" className="w-full" disabled={loading}>
						{loading ? "Creating account" : "Create account"}<span className="btn-arrow"><svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8h9M8.5 4l4 4-4 4" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /></svg></span>
					</AppButton>
				</form>
				<p className="auth-switch">Already have an account? <Link href={role === "doctor" ? "/login?role=doctor" : "/login"}>Log in</Link></p>
			</div>
		</section>
	);
}
