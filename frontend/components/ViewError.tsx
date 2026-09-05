"use client";

// The one error state every view renders on a failed load — never an empty
// list. An empty list and a failed load look identical on screen, and only
// one of them means the user can stop worrying, so the two must never read
// the same. 401 and 503 need different actions from the user (fix a token vs.
// wait for the backend), so they're called out by name rather than folded
// into one generic "could not load" line.
import { ApiError } from "@/lib/api";

export function ViewError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const status = error instanceof ApiError ? error.status : undefined;
  const msg =
    status === 401
      ? "Not authorised — check VC_AUTH_TOKEN."
      : status === 503
        ? "Yuri's storage is unavailable. The backend may still be starting."
        : `Could not load this view: ${error instanceof Error ? error.message : String(error)}`;
  return (
    <div className="apr-error dash-loaderror">
      <span>{msg}</span>
      <button className="txtoggle" onClick={onRetry}>
        Retry
      </button>
    </div>
  );
}
