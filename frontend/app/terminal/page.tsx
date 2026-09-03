"use client";

// The Terminal view gives LiveTerminal a proper home: a picker over the
// sessions where `can_watch` is true, and the terminal itself filling the
// rest of the view.
//
// can_watch, not backend === "cli": Claude Code's CLI backend can open a
// terminal, its SDK backend and OpenCode cannot — can_watch is the provider's
// own can_open_terminal() answer, so this never has to know which backend
// draws that line.
import { useEffect, useRef, useState } from "react";
import { useYuri } from "@/components/VoiceProvider";
import LiveTerminal from "@/components/LiveTerminal";
import { abbrevHome } from "@/lib/format";

export default function Page() {
  const { sessions } = useYuri();
  const watchable = sessions.filter((s) => s.can_watch);

  const [selected, setSelected] = useState<string | null>(null);

  // Exactly one watchable session: default to it. Several: default to none —
  // picking one arbitrarily would attach the user to a session they didn't
  // choose. This is a ONE-TIME initial pick, not an ongoing rule: it applies
  // once, the first time the session list has actually loaded (sessions
  // starts empty until the provider's first poll returns, so this waits for
  // that rather than judging "exactly one" off a still-empty list). Without
  // the once-only guard, a user who deliberately picks "— Select a session —"
  // while exactly one session is watchable would immediately be defaulted
  // right back into it on the next poll tick.
  const didDefault = useRef(false);
  useEffect(() => {
    if (didDefault.current || sessions.length === 0) return;
    didDefault.current = true;
    if (watchable.length === 1) setSelected(watchable[0].handle);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessions]);

  // If the selected session stops being watchable (closed, or a mode/backend
  // change revoked its terminal), fall back to nothing rather than pointing
  // LiveTerminal at a handle that no longer applies.
  useEffect(() => {
    if (selected && !watchable.some((s) => s.handle === selected)) setSelected(null);
  }, [selected, watchable]);

  return (
    <div className="term-view">
      <h2 className="viewtitle">Terminal</h2>

      <div className="term-toolbar">
        <div className="modelpick">
          <span className="modelpick-lab">Session</span>
          <select
            className="modelsel"
            aria-label="Session to watch"
            value={selected ?? ""}
            onChange={(e) => setSelected(e.target.value || null)}
            disabled={watchable.length === 0}
          >
            <option value="">— Select a session —</option>
            {watchable.map((s) => (
              <option key={s.handle} value={s.handle}>
                {(s.name || s.cwd.split("/").pop() || s.handle.slice(0, 8)) as string} · {abbrevHome(s.cwd)}
              </option>
            ))}
          </select>
        </div>
      </div>

      {selected ? (
        <div className="term-frame">
          <LiveTerminal handle={selected} />
        </div>
      ) : (
        <div className="term-empty">
          {watchable.length === 0
            ? "No session has a live terminal. Claude Code's CLI backend does; its SDK backend and OpenCode do not."
            : "Pick a session above to watch it live."}
        </div>
      )}
    </div>
  );
}
