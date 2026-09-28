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
// The same-origin / Origin-host checks above are name-based, so they cannot tell
// a rebound attacker hostname (evil.example, re-pointed at 127.0.0.1) apart from
// our own server: under DNS rebinding the malicious page and the local server
// share an origin, so `Sec-Fetch-Site: same-origin` and `Origin.host === Host`
// both pass and the command-executing proxy would be driven from an arbitrary
// site. DNS rebinding fundamentally requires a DNS *name* to rebind, so we
// require the Host header to be a literal IP (loopback or LAN) or `localhost` —
// which every legitimate access pattern uses (dev binds 127.0.0.1; network mode
// is reached at a LAN IP), while a rebound domain name is rejected. Extra
// hostnames (e.g. an mDNS `.local` name) can be allowed via VC_ALLOWED_HOSTS
// (comma-separated hostnames, no port).
const EXTRA_ALLOWED_HOSTS: Set<string> = new Set(
  (process.env.VC_ALLOWED_HOSTS || "")
    .split(",")
    .map((h) => h.trim().toLowerCase())
    .filter(Boolean),
);

function hostnameOf(hostHeader: string): string {
  // Strip a :port suffix. IPv6 literals arrive bracketed ("[::1]:3000"); keep
  // the bracketed form as the hostname after removing the port.
  const h = hostHeader.trim().toLowerCase();
  if (h.startsWith("[")) {
    const end = h.indexOf("]");
    return end === -1 ? h : h.slice(0, end + 1);
  }
  const colon = h.indexOf(":");
  return colon === -1 ? h : h.slice(0, colon);
}

function isAllowedHost(hostHeader: string | null): boolean {
  if (!hostHeader) return false; // HTTP/1.1 requires Host; treat absence as hostile
  const host = hostnameOf(hostHeader);
  if (!host) return false;
  if (host === "localhost") return true;
  if (host === "[::1]") return true; // bracketed IPv6 loopback
  if (EXTRA_ALLOWED_HOSTS.has(host)) return true;
  // IPv4 literal (dotted quad) — loopback or LAN.
  if (/^\d{1,3}(?:\.\d{1,3}){3}$/.test(host)) return true;
  // Bracketed IPv6 literal, e.g. "[fe80::1]".
  if (/^\[[0-9a-f:]+\]$/.test(host)) return true;
  // Anything else is a DNS name — reject (defeats DNS rebinding).
  return false;
}

// Returns a 403 response if the request is cross-site OR its Host header is not
// an allowed literal-IP/localhost host (DNS-rebinding defense), else null. Call
// at the top of every /api/* route handler before doing any work or forwarding.
export function blockCrossSite(req: NextRequest): NextResponse | null {
  if (!isAllowedHost(req.headers.get("host"))) {
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
