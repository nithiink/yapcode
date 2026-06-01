// Client-side shared-secret token handling for remote (LAN / phone) access.
//
// On localhost the backend trusts loopback, so no token is needed and the app
// works with zero config. When the backend is started with VC_AUTH_TOKEN set
// (network mode, run-network.sh), every backend call must carry that token. The
// user supplies it once via the URL — open `https://<laptop>:3000/#vc_token=SECRET`
// on the phone/laptop — and we persist it to localStorage and strip it from the
// address bar so it isn't left in history or shoulder-surfed.
//
// The token is NEVER baked into the JS bundle (unlike BACKEND_URL): it's only
// ever supplied by the legitimate user at runtime, so loading the open frontend
// yields a non-functional app without the secret.

const KEY = "vc_auth_token";

function captureFromUrl(): string | null {
  if (typeof window === "undefined") return null;
  try {
    const hash = new URLSearchParams((window.location.hash || "").replace(/^#/, ""));
    const query = new URLSearchParams(window.location.search);
    const token = hash.get("vc_token") || query.get("vc_token");
    if (!token) return null;
    window.localStorage.setItem(KEY, token);
    // Remove the token from the visible URL (history, bookmarks, screen-share).
    hash.delete("vc_token");
    query.delete("vc_token");
    const h = hash.toString();
    const q = query.toString();
    const url = window.location.pathname + (q ? `?${q}` : "") + (h ? `#${h}` : "");
    window.history.replaceState(null, "", url);
    return token;
  } catch {
    return null;
  }
}

let captured = false;

export function getAuthToken(): string | null {
  if (typeof window === "undefined") return null;
  // Capture a token passed in the URL exactly once per page load, then fall back
  // to whatever is persisted. Re-read localStorage each call so a token set in
  // another tab is picked up without a reload.
  if (!captured) {
    captured = true;
    captureFromUrl();
  }
  try {
    return window.localStorage.getItem(KEY);
  } catch {
    return null;
  }
}

// Merge the auth token into a fetch headers object (for requests that can set
// headers — i.e. fetch()). No-op when no token is configured (localhost).
export function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const t = getAuthToken();
  const base = { ...(extra || {}) };
  if (t) base["X-VC-Token"] = t;
  return base;
}

// Append the token as a ?token= query param, for transports that can't set
// headers: EventSource (SSE) and WebSocket. No-op when no token is configured.
export function withAuthParam(url: string): string {
  const t = getAuthToken();
  if (!t) return url;
  return url + (url.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(t);
}
