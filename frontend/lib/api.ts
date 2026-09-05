// REST client for the Yuri control API, proxied same-origin via
// app/api/yuri/[...path]/route.ts (which forwards the auth token and rejects
// cross-site calls). REST only: EventSource can't send headers, so both SSE
// streams (debug pipeline + yuri events) connect straight to backendBase()
// with the token as a query param instead — see VoiceProvider.tsx. Do not
// route them through here.
import { authHeaders } from "./auth";

export class ApiError extends Error {
  status: number;
  /** The raw `detail` from the body, when there was one. Some endpoints send
   *  an object (the MCP save returns `{error, reason, stderr}` so the UI can
   *  show a server's own stderr), and `message` is only its readable summary. */
  detail?: unknown;
  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

/** A readable line from a FastAPI `detail`, which may be a string, an object,
 *  or a validation-error array. Without this an object detail rendered as
 *  "[object Object]" — a message that tells the user nothing at all. */
function readable(detail: unknown, status: number): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const first = detail[0] as { msg?: string } | undefined;
    return first?.msg ? String(first.msg) : `HTTP ${status}`;
  }
  if (detail && typeof detail === "object") {
    const d = detail as Record<string, unknown>;
    const parts = [d.error, d.reason, d.stderr].filter(Boolean).map(String);
    if (parts.length) return parts.join("\n");
  }
  return `HTTP ${status}`;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers = authHeaders(body !== undefined ? { "Content-Type": "application/json" } : undefined);
  const r = await fetch(`/api/yuri/${path.replace(/^\/+/, "")}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
    cache: "no-store",
  });
  const text = await r.text();
  // The body isn't guaranteed to be JSON: a backend-unreachable failure comes
  // back through the Next proxy as an HTML error page with a non-2xx status,
  // not a JSON error body. JSON.parse would throw on that and this function
  // would never reach the ApiError below — exactly the backend-down case the
  // caller most needs status out of. Fall back to null and still throw.
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }
  if (!r.ok) {
    const detail = data && typeof data === "object" && "detail" in data
      ? (data as { detail: unknown }).detail : undefined;
    throw new ApiError(r.status, readable(detail, r.status), detail);
  }
  return data as T;
}

export function yget<T>(path: string): Promise<T> {
  return request<T>("GET", path);
}

export function ypost<T>(path: string, body?: unknown): Promise<T> {
  return request<T>("POST", path, body ?? {});
}

export function yput<T>(path: string, body?: unknown): Promise<T> {
  return request<T>("PUT", path, body ?? {});
}

export function ydelete<T>(path: string): Promise<T> {
  return request<T>("DELETE", path);
}
