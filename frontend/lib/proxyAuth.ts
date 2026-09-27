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

// --- DNS-rebinding defense (Host allowlist) ---------------------------------
//
// Sec-Fetch-Site / Origin above cannot stop DNS rebinding. Once an attacker
// rebinds their OWN hostname to 127.0.0.1, a request from their page to our port
// is GENUINELY same-origin — nothing is forged — so isCrossSiteRequest() returns
// false and the command-executing proxy would forward it to the loopback-trusted
// backend (which, in the default no-token localhost mode, runs the tool). The
// standard defense is a Host allowlist: the legitimate app is only ever reached
// at a loopback address (localhost run mode) or a private-range LAN IP (network
// mode) — never at a public domain name, which is exactly what a rebinding
// attack MUST use to get the browser to treat it as same-origin. So we reject
// any Host that isn't loopback or a private LAN address. Custom hostnames (e.g.
// behind a reverse proxy) can be permitted via VC_ALLOWED_HOSTS (comma-separated
// host[:port] or bare-host values).
const PRIVATE_IPV4 =
  /^(?:127\.\d{1,3}\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|169\.254\.\d{1,3}\.\d{1,3})$/;

// The hostname (no port) of a Host header, lowercased. Handles bracketed IPv6
// literals ("[::1]:3000" -> "::1") and the common "host:port" form.
function hostnameOf(hostHeader: string): string {
  const h = hostHeader.trim().toLowerCase();
  if (h.startsWith("[")) {
    const end = h.indexOf("]");
    return end >= 0 ? h.slice(1, end) : h.slice(1);
  }
  const colon = h.indexOf(":");
  return colon >= 0 ? h.slice(0, colon) : h;
}

// True if the request's Host header names an address the app is legitimately
// served at (loopback or a private LAN range), or one explicitly allowlisted via
// VC_ALLOWED_HOSTS. A missing Host (browsers always send one) is treated as
// hostile. This is what closes the DNS-rebinding gap that Sec-Fetch-Site/Origin
// structurally cannot.
export function isAllowedHost(req: NextRequest): boolean {
  const hostHeader = req.headers.get("host");
  if (!hostHeader) return false;
  const host = hostnameOf(hostHeader);
  if (host === "localhost" || host === "::1") return true;
  if (PRIVATE_IPV4.test(host)) return true;
  const extra = (process.env.VC_ALLOWED_HOSTS || "")
    .split(",")
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean);
  // Match either the full host[:port] value or the bare hostname.
  return (
    extra.includes(hostHeader.trim().toLowerCase()) || extra.includes(host)
  );
}

// Returns a 403 response if the request is cross-site OR its Host header is not
// an allowed local address (DNS-rebinding defense), else null. Call at the top of
// every /api/* route handler before doing any work or forwarding.
export function blockCrossSite(req: NextRequest): NextResponse | null {
  if (isCrossSiteRequest(req) || !isAllowedHost(req)) {
    return NextResponse.json(
      { error: "cross-site request blocked" },
      { status: 403 },
    );
  }
  return null;
}
