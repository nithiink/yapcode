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
  // Security headers. This UI drives a command-executing backend (flip a session
  // to `auto`, approve a pending permission), and the app is never meant to be
  // embedded, so deny all framing to block clickjacking / UI-redressing: an
  // invisible <iframe> of http://localhost:3000 under decoy UI could otherwise
  // land same-origin clicks on those controls. frame-ancestors 'none' is the
  // modern equivalent; X-Frame-Options: DENY covers older browsers. nosniff
  // stops content-type sniffing. These are scoped deliberately narrow — no
  // default-src CSP — so the WebRTC / WebSocket / realtime-provider connections
  // the voice app relies on are untouched.
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
