// .ts extensions: `node --test` resolves relative imports literally, and
// tsconfig has allowImportingTsExtensions, so Next accepts them too.
import { PERSONA } from "./persona.ts";
import { CONDUCT } from "./operating.ts";
import { isNarrationMode, type NarrationMode } from "./narration.ts";

export const INSTRUCTIONS = PERSONA + "\n\n" + CONDUCT;

export type YuriContext = {
  home: string;
  // "Thursday 04 September, 11:52" — she reads it aloud, so it is formatted
  // by the backend rather than an ISO stamp she has to interpret. Without it
  // she cannot tell morning from midnight and every greeting is a guess.
  now?: string | null;
  // When she last did anything for the user, or null if never — which is a
  // real answer she can use ("first time we've talked"). Stamped on voice tool
  // dispatch, not on disconnect; see the backend's SETTINGS_LAST_SPOKE.
  last_spoke_at?: string | null;
  memory_user: string;
  journal_today: string;
  // Design §6: a fresh voice session must know the remembered mode without
  // being told. Without it in the prompt, OPERATING's "if it's already quiet
  // don't apologise for being quiet" is not actionable. Optional and validated
  // rather than asserted: it crosses the network, and an older backend or an
  // unreachable /yuri/context must still produce a usable block.
  narration_mode?: NarrationMode | string | null;
  active_missions: { id: string; title: string; goal: string | null; status: string; project: string | null }[];
  agents: { id: string; name: string; online: boolean; version?: string | null; detail?: string }[];
};

const cap = (s: string, n: number) => (s.length > n ? s.slice(s.length - n) : s);

// Block appended to the connect-time snapshot. Pure so it can be unit-tested;
// returns "" when the backend context is unavailable so connect still works.
export function yuriContextBlock(ctx: YuriContext | null | undefined): string {
  if (!ctx) return "";
  // ORDER MATTERS, and it is the order she would experience rather than a work
  // handover: the moment, then who she is talking to, then her day, then — last
  // — what happens to be running. The old block opened with her home and gave
  // ACTIVE MISSIONS equal billing with the person, which is a large part of why
  // work was all she ever talked about.
  const lines: string[] = ["", ""];

  if (ctx.now) lines.push(`RIGHT NOW: ${ctx.now}`);
  if (ctx.last_spoke_at) {
    lines.push(`YOU LAST DID SOMETHING FOR THEM AT: ${ctx.last_spoke_at} (a long gap is worth noticing; a short one is not)`);
  } else {
    lines.push("YOU HAVE NOT SPOKEN BEFORE (as far as you can tell) — this is the first time.");
  }

  const mem = (ctx.memory_user || "").trim();
  lines.push("", "WHAT YOU REMEMBER ABOUT THEM:",
    mem ? cap(mem, 4000) : "(nothing yet — use remember when you learn something)");

  const journal = (ctx.journal_today || "").trim();
  // Absent on a quiet day, and that is not a prompt to invent one.
  lines.push("", journal ? `YOUR DAY SO FAR:\n${cap(journal, 4000)}`
                         : "YOUR DAY SO FAR: nothing worth mentioning yet.");

  lines.push("", `YOUR HOME: ${ctx.home}`);
  if (isNarrationMode(ctx.narration_mode)) {
    lines.push(`YOUR NARRATION MODE: ${ctx.narration_mode} (remembered from last time; set_narration changes it)`);
  }

  const agents = (ctx.agents || []).map((a) => `- ${a.name}: ${a.online ? "online" : "OFFLINE"}${a.version ? ` (${a.version})` : ""}`);
  if (agents.length) lines.push("", "CODING AGENTS AVAILABLE:", ...agents);

  const missions = (ctx.active_missions || []).map(
    (m) => `- "${m.title}"${m.project ? ` · ${m.project}` : ""} · ${m.status}${m.goal ? ` · goal: ${m.goal}` : ""}`,
  );
  // Last, and silent when there is none — an empty work section is not a
  // conversation starter.
  if (missions.length) lines.push("", `WORK RUNNING RIGHT NOW:\n${missions.join("\n")}`);
  return lines.join("\n");
}
