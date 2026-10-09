// Host-allowlist (DNS-rebinding defense) tests. Run with Node's built-in runner:
//     node --test lib/proxyAuth.test.ts     # from frontend/
//
// Targets the pure hostAllow.ts helper that proxyAuth.blockCrossSite enforces
// before forwarding any /api/* request to the loopback-trusted backend.

import { test } from "node:test";
import assert from "node:assert/strict";
import { hostIsAllowed } from "./hostAllow.ts";

test("loopback and LAN hosts the app is served on are allowed", () => {
  for (const h of [
    "localhost:3000",
    "127.0.0.1:3000",
    "[::1]:3000",
    "localhost",
    "192.168.1.50:3000",
    "10.0.0.7:3000",
    "172.16.5.9:3000",
    "172.31.255.255:8443",
  ]) {
    assert.equal(hostIsAllowed(h), true, `expected allowed: ${h}`);
  }
});

test("a rebound attacker hostname is rejected (the core defense)", () => {
  // DNS rebinding keeps the attacker's hostname in the Host header even though
  // the socket now points at 127.0.0.1 and the request is 'same-origin'.
  for (const h of [
    "attacker.com:3000",
    "evil.example:3000",
    "localhost.attacker.com:3000", // must not slip past the anchored regex
    "127.0.0.1.attacker.com:3000",
    "172.15.0.1:3000", // just outside the 172.16-31 private range
    "172.32.0.1:3000",
  ]) {
    assert.equal(hostIsAllowed(h), false, `expected rejected: ${h}`);
  }
});

test("a missing or blank Host fails closed", () => {
  assert.equal(hostIsAllowed(null), false);
  assert.equal(hostIsAllowed(undefined), false);
  assert.equal(hostIsAllowed(""), false);
  assert.equal(hostIsAllowed("   "), false);
});

test("Host matching is case-insensitive and trims surrounding whitespace", () => {
  assert.equal(hostIsAllowed("LOCALHOST:3000"), true);
  assert.equal(hostIsAllowed("  127.0.0.1:3000  "), true);
});
