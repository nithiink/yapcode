"use client";

// The stage's top strip: wordmark, the voice pill, narration mode, clock.
// pointer-events are off on the strip itself and back on for the controls, so
// the transparent band between them never eats a click meant for the orb.
import { useEffect, useState } from "react";
import { useYuri } from "@/components/VoiceProvider";
import { orbCaption } from "@/lib/voiceui.ts";
import { NARRATION_MODES } from "@/lib/narration.ts";

function Clock() {
  // Rendered empty on the server and filled on mount: a clock printed during
  // SSR is wrong by the time it reaches the browser, and it hydration-mismatches.
  const [now, setNow] = useState<string>("");
  useEffect(() => {
    const tick = () => setNow(new Date().toTimeString().slice(0, 8));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);
  return <span className="clock" suppressHydrationWarning>{now}</span>;
}

export function TopBar() {
  const {
    connected, muted, vstate, connect, disconnect, toggleMute,
    narrationMode, setNarrationMode, narrationBusy,
  } = useYuri();

  const caption = orbCaption(connected, muted, vstate);
  const speaking = vstate === "speaking";

  return (
    <div className="top">
      <span className="wordmark">YURI<sup>OS</sup></span>

      <div className="top-mid">
        <button
          className="vpill"
          data-voice={speaking ? "speaking" : muted ? "muted" : connected ? "live" : "idle"}
          onClick={() => (connected ? disconnect() : connect())}
          // The pill both connects and disconnects, so its name has to say
          // which — "Voice" alone leaves the user guessing what a click does.
          aria-label={connected ? "Disconnect voice" : "Connect voice"}
        >
          <span className="dot" aria-hidden="true" />
          <span>{connected ? caption : "Connect voice"}</span>
        </button>
        {connected && (
          <button
            className="vmute"
            onClick={toggleMute}
            aria-pressed={muted}
            aria-label={muted ? "Unmute microphone" : "Mute microphone"}
          >
            {muted ? "Unmute" : "Mute"}
          </button>
        )}
      </div>

      <div className="seg" role="group" aria-label="Narration mode">
        {NARRATION_MODES.map((m) => (
          <button
            key={m}
            aria-pressed={narrationMode === m}
            disabled={narrationBusy}
            onClick={() => setNarrationMode(m)}
          >
            {m}
          </button>
        ))}
      </div>
      <Clock />
    </div>
  );
}
