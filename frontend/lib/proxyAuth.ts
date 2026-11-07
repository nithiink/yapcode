import { NextRequest, NextResponse } from "next/server";

// Forward the browser-presented shared-secret token to the backend.
//
// The Next /api/* routes run server-side and proxy to the FastAPI backend. They
// hold NO secret of their own — they simply pass through whatever token the
// browser presented (X-VC-Token / Authorization). The backend validates the
// token (and, for browser-direct transports, the Origin). No-op on localhost,
// where no token is presented and the backend trusts loopback.
//
// NOTE: we deliberately do NOT forward the browser Origin here. Node/undici
// treats `Origin` as a forbidden request header and may silently drop it, so a
// "forward the Origin and let the backend allowlist judge it" scheme could fail
// open. Cross-origin (CSRF) requests are instead rejected up front by
// blockCrossSite() below — at the proxy, which is the boundary the browser
// actually reaches.
export function forwardAuth(
  req: NextRequest,
  base: Record<string, string> = {},
): Record<string, string> {
  const headers = { ...base };
  const xt = req.headers.get("x-vc-token");
  if (xt) headers["X-VC-Token"] = xt;
  const auth = req.headers.get("authorization");
  if (auth) headers["Authorization"] = auth;
  return headers;
}

// --- Cross-site (CSRF) defense ----------------------------------------------
//
// The /api/* routes are an open same-origin proxy into a backend that turns
// requests into real command execution. The legitimate frontend ALWAYS calls
// them same-origin (a relative `fetch("/api/...")` from the app's own page), so
// any request the browser labels cross-site is hostile — a drive-by CSRF from
// another page open in the user's browser. We must reject those HERE, at the
// proxy, because the backend can't: the proxy hop drops the browser Origin, so
// the backend's own Origin allowlist never sees it for proxied requests (that
// allowlist still guards the browser-DIRECT WS terminal / SSE / debug paths,
// which the browser hits at :8000 without a proxy in between).
//
// Why this is reliable: `Sec-Fetch-Site` is set by the browser and cannot be
// forged or removed by page JS, so any value other than same-origin is proof
// the request did not originate from our own page. For the rare client
// that omits it we fall back to comparing the Origin's host to the request Host
// (a cross-site POST always carries an Origin). Non-browser callers (curl,
// native apps) send neither signal and are NOT a CSRF vector — they carry no
// ambient credentials — so they pass here and are left to the backend's own
// token/loopback auth.
export function isCrossSiteRequest(req: NextRequest): boolean {
  const site = req.headers.get("sec-fetch-site");
  if (site) {
    // The legitimate frontend ALWAYS calls these routes same-origin (a relative
    // `fetch("/api/...")` from the app's own page), so only same-origin is ours.
    // Accept nothing else: `same-site` would trust any other origin on the same
    // host (e.g. a different localhost/LAN port), which could drive the
    // command-executing proxy via CSRF. `cross-site` / `none` are likewise not
    // from our page.
    return site !== "same-origin";
  }
  const origin = req.headers.get("origin");
  if (origin) {
    try {
      return new URL(origin).host !== req.headers.get("host");
    } catch {
      return true; // unparseable Origin -> treat as hostile
    }
  }
  return false; // no browser-origin signals -> not a cross-site browser request
}

// --- Host allowlist (DNS-rebinding defense) ---------------------------------
//
// isCrossSiteRequest() trusts Sec-Fetch-Site and (in its fallback) Origin.host
// == Host. Those defeat ordinary CSRF but NOT DNS rebinding: a page served from
// a public origin (http://evil.example:3000) whose name is rebound to 127.0.0.1
// issues a *relative* fetch the browser legitimately labels `same-origin`, so
// both signals pass — yet the request lands on our loopback proxy and, in the
// default tokenless mode, the loopback-trusted backend (arbitrary command
// execution). The one request property page JS cannot forge or remove is the
// Host header: the browser always sets it to the host the page was loaded from
// (the attacker's domain under rebinding), never to our own. So we additionally
// require Host to be one we actually serve on.
//
// Defaults mirror the backend's Origin allowlist (config.ALLOWED_ORIGIN_REGEX):
// loopback plus RFC1918 private-LAN ranges (network mode binds the LAN). Any
// port is accepted. Extend with VC_ALLOWED_HOSTS (comma-separated host or
// host:port values) for custom hostnames/front-door domains.
const PRIVATE_HOST_RE =
  /^(?:localhost|127\.\d{1,3}\.\d{1,3}\.\d{1,3}|::1|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})$/;

const EXTRA_HOSTS = new Set(
  (process.env.VC_ALLOWED_HOSTS || "")
    .split(",")
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean),
);

// Extract the hostname from a Host header value, stripping a trailing :port and
// IPv6 brackets ([::1]:3000 -> ::1, 127.0.0.1:3000 -> 127.0.0.1).
function hostnameOf(host: string): string {
  const h = host.trim().toLowerCase();
  if (h.startsWith("[")) {
    const end = h.indexOf("]");
    return end >= 0 ? h.slice(1, end) : h.slice(1);
  }
  const colon = h.lastIndexOf(":");
  if (colon >= 0 && /^\d+$/.test(h.slice(colon + 1))) {
    return h.slice(0, colon);
  }
  return h;
}

// Whether the request's Host header is one the legitimate frontend is served on.
// A missing Host (non-browser clients, which carry no ambient credentials and so
// are not a CSRF/rebinding vector) is left to the backend's own token/loopback
// auth, matching how isCrossSiteRequest() treats signal-less callers.
export function isAllowedHost(req: NextRequest): boolean {
  const host = req.headers.get("host");
  if (!host) return true;
  const full = host.trim().toLowerCase();
  if (EXTRA_HOSTS.has(full)) return true; // exact host:port match
  const name = hostnameOf(full);
  if (EXTRA_HOSTS.has(name)) return true; // host-only match (any port)
  return PRIVATE_HOST_RE.test(name);
}

// Returns a 403 response if the request is cross-site or targets an unexpected
// Host, else null. Call at the top of every /api/* route handler before doing
// any work or forwarding.
export function blockCrossSite(req: NextRequest): NextResponse | null {
  // Host allowlist first: the rebinding-resistant check. Reject before the
  // Sec-Fetch-Site/Origin heuristics, which a rebound same-origin request passes.
  if (!isAllowedHost(req)) {
    return NextResponse.json(
      { error: "host not allowed" },
      { status: 403 },
    );
  }
  if (isCrossSiteRequest(req)) {
    return NextResponse.json(
      { error: "cross-site request blocked" },
      { status: 403 },
    );
  }
  return null;
}
