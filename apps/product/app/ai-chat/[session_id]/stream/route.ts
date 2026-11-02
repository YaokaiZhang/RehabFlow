export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const configuredBackendBase = process.env.REHAB_BACKEND_BASE?.trim();
const backendPort = process.env.BACKEND_PORT?.trim() || process.env.NEXT_PUBLIC_BACKEND_PORT?.trim() || "8000";
const BACKEND_BASE = configuredBackendBase || "http://127.0.0.1:" + backendPort;

type RouteParams = { session_id?: string };
type RouteContext = { params: Promise<RouteParams> };

// Proxies the AI chat turn stream without buffering, so progress events reach the
// browser as the backend emits them.
export async function POST(request: Request, context: RouteContext): Promise<Response> {
	const authorization = request.headers.get("Authorization");
	if (!authorization) {
		return Response.json({ detail: "Missing Authorization header." }, { status: 401 });
	}

	const params = await context.params;
	const sessionId = params.session_id || "new";
	try {
		const response = await fetch(BACKEND_BASE + "/ai/chat/" + encodeURIComponent(sessionId) + "/stream", {
			method: "POST",
			headers: {
				"Content-Type": request.headers.get("Content-Type") || "application/json",
				Accept: "text/event-stream",
				Authorization: authorization,
			},
			body: await request.text(),
			signal: request.signal,
		});
		return new Response(response.body, {
			status: response.status,
			headers: {
				"Content-Type": response.headers.get("Content-Type") || "text/event-stream",
				"Cache-Control": "no-cache, no-transform",
				"X-Accel-Buffering": "no",
			},
		});
	} catch (error) {
		return Response.json(
			{ detail: error instanceof Error ? error.message : "Could not reach the triage service." },
			{ status: 502 },
		);
	}
}
