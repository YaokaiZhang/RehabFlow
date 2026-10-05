"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import { clearAuth, loadAuth, type AuthState } from "@rehab/shared/auth";

import { AppButton, DashboardCard, SectionHeader, StatusBadge } from "../../components/ui";
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
		return <p className="text-sm text-slate-500">Loading settings...</p>;
	}

	if (!auth) {
		return (
			<section className="settings-page">
				<DashboardCard className="settings-guest-card max-w-2xl p-5 md:p-6">
					<SectionHeader
						eyebrow="Settings"
						title="Sign in to manage settings"
						description="Use your RehabFlow account to manage account preferences and patient-owned memory settings."
						action={<Link className="product-auth-link" href="/login?next=/settings">Login</Link>}
					/>
					<div className="mt-4 flex flex-wrap gap-2">
						<Link className="product-auth-link product-auth-link-secondary" href="/register">Register</Link>
					</div>
				</DashboardCard>
			</section>
		);
	}

	const role = auth.role;

	return (
		<div className="settings-page">
			<section className="settings-intro">
				<div>
					<h1>Account and preferences</h1>
					<p>Manage account basics and role-specific RehabFlow settings without changing care workflow data.</p>
				</div>
				<div className="settings-intro-actions">
					<Link className="product-auth-link product-auth-link-secondary" href={roleHome(auth)}>Back to workspace</Link>
					<AppButton variant="ghost" onClick={signOut}>Sign out</AppButton>
				</div>
			</section>

			<div className="settings-layout">
				<aside className="settings-sidebar">
					<div className="settings-account-summary">
						<p className="settings-side-label">Signed in as</p>
						<h2>{auth.username}</h2>
						<div className="settings-account-badges">
							<StatusBadge tone="info">{auth.role}</StatusBadge>
							<StatusBadge tone="neutral">Signed in</StatusBadge>
						</div>
					</div>
					<nav className="settings-nav" aria-label="Settings sections">
						<a className={showAccount ? "settings-nav-link settings-nav-link-active" : "settings-nav-link"} href="/settings">Account</a>
						{role === "patient" ? <a className={showPatientMemory ? "settings-nav-link settings-nav-link-active" : "settings-nav-link"} href="/settings?section=patient-memory">Patient Memory</a> : null}
						{role === "doctor" ? <a className={showProfessionalProfile ? "settings-nav-link settings-nav-link-active" : "settings-nav-link"} href="/settings?section=professional-profile">Professional Profile</a> : null}
					</nav>
				</aside>

				<main className="settings-content">
					{showAccount ? (
						<DashboardCard className="settings-content-card p-5 md:p-6">
								<SectionHeader
									title="Account settings"
								description="Your current RehabFlow identity and role. Care Episodes, Professional Care, and doctor workflow data stay in their own workspaces."
							/>
							<dl className="settings-detail-grid mt-4 grid gap-3 text-sm sm:grid-cols-2">
								<div className="settings-detail-item rounded-md bg-slate-50 p-3 ring-1 ring-slate-200">
									<dt className="font-semibold text-slate-600">Name</dt>
									<dd className="mt-1 text-slate-950">{auth.username}</dd>
								</div>
								<div className="settings-detail-item rounded-md bg-slate-50 p-3 ring-1 ring-slate-200">
									<dt className="font-semibold text-slate-600">Role</dt>
									<dd className="mt-1 capitalize text-slate-950">{auth.role}</dd>
								</div>
							</dl>
						</DashboardCard>
					) : null}

					{role === "patient" && showPatientMemory ? <PatientMemorySettingsSection /> : null}

					{role === "doctor" && showProfessionalProfile ? <DoctorProfessionalProfileSection /> : null}
				</main>
			</div>
		</div>
	);
}
