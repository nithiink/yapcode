"use client";

// A mission's plan: the steps, in dependency order, with who has each one and
// what the checks said.
//
// Fetches /yuri/missions/{id}/workflow itself rather than reading the mission
// detail's `steps`: that payload has no dependency map and no verdicts, and
// the order is the whole point of a timeline.
//
// Every ordering and gating decision is in lib/workflow.ts so `node --test`
// can reach it. Two rules from docs/yuri/design/GUIDE.md do the work here:
// a control that would fail is not rendered, and empty is not the same as
// broken.
import { useCallback, useEffect, useState } from "react";
import { ApiError, yget, ypost } from "@/lib/api";
import {
  canAssign, canRetry, canSkip, durationOf, pendingCheckLabel, progressOf, statusLabel,
  taskClass, timelineOrder, verdictLabel, verdictsOf, type Deps, type Task,
} from "@/lib/workflow";
import type { Specialist } from "@/lib/roster";
import { ViewError } from "./ViewError";

type WorkflowPayload = {
  workflow: { id: string; status: string; template?: string | null } | null;
  tasks: Task[];
  deps: Deps;
};

export function WorkflowTimeline({ missionId, refreshKey }: {
  missionId: string; refreshKey?: number;
}) {
  const [data, setData] = useState<WorkflowPayload | null>(null);
  const [people, setPeople] = useState<Specialist[]>([]);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [busy, setBusy] = useState("");
  const [actionError, setActionError] = useState("");
  const [assigning, setAssigning] = useState("");

  const load = useCallback(async () => {
    try {
      const [wf, roster] = await Promise.all([
        yget<WorkflowPayload>(`missions/${missionId}/workflow`),
        yget<{ specialists: Specialist[] }>("specialists"),
      ]);
      setData(wf);
      setPeople(roster.specialists || []);
      setLoadError(null);
    } catch (e) {
      setLoadError(e);
    }
  }, [missionId]);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  const act = async (task: Task, path: string, body?: unknown) => {
    setBusy(task.id);
    setActionError("");
    try {
      await ypost(`tasks/${task.id}/${path}`, body);
      setAssigning("");
      await load();
    } catch (e) {
      // A 409 here says something real — the step moved on, or an agent is
      // mid-turn — so it goes where the user is looking.
      setActionError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy("");
    }
  };

  if (loadError) return <ViewError error={loadError} onRetry={() => void load()} />;
  if (data === null) return <div className="empty">Loading the plan…</div>;
  // A mission with no workflow is a single session, not a broken page.
  if (!data.workflow) {
    return <div className="empty">This mission has no plan — it&rsquo;s a single session.</div>;
  }

  const ordered = timelineOrder(data.tasks, data.deps);
  const progress = progressOf(data.tasks);
  const byId = new Map(people.map((p) => [p.id, p]));

  return (
    <div className="wf">
      <div className="wf-top">
        <span className="wf-progress">
          {progress.done} of {progress.total} done
        </span>
        {/* Surfaced separately: "3 of 4" hides a blocked step, which is the
            one thing worth saying out loud. */}
        {progress.needsYou > 0 && (
          <span className="wf-needs">
            {progress.needsYou} need{progress.needsYou === 1 ? "s" : ""} you
          </span>
        )}
        <span className="wf-meta">
          {data.workflow.template ? `${data.workflow.template} · ` : ""}
          {data.workflow.status.replace(/_/g, " ")}
        </span>
      </div>

      {actionError && <pre className="mcp-err">{actionError}</pre>}

      <ol className="wf-list">
        {ordered.map((t, i) => {
          const who = t.specialist_id ? byId.get(t.specialist_id) : undefined;
          const verdicts = verdictsOf(t);
          const duration = durationOf(t);
          const blockers = (data.deps[t.id] || [])
            .map((id) => data.tasks.find((x) => x.id === id)?.title)
            .filter(Boolean);

          return (
            <li className={`wf-step ${taskClass(t.status)}`} key={t.id}>
              <div className="wf-stephead">
                <span className="wf-n">{i + 1}</span>
                {/* The agent's own colour, the same one its card shows in the
                    roster, so the two views are recognisably one agent. */}
                <span className="wf-dot" style={{ background: who?.color || "var(--line2)" }} />
                <span className="wf-title">{t.title}</span>
                <span className="wf-status">{statusLabel(t.status)}</span>
              </div>

              <div className="wf-stepmeta">
                {who ? who.name : t.role || "unassigned"}
                {duration ? ` · ${duration}` : ""}
                {t.read_only ? " · reads only" : ""}
                {(t.attempts || 0) > 1 ? ` · attempt ${t.attempts}` : ""}
                {t.status === "pending" && blockers.length > 0
                  ? ` · after ${blockers.join(", ")}`
                  : ""}
              </div>

              {/* In full: the reason is the only thing the user can act on. */}
              {t.error && <pre className="wf-err">{t.error}</pre>}

              {verdicts.length > 0 && (
                <div className="wf-verdicts">
                  {verdicts.map((v) => {
                    const label = verdictLabel(v);
                    return (
                      <span className={`wf-verdict ${label.tone}`} key={v.check}>
                        {label.text}
                      </span>
                    );
                  })}
                </div>
              )}

              {/* Declared but with no verdict: says the check exists without
                  implying an outcome — and on a skipped step says it never
                  ran, which is the fact that matters after skipping a test. */}
              {verdicts.length === 0 && (t.verification || []).length > 0 && (
                <div className="wf-verdicts">
                  {(t.verification || []).map((c) => {
                    const label = pendingCheckLabel(t, c);
                    return (
                      <span className={`wf-verdict ${label.tone}`} key={c}>{label.text}</span>
                    );
                  })}
                </div>
              )}

              {assigning === t.id ? (
                <div className="wf-assign">
                  {people
                    .filter((p) => !p.archived)
                    .map((p) => (
                      <button className="txtoggle" key={p.id} disabled={busy === t.id}
                              onClick={() => void act(t, "assign", { specialist_id: p.id })}>
                        {p.name}
                      </button>
                    ))}
                  <button className="txtoggle" onClick={() => setAssigning("")}>Cancel</button>
                </div>
              ) : (
                /* Absent, not disabled: each gate mirrors the engine's own
                   precondition, so no button here can answer 409. */
                (canRetry(t) || canSkip(t) || canAssign(t)) && (
                  <div className="wf-actions">
                    {canRetry(t) && (
                      <button className="txtoggle" disabled={busy === t.id}
                              onClick={() => void act(t, "retry")}>
                        Run it again
                      </button>
                    )}
                    {canAssign(t) && people.length > 0 && (
                      <button className="txtoggle" disabled={busy === t.id}
                              onClick={() => setAssigning(t.id)}>
                        Give it to…
                      </button>
                    )}
                    {canSkip(t) && (
                      <button className="txtoggle" disabled={busy === t.id}
                              onClick={() => void act(t, "skip")}>
                        Skip
                      </button>
                    )}
                  </div>
                )
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
