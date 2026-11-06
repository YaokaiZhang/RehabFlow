"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import { clearAuth, loadAuth, type AuthState } from "@rehab/shared/auth";

import { AppButton } from "../../components/ui";
import DoctorProfessionalProfileSection from "./DoctorProfessionalProfileSection";
import PatientMemorySettingsSection from "./PatientMemorySettingsSection";

function roleHome(auth: AuthState) {
	return auth.role === "patient" ? "/episodes" : "/doctor";
}

export default function SettingsPageClient() {
	const searchParams = useSearchParams();
	const [auth, setAuth] = useState<AuthState | null | undefined>(undefined);
	const section = searchParams.get("section");
	const showPatientMemory = section === "patient-memory";
	const showProfessionalProfile = section === "professional-profile";
	const showAccount = !showPatientMemory && !showProfessionalProfile;

	useEffect(() => {
		const nextAuth = loadAuth();
		setAuth(nextAuth);
	}, []);

	const signOut = () => {
		clearAuth();
		setAuth(null);
		window.location.href = "/login?next=/settings";
	};

	if (auth === undefined) {
		return <p className="dashboard-loading">Loading settings...</p>;
	}

	if (!auth) {
		return (
			<section className="dashboard-guest">
				<div className="card dashboard-guest-card">
					<h1 className="font-hand">Sign in to manage settings</h1>
					<Link className="btn-primary" href="/login?next=/settings">Log in</Link>
				</div>
			</section>
		);
	}

	const role = auth.role;
	const navClass = (active: boolean) => (active ? "settings-nav-link settings-nav-link-active" : "settings-nav-link");

	return (
		<div className="settings-page">
			<header className="page-header">
				<h1>Settings</h1>
			</header>

			<div className="settings-layout">
				<nav className="settings-nav" aria-label="Settings sections">
					<a className={navClass(showAccount)} href="/settings" aria-current={showAccount ? "page" : undefined}>Account</a>
					{role === "patient" ? <a className={navClass(showPatientMemory)} data-tone="lilac" href="/settings?section=patient-memory" aria-current={showPatientMemory ? "page" : undefined}>Patient memory</a> : null}
					{role === "doctor" ? <a className={navClass(showProfessionalProfile)} data-tone="sky" href="/settings?section=professional-profile" aria-current={showProfessionalProfile ? "page" : undefined}>Professional profile</a> : null}
				</nav>

				<div className="settings-content">
					{showAccount ? (
						<section className="card settings-card" aria-labelledby="settings-account-title">
							<h2 className="sr-only" id="settings-account-title">Account</h2>
							<div className="settings-account" data-tone={role === "patient" ? "mint" : "sky"}>
								<dl className="settings-account-details">
									<dt>Username</dt>
									<dd className="settings-account-name">{auth.username}</dd>
									<dt>Account type</dt>
									<dd><span className="tag">{role === "patient" ? "Patient" : "Doctor"}</span></dd>
								</dl>
								<AppButton variant="secondary" onClick={signOut}>Sign out</AppButton>
							</div>
						</section>
					) : null}

					{role === "patient" && showPatientMemory ? <PatientMemorySettingsSection /> : null}

					{role === "doctor" && showProfessionalProfile ? <DoctorProfessionalProfileSection /> : null}
				</div>
			</div>
		</div>
	);
}
