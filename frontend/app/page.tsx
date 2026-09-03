"use client";

// The Dashboard answers one question: "what needs me right now?" Three bands,
// in the order that matters — unanswered approvals, then blocked or failed
// work, then what is running — built on `bands()` (lib/dashboard.ts), which
// already owns every sorting/classification rule this view has to honor.
//
// The empty state is the good state: when all three bands are empty this
// renders one line, not three empty headings. And a failed fetch must never
// look like an empty dashboard — an empty list and a failed load look
// identical on screen, and only one of them means the user can stop
// worrying — so a load failure renders an error + retry instead of quietly
// falling back to empty arrays.
import { useCallback, useEffect, useState } from "react";
import { useYuri } from "@/components/VoiceProvider";
import { ApprovalCard } from "@/components/ApprovalCard";
import { bands } from "@/lib/dashboard";
import { canCancel, canPause, canResume } from "@/lib/missions";
import { sessionStatus } from "@/lib/sessions";
import { yget, ypost, ApiError } from "@/lib/api";
import type { Approval, Mission } from "@/lib/yuriTypes";

export default function Page() {
  const { approvals, missions, sessions, refresh, onYuriEvent } = useYuri();
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busyApproval, setBusyApproval] = useState<string | null>(null);
  const [busyMission, setBusyMission] = useState<string | null>(null);

  // useYuri()'s own refresh(what) swallows fetch failures (a stale/empty list
  // is fine for the shared context — sessions poll again in 2.5s regardless).
  // This view can't accept that: it has to tell "nothing to do" apart from
  // "couldn't find out." So it probes the same two endpoints itself, purely
  // to observe success/failure, and only then asks the context to adopt the
  // fresh data.
  const load = useCallback(async () => {
    try {
      await Promise.all([
        yget<{ approvals: Approval[] }>("approvals"),
        yget<{ missions: Mission[] }>("missions"),
      ]);
      setLoadError(null);
      await Promise.all([refresh("approvals"), refresh("missions")]);
    } catch (e) {
      setLoadError(e instanceof ApiError ? e.message : "Could not reach Yuri's backend.");
    }
  }, [refresh]);

  useEffect(() => {
    void load();
  }, [load]);

  // Refresh on the events that invalidate this view, not on a timer. Sessions
  // already refresh on the provider's own 2.5s poll.
  useEffect(
    () =>
      onYuriEvent((ev) => {
        if (ev.type.startsWith("approval.")) void refresh("approvals");
        if (ev.type.startsWith("mission.")) void refresh("missions");
      }),
    [onYuriEvent, refresh],
  );

  const decideApproval = async (id: string, decision: "approve" | "deny") => {
    setBusyApproval(id);
    setActionError(null);
    try {
      await ypost(`/approvals/${id}/${decision}`);
      // No local mutation: the resulting approval.* event refreshes the list.
    } catch (e) {
      setActionError(
        e instanceof ApiError && e.status === 409
          ? "That approval was already decided — someone answered it first."
          : `Could not record the decision: ${(e as Error).message}`,
      );
    } finally {
      setBusyApproval(null);
    }
  };

  const missionAction = async (id: string, action: "pause" | "resume" | "cancel") => {
    setBusyMission(id);
    setActionError(null);
    try {
      await ypost(`/missions/${id}/${action}`);
      // No local mutation: the resulting mission.* event refreshes the list.
    } catch (e) {
      setActionError(`Could not ${action} that mission: ${(e as Error).message}`);
    } finally {
      setBusyMission(null);
    }
  };

  const b = bands(approvals, missions, sessions);
  const isEmpty = b.needsYou.length === 0 && b.blocked.length === 0 && b.running.length === 0;

  return (
    <div className="dash-view">
      <h2 className="viewtitle">Dashboard</h2>

      {loadError ? (
        <div className="apr-error dash-loaderror">
          <span>{loadError}</span>
          <button className="txtoggle" onClick={() => void load()}>
            Retry
          </button>
        </div>
      ) : (
        <>
          {actionError && <div className="apr-error">{actionError}</div>}

          {isEmpty ? (
            <div className="empty">Nothing needs you. Nothing is blocked.</div>
          ) : (
            <>
              {b.needsYou.length > 0 && (
                <section className="dash-band">
                  <h3 className="dash-band-title">Needs you</h3>
                  <div className="apr-list">
                    {b.needsYou.map((a) => (
                      <ApprovalCard
                        key={a.id}
                        a={a}
                        busy={busyApproval === a.id}
                        onDecide={(decision) => void decideApproval(a.id, decision)}
                      />
                    ))}
                  </div>
                </section>
              )}

              {b.blocked.length > 0 && (
                <section className="dash-band">
                  <h3 className="dash-band-title">Blocked</h3>
                  <div className="dash-list">
                    {b.blocked.map((item) =>
                      item.kind === "mission" ? (
                        <div className="dash-row" key={`m-${item.mission.id}`}>
                          <div className="dash-row-top">
                            <span className="dash-row-title">{item.mission.title}</span>
                            <span className="dash-row-meta">{item.mission.status.replace(/_/g, " ")}</span>
                          </div>
                          {(canResume(item.mission) || canPause(item.mission) || canCancel(item.mission)) && (
                            <div className="dash-row-actions">
                              {canResume(item.mission) && (
                                <button
                                  className="dash-btn"
                                  disabled={busyMission === item.mission.id}
                                  onClick={() => void missionAction(item.mission.id, "resume")}
                                >
                                  Resume
                                </button>
                              )}
                              {canPause(item.mission) && (
                                <button
                                  className="dash-btn"
                                  disabled={busyMission === item.mission.id}
                                  onClick={() => void missionAction(item.mission.id, "pause")}
                                >
                                  Pause
                                </button>
                              )}
                              {canCancel(item.mission) && (
                                <button
                                  className="dash-btn danger"
                                  disabled={busyMission === item.mission.id}
                                  onClick={() => void missionAction(item.mission.id, "cancel")}
                                >
                                  Cancel
                                </button>
                              )}
                            </div>
                          )}
                        </div>
                      ) : (
                        <div className="dash-row" key={`s-${item.session.handle}`}>
                          <div className="dash-row-top">
                            <span className="dash-row-title">{item.session.name || item.session.handle}</span>
                          </div>
                          <span className="dash-row-task">Lost — did not survive a restart</span>
                        </div>
                      ),
                    )}
                  </div>
                </section>
              )}

              {b.running.length > 0 && (
                <section className="dash-band">
                  <h3 className="dash-band-title">Running</h3>
                  <div className="dash-list">
                    {b.running.map((s) => (
                      <div className="dash-row" key={s.handle}>
                        <div className="dash-row-top">
                          <span className="dash-row-title">{s.name || s.handle}</span>
                          <span className="dash-row-meta">{s.agent_name || s.agent_id || s.backend}</span>
                        </div>
                        <span className="dash-row-task">{sessionStatus(s).task}</span>
                      </div>
                    ))}
                  </div>
                </section>
              )}
            </>
          )}
        </>
      )}
    </div>
  );
}
