/** @type {import('next').NextConfig} */
const nextConfig = {
  env: {
    BACKEND_URL: process.env.BACKEND_URL || "http://localhost:8000",
    // Port for the browser-direct connections (live-terminal WS, debug stream)
    // that bypass the same-origin proxy. Defaults to the standard backend port.
    BACKEND_PORT: process.env.BACKEND_PORT || "8000",
  },
  // Allow loading the dev server's /_next/* resources (JS chunks, HMR) when the
  // app is opened from another device on the LAN — otherwise Next 16 blocks them
  // as cross-origin and the client never hydrates (toggles/buttons do nothing).
  // Private-range wildcards cover any LAN IP; add a specific origin here only
  // if your network uses something outside these ranges.
  allowedDevOrigins: [
    "192.168.*.*",
    "10.*.*.*",
    "172.16.*.*",
  ],
  // Security headers on every route. This app is the control surface for a
  // backend that turns clicks into real host command execution (permission
  // Approve/Deny, the "Auto" permission-mode toggle), so it must never be
  // framable by another origin — otherwise a decoy overlay on an attacker page
  // could clickjack an approval or a downgrade to auto mode (the same-origin
  // CSRF guard in lib/proxyAuth.ts does not stop UI redressing, since a click
  // inside the framed app is genuinely same-origin). The app is never
  // legitimately framed, so frame-ancestors 'none' + X-Frame-Options: DENY have
  // zero functional cost.
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Content-Security-Policy", value: "frame-ancestors 'none'" },
          { key: "X-Content-Type-Options", value: "nosniff" },
        ],
      },
    ];
  },
};
export default nextConfig;
