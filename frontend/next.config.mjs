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
};
export default nextConfig;
