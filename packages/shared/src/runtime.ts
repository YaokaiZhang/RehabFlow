const DEFAULT_BACKEND_PORT = "8000";

export function getBackendPort(): string {
  const configuredPort = process.env.BACKEND_PORT?.trim() || process.env.NEXT_PUBLIC_BACKEND_PORT?.trim();
  return configuredPort || DEFAULT_BACKEND_PORT;
}

export function getApiBase(): string {
  if (process.env.NEXT_PUBLIC_API_BASE) return process.env.NEXT_PUBLIC_API_BASE;
  if (typeof window === "undefined") return `http://127.0.0.1:${getBackendPort()}`;
  return "/api";
}

export function getWsBase(): string {
  if (process.env.NEXT_PUBLIC_WS_BASE) return process.env.NEXT_PUBLIC_WS_BASE;
  const backendPort = getBackendPort();
  if (typeof window === "undefined") return `ws://localhost:${backendPort}`;
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.hostname}:${backendPort}`;
}

const requestTimeoutMessage = "Request timed out while contacting RehabFlow. Check that the backend is reachable and try again.";

export async function fetchWithTimeout(input: RequestInfo | URL, init: RequestInit = {}, timeoutMs: number | null = 30000): Promise<Response> {
  if (timeoutMs === null) {
    return fetch(input, init);
  }
  const controller = new AbortController();
  let timedOut = false;
  const abortFromCaller = () => controller.abort(init.signal?.reason);

  if (init.signal?.aborted) {
    abortFromCaller();
  } else {
    init.signal?.addEventListener("abort", abortFromCaller, { once: true });
  }

  const timeout = timeoutMs === null ? null : globalThis.setTimeout(() => {
    timedOut = true;
    controller.abort(new Error(requestTimeoutMessage));
  }, timeoutMs);

  try {
    return await fetch(input, { ...init, signal: controller.signal });
  } catch (error) {
    if (timedOut) throw new Error(requestTimeoutMessage);
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error("The request was cancelled. Please try again.");
    }
    if (error instanceof Error && /signal is aborted without reason/i.test(error.message)) {
      throw new Error("The request was cancelled. Please try again.");
    }
    throw error;
  } finally {
    if (timeout !== null) globalThis.clearTimeout(timeout);
    init.signal?.removeEventListener("abort", abortFromCaller);
  }
}
