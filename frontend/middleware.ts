import { NextRequest, NextResponse } from "next/server";

// Anti-DNS-rebinding guard for the /api/* proxy.
//
// The /api/* routes proxy into a backend that turns requests into real command
// execution. In the default loopback mode (run.sh, no VC_AUTH_TOKEN) the backend
// trusts any localhost client and the proxy holds no secret, so the proxy's only
// drive-by defense is blockCrossSite()'s Sec-Fetch-Site check. A DNS-rebinding
// attacker (evil.com rebound to 127.0.0.1) issues a RELATIVE fetch that the
// browser labels genuinely same-origin, so Sec-Fetch-Site: same-origin is sent
// honestly and that gate passes — yielding unauthenticated command execution.
//
// The rebound request still carries the attacker's hostname in the Host header,
// so validate it here against an allowlist (loopback + private-LAN, mirroring the
// backend's ALLOWED_ORIGIN regex). A rebound public host (e.g. evil.com) is
// rejected. Sec-Fetch-Site: same-origin is necessary but NOT sufficient.
//
// Custom deployments (a real hostname behind a reverse proxy) can extend the
// allowlist via YAPCODE_ALLOWED_HOSTS (comma-separated host[:port] or bare host).

function extraAllowedHosts(): Set<string> {
  const raw = process.env.YAPCODE_ALLOWED_HOSTS || "";
  return new Set(
    raw
      .split(",")
      .map((h) => h.trim().toLowerCase())
      .filter(Boolean),
  );
}

function hostAllowed(hostHeader: string): boolean {
  if (!hostHeader) return false;
  const host = hostHeader.trim().toLowerCase();

  // Operator-provided exact allowlist (matched with and without the port).
  const extra = extraAllowedHosts();
  if (extra.has(host)) return true;

  // Strip the port and IPv6 brackets to get the bare hostname/IP.
  const name = host.replace(/:\d+$/, "").replace(/^\[/, "").replace(/\]$/, "");
  if (extra.has(name)) return true;

  if (name === "localhost" || name === "127.0.0.1" || name === "::1") return true;

  // Private-LAN ranges (network mode: phone/laptop reach the app over the LAN) —
  // mirrors backend config._DEFAULT_ORIGIN_REGEX and next.config allowedDevOrigins.
  if (/^10\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(name)) return true;
  if (/^192\.168\.\d{1,3}\.\d{1,3}$/.test(name)) return true;
  if (/^172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}$/.test(name)) return true;

  return false;
}

export function middleware(req: NextRequest) {
  const host = req.headers.get("host") || "";
  if (!hostAllowed(host)) {
    return NextResponse.json({ error: "host not allowed" }, { status: 403 });
  }
  return NextResponse.next();
}

// Guard only the command-executing proxy surface.
export const config = {
  matcher: "/api/:path*",
};
