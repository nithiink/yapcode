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
// `Sec-Fetch-Site` cannot catch DNS rebinding: a page served from
// `http://evil.com:3000`, after re-resolving evil.com to 127.0.0.1, fetches its
// OWN origin, so the browser legitimately stamps `Sec-Fetch-Site: same-origin`
// (it is computed from the URL origin tuple, before DNS) and isCrossSiteRequest()
// passes. In the default zero-config mode the Next server binds 127.0.0.1, the
// proxy forwards no token, and the backend trusts loopback — so a rebound page
// gets real command execution. The one remaining tell is the `Host` header, which
// still carries the attacker's public domain (`evil.com:3000`), never a loopback
// name.
//
// So: when NO shared-secret token rides the request (i.e. the loopback-trusted
// mode — any exposed/network deployment requires and presents VC_AUTH_TOKEN,
// which is the real boundary there and which a cross-origin/rebound page cannot
// read), require the Host to be a loopback name. This blocks the rebound Host
// without affecting (a) legitimate loopback traffic, whose Host is always
// localhost/127.0.0.1/::1 since the server binds 127.0.0.1, or (b) any
// token-authenticated request, regardless of its hostname.
const LOOPBACK_HOSTNAMES = new Set(["localhost", "127.0.0.1", "::1"]);

function hostHostname(req: NextRequest): string | null {
  const host = req.headers.get("host");
  if (!host) return null;
  // IPv6 literal, e.g. "[::1]:3000" -> "::1"
  if (host.startsWith("[")) {
    const end = host.indexOf("]");
    return end > 0 ? host.slice(1, end).toLowerCase() : null;
  }
  // "host:port" -> "host"
  return host.split(":")[0].toLowerCase();
}

// True when the request is a DNS-rebinding risk: no shared-secret token is
// presented (loopback-trusted mode) AND the Host is not a loopback name.
export function isRebindingRisk(req: NextRequest): boolean {
  const hasToken =
    req.headers.get("x-vc-token") || req.headers.get("authorization");
  if (hasToken) return false; // token mode: the backend token check is the boundary
  const hn = hostHostname(req);
  if (hn === null) return false; // no Host (non-browser clients) -> backend loopback auth applies
  return !LOOPBACK_HOSTNAMES.has(hn);
}

// Returns a 403 response if the request is cross-site or a DNS-rebinding risk,
// else null. Call at the top of every /api/* route handler before doing any work
// or forwarding.
export function blockCrossSite(req: NextRequest): NextResponse | null {
  if (isCrossSiteRequest(req) || isRebindingRisk(req)) {
    return NextResponse.json(
      { error: "cross-site request blocked" },
      { status: 403 },
    );
  }
  return null;
}
