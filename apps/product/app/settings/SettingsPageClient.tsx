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
					<h1>Settings</h1>
					<p>{role === "patient" ? "Your account, and what RehabFlow remembers about you." : "Your account and the profile patients see when they choose a doctor."}</p>
				</div>
			</section>

			<div className="settings-layout">
				<aside className="settings-sidebar">
					<nav className="settings-nav" aria-label="Settings sections">
						<a className={showAccount ? "settings-nav-link settings-nav-link-active" : "settings-nav-link"} href="/settings">Account</a>
						{role === "patient" ? <a className={showPatientMemory ? "settings-nav-link settings-nav-link-active" : "settings-nav-link"} href="/settings?section=patient-memory">Patient Memory</a> : null}
						{role === "doctor" ? <a className={showProfessionalProfile ? "settings-nav-link settings-nav-link-active" : "settings-nav-link"} href="/settings?section=professional-profile">Professional Profile</a> : null}
					</nav>
				</aside>

				<div className="settings-content">
					{showAccount ? (
						<DashboardCard className="settings-content-card p-5 md:p-6">
							<SectionHeader title="Account" />
							<dl className="settings-detail-grid mt-4 grid gap-3 text-sm sm:grid-cols-2">
								<div className="settings-detail-item rounded-control border border-line p-3">
									<dt className="font-semibold text-slate-600">Username</dt>
									<dd className="mt-1 break-all text-slate-950">{auth.username}</dd>
								</div>
								<div className="settings-detail-item rounded-control border border-line p-3">
									<dt className="font-semibold text-slate-600">Account type</dt>
									<dd className="mt-1 capitalize text-slate-950">{auth.role}</dd>
								</div>
							</dl>
							<div className="mt-6 flex flex-wrap items-center justify-between gap-3 border-t border-line pt-5">
								<p className="text-sm text-slate-600">Signing out removes your login from this browser.</p>
								<AppButton variant="secondary" onClick={signOut}>Sign out</AppButton>
							</div>
						</DashboardCard>
					) : null}

					{role === "patient" && showPatientMemory ? <PatientMemorySettingsSection /> : null}

					{role === "doctor" && showProfessionalProfile ? <DoctorProfessionalProfileSection /> : null}
				</div>
			</div>
		</div>
	);
}
