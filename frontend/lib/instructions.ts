// .ts extensions: `node --test` resolves relative imports literally, and
// tsconfig has allowImportingTsExtensions, so Next accepts them too.
import { PERSONA } from "./persona.ts";
import { CONDUCT } from "./operating.ts";
import { isNarrationMode, type NarrationMode } from "./narration.ts";
import { type ToolDef } from "./voice.ts";

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

// Display order and headings for the capability map. A category absent from
// here still renders, under "Other" — the map must never silently drop a tool,
// because a tool she cannot see is an ability she will deny having.
const CATEGORY_LABELS: [string, string][] = [
  ["herself", "Things you do yourself"],
  ["own", "Things you can find out or do directly"],
  ["macos", "Things you can do to this computer"],
  ["orchestration", "Running the coding agents"],
];

/** The first sentence of a tool description, capped.
 *
 *  Only the first sentence, deliberately: the personality work moved ~1,500
 *  words of per-tool guidance onto these descriptions, and rendering them in
 *  full here would put all of it straight back into the system prompt and
 *  undo that. The full text still reaches the model through the function
 *  declarations — this map only has to be scannable. */
// Periods that are not sentence ends. Without these, `set_mode`'s description
// truncated to "…when the user asks (e.g." — the abbreviation's full stop read
// as the end of the sentence, and the resulting line said nothing at all.
const NOT_A_SENTENCE_END = /(?:\be\.g|\bi\.e|\betc|\bvs|\bapprox|\bmin|\bmax|\bDr|\bMr|\bMs|\bSt|\bNo)\.$/i;

function firstSentence(text: string, max = 130): string {
  const flat = (text || "").replace(/\s+/g, " ").trim();
  let one = flat;
  // Walk candidate sentence ends and take the first that is not an
  // abbreviation. Anchored to the END of the candidate so "e.g." only
  // suppresses the break when it is the thing immediately before it.
  const re = /[.!?](?=\s|$)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(flat)) !== null) {
    const candidate = flat.slice(0, m.index + 1);
    if (!NOT_A_SENTENCE_END.test(candidate)) {
      one = candidate;
      break;
    }
  }
  return one.length > max ? one.slice(0, max - 1).trimEnd() + "…" : one;
}

/** What she can actually do right now, derived from the tools she was given.
 *
 *  Her persona used to carry a hand-written list ("Talk, remember things, keep
 *  a journal, and run the coding agents") which was honest when written and
 *  went stale the moment a tool was added. This is built from the SAME payload
 *  both transports hand the model (`fetch("/api/tools")` in realtime.ts and
 *  gemini.ts), so the prose describing her abilities and the declarations
 *  enabling them are one list rendered twice — there is no second source to
 *  drift.
 *
 *  Returns "" for an empty list rather than a heading with nothing under it: a
 *  failed fetch must not read as "you can do nothing", and it must certainly
 *  not read as a list she should invent entries for.
 */
export function capabilityBlock(tools: ToolDef[]): string {
  if (!tools || tools.length === 0) return "";

  const byCategory = new Map<string, ToolDef[]>();
  for (const t of tools) {
    const key = t.category || "other";
    byCategory.set(key, [...(byCategory.get(key) || []), t]);
  }

  const line = (t: ToolDef) => {
    // Derived from the tier, never written out, so the marking cannot drift
    // from what the gate actually enforces.
    const asks = t.tier === "confirm" ? " (asks first — you get one chance to read it back)" : "";
    const what = firstSentence(t.description || "");
    return `- ${t.name}${asks}${what ? ` — ${what}` : ""}`;
  };

  const out: string[] = [
    "",
    "",
    "WHAT YOU CAN DO RIGHT NOW:",
    "This list is generated from the tools you actually have. Never claim an ability that is not on it, and never apologise for one that is.",
  ];

  for (const [key, label] of CATEGORY_LABELS) {
    const list = byCategory.get(key);
    if (!list?.length) continue;
    out.push("", `${label}:`, ...list.map(line));
    byCategory.delete(key);
  }
  // Anything with an unrecognised category still gets rendered.
  const rest = [...byCategory.values()].flat();
  if (rest.length) out.push("", "Other:", ...rest.map(line));
  return out.join("\n");
}
