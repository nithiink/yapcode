// DNS-rebinding defense for the command-executing /api/* proxy — pure, Next-free
// logic so it can be unit-tested with Node's built-in runner (see
// proxyAuth.ts for how it is wired into blockCrossSite).
//
// Sec-Fetch-Site / Origin alone cannot protect a loopback-trusting proxy: both
// describe the URL the browser believes it is talking to, not the socket's real
// IP. Under DNS rebinding an attacker serves a page from a domain they control
// (e.g. http://evil.example:3000), then rebinds that name to 127.0.0.1. The
// victim's browser then reaches the local dev server while still labeling the
// request `same-origin`, so the origin signals are fooled. Pinning the request
// Host to the local addresses the frontend is actually served at defeats the
// rebind: a rebound public hostname (evil.example) still rides in the Host
// header and never matches loopback / private-LAN literals.
//
// Extend the allowlist with VC_ALLOWED_HOSTS (comma-separated host or host:port)
// for custom local hostnames.
const LOCAL_HOST_RE =
  /^(?:localhost|127\.0\.0\.1|::1|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})$/;

// Strip the optional :port, handling bracketed IPv6 ("[::1]:3000" -> "::1").
function hostname(hostHeader: string): string {
  const h = hostHeader.trim();
  if (h.startsWith("[")) {
    const end = h.indexOf("]");
    return end >= 0 ? h.slice(1, end) : h;
  }
  const colon = h.indexOf(":");
  return colon >= 0 ? h.slice(0, colon) : h;
}

export function isAllowedLocalHost(hostHeader: string | null | undefined): boolean {
  if (!hostHeader) return false; // no Host -> can't validate -> treat as hostile
  const name = hostname(hostHeader).toLowerCase();
  if (LOCAL_HOST_RE.test(name)) return true;
  const extra = (process.env.VC_ALLOWED_HOSTS || "")
    .split(",")
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean);
  // Accept a configured value matching either the bare hostname or full host:port.
  return extra.some((e) => e === name || e === hostHeader.trim().toLowerCase());
}
