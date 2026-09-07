export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const configuredBackendBase = process.env.REHAB_BACKEND_BASE?.trim();
const backendPort = process.env.BACKEND_PORT?.trim() || process.env.NEXT_PUBLIC_BACKEND_PORT?.trim() || "8000";
const BACKEND_BASE = configuredBackendBase || "http://127.0.0.1:" + backendPort;

type RouteParams = { session_id?: string };
type RouteContext = { params: Promise<RouteParams> };

function jsonError(detail: string, status: number): Response {
	return Response.json({ detail }, { status });
}

export async function POST(request: Request, context: RouteContext): Promise<Response> {
	const authorization = request.headers.get("Authorization");
	if (!authorization) {
		return jsonError("Missing Authorization header.", 401);
	}

	const params = await context.params;
	const sessionId = params.session_id || "new";
	const body = await request.text();

	try {
		const response = await fetch(BACKEND_BASE + "/ai/chat/" + encodeURIComponent(sessionId), {
			method: "POST",
			headers: {
				"Content-Type": request.headers.get("Content-Type") || "application/json",
				Authorization: authorization,
			},
			body,
		});
		const responseBody = await response.text();
		const contentType = response.headers.get("Content-Type") || "application/json";

		return new Response(responseBody, {
			status: response.status,
			headers: { "Content-Type": contentType },
		});
	} catch (error) {
		return jsonError(error instanceof Error ? error.message : "Could not reach the triage service.", 502);
	}
}
