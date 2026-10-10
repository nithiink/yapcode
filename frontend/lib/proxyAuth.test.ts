// DNS-rebinding / CSRF proxy-guard tests. Run with Node's built-in runner:
//     node --test lib/proxyAuth.test.ts     # from frontend/ (after npm install)

import { test } from "node:test";
import assert from "node:assert/strict";
import { isUntrustedHost, isCrossSiteRequest, blockCrossSite } from "./proxyAuth.ts";
import type { NextRequest } from "next/server";

// Minimal stand-in: the guards only read req.headers.get(...).
function reqWith(headers: Record<string, string>): NextRequest {
  return { headers: new Headers(headers) } as unknown as NextRequest;
}

test("loopback and LAN hosts are trusted", () => {
  for (const host of [
    "localhost:3000",
    "127.0.0.1:3000",
    "[::1]:3000",
    "192.168.1.5:3000",
    "10.0.0.9:3000",
    "172.16.5.5:3000",
  ]) {
    assert.equal(isUntrustedHost(reqWith({ host })), false, host);
  }
});

test("a public (DNS-rebinding) host is untrusted", () => {
  for (const host of [
    "attacker.com:3000", // classic rebinding domain
    "evil.example",
    "127.0.0.1.evil.com", // suffix trick must not match the loopback prefix
    "localhostx:3000",
    "172.32.5.5:3000", // just outside the 172.16-31 private range
    "", // a real browser always sends Host; absence is suspicious
  ]) {
    assert.equal(isUntrustedHost(reqWith({ host })), true, host);
  }
});

test("blockCrossSite rejects a same-origin request with a rebinding Host", () => {
  // Under DNS rebinding the browser truthfully reports same-origin, so only the
  // Host allowlist can catch it.
  const res = blockCrossSite(
    reqWith({ "sec-fetch-site": "same-origin", host: "attacker.com:3000" }),
  );
  assert.ok(res, "expected a 403 response, got null");
  assert.equal(res!.status, 403);
});

test("blockCrossSite passes a legitimate same-origin localhost request", () => {
  const res = blockCrossSite(
    reqWith({ "sec-fetch-site": "same-origin", host: "localhost:3000" }),
  );
  assert.equal(res, null);
});

test("blockCrossSite still rejects a cross-site request", () => {
  const res = blockCrossSite(
    reqWith({ "sec-fetch-site": "cross-site", host: "localhost:3000" }),
  );
  assert.ok(res);
  assert.equal(res!.status, 403);
});

test("isCrossSiteRequest treats same-origin as not cross-site", () => {
  assert.equal(
    isCrossSiteRequest(reqWith({ "sec-fetch-site": "same-origin" })),
    false,
  );
});
