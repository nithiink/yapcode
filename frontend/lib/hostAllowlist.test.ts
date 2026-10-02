// DNS-rebinding / Host-allowlist tests for the /api/* proxy CSRF defense.
// Run with Node's built-in runner (no deps):
//     node --test lib/hostAllowlist.test.ts     # from frontend/

import { test } from "node:test";
import assert from "node:assert/strict";
import { isAllowedLocalHost } from "./hostAllowlist.ts";

test("loopback and private-LAN hosts are allowed", () => {
  for (const host of [
    "localhost:3000", "127.0.0.1:3000", "localhost", "127.0.0.1",
    "[::1]:3000", "[::1]", "192.168.1.5:3000", "10.0.0.9", "172.16.4.2:8080",
    "LOCALHOST:3000",
  ]) {
    assert.equal(isAllowedLocalHost(host), true, host);
  }
});

test("a rebound public hostname is rejected (DNS-rebinding defense)", () => {
  // The crux: after rebinding evil.example -> 127.0.0.1 the browser still sends
  // the attacker's domain in the Host header, so it must not match.
  for (const host of ["evil.example:3000", "evil.example", "attacker.test:3000",
                      "169.254.1.1", "8.8.8.8:3000", "172.32.0.1"]) {
    assert.equal(isAllowedLocalHost(host), false, host);
  }
});

test("a missing Host header is rejected", () => {
  assert.equal(isAllowedLocalHost(null), false);
  assert.equal(isAllowedLocalHost(undefined), false);
  assert.equal(isAllowedLocalHost(""), false);
});

test("VC_ALLOWED_HOSTS extends the allowlist for custom local names", () => {
  const prev = process.env.VC_ALLOWED_HOSTS;
  process.env.VC_ALLOWED_HOSTS = "mybox.local, dev.internal:3000";
  try {
    assert.equal(isAllowedLocalHost("mybox.local:3000"), true); // hostname match
    assert.equal(isAllowedLocalHost("dev.internal:3000"), true); // host:port match
    assert.equal(isAllowedLocalHost("other.example"), false);
  } finally {
    if (prev === undefined) delete process.env.VC_ALLOWED_HOSTS;
    else process.env.VC_ALLOWED_HOSTS = prev;
  }
});
