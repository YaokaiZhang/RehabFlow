export type AuthState = {
	access_token: string;
	role: "patient" | "doctor";
	user_id: string;
	username: string;
};

const AUTH_KEY = "rehab_auth";

function notifyAuthChanged(): void {
	if (typeof window === "undefined") return;
	window.dispatchEvent(new Event("rehab-auth-changed"));
}

function decodeJwtPayload(token: string): { exp?: number } | null {
	const [, payload] = token.split(".");
	if (!payload) return null;
	try {
		const normalized = payload.replace(/-/g, "+").replace(/_/g, "/");
		const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=");
		return JSON.parse(window.atob(padded)) as { exp?: number };
	} catch {
		return null;
	}
}

export function isAuthExpired(auth: AuthState): boolean {
	if (typeof window === "undefined") return false;
	const payload = decodeJwtPayload(auth.access_token);
	if (typeof payload?.exp !== "number") return true;
	return payload.exp * 1000 <= Date.now();
}

export function saveAuth(auth: AuthState): void {
	localStorage.setItem(AUTH_KEY, JSON.stringify(auth));
	notifyAuthChanged();
}

export function loadAuth(): AuthState | null {
	const raw = localStorage.getItem(AUTH_KEY);
	if (!raw) return null;
	try {
		const auth = JSON.parse(raw) as AuthState;
		if (!auth.access_token || isAuthExpired(auth)) {
			clearAuth();
			return null;
		}
		return auth;
	} catch {
		clearAuth();
		return null;
	}
}

export function clearAuth(): void {
	localStorage.removeItem(AUTH_KEY);
	notifyAuthChanged();
}
