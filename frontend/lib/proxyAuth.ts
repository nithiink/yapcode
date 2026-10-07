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
// isCrossSiteRequest() cannot stop DNS rebinding: an attacker page served from
// `attacker.com` (rebound to 127.0.0.1) makes a GENUINELY same-origin request, so
// the browser truthfully sends `Sec-Fetch-Site: same-origin` and the request
// sails past the cross-site check — reaching the loopback-trusted backend. The
// canonical defense is to validate the Host header against an allowlist of the
// hostnames this app is actually served on, which `Sec-Fetch-Site` cannot provide.
//
// Legitimate Hosts: localhost / loopback (default mode) and private-LAN IP
// literals (network mode, served at http(s)://<lan-ip>:3000). A rebinding attack
// carries the attacker's DOMAIN in the Host header (e.g. `attacker.com:3000`),
// never a loopback name or LAN-IP literal, so it is rejected. Custom hostnames
// used to reach the app can be allowlisted via VC_ALLOWED_HOSTS (comma-separated
// host or host:port entries).
const _LOOPBACK_HOSTNAMES = new Set(["localhost", "127.0.0.1", "::1", "[::1]"]);

function _isPrivateLanIp(hostname: string): boolean {
  // IPv4 private ranges: 10/8, 192.168/16, 172.16-31/12. Mirrors the backend's
  // allowed-origin regex so network-mode LAN access keeps working.
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(hostname);
  if (!m) return false;
  const [a, b] = [Number(m[1]), Number(m[2])];
  if ([a, b, Number(m[3]), Number(m[4])].some((n) => n > 255)) return false;
  if (a === 10) return true;
  if (a === 192 && b === 168) return true;
  if (a === 172 && b >= 16 && b <= 31) return true;
  return false;
}

function _extraAllowedHosts(): Set<string> {
  // Full host entries (host or host:port), matched against the raw Host header.
  return new Set(
    (process.env.VC_ALLOWED_HOSTS || "")
      .split(",")
      .map((h) => h.trim().toLowerCase())
      .filter(Boolean),
  );
}

export function isAllowedHost(req: NextRequest): boolean {
  const rawHost = (req.headers.get("host") || "").trim().toLowerCase();
  if (!rawHost) return false; // no Host -> can't vouch for it -> reject
  if (_extraAllowedHosts().has(rawHost)) return true;
  // Strip the port to classify the hostname (bracketed IPv6 kept intact).
  let hostname = rawHost;
  if (rawHost.startsWith("[")) {
    hostname = rawHost.slice(0, rawHost.indexOf("]") + 1) || rawHost;
  } else if (rawHost.includes(":")) {
    hostname = rawHost.slice(0, rawHost.lastIndexOf(":"));
  }
  if (_LOOPBACK_HOSTNAMES.has(hostname)) return true;
  if (_isPrivateLanIp(hostname)) return true;
  return false;
}

// Returns a 403 response if the request is cross-site OR carries an unexpected
// Host (DNS-rebinding), else null. Call at the top of every /api/* route handler
// before doing any work or forwarding.
export function blockCrossSite(req: NextRequest): NextResponse | null {
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
