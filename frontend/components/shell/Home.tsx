"use client";

// What sits under Yuri when nothing has taken the stage: her name, and one
// line about right now. This replaces the Dashboard's three bands of cards —
// the triage is the same (presenceLine is built from bands()), said instead of
// tabulated, with anything needing a decision arriving in the dock.
//
// It fades and slides out rather than unmounting: she is a persistent presence,
// and content that pops out of existence when a panel opens reads as a page
// change, which is the thing this shell exists not to be.
import { useYuri } from "@/components/VoiceProvider";
import { presenceLine } from "@/lib/presence.ts";

export function Home({ away }: { away: boolean }) {
  const { approvals, missions, sessions, vstate } = useYuri();
  const line = presenceLine(approvals, missions, sessions, vstate === "speaking");

  return (
    <>
      <div className="naming" data-away={away} aria-hidden={away}>
        <div className="sname">YURI</div>
        <div className="smeta">{line}</div>
      </div>
      <div className="hint" data-away={away} aria-hidden="true">
        Open anything — she steps aside and keeps watching.
      </div>
    </>
  );
}
