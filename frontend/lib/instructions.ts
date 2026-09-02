// .ts extensions: `node --test` resolves relative imports literally, and
// tsconfig has allowImportingTsExtensions, so Next accepts them too.
import { PERSONA } from "./persona.ts";
import { OPERATING } from "./operating.ts";
import { isNarrationMode, type NarrationMode } from "./narration.ts";

export const INSTRUCTIONS = PERSONA + "\n\n" + OPERATING;

export type YuriContext = {
  home: string;
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
  const lines: string[] = ["", "", `YOUR HOME: ${ctx.home}`];
  if (isNarrationMode(ctx.narration_mode)) {
    lines.push(`YOUR NARRATION MODE: ${ctx.narration_mode} (remembered from last time; set_narration changes it)`);
  }
  const mem = (ctx.memory_user || "").trim();
  lines.push("WHAT YOU REMEMBER ABOUT THE USER:", mem ? cap(mem, 4000) : "(nothing yet — use remember when you learn something)");
  const journal = (ctx.journal_today || "").trim();
  if (journal) lines.push("", "TODAY SO FAR (your journal):", cap(journal, 4000));
  const agents = (ctx.agents || []).map((a) => `- ${a.name}: ${a.online ? "online" : "OFFLINE"}${a.version ? ` (${a.version})` : ""}`);
  if (agents.length) lines.push("", "AGENTS:", ...agents);
  const missions = (ctx.active_missions || []).map(
    (m) => `- "${m.title}"${m.project ? ` · ${m.project}` : ""} · ${m.status}${m.goal ? ` · goal: ${m.goal}` : ""}`,
  );
  lines.push("", missions.length ? `ACTIVE MISSIONS:\n${missions.join("\n")}` : "ACTIVE MISSIONS: none");
  return lines.join("\n");
}
