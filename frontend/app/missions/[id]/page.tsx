"use client";

// The Mission detail view. Fetches its own steps/sessions/approvals/events
// through lib/api.ts rather than reading them off useYuri() — the provider
// deliberately holds only global-and-continuous state (the boundary Task 3
// drew), and this detail is per-view, fetched on demand.
import { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { useYuri } from "@/components/VoiceProvider";
import { ApprovalCard } from "@/components/ApprovalCard";
import { MISSION_CLASS, canCancel, canPause, canResume } from "@/lib/missions";
import { yget, ypost, ApiError } from "@/lib/api";
import { fmtLogTime, fmtLogTimeTitle, clip } from "@/lib/format";
import { isFlatObject } from "@/lib/timeline";
import type { Approval, AgentSession, Mission, MissionStep, YuriEvent } from "@/lib/yuriTypes";

type MissionDetail = {
  mission: Mission;
  steps: MissionStep[];
  sessions: AgentSession[];
  approvals: Approval[]; // this mission's, oldest first
  events: YuriEvent[]; // its last 50
};

// A one-line summary of an event's payload for the History row — fmtPayload
// (lib/timeline.ts) pretty-prints with indentation, which is right for
// ApprovalCard's multi-line code block but wrong for a single log line here.
function payloadSummary(payload: Record<string, unknown>): string {
  const text = isFlatObject(payload)
    ? Object.entries(payload)
        .map(([k, v]) => `${k}=${v ?? "—"}`)
        .join(" ")
    : JSON.stringify(payload);
  return clip(text, 140);
}

export default function MissionDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { onYuriEvent } = useYuri();
  const [detail, setDetail] = useState<MissionDetail | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [busyMission, setBusyMission] = useState(false);
  const [busyApproval, setBusyApproval] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setDetail(await yget<MissionDetail>(`/missions/${id}`));
      setErr(null);
    } catch (e) {
      setErr(e);
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  // Refresh on this mission's own events rather than a timer — the same
  // onYuriEvent subscription every other view uses (see app/page.tsx,
  // app/approvals/page.tsx). Scoped to this mission_id so another mission's
  // status change elsewhere doesn't refetch a detail nobody is looking at.
  useEffect(
    () =>
      onYuriEvent((ev) => {
        if (ev.type.startsWith("mission.") && ev.mission_id === id) void load();
      }),
    [onYuriEvent, load, id],
  );

  const missionAction = async (action: "pause" | "resume" | "cancel") => {
    setBusyMission(true);
    setActionError(null);
    try {
      await ypost(`/missions/${id}/${action}`);
    } catch (e) {
      setActionError(`Could not ${action} that mission: ${(e as Error).message}`);
    } finally {
      setBusyMission(false);
      await load();
    }
  };

  const decideApproval = async (approvalId: string, decision: "approve" | "deny") => {
    setBusyApproval(approvalId);
    setActionError(null);
    try {
      await ypost(`/approvals/${approvalId}/${decision}`);
    } catch (e) {
      setActionError(
        e instanceof ApiError && e.status === 409
          ? "That approval was already decided — someone answered it first."
          : `Could not record the decision: ${(e as Error).message}`,
      );
    } finally {
      setBusyApproval(null);
      await load();
    }
  };

  if (err instanceof ApiError && err.status === 404) {
    return (
      <div className="miss-view">
        <h2 className="viewtitle">Mission</h2>
        <div className="empty">
          That mission no longer exists.{" "}
          <Link href="/missions" className="textbtn">
            Back to Missions
          </Link>
        </div>
      </div>
    );
  }

  if (err) {
    return (
      <div className="miss-view">
        <h2 className="viewtitle">Mission</h2>
        <div className="apr-error">
          Could not load that mission: {err instanceof Error ? err.message : String(err)}
        </div>
      </div>
    );
  }

  if (!detail) {
    return (
      <div className="miss-view">
        <h2 className="viewtitle">Mission</h2>
      </div>
    );
  }

  const { mission, steps, sessions, approvals, events } = detail;
  const orderedSteps = steps.slice().sort((a, b) => a.ordinal - b.ordinal);
  // Newest first — a mission's own history reads like a log, not a queue.
  const recentEvents = events.slice().reverse();

  return (
    <div className="miss-view miss-detail">
      <Link href="/missions" className="miss-back">
        ← Missions
      </Link>

      <div className="miss-head">
        <h2 className="viewtitle miss-title">{mission.title}</h2>
        <span className={`misschip ${MISSION_CLASS[mission.status]}`}>{mission.status.replace(/_/g, " ")}</span>
      </div>

      {mission.goal && <p className="miss-goal">{mission.goal}</p>}

      {actionError && <div className="apr-error">{actionError}</div>}

      {(canResume(mission) || canPause(mission) || canCancel(mission)) && (
        <div className="dash-row-actions miss-actions">
          {canResume(mission) && (
            <button className="dash-btn" disabled={busyMission} onClick={() => void missionAction("resume")}>
              Resume
            </button>
          )}
          {canPause(mission) && (
            <button className="dash-btn" disabled={busyMission} onClick={() => void missionAction("pause")}>
              Pause
            </button>
          )}
          {canCancel(mission) && (
            <button className="dash-btn danger" disabled={busyMission} onClick={() => void missionAction("cancel")}>
              Cancel
            </button>
          )}
        </div>
      )}

      <section className="miss-section">
        <h3 className="apr-subhead">Steps</h3>
        {orderedSteps.length === 0 ? (
          <div className="empty">No steps yet.</div>
        ) : (
          <div className="dash-list">
            {orderedSteps.map((s) => (
              <div className="dash-row" key={s.id}>
                <div className="dash-row-top">
                  <span className="dash-row-title">
                    {s.ordinal}. {s.title}
                  </span>
                  <span className="dash-row-meta">{s.status}</span>
                </div>
                {s.agent_id && <span className="dash-row-task">{s.agent_id}</span>}
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="miss-section">
        <h3 className="apr-subhead">Sessions</h3>
        {sessions.length === 0 ? (
          <div className="empty">No sessions yet.</div>
        ) : (
          <div className="dash-list">
            {sessions.map((s) => (
              <Link className="dash-row miss-row" href="/sessions" key={s.id}>
                <div className="dash-row-top">
                  <span className="dash-row-title">{s.name || s.native_session_id}</span>
                  <span className="dash-row-meta">{s.status}</span>
                </div>
                <span className="dash-row-task">{s.agent_id || s.backend}</span>
              </Link>
            ))}
          </div>
        )}
      </section>

      <section className="miss-section">
        <h3 className="apr-subhead">Approvals</h3>
        {approvals.length === 0 ? (
          <div className="empty">No approvals on this mission.</div>
        ) : (
          <div className="apr-list">
            {approvals.map((a) => (
              <ApprovalCard
                key={a.id}
                a={a}
                busy={busyApproval === a.id}
                showInput
                onDecide={(decision) => void decideApproval(a.id, decision)}
              />
            ))}
          </div>
        )}
      </section>

      <section className="miss-section">
        <h3 className="apr-subhead">History</h3>
        {recentEvents.length === 0 ? (
          <div className="empty">No events recorded yet.</div>
        ) : (
          <div className="miss-events">
            {recentEvents.map((ev) => (
              <div className="miss-event-row" key={ev.id}>
                <span className="miss-event-ts" title={fmtLogTimeTitle(ev.ts)}>
                  {fmtLogTime(ev.ts)}
                </span>
                <span className="miss-event-type">{ev.type}</span>
                <span className="miss-event-payload">{payloadSummary(ev.payload)}</span>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
