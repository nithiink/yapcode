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

// --- DNS-rebinding defense (positive Host allowlist) ------------------------
//
// `Sec-Fetch-Site` / `Origin === Host` alone cannot stop a DNS-rebinding attack.
// A page served from `http://attacker.example:3000` whose DNS is rebound to
// 127.0.0.1 issues a request the browser labels `Sec-Fetch-Site: same-origin`
// (the initiating page and the target share the identical textual origin, so the
// browser — which derives this from hostnames, not resolved IPs — treats it as
// same-origin), so isCrossSiteRequest() lets it through and the fallback
// `Origin.host === Host` also matches. The load-bearing defense is a POSITIVE
// Host allowlist: a rebound request necessarily carries `Host: attacker.example`,
// which is not one of our expected local/LAN hosts, so we reject it regardless
// of Sec-Fetch-Site. This mirrors the backend's config.origin_allowed allowlist,
// which the proxy hop hides from the backend (undici drops the browser Origin,
// so the backend never sees it for proxied requests).
//
// Hosts allowed by default: loopback (localhost / 127.0.0.1 / [::1]) and the
// private-LAN ranges (10/8, 192.168/16, 172.16-31/12) on any port — mirroring
// next.config.mjs's allowedDevOrigins so the phone/laptop can reach the app over
// the LAN in network mode. Extend with VC_ALLOWED_HOSTS (comma-separated
// host[:port]) for a deployment behind a custom hostname or reverse proxy.
const HOST_ALLOWLIST_RE =
  /^(?:localhost|127\.0\.0\.1|\[::1\]|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?::\d{1,5})?$/i;

function extraAllowedHosts(): string[] {
  return (process.env.VC_ALLOWED_HOSTS || "")
    .split(",")
    .map((h) => h.trim().toLowerCase())
    .filter(Boolean);
}

export function isAllowedHost(req: NextRequest): boolean {
  // HTTP/1.1+ always carries a Host header; a missing/empty one is not a request
  // from a normal browser reaching our own origin, so fail closed.
  const host = (req.headers.get("host") || "").trim().toLowerCase();
  if (!host) return false;
  if (HOST_ALLOWLIST_RE.test(host)) return true;
  return extraAllowedHosts().includes(host);
}

// Returns a 403 response if the request is cross-site, else null. Call at the
// top of every /api/* route handler before doing any work or forwarding.
export function blockCrossSite(req: NextRequest): NextResponse | null {
  // DNS-rebinding defense first: a rebound request looks same-origin to
  // isCrossSiteRequest() (its textual origin matches the target), so it would
  // pass the checks below. Validate the Host against a positive allowlist up
  // front — a rebound request carries an attacker Host that is not allowlisted.
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
