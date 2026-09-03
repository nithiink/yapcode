"use client";

// The Missions list: one row per mission, the status chip, its project (a
// name resolved against GET /projects — Mission itself carries only the id),
// current_step, and whichever of pause/resume/cancel the backend's own
// transition table (lib/missions.ts) allows right now.
//
// No create button: missions are created by starting a session (Phase 4
// ruled out an orchestrator), so a create form here would imply a queue that
// does not exist.
import { useCallback, useEffect, useState, type MouseEvent } from "react";
import { useRouter } from "next/navigation";
import { useYuri } from "@/components/VoiceProvider";
import { ViewError } from "@/components/ViewError";
import { MISSION_CLASS, canCancel, canPause, canResume } from "@/lib/missions";
import { yget, ypost, ApiError } from "@/lib/api";
import type { Mission, ProjectRow } from "@/lib/yuriTypes";

export default function Page() {
  const router = useRouter();
  const { missions, refresh } = useYuri();
  const [projects, setProjects] = useState<ProjectRow[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);

  // Projects aren't part of useYuri()'s shared state (nothing else needs them
  // continuously), so this view fetches its own copy purely to resolve
  // project_id -> name for display. Best-effort: a failure here just falls
  // back to showing raw project ids, so it doesn't get the same
  // fetch-then-adopt treatment as missions below.
  useEffect(() => {
    yget<{ projects: ProjectRow[] }>("projects")
      .then((r) => setProjects(Array.isArray(r?.projects) ? r.projects : []))
      .catch(() => {
        /* fall back to showing raw project ids below */
      });
  }, []);

  // refresh("missions") now rejects on a failed fetch (see VoiceProvider's
  // refreshMissions), so this view awaits it directly for its one and only
  // list — an empty `missions` and a load failure used to render
  // identically ("No missions yet"), and only one of those means there
  // really are none. No onYuriEvent subscription for missions: the provider
  // itself now refreshes missions on every mission.* event, and this view
  // reads the list straight off useYuri().
  const load = useCallback(async () => {
    try {
      await refresh("missions");
      setLoadError(null);
    } catch (e) {
      setLoadError(e);
    }
  }, [refresh]);

  useEffect(() => {
    void load();
  }, [load]);

  const projectName = useCallback(
    (projectId: string) => projects.find((p) => p.id === projectId)?.name ?? projectId,
    [projects],
  );

  const act = async (id: string, action: "pause" | "resume" | "cancel") => {
    setBusy(id);
    setError(null);
    try {
      await ypost(`/missions/${id}/${action}`);
      // No local mutation: the resulting mission.* event refreshes the list.
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 409
          ? "That mission already moved on — someone else changed it first."
          : `Could not ${action} that mission: ${(e as Error).message}`,
      );
    } finally {
      setBusy(null);
    }
  };

  const sorted = missions.slice().sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1));

  return (
    <div className="miss-view">
      <h2 className="viewtitle">Missions</h2>

      {loadError ? (
        <ViewError error={loadError} onRetry={() => void load()} />
      ) : (
        <>
          {error && <div className="apr-error">{error}</div>}

          {sorted.length === 0 ? (
            <div className="empty">No missions yet — start a session to create one.</div>
          ) : (
            <div className="dash-list">
              {sorted.map((m) => (
                <MissionRow
                  key={m.id}
                  m={m}
                  projectName={projectName(m.project_id)}
                  busy={busy === m.id}
                  onOpen={() => router.push(`/missions/${m.id}`)}
                  onAct={(action) => void act(m.id, action)}
                />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function MissionRow({
  m,
  projectName,
  busy,
  onOpen,
  onAct,
}: {
  m: Mission;
  projectName: string;
  busy: boolean;
  onOpen: () => void;
  onAct: (action: "pause" | "resume" | "cancel") => void;
}) {
  const stop = (e: MouseEvent) => e.stopPropagation();

  return (
    <div
      className="dash-row miss-row"
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") onOpen();
      }}
    >
      <div className="dash-row-top">
        <span className="dash-row-title">{m.title}</span>
        <span className={`misschip ${MISSION_CLASS[m.status]}`}>{m.status.replace(/_/g, " ")}</span>
      </div>
      <div className="dash-row-top">
        <span className="dash-row-meta">{projectName}</span>
        {m.current_step && <span className="dash-row-task">{m.current_step}</span>}
      </div>
      {(canPause(m) || canResume(m) || canCancel(m)) && (
        <div className="dash-row-actions" onClick={stop}>
          {canResume(m) && (
            <button className="dash-btn" disabled={busy} onClick={() => onAct("resume")}>
              Resume
            </button>
          )}
          {canPause(m) && (
            <button className="dash-btn" disabled={busy} onClick={() => onAct("pause")}>
              Pause
            </button>
          )}
          {canCancel(m) && (
            <button className="dash-btn danger" disabled={busy} onClick={() => onAct("cancel")}>
              Cancel
            </button>
          )}
        </div>
      )}
    </div>
  );
}
