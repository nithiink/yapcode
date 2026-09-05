"use client";

// Activity: the unified pipeline feed (voice<->backend<->Claude), at full
// width. No new fetching -- `debugEvents` is already streamed and
// seq-deduped by VoiceProvider from /debug/stream; this view just owns the
// filter/toggle/scroll UI state ActivityFeed (Task 2) needs and hands it the
// stream straight off useYuri().
import { useEffect, useRef, useState, type UIEvent } from "react";
import { useYuri } from "@/components/VoiceProvider";
import { ActivityFeed } from "@/components/ActivityFeed";
import { filterEvents } from "@/lib/activity";

export default function Page() {
  const { debugEvents, debugStreamConnected, clearDebugEvents } = useYuri();
  const [filter, setFilter] = useState("");
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [paused, setPaused] = useState(false);
  const [copied, setCopied] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const atBottomRef = useRef(true);

  // Auto-scroll to the newest line only when the user is already at the
  // bottom and isn't paused, mirroring the old single-screen log panel this
  // view was decomposed from.
  useEffect(() => {
    if (paused) return;
    if (!atBottomRef.current) return;
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [debugEvents, paused]);

  const copyLog = (text: string) => {
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text).then(
        () => {
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1200);
        },
        () => undefined,
      );
    }
  };

  const onScroll = (e: UIEvent<HTMLDivElement>) => {
    const el = e.currentTarget;
    atBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  };

  return (
    <div className="activity-view">
      <h2 className="viewtitle">
        Activity{" "}
        <span className="viewcount">
          {filterEvents(debugEvents, filter, errorsOnly).length} / {debugEvents.length}
        </span>
      </h2>
      <ActivityFeed
        events={debugEvents}
        streamConnected={debugStreamConnected}
        filter={filter}
        onFilter={setFilter}
        errorsOnly={errorsOnly}
        onToggleErrorsOnly={() => setErrorsOnly((v) => !v)}
        paused={paused}
        onTogglePaused={() => setPaused((v) => !v)}
        copied={copied}
        onCopy={copyLog}
        onClear={clearDebugEvents}
        scrollRef={scrollRef}
        onScroll={onScroll}
      />
    </div>
  );
}
