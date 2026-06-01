import { NextRequest, NextResponse } from "next/server";
import { forwardAuth } from "@/lib/proxyAuth";

const BACKEND = process.env.BACKEND_URL || "http://localhost:8000";

// Cost-log writes are tiny and frequent — keep them snappy and fire-and-forget
// from the UI's perspective (the UI doesn't care about the response body).
export async function POST(req: NextRequest) {
  const body = await req.text();
  const resp = await fetch(`${BACKEND}/cost/log`, {
    method: "POST",
    headers: forwardAuth(req, { "Content-Type": "application/json" }),
    body,
  });
  const text = await resp.text();
  return new NextResponse(text, {
    status: resp.status,
    headers: { "Content-Type": "application/json" },
  });
}
