// DNS-rebinding / trusted-host tests. Run with Node's built-in runner (no deps):
//     node --test lib/trustedHost.test.ts     # from frontend/

import { test } from "node:test";
import assert from "node:assert/strict";
import { isTrustedHost } from "./trustedHost.ts";

test("loopback hosts are trusted (localhost / 127.x / ::1, with or without port)", () => {
  for (const h of [
    "localhost",
    "localhost:3000",
    "127.0.0.1",
    "127.0.0.1:8000",
    "127.5.6.7",
    "::1",
    "[::1]",
    "[::1]:3000",
  ]) {
    assert.equal(isTrustedHost(h), true, h);
  }
});

test("documented private-LAN ranges are trusted (10/8, 192.168/16, 172.16/12)", () => {
  for (const h of [
    "10.0.0.5:8000",
    "192.168.1.42:3000",
    "172.16.3.4",
    "172.31.255.255:3000",
  ]) {
    assert.equal(isTrustedHost(h), true, h);
  }
});

test("public hostnames and IPs are rejected — the core DNS-rebinding defense", () => {
  // After rebinding attacker.com -> 127.0.0.1 the Host header still says
  // attacker.com, which must NOT be trusted even though the request is now
  // (genuinely) same-origin and loopback-delivered.
  for (const h of [
    "attacker.com",
    "attacker.com:3000",
    "evil.example",
    "8.8.8.8",
    "169.254.1.1", // link-local, not in the trusted set
  ]) {
    assert.equal(isTrustedHost(h), false, h);
  }
});

test("near-miss / malformed hosts are rejected (no prefix or substring escapes)", () => {
  for (const h of [
    "127.0.0.1.evil.com", // trailing label must not match the IPv4 regex
    "172.15.0.1", // just below the 172.16/12 range
    "172.32.0.1", // just above the 172.16/12 range
    "999.1.1.1", // octet out of range
    "", // empty
    null, // absent Host header
    undefined,
  ]) {
    assert.equal(isTrustedHost(h), false, String(h));
  }
});
