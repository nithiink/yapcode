// Trusted-host (DNS-rebinding) check for the command-executing /api/* proxy.
//
// Kept dependency-free (no next/server import) so it can be unit-tested with
// Node's built-in runner, like lib/promptState.ts.
//
// Why this exists: the Sec-Fetch-Site / Origin checks in proxyAuth.ts cannot
// stop DNS rebinding. An attacker page served at http://evil.example:3000 that
// rebinds its own hostname to 127.0.0.1 makes a *genuinely* same-origin
// `fetch("/api/...")`: the browser honestly reports `Sec-Fetch-Site:
// same-origin` and the Origin host equals the Host, so both checks pass — yet
// the request now hits the victim's loopback-bound Next server, which forwards
// it (Origin-stripped) to a backend that trusts loopback in the zero-config
// default. The one signal the attack cannot fake is the Host header: it still
// carries the attacker's hostname, not a name the server actually listens on.
//
// So we require the request Host to be loopback or a private-LAN address — the
// only names yapcode binds (localhost in default mode, a private-LAN IP in
// network/phone mode) — with an optional VC_ALLOWED_HOSTS escape hatch
// (comma-separated hostnames) for non-standard deployments such as a tunnel.

const EXTRA_ALLOWED_HOSTS = (process.env.VC_ALLOWED_HOSTS || "")
  .split(",")
  .map((h) => h.trim().toLowerCase())
  .filter(Boolean);

export function isTrustedHost(hostHeader: string | null | undefined): boolean {
  if (!hostHeader) return false; // a browser always sends Host; absence is suspicious
  let host = hostHeader.trim().toLowerCase();
  // Strip the port, taking care with IPv6 literals.
  if (host.startsWith("[")) {
    const end = host.indexOf("]"); // [::1] or [::1]:3000 -> ::1
    host = end !== -1 ? host.slice(1, end) : host.slice(1);
  } else if (host.split(":").length <= 2) {
    const colon = host.indexOf(":"); // name:3000 / 127.0.0.1:3000 -> strip :port
    if (colon !== -1) host = host.slice(0, colon);
  }
  // else: a bracketless IPv6 literal (multiple colons, no port) — leave as-is.

  if (host === "localhost" || host === "::1") return true;
  if (EXTRA_ALLOWED_HOSTS.includes(host)) return true;

  // IPv4 literals: loopback 127.0.0.0/8 and the documented private-LAN ranges
  // (10/8, 192.168/16, 172.16/12) — the same ranges the backend's Origin
  // allowlist and next.config.mjs allowedDevOrigins trust.
  const m = host.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  if (!m) return false;
  const octets = m.slice(1).map((n) => Number(n));
  if (octets.some((n) => n > 255)) return false;
  const [a, b] = octets;
  if (a === 127) return true; // 127.0.0.0/8 loopback
  if (a === 10) return true; // 10.0.0.0/8
  if (a === 192 && b === 168) return true; // 192.168.0.0/16
  if (a === 172 && b >= 16 && b <= 31) return true; // 172.16.0.0/12
  return false;
}
