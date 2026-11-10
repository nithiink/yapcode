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

// --- Host (DNS-rebinding) defense -------------------------------------------
//
// `Sec-Fetch-Site` alone does NOT stop DNS rebinding. An attacker page served
// from http://attacker.com:3000 that rebinds attacker.com -> 127.0.0.1 is
// same-origin WITH ITSELF, so the browser sends `Sec-Fetch-Site: same-origin`
// and isCrossSiteRequest() passes — yet the request now reaches our loopback
// proxy, which forwards to a backend that executes commands and, in the default
// no-token localhost mode, trusts any loopback client. The request's `Host`
// header, however, still carries the attacker's hostname (attacker.com:3000),
// never a loopback/LAN host, because Host reflects the URL the browser was given,
// not the IP it resolved to. So we reject any request whose Host is not an
// expected local hostname before forwarding. Operators fronting the app with a
// real hostname can allowlist it via PROXY_ALLOWED_HOSTS (comma-separated
// host[:port] entries or bare hostnames).
const LOOPBACK_HOST_NAMES = new Set(["localhost", "127.0.0.1", "::1"]);

// Private-LAN IPv4 literals (any port) — mirrors the backend's
// ALLOWED_ORIGIN_REGEX so phone/laptop access over the LAN in network mode keeps
// working. A rebinding attack always carries a *hostname* Host, never a bare
// private-IP literal, so allowing these literals does not reopen the hole.
const PRIVATE_LAN_IPV4 =
  /^(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})$/;

// Extract the hostname from a Host header value, dropping any port. Handles
// bracketed IPv6 ("[::1]:3000"), bare IPv6 ("::1"), and "host:port".
function hostnameOf(host: string): string {
  const h = host.trim().toLowerCase();
  if (h.startsWith("[")) {
    const end = h.indexOf("]");
    return end === -1 ? h.slice(1) : h.slice(1, end);
  }
  // A bare IPv6 literal (no brackets, no port) has multiple colons; a
  // "hostname:port" has exactly one.
  if ((h.match(/:/g) || []).length > 1) return h;
  const colon = h.lastIndexOf(":");
  return colon === -1 ? h : h.slice(0, colon);
}

function extraAllowedHosts(): Set<string> {
  return new Set(
    (process.env.PROXY_ALLOWED_HOSTS || "")
      .split(",")
      .map((s) => s.trim().toLowerCase())
      .filter(Boolean),
  );
}

// True if the request's Host header is NOT an expected local/allowlisted host.
// A missing Host is treated as untrusted (browsers always send one).
export function isUntrustedHost(req: NextRequest): boolean {
  const host = req.headers.get("host");
  if (!host) return true;
  const extra = extraAllowedHosts();
  if (extra.has(host.trim().toLowerCase())) return false; // full host[:port] match
  const name = hostnameOf(host);
  if (extra.has(name)) return false; // bare hostname match
  if (LOOPBACK_HOST_NAMES.has(name)) return false;
  if (PRIVATE_LAN_IPV4.test(name)) return false;
  return true;
}

// Returns a 403 response if the request is cross-site OR targets an untrusted
// Host (DNS-rebinding), else null. Call at the top of every /api/* route handler
// before doing any work or forwarding.
export function blockCrossSite(req: NextRequest): NextResponse | null {
  if (isCrossSiteRequest(req) || isUntrustedHost(req)) {
    return NextResponse.json(
      { error: "cross-site request blocked" },
      { status: 403 },
    );
  }
  return null;
}
