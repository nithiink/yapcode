import { NextRequest, NextResponse } from "next/server";
import { forwardAuth } from "@/lib/proxyAuth";

const BACKEND = process.env.BACKEND_URL || "http://localhost:8000";

export async function GET(req: NextRequest) {
  const resp = await fetch(`${BACKEND}/tools`, {
    cache: "no-store",
    headers: forwardAuth(req),
  });
  const text = await resp.text();
  return new NextResponse(text, {
    status: resp.status,
    headers: { "Content-Type": "application/json" },
  });
}
