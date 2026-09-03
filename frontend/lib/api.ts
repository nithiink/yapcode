// REST client for the Yuri control API, proxied same-origin via
// app/api/yuri/[...path]/route.ts (which forwards the auth token and rejects
// cross-site calls). REST only: EventSource can't send headers, so both SSE
// streams (debug pipeline + yuri events) connect straight to backendBase()
// with the token as a query param instead — see VoiceProvider.tsx. Do not
// route them through here.
import { authHeaders } from "./auth";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
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
  const data = text ? JSON.parse(text) : null;
  if (!r.ok) {
    const detail = data && typeof data === "object" && "detail" in data ? String(data.detail) : `HTTP ${r.status}`;
    throw new ApiError(r.status, detail);
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
