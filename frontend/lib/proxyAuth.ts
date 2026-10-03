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
// The cross-site check above trusts `Sec-Fetch-Site: same-origin`, but a DNS
// rebinding attack produces a GENUINELY same-origin request: after an attacker
// rebinds evil.com -> 127.0.0.1, the browser sends `fetch("/api/...")` with a
// real `Sec-Fetch-Site: same-origin` and `Host: evil.com`, so the cross-site
// gate passes and the request would be laundered into a trusted loopback call
// to the command-executing backend. The reliable defense is a Host allowlist:
// the proxy only ever legitimately serves the local app, reached at loopback or
// (network mode) a private-LAN IP. Rebinding can only ever present a DOMAIN NAME
// in Host (you cannot register a domain that is a raw private-IP literal), so
// allowing only loopback + private-LAN IP literals defeats it while leaving both
// localhost and LAN modes working. Operators fronting the app with a real
// hostname can allowlist it via ALLOWED_PROXY_HOSTS (comma-separated).
const _EXTRA_PROXY_HOSTS = new Set(
  (process.env.ALLOWED_PROXY_HOSTS || "")
    .split(",")
    .map((h) => h.trim().toLowerCase())
    .filter(Boolean),
);

export function isTrustedProxyHost(req: NextRequest): boolean {
  const hostHeader = req.headers.get("host");
  if (!hostHeader) return false; // no Host -> cannot vouch for it
  // Strip the port, handling bracketed IPv6 (e.g. "[::1]:3000").
  let hostname: string;
  if (hostHeader.startsWith("[")) {
    const end = hostHeader.indexOf("]");
    hostname = end === -1 ? hostHeader : hostHeader.slice(1, end);
  } else {
    const colon = hostHeader.lastIndexOf(":");
    hostname = colon === -1 ? hostHeader : hostHeader.slice(0, colon);
  }
  hostname = hostname.toLowerCase();
  if (hostname === "localhost" || hostname === "127.0.0.1" || hostname === "::1") {
    return true;
  }
  if (_EXTRA_PROXY_HOSTS.has(hostname)) return true;
  // Private-LAN IPv4 literals (network mode), mirroring the backend origin regex.
  return (
    /^10\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(hostname) ||
    /^192\.168\.\d{1,3}\.\d{1,3}$/.test(hostname) ||
    /^172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}$/.test(hostname)
  );
}

// Returns a 403 response if the request is cross-site OR arrives with an
// untrusted Host (DNS rebinding / off-host access), else null. Call at the top
// of every /api/* route handler before doing any work or forwarding.
export function blockCrossSite(req: NextRequest): NextResponse | null {
  if (!isTrustedProxyHost(req) || isCrossSiteRequest(req)) {
    return NextResponse.json(
      { error: "cross-site request blocked" },
      { status: 403 },
    );
  }
  return null;
}
