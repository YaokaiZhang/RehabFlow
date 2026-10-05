"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ReactNode, useEffect, useState } from "react";

import { clearAuth, loadAuth, type AuthState } from "@rehab/shared/auth";
import EpisodeRail from "./EpisodeRail";

function homeFor(auth: AuthState | null | undefined) {
  if (!auth) return "/";
  return auth.role === "patient" ? "/episodes" : "/doctor";
}

function isActive(pathname: string, href: string) {
  if (href === "/") return pathname === "/";
  if (href === "/episodes") return pathname === "/episodes";
  if (href === "/doctor") {
    const isDoctorDashboard = pathname === "/doctor/dashboard" || pathname.startsWith("/doctor/dashboard/");
    return !isDoctorDashboard && (pathname === href || pathname.startsWith(`${href}/`));
  }
  return pathname === href || pathname.startsWith(`${href}/`);
}

function navClass(active: boolean) {
  return active ? "product-nav-link product-nav-link-active" : "product-nav-link";
}

function ShellNavLink({ href, children }: { href: string; children: ReactNode }) {
  const pathname = usePathname();
  const active = isActive(pathname, href);

  return (
    <Link href={href} className={navClass(active)} aria-current={active ? "page" : undefined}>
      {children}
    </Link>
  );
}

export default function ProductShell({ children }: { children: ReactNode }) {
  const [auth, setAuth] = useState<AuthState | null | undefined>(undefined);
  const showTriage = auth === null || auth?.role === "patient" || auth === undefined;
  const pathname = usePathname();

  useEffect(() => {
    const syncAuth = () => setAuth(loadAuth());
    syncAuth();
    window.addEventListener("storage", syncAuth);
    window.addEventListener("rehab-auth-changed", syncAuth);
    return () => {
      window.removeEventListener("storage", syncAuth);
      window.removeEventListener("rehab-auth-changed", syncAuth);
    };
  }, []);

  const signOut = () => {
    clearAuth();
    setAuth(null);
    window.location.href = "/login";
  };

  const homeHref = homeFor(auth);
  const episodeRailVisible = auth?.role !== "doctor";

  const navLinks = (
    <>
      {showTriage ? <ShellNavLink href="/">AI Triage Intake</ShellNavLink> : null}
      {auth?.role !== "doctor" ? (
        <>
          {auth?.role === "patient" ? <ShellNavLink href="/episodes">Patient Dashboard</ShellNavLink> : null}
          <ShellNavLink href="/settings">Settings</ShellNavLink>
        </>
      ) : null}
      {auth?.role === "doctor" ? (
        <>
          <ShellNavLink href="/doctor">Care Worklist</ShellNavLink>
          <ShellNavLink href="/doctor/dashboard">Doctor Dashboard</ShellNavLink>
          <ShellNavLink href="/settings">Settings</ShellNavLink>
        </>
      ) : null}
    </>
  );

  return (
    <div className="product-app min-h-screen bg-[var(--rf-app-bg)] text-slate-900">
      <header className="product-global-nav">
        <div className="product-global-nav-inner">
          <Link href={homeHref} className="product-brand" aria-label="RehabFlow home">
            <span className="product-brand-copy">
              <span className="product-brand-name">
                <span className="product-brand-name-rehab">Rehab</span><span className="product-brand-name-flow">Flow</span>
              </span>
            </span>
          </Link>
          <div className="product-navigation">
            <nav className="product-top-nav" aria-label="Global navigation">{navLinks}</nav>
          </div>
          <div className="product-account">
            {auth ? (
              <>
                <span className="product-account-name">{auth.username}</span>
                <button onClick={signOut} className="product-signout-button">Sign out</button>
              </>
            ) : auth === null ? (
              <div className="product-account-actions">
                <Link href="/login" className="product-account-link">Login</Link>
                <Link href="/register" className="product-account-link product-account-link-primary">Register</Link>
              </div>
            ) : (
              <span className="product-account-loading">Loading...</span>
            )}
          </div>
        </div>
      </header>

      <div className={`product-workspace ${episodeRailVisible ? "product-workspace-with-rail" : ""}`}>
        {episodeRailVisible ? <EpisodeRail auth={auth} /> : null}
        <main className="product-main">{children}</main>
      </div>
    </div>
  );
}
