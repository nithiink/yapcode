// Host-header allowlist (DNS-rebinding defense). Pure string logic with no Next
// dependency, so it stays unit-testable in isolation (Node's built-in runner).
//
// isCrossSiteRequest (proxyAuth.ts) relies on Sec-Fetch-Site / Origin, which DNS
// rebinding does not FORGE — it makes the request GENUINELY same-origin: a page
// served from http://attacker.com:3000 that rebinds attacker.com -> 127.0.0.1
// reaches the real local Next server while its origin stays attacker.com:3000,
// so a relative fetch("/api/...") sails past that check. The one thing that
// still distinguishes the attack is the network-layer target: the Host header
// carries the attacker's hostname. Pinning Host to the local/LAN addresses the
// app is actually served on closes the rebind; the loopback-trusting backend
// sits behind this proxy, so the proxy is where it must be enforced.
//
// Allowed by default: loopback (localhost / 127.0.0.1 / [::1]) and the private
// LAN ranges network mode uses — mirrors backend config's ALLOWED_ORIGIN_REGEX
// and next.config.mjs's allowedDevOrigins. Add a custom host (e.g. an mDNS name)
// via VC_ALLOWED_HOSTS (comma-separated `host` or `host:port`).
const HOST_ALLOW_RE =
  /^(localhost|127\.0\.0\.1|\[::1\]|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(:\d{1,5})?$/;

// Whether a Host header targets an address the app is legitimately served on.
// A missing/blank Host can't be verified, so it is rejected (fail closed).
export function hostIsAllowed(host: string | null | undefined): boolean {
  if (!host) return false;
  const h = host.trim().toLowerCase();
  if (!h) return false;
  if (HOST_ALLOW_RE.test(h)) return true;
  const extra = (process.env.VC_ALLOWED_HOSTS || "")
    .split(",")
    .map((x) => x.trim().toLowerCase())
    .filter(Boolean);
  return extra.includes(h);
}
