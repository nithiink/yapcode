/** @type {import('next').NextConfig} */
const nextConfig = {
  env: {
    BACKEND_URL: process.env.BACKEND_URL || "http://localhost:8000",
  },
  // Allow loading the dev server's /_next/* resources (JS chunks, HMR) when the
  // app is opened from another device on the LAN — otherwise Next 16 blocks them
  // as cross-origin and the client never hydrates (toggles/buttons do nothing).
  // Add your laptop's LAN IP(s) here; private-range wildcards cover IP changes.
  allowedDevOrigins: [
    "192.168.29.81",
    "192.168.*.*",
    "10.*.*.*",
    "172.16.*.*",
  ],
};
export default nextConfig;
