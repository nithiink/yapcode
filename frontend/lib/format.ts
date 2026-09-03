// Pure text/time formatting used by the timeline and session views — no React,
// so it is reachable from `node --test`.

// The backend stamps activity-log timestamps as UTC ISO strings ("…Z"). Show
// them in the viewer's own timezone: parse to a Date and format a compact local
// clock (HH:MM:SS.mmm). Falls back to the raw UTC time slice if unparseable.
export function fmtLogTime(ts: string): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts.slice(11, 23);
  const p = (n: number, w = 2) => String(n).padStart(w, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
}

// Full local date-time (incl. timezone) for the timestamp's hover title, so the
// date — omitted from the compact row — is still available on demand.
export function fmtLogTimeTitle(ts: string): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "long" });
}

export const clip = (s: string, n = 90) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

// Abbreviate the user's home dir to ~ for a compact path display.
export function abbrevHome(path: string): string {
  return path.replace(/^\/(Users|home)\/[^/]+/, "~");
}
