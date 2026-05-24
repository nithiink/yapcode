import { NextRequest, NextResponse } from "next/server";

const BACKEND = process.env.BACKEND_URL || "http://localhost:8000";

// Claude turns can run for minutes — don't let the route handler time out early.
export const maxDuration = 800;

export async function POST(req: NextRequest) {
  const body = await req.text();
  const resp = await fetch(`${BACKEND}/tools/execute`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body,
  });
  const text = await resp.text();
  return new NextResponse(text, {
    status: resp.status,
    headers: { "Content-Type": "application/json" },
  });
}
