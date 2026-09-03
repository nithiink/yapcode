"use client";

// Agents: one card per registered provider. Renders `detail`, not just the
// `online` boolean -- `detail` is the field that explains a state, and the
// reason Phase 5 had to teach health() that a not-yet-running but spawnable
// OpenCode is online at all. The `capabilities` dict renders as a labelled
// grid of ticks and crosses: this is where "Claude Code is one provider
// among several" stops being only a test result.
//
// Agent health has no driving event (there's no `agent.*` event beyond
// agent.error), so unlike Approvals/Missions this doesn't subscribe to
// onYuriEvent to refresh -- it probes once on mount, same fetch-then-adopt
// shape as app/page.tsx and app/missions/page.tsx, so a backend-down load
// renders an error instead of an empty list.
import { useCallback, useEffect, useState } from "react";
import { useYuri, type Agent } from "@/components/VoiceProvider";
import { ViewError } from "@/components/ViewError";
import { yget } from "@/lib/api";

const CAP_LABEL: Record<string, string> = {
  interactive_terminal: "Interactive terminal",
  slash_commands: "Slash commands",
  send_keys: "Send keys",
  permission_modes: "Permission modes",
  supports_interrupt: "Interrupt",
  supports_rehydrate: "Rehydrate",
  supports_resume: "Resume",
  supports_events: "Events",
  cost_tracking: "Cost tracking",
};

function capLabel(key: string): string {
  return CAP_LABEL[key] ?? key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

function capDisplay(value: unknown): string {
  if (Array.isArray(value)) return value.length ? value.join(", ") : "—";
  if (value === null || value === undefined) return "—";
  return String(value);
}

export default function Page() {
  const { agents, refresh } = useYuri();
  const [loadError, setLoadError] = useState<unknown>(null);

  const load = useCallback(async () => {
    try {
      await yget<{ agents: Agent[] }>("agents");
      setLoadError(null);
      await refresh("agents");
    } catch (e) {
      setLoadError(e);
    }
  }, [refresh]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="agents-view">
      <h2 className="viewtitle">Agents</h2>

      {loadError ? (
        <ViewError error={loadError} onRetry={() => void load()} />
      ) : agents.length === 0 ? (
        <div className="empty">No agents registered.</div>
      ) : (
        <div className="agents-list">
          {agents.map((a) => (
            <AgentCard key={a.id} a={a} />
          ))}
        </div>
      )}
    </div>
  );
}

function AgentCard({ a }: { a: Agent }) {
  const caps = a.capabilities && typeof a.capabilities === "object" ? a.capabilities : {};
  const entries = Object.entries(caps);
  const sessions = a.active_sessions ?? 0;

  return (
    <div className="agent-card">
      <div className="agent-head">
        <span className="agent-name">{a.name}</span>
        <span className={`agentchip ${a.online ? "online" : "offline"}`}>{a.online ? "Online" : "Offline"}</span>
        {a.version && <span className="agent-version">{a.version}</span>}
      </div>
      {a.detail && <p className="agent-detail">{a.detail}</p>}
      <div className="agent-meta">
        {sessions} active session{sessions === 1 ? "" : "s"}
      </div>
      {entries.length > 0 && (
        <div className="agent-caps">
          {entries.map(([key, value]) => (
            <div className="agent-cap" key={key}>
              <span className="agent-cap-label">{capLabel(key)}</span>
              {typeof value === "boolean" ? (
                <span className={`agent-cap-mark ${value ? "yes" : "no"}`}>{value ? "✓" : "✗"}</span>
              ) : (
                <span className="agent-cap-value">{capDisplay(value)}</span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
