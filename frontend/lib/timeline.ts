// The pure helpers behind the "Conversation" panel's timeline: the tool/turn
// item shape and the functions that classify and summarize a tool call — kept
// out of the component so they are reachable from `node --test`.
import { clip } from "./format.ts";

// One ordered list of bubbles + tool rows so the live "Conversation" panel renders
// tool calls inline with the surrounding turns instead of piling them at the end.
export type TimelineItem =
  | { kind: "turn"; role: "user" | "assistant"; text: string; final: boolean }
  | { kind: "tool"; id: number; name: string; ok?: boolean; args?: unknown; result?: unknown };

// A plan-approval prompt carries the plan markdown after this marker (set in
// the backend's _summarize_tool); split it off so the card can render it
// formatted instead of as one raw blob.
export function splitPlan(text: string): { lead: string; plan: string | null } {
  const i = text.indexOf("The full plan follows");
  if (i < 0) return { lead: text, plan: null };
  const nl = text.indexOf("\n", i);
  return {
    lead: text.slice(0, i).replace(/[—.\s]+$/, ""),
    plan: nl < 0 ? null : text.slice(nl).trim(),
  };
}

// Pretty-print a tool call's input/output for the expandable detail view.
// Strings pass through; objects are JSON-formatted; nullish renders as a dash.
export function fmtPayload(v: unknown): string {
  if (v === undefined || v === null) return "—";
  if (typeof v === "string") return v;
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

export type ToolItem = Extract<TimelineItem, { kind: "tool" }>;

// A flat object (all primitive values) renders as an aligned key/value grid;
// anything nested falls back to a JSON code block.
export function isFlatObject(v: unknown): v is Record<string, unknown> {
  if (!v || typeof v !== "object" || Array.isArray(v)) return false;
  return Object.values(v as Record<string, unknown>).every(
    (x) => x === null || ["string", "number", "boolean"].includes(typeof x),
  );
}

// Done / working / error — drives the status dot and accent. `working` is the
// transient state tell_claude & friends return before the real result polls in.
export function toolState(item: ToolItem): "done" | "working" | "error" {
  if (item.ok === false) return "error";
  const status = (item.result as { status?: string } | undefined)?.status;
  if (status === "working") return "working";
  if (status === "error") return "error";
  return "done";
}

// A short, human-readable gloss of what the call actually did, so the row reads
// like an action ("told Claude to…", "mode → auto") instead of bare jargon.
export function toolSummary(name: string, args: unknown, result: unknown): string {
  const a = (args ?? {}) as Record<string, any>;
  const r = (result ?? {}) as Record<string, any>;
  switch (name) {
    case "tell_claude":
      return a.message ? clip(String(a.message)) : "";
    case "answer_prompt":
      return a.choice ? `“${clip(String(a.choice), 60)}”` : "";
    case "run_slash_command":
      return String(r.sent || `/${a.command ?? ""}${a.args ? " " + a.args : ""}`).trim();
    case "set_mode":
      return r.mode || a.mode ? `mode → ${r.mode || a.mode}` : "";
    case "rename_session":
      return r.name ? `→ ${r.name}` : a.name || "";
    case "start_session":
      return r.name ? `${r.name}${r.project_path ? " · " + String(r.project_path).split("/").pop() : ""}` : "";
    case "list_sessions":
      return Array.isArray(r.sessions) ? `${r.sessions.length} session${r.sessions.length === 1 ? "" : "s"}` : "";
    case "list_projects":
      return Array.isArray(r.projects) ? `${r.projects.length} projects` : "";
  }
  if (typeof r.message === "string") return clip(r.message);
  if (typeof r.status === "string") return r.status;
  return "";
}
