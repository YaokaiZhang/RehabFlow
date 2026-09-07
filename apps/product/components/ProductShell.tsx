"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ReactNode, useEffect, useState } from "react";

import { clearAuth, loadAuth, type AuthState } from "@rehab/shared/auth";

function homeFor(auth: AuthState | null | undefined) {
  if (!auth) return "/";
  return auth.role === "patient" ? "/episodes" : "/doctor";
}

function isActive(pathname: string, href: string) {
  if (href === "/") return pathname === "/";
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
  const showTriage = auth === null || auth?.role === "patient";

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

  const navLinks = (
    <>
      {showTriage ? <ShellNavLink href="/">AI Triage Intake</ShellNavLink> : null}
      {auth?.role === "patient" ? (
        <>
          <ShellNavLink href="/episodes">Patient Dashboard</ShellNavLink>
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
    <div className="min-h-screen bg-[var(--rf-app-bg)] text-slate-900">
      <header className="sticky top-0 z-30 border-b border-slate-200 bg-white/95 backdrop-blur md:hidden">
        <div className="flex items-center justify-between gap-3 px-4 py-3">
          <Link href={homeHref} className="flex items-center gap-3 text-slate-950">
            <span className="product-logo-mark">RF</span>
            <span>
              <span className="block text-sm font-semibold leading-5">RehabFlow</span>
              <span className="block text-xs text-slate-500">Episode-centered rehab</span>
            </span>
          </Link>
          {auth ? (
            <button onClick={signOut} className="product-signout-button">Sign out</button>
          ) : auth === null ? (
            <Link href="/login" className="product-signout-button">Login</Link>
          ) : null}
        </div>
        <nav className="flex gap-2 overflow-x-auto px-4 pb-3 text-sm">{navLinks}</nav>
      </header>

      <div className="mx-auto grid min-h-screen max-w-7xl md:grid-cols-[244px_minmax(0,1fr)]">
        <aside className="sticky top-0 hidden h-screen flex-col border-r border-slate-200 bg-white px-4 py-5 md:flex">
          <Link href={homeHref} className="flex items-center gap-3 rounded-md px-2 py-2 text-slate-950 hover:bg-slate-50">
            <span className="product-logo-mark">RF</span>
            <span>
              <span className="block text-base font-semibold leading-5">RehabFlow</span>
              <span className="block text-xs text-slate-500">Friendly clinical rehab</span>
            </span>
          </Link>
          <nav className="mt-8 flex flex-1 flex-col gap-2 text-sm">{navLinks}</nav>
          <div className="border-t border-slate-200 pt-4">
            {auth ? (
              <div className="space-y-3">
                <div className="rounded-md bg-slate-50 px-3 py-2 ring-1 ring-slate-200">
                  <p className="text-sm font-semibold text-slate-900">{auth.username}</p>
                  <p className="text-xs capitalize text-slate-500">{auth.role}</p>
                </div>
                <button onClick={signOut} className="product-signout-button w-full">Sign out</button>
              </div>
            ) : auth === null ? (
              <div className="space-y-2">
                <Link href="/login" className="product-auth-link">Login</Link>
                <Link href="/register" className="product-auth-link product-auth-link-secondary">Register</Link>
                <p className="px-2 text-xs text-slate-500">Guest</p>
              </div>
            ) : (
              <p className="px-2 text-xs text-slate-500">Loading account...</p>
            )}
          </div>
        </aside>
        <main className="min-w-0 px-4 py-5 md:px-6 md:py-8">{children}</main>
      </div>
    </div>
  );
}
