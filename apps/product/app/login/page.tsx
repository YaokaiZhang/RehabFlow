"use client";

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
			setError((err as Error).message);
		} finally {
			setLoading(false);
		}
	};

	return (
		<section className="card max-w-lg">
			<h1 className="text-xl font-semibold">Login</h1>
			<form onSubmit={onSubmit} className="mt-4 space-y-3">
				<div>
					<label className="mb-1 block text-sm text-slate-600">Role</label>
					<select
						name="role"
						value={role}
						onChange={(e) => updateRole(e.target.value as LoginRole)}
						className="w-full rounded border border-slate-300 px-3 py-2"
					>
						<option value="patient">Patient</option>
						<option value="doctor">Doctor</option>
					</select>
				</div>
				<div>
					<label className="mb-1 block text-sm text-slate-600">Name</label>
					<input
						required
						name="name"
						autoComplete="username"
						className="w-full rounded border border-slate-300 px-3 py-2"
					/>
				</div>
				<div>
					<label className="mb-1 block text-sm text-slate-600">Password</label>
					<input
						type="password"
						required
						minLength={8}
						name="password"
						autoComplete="current-password"
						placeholder="At least 8 characters"
						className="w-full rounded border border-slate-300 px-3 py-2"
					/>
				</div>
				{error ? <p className="text-sm text-red-600">{error}</p> : null}
				<AppButton type="submit" disabled={loading}>
					Login
				</AppButton>
			</form>
		</section>
	);
}
