// The one line under Yuri's name at home.
//
// Home is the whole of the old Dashboard now: instead of three bands of cards
// the user has to read, she says what is true in one sentence, and the things
// that need a decision arrive in the dock. So this line has to carry the same
// triage the bands did — which is why it is built from bands() rather than
// from a status count, and why "needs you" always wins the sentence.
import { bands } from "./dashboard.ts";
import type { Approval, Mission } from "./yuriTypes.ts";
import type { Sess } from "./sessions.ts";
import { sessionLabel } from "./sessions.ts";

function plural(n: number, one: string, many = one + "s"): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** Reads as a sentence about now, not a dashboard. Priority matches
 *  orbState(): a decision waiting on the user, then her own speech, then work
 *  in flight, then quiet. */
export function presenceLine(
  approvals: Approval[],
  missions: Mission[],
  sessions: Sess[],
  speaking: boolean,
): string {
  const b = bands(approvals, missions, sessions);

  if (b.needsYou.length > 0) {
    return b.needsYou.length === 1
      ? "one decision is waiting on you"
      : `${plural(b.needsYou.length, "decision")} are waiting on you`;
  }
  if (speaking) return "telling you what just happened";

  const watching = sessions.length > 0 ? `watching ${plural(sessions.length, "session")}` : null;

  if (b.blocked.length > 0) {
    const stuck = b.blocked[0];
    const name = stuck.kind === "mission" ? stuck.mission.title : sessionLabel(stuck.session);
    const rest = b.blocked.length > 1 ? ` (+${b.blocked.length - 1} more)` : "";
    return [watching, `${name} is stuck${rest}`].filter(Boolean).join(" · ");
  }
  // A turn actually executing, not merely a live session. bands().running is
  // every session whose process is up — which is every idle one too — so
  // saying "X is running" off it told the user work was in flight while the
  // dock right beside it said "Ready".
  const busy = sessions.filter((s) => s.running);
  if (busy.length > 0) {
    return [watching, busy.length === 1
      ? `${sessionLabel(busy[0])} is working`
      : `${plural(busy.length, "session")} working`].filter(Boolean).join(" · ");
  }
  if (watching) return `${watching} · nothing needs you`;
  return "nothing running — say the word and I'll start something";
}
