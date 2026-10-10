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

// --- DNS-rebinding defense (Host-header allowlist) --------------------------
//
// Sec-Fetch-Site / Origin checks CANNOT stop DNS rebinding: after an attacker
// rebinds their own hostname to 127.0.0.1, the victim's browser truthfully
// labels a relative `fetch("/api/...")` as same-origin and sends the attacker's
// hostname in the Host header, while the rebound TCP connection lands on the
// victim's local Next frontend — so the request sails past isCrossSiteRequest()
// and is proxied to the loopback backend (which trusts any loopback caller when
// VC_AUTH_TOKEN is unset) = drive-by command execution. The standard mitigation
// for a loopback service is to validate the Host against a fixed allowlist, which
// the attacker's public hostname can't satisfy.
//
// Allowed by default: loopback (localhost / 127.0.0.1 / [::1]) and private-LAN
// hosts (so network mode on a phone/laptop LAN IP keeps working) — mirrors the
// backend's ALLOWED_ORIGIN_REGEX and next.config.mjs's allowedDevOrigins. Any
// other host (a public rebinding domain) is rejected. Add exact extra hosts
// (e.g. a tunnel/MagicDNS name, "host" or "host:port") with VC_ALLOWED_HOSTS.
const TRUSTED_HOST_RE =
  /^(localhost|127\.0\.0\.1|\[::1\]|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(:\d{1,5})?$/;

const EXTRA_ALLOWED_HOSTS: ReadonlySet<string> = new Set(
  (process.env.VC_ALLOWED_HOSTS || "")
    .split(",")
    .map((h) => h.trim().toLowerCase())
    .filter(Boolean),
);

// True when the request's Host header is NOT one we expect — the DNS-rebinding
// signal. The legitimate frontend is always reached at a loopback or LAN host.
export function isUntrustedHost(req: NextRequest): boolean {
  const host = (req.headers.get("host") || "").trim().toLowerCase();
  if (!host) return true; // a real browser always sends Host; absence is suspicious
  if (EXTRA_ALLOWED_HOSTS.has(host)) return false;
  return !TRUSTED_HOST_RE.test(host);
}

// Returns a 403 response if the request is cross-site OR carries an unexpected
// Host (DNS rebinding), else null. Call at the top of every /api/* route handler
// before doing any work or forwarding.
export function blockCrossSite(req: NextRequest): NextResponse | null {
  if (isCrossSiteRequest(req)) {
    return NextResponse.json(
      { error: "cross-site request blocked" },
      { status: 403 },
    );
  }
  if (isUntrustedHost(req)) {
    return NextResponse.json(
      { error: "untrusted host" },
      { status: 403 },
    );
  }
  return null;
}
