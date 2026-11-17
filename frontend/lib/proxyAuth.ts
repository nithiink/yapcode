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
//
// Anti-DNS-rebinding: the signals above are NOT sufficient on their own. Under a
// DNS-rebinding attack the attacker serves a page from `evil.com` and rebinds
// that name to 127.0.0.1, so the page and the local target share the origin
// `evil.com:3000`. The browser then LEGITIMATELY labels the request
// `Sec-Fetch-Site: same-origin` and `Origin.host === Host`, so both checks below
// pass. The only reliable tell is the `Host` header itself: our server only ever
// answers to its own localhost / private-LAN identity, never to a public domain.
// So we pin an allowlist of Hosts the proxy is willing to serve (mirrors the
// backend's VC_ALLOWED_ORIGIN_REGEX host set in backend/config.py). This also
// keeps the token-gated network/phone mode working (private-LAN Host), where the
// token already defeats rebinding, and can be overridden via
// VC_ALLOWED_HOST_REGEX (set empty to disable the Host check).
const _DEFAULT_HOST_REGEX =
  "^(" +
  "localhost|127\\.0\\.0\\.1|\\[::1\\]|" +
  "10\\.\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}|" +
  "192\\.168\\.\\d{1,3}\\.\\d{1,3}|" +
  "172\\.(1[6-9]|2\\d|3[01])\\.\\d{1,3}\\.\\d{1,3}" +
  ")(:\\d{1,5})?$";
const _hostRegexRaw = process.env.VC_ALLOWED_HOST_REGEX;
const _HOST_RE =
  _hostRegexRaw === undefined
    ? new RegExp(_DEFAULT_HOST_REGEX)
    : _hostRegexRaw.trim()
      ? new RegExp(_hostRegexRaw.trim())
      : null; // explicitly disabled

// Whether the request's Host header names a host this proxy is willing to serve.
// A missing Host (HTTP/1.1 requires one) or any public domain -> not allowed,
// which is treated as a cross-site (rebinding) request by isCrossSiteRequest.
export function isAllowedHost(req: NextRequest): boolean {
  if (!_HOST_RE) return true; // Host check disabled via empty VC_ALLOWED_HOST_REGEX
  const host = req.headers.get("host");
  if (!host) return false;
  return _HOST_RE.test(host);
}

export function isCrossSiteRequest(req: NextRequest): boolean {
  // Anti-DNS-rebinding Host-allowlist check — evaluated FIRST, because under
  // rebinding the Sec-Fetch-Site / Origin signals below genuinely say
  // "same-origin" and would otherwise let the request through. A Host that is
  // not our own localhost/LAN identity (e.g. an attacker's rebound public
  // domain) is treated as hostile.
  if (!isAllowedHost(req)) return true;

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

// Returns a 403 response if the request is cross-site, else null. Call at the
// top of every /api/* route handler before doing any work or forwarding.
export function blockCrossSite(req: NextRequest): NextResponse | null {
  if (isCrossSiteRequest(req)) {
    return NextResponse.json(
      { error: "cross-site request blocked" },
      { status: 403 },
    );
  }
  return null;
}
