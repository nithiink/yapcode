import { NextRequest, NextResponse } from "next/server";
import { forwardAuth, blockCrossSite } from "@/lib/proxyAuth";

const BACKEND = process.env.BACKEND_URL || "http://localhost:8000";

// Same-origin proxy for the Yuri control API (/yuri/*). Mirrors app/api/tools:
// no secret of its own, forwards the browser's token, rejects cross-site calls.
// The SSE stream (/yuri/events/stream) is browser-direct like /debug/stream.
async function proxy(req: NextRequest, path: string[]) {
  const blocked = blockCrossSite(req);
  if (blocked) return blocked;
  const qs = req.nextUrl.search || "";
  const init: RequestInit = { method: req.method, headers: forwardAuth(req, {}), cache: "no-store" };
  if (req.method !== "GET" && req.method !== "HEAD") {
    // Only declare a JSON body when there actually is one. A DELETE carries
    // none, and Content-Type: application/json over an empty body is a lie
    // some servers reject.
    const body = await req.text();
    if (body) {
      (init.headers as Record<string, string>)["Content-Type"] = "application/json";
      init.body = body;
    }
  }
  const resp = await fetch(`${BACKEND}/yuri/${path.map(encodeURIComponent).join("/")}${qs}`, init);
  const text = await resp.text();
  return new NextResponse(text, { status: resp.status, headers: { "Content-Type": "application/json" } });
}

type Ctx = { params: Promise<{ path: string[] }> };

export async function GET(req: NextRequest, ctx: Ctx) {
  return proxy(req, (await ctx.params).path);
}

export async function POST(req: NextRequest, ctx: Ctx) {
  return proxy(req, (await ctx.params).path);
}

// proxy() is already method-agnostic (it forwards req.method and the body), so
// each of these is just the export Next.js needs to route the method at all.
// Miss one and the endpoint answers 405 no matter what the backend supports —
// which is how the narration toggle, and later the mission delete, each shipped
// with a working backend route the UI could not reach.
export async function PUT(req: NextRequest, ctx: Ctx) {
  return proxy(req, (await ctx.params).path);
}

export async function DELETE(req: NextRequest, ctx: Ctx) {
  return proxy(req, (await ctx.params).path);
}
