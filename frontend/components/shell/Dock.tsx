"use client";

// The session dock: the bottom-right panel that carries the live conversation.
// It replaces the 380px right rail, and it is the one piece of chrome that
// stays put while panels come and go — the transcript is the thread of the
// whole session, so a view opening must not scroll it away.
//
// Lives in the layout (see app/layout.tsx), above the routed children, for the
// same reason VoiceProvider does: a route change must not remount it, or the
// transcript's scroll position and the open/closed state of every tool call
// reset on every click of the rail.
import { useCallback, useEffect, useRef, useState } from "react";
import { useYuri } from "@/components/VoiceProvider";
import { Timeline } from "@/components/conversation/Timeline";
import { MarkdownLite } from "@/components/conversation/MarkdownLite";
import { VoiceSettings } from "./VoiceSettings";
import { splitPlan } from "@/lib/timeline";
import { activeHandle, agentHue, dockTabs, liveDot, FALLBACK_HUE } from "@/lib/dock.ts";
import { sessionLabel, sessionStatus } from "@/lib/sessions";
import { composerState } from "@/lib/compose";

export function Dock({ onEngage }: { onEngage: () => void }) {
  const { timeline, pending, sessions, answerPrompt, say, connected, vstate, provider } = useYuri();

  const [picked, setPicked] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);

  const tabs = dockTabs(sessions);
  const active = activeHandle(tabs, picked);
  const current = tabs.find((t) => t.handle === active) ?? null;
  const dot = liveDot(sessions);

  // Auto-scroll to the latest message, with the auto-hiding themed scrollbar
  // and the top/bottom fade hints carried over from the rail. The fades are
  // the only cue that there is more above once the thread is long.
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const hideRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const updateFades = useCallback(() => {
    const el = scrollRef.current;
    const wrap = wrapRef.current;
    if (!el || !wrap) return;
    wrap.classList.toggle("more-above", el.scrollTop > 4);
    wrap.classList.toggle("more-below", el.scrollHeight - el.scrollTop - el.clientHeight > 4);
  }, []);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
    updateFades();
  }, [timeline, updateFades]);

  useEffect(() => {
    window.addEventListener("resize", updateFades);
    return () => window.removeEventListener("resize", updateFades);
  }, [updateFades]);

  // Reconnecting (or switching provider) must not leave a stale error under
  // the composer explaining a condition that no longer holds.
  useEffect(() => setSendError(null), [connected, provider]);

  // The composer talks to YURI, not to the selected session. The thread above
  // it renders her conversation, so anything else typed here would land in a
  // conversation the user cannot see — which is exactly the bug this replaced.
  // To message one agent session directly, /sessions has a per-session
  // composer where its reply is actually visible.
  const comp = composerState({ provider, connected, vstate, sending, draft });

  const send = async () => {
    if (!comp.canSend) return;
    const text = draft.trim();
    setSending(true);
    setSendError(null);
    try {
      await say(text);
      setDraft("");
    } catch (e) {
      // Keep the draft: losing what the user typed to a failed send is worse
      // than showing the failure.
      setSendError((e as Error).message || "Could not send that.");
    } finally {
      setSending(false);
    }
  };

  const status = current ? sessionStatus(current) : null;

  return (
    <section className="dock" aria-label="Session conversation">
      {/* The tabs no longer choose who the composer talks to (that is always
          Yuri now) — they choose which session's status line shows below, and
          nothing else. Kept rather than deleted because that line is the one
          place the dock says what an agent is doing right now; a strip of
          several sessions with only the first one's status readable would be
          worse than no strip. The aria-label says so out loud so the control
          is not silently repurposed. */}
      <div className="dtabs" role="tablist" aria-label="Sessions — pick one to see its status">
        {tabs.length === 0 ? (
          <span className="dtab-empty">No sessions yet</span>
        ) : (
          tabs.map((t) => (
            <button
              key={t.handle}
              role="tab"
              className="tab"
              aria-selected={t.handle === active}
              onClick={() => {
                setPicked(t.handle);
                onEngage();
              }}
            >
              <span className="tab-hue" style={{ background: agentHue(t.agent_id) }} aria-hidden="true" />
              {sessionLabel(t)}
            </button>
          ))
        )}
        <span className={`dlive ${dot}`} title={`Sessions: ${dot}`} aria-hidden="true" />
      </div>

      {status && (
        <div className={`dstatus ${status.cls}`}>
          <b>{status.lead}</b> {status.task}
        </div>
      )}

      <div className="dthread-wrap" ref={wrapRef}>
        <div
          className="dthread scroll"
          ref={scrollRef}
          onScroll={() => {
            const el = scrollRef.current;
            if (!el) return;
            el.classList.add("scrolling");
            if (hideRef.current) clearTimeout(hideRef.current);
            hideRef.current = setTimeout(() => el.classList.remove("scrolling"), 1000);
            updateFades();
          }}
        >
          {timeline.length === 0 ? (
            <div className="dthread-empty">
              {/* Both routes into this thread need the voice connection —
                  typing goes to Yuri over the same session speaking does. */}
              Nothing said yet. Connect voice, then talk or type below.
            </div>
          ) : (
            <Timeline items={timeline} />
          )}
        </div>
      </div>

      {pending && (
        <div className="appr">
          <div className="hd">
            <span className="risk">{pending.kind === "choice" ? "Question" : "Permission"}</span>
          </div>
          <div className="what">
            {pending.kind === "choice" ? (
              pending.text
            ) : (
              (() => {
                const { lead, plan } = splitPlan(pending.text);
                return (
                  <>
                    Claude wants to <code>{lead}</code>
                    {plan && <MarkdownLite md={plan} />}
                  </>
                );
              })()
            )}
          </div>
          <div className="acts">
            {pending.kind === "choice" ? (
              pending.options.map((o) => (
                <button key={o} onClick={() => void answerPrompt(o)}>{o}</button>
              ))
            ) : (
              <>
                <button className="allow" onClick={() => void answerPrompt("allow")}>Allow once</button>
                <button className="deny" onClick={() => void answerPrompt("deny")}>Deny</button>
              </>
            )}
          </div>
          <div className="permhint">You can also just say it out loud.</div>
        </div>
      )}

      {sendError && <div className="dsend-error">{sendError}</div>}

      <form
        className="comp"
        onSubmit={(e) => {
          e.preventDefault();
          void send();
        }}
      >
        {/* Yuri's accent, fixed: the chip used to carry the selected session's
            hue because the text went there. It goes to her now, so a per-session
            colour here would be a lie about the destination. */}
        <span className="chip" style={{ background: FALLBACK_HUE }} aria-hidden="true" />
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onFocus={onEngage}
          disabled={comp.disabled}
          placeholder={comp.placeholder}
          title={comp.reason ?? undefined}
          aria-label={comp.reason ?? "Message Yuri"}
          aria-disabled={comp.disabled}
        />
        <button className="send" type="submit" disabled={!comp.canSend} aria-label="Send">
          <svg viewBox="0 0 24 24"><path d="M12 19V5M5 12l7-7 7 7" /></svg>
        </button>
      </form>

      <VoiceSettings />
    </section>
  );
}
