"use client";

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
			setError((err as Error).message);
		} finally {
			setLoading(false);
		}
	};

	return (
		<section className="card max-w-lg">
			<h1 className="text-xl font-semibold">Register</h1>
			<form onSubmit={onSubmit} className="mt-4 space-y-3">
				<div>
					<label className="mb-1 block text-sm text-slate-600">Role</label>
					<select
						value={role}
						onChange={(e) => updateRole(e.target.value as LoginRole)}
						className="input mt-0"
					>
						<option value="patient">Patient</option>
						<option value="doctor">Doctor</option>
					</select>
				</div>

				<div>
					<label className="mb-1 block text-sm text-slate-600">Name</label>
					<input
						required
						className="input mt-0"
						value={name}
						onChange={(e) => setName(e.target.value)}
					/>
				</div>

				<div>
					<label className="mb-1 block text-sm text-slate-600">Password</label>
					<input
						type="password"
						required
						minLength={8}
						placeholder="At least 8 characters"
						className="input mt-0"
						value={password}
						onChange={(e) => setPassword(e.target.value)}
					/>
				</div>

				{error ? <p className="text-sm text-red-600">{error}</p> : null}

				<AppButton type="submit" disabled={loading}>
					Create account
				</AppButton>
			</form>
		</section>
	);
}
