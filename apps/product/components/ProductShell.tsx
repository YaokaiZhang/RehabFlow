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
  // The episode folder rail only means something to a signed-in patient.
  const episodeRailVisible = auth?.role === "patient";
  const [menuOpen, setMenuOpen] = useState(false);

  useEffect(() => {
    setMenuOpen(false);
  }, [pathname]);

  const navLinks = (
    <>
      {showTriage ? <ShellNavLink href="/">Triage</ShellNavLink> : null}
      {auth?.role !== "doctor" ? (
        <>
          {auth?.role === "patient" ? <ShellNavLink href="/episodes">Episodes</ShellNavLink> : null}
          <ShellNavLink href="/settings">Settings</ShellNavLink>
        </>
      ) : null}
      {auth?.role === "doctor" ? (
        <>
          <ShellNavLink href="/doctor">Worklist</ShellNavLink>
          <ShellNavLink href="/doctor/dashboard">Dashboard</ShellNavLink>
          <ShellNavLink href="/settings">Settings</ShellNavLink>
        </>
      ) : null}
    </>
  );

  return (
    <div className="product-app min-h-screen bg-[var(--rf-paper)] text-ink">
      <header className={menuOpen ? "product-global-nav product-global-nav-open" : "product-global-nav"}>
        <div className="product-global-nav-inner">
          <Link href={homeHref} className="product-brand" aria-label="RehabFlow home">
            <svg className="product-brand-mark" viewBox="0 0 28 28" aria-hidden="true">
              <path d="M3 20c3.4 0 4.6-7 8-7s3.8 4.6 6.8 4.6S21.4 8.4 25 6.5" fill="none" stroke="#111" strokeWidth="2.8" strokeLinecap="round" />
              <circle cx="25" cy="6.5" r="2.6" fill="#10b981" />
            </svg>
            <span className="product-brand-name">RehabFlow.</span>
          </Link>
          <button
            type="button"
            className="product-menu-button"
            aria-expanded={menuOpen}
            aria-controls="product-navigation"
            onClick={() => setMenuOpen((open) => !open)}
          >
            {menuOpen ? "Close" : "Menu"}
          </button>
          <div className="product-navigation" id="product-navigation">
            <nav className="product-top-nav" aria-label="Global navigation">
              {navLinks}
              {auth ? <button type="button" onClick={signOut} className="product-nav-link product-nav-signout">Sign out</button> : null}
              {auth === null ? <Link href="/login" className="product-nav-link product-nav-login">Log in</Link> : null}
            </nav>
          </div>
          <div className="product-account">
            {auth ? (
              <>
                <span className="product-account-name">
                  <span className="product-account-name-text">{auth.username}</span>
                </span>
                <button onClick={signOut} className="product-signout-button">Sign out</button>
              </>
            ) : auth === null ? (
              <div className="product-account-actions">
                <Link href="/login" className="product-account-link">Log in</Link>
                <Link href="/register" className="product-account-link product-account-link-primary">Register</Link>
              </div>
            ) : (
              <span className="product-account-loading" aria-label="Loading account" />
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
