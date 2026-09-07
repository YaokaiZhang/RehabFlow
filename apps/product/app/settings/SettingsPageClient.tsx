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
			<section className="space-y-5">
				<DashboardCard className="max-w-2xl p-5 md:p-6">
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
		<div className="space-y-6">
			<section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm md:p-6">
				<div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
					<SectionHeader
						eyebrow="Settings"
						title="Account and preferences"
						description="Manage account basics and role-specific RehabFlow settings without changing care workflow data."
					/>
					<div className="flex flex-wrap gap-2">
						<Link className="product-auth-link product-auth-link-secondary" href={roleHome(auth)}>Back to workspace</Link>
						<AppButton variant="ghost" onClick={signOut}>Sign out</AppButton>
					</div>
				</div>
			</section>

			<div className="grid gap-4 lg:grid-cols-[280px_minmax(0,1fr)]">
				<aside className="space-y-3">
					<DashboardCard className="p-4">
						<p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Account</p>
						<h2 className="mt-1 text-lg font-semibold text-slate-950">{auth.username}</h2>
						<div className="mt-3 flex flex-wrap gap-2">
							<StatusBadge tone="info">{auth.role}</StatusBadge>
							<StatusBadge tone="neutral">Signed in</StatusBadge>
						</div>
					</DashboardCard>
					<nav className="space-y-2 text-sm">
						<a className={showAccount ? "product-nav-link product-nav-link-active" : "product-nav-link"} href="/settings">Account</a>
						{role === "patient" ? <a className={showPatientMemory ? "product-nav-link product-nav-link-active" : "product-nav-link"} href="/settings?section=patient-memory">Patient Memory</a> : null}
						{role === "doctor" ? <a className={showProfessionalProfile ? "product-nav-link product-nav-link-active" : "product-nav-link"} href="/settings?section=professional-profile">Professional Profile</a> : null}
					</nav>
				</aside>

				<main className="min-w-0 space-y-5">
					{showAccount ? (
						<DashboardCard className="p-5 md:p-6">
							<SectionHeader
								eyebrow="Account"
								title="Account settings"
								description="Your current RehabFlow identity and role. Care Episodes, Professional Care, and doctor workflow data stay in their own workspaces."
							/>
							<dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2">
								<div className="rounded-md bg-slate-50 p-3 ring-1 ring-slate-200">
									<dt className="font-semibold text-slate-600">Name</dt>
									<dd className="mt-1 text-slate-950">{auth.username}</dd>
								</div>
								<div className="rounded-md bg-slate-50 p-3 ring-1 ring-slate-200">
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
