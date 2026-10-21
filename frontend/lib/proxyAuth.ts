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

// --- DNS-rebinding defense --------------------------------------------------
//
// isCrossSiteRequest() leans on Sec-Fetch-Site / Origin, both of which assume
// the attacker's page lives at a DIFFERENT origin than ours. DNS rebinding
// breaks that assumption: a victim visits http://attacker.com:3000, the
// attacker then rebinds attacker.com to 127.0.0.1, and page JS issues a
// *relative* fetch("/api/...") that the browser now treats as same-origin
// (attacker.com:3000). Sec-Fetch-Site is same-origin and the Origin-host==Host
// fallback also matches, so the request sails into the command-executing proxy.
//
// The reliable defense is a Host-header allowlist: the browser sets Host to the
// hostname in the URL bar, which for a rebinding attack is the attacker's DNS
// name — never a loopback/LAN literal. yapcode is only ever served at loopback
// or a private-LAN IP (this mirrors the backend's _DEFAULT_ORIGIN_REGEX and
// next.config.mjs's allowedDevOrigins, and phone-url.mjs hands out exactly those
// URLs), so we require the request Host to be one of those. A custom hostname
// can be allowed by listing it (host[:port], comma-separated) in VC_ALLOWED_HOSTS.
// An IPv6 literal in a Host header is always bracketed ([::1] / [::1]:3000 per
// RFC 7230), so we match the bracketed loopback form only.
const TRUSTED_HOST_RE =
  /^(localhost|127\.0\.0\.1|\[::1\]|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})$/;

export function isTrustedHost(req: NextRequest): boolean {
  const host = (req.headers.get("host") || "").trim().toLowerCase();
  if (!host) return false; // no Host header -> can't vouch for the target; refuse
  const extra = (process.env.VC_ALLOWED_HOSTS || "")
    .split(",")
    .map((h) => h.trim().toLowerCase())
    .filter(Boolean);
  if (extra.includes(host)) return true;
  // Strip the port before matching: "127.0.0.1:3000" -> "127.0.0.1",
  // "[::1]:3000" -> "[::1]". A bare IPv6 literal keeps its brackets.
  const hostname = host.replace(/:\d+$/, "");
  return TRUSTED_HOST_RE.test(hostname);
}

// Returns a 403 response if the request is cross-site OR carries an untrusted
// Host header (DNS-rebinding guard), else null. Call at the top of every /api/*
// route handler before doing any work or forwarding.
export function blockCrossSite(req: NextRequest): NextResponse | null {
  // Host allowlist FIRST: a rebound attacker origin looks same-origin to
  // isCrossSiteRequest(), so this is the check that actually stops rebinding.
  if (!isTrustedHost(req)) {
    return NextResponse.json(
      { error: "untrusted Host header" },
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
