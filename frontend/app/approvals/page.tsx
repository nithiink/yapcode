"use client";

// Approvals: like app/page.tsx and app/missions/page.tsx, useYuri()'s own
// refreshApprovals() swallows fetch failures (a stale/empty list is fine for
// the shared context — it's re-fetched on every approval.* event regardless).
// This view can't accept that for its one and only list: an empty `approvals`
// and a load failure both render as "Nothing is waiting on you," and only one
// of those means there is really nothing to do. So this probes the endpoint
// itself, purely to observe success/failure, and only then asks the context
// to adopt the fresh data.
import { useCallback, useEffect, useState } from "react";
import { useYuri } from "@/components/VoiceProvider";
import { ApprovalCard } from "@/components/ApprovalCard";
import { ViewError } from "@/components/ViewError";
import { yget, ypost, ApiError } from "@/lib/api";
import { RISK_CLASS, RISK_LABEL, approvalTitle } from "@/lib/approvals";
import type { Approval } from "@/lib/yuriTypes";

export default function ApprovalsPage() {
  const { approvals, refresh, onYuriEvent } = useYuri();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);

  const load = useCallback(async () => {
    try {
      await yget<{ approvals: Approval[] }>("approvals");
      setLoadError(null);
      await refresh("approvals");
    } catch (e) {
      setLoadError(e);
    }
  }, [refresh]);

  useEffect(() => {
    void load();
  }, [load]);

  // Refresh on the events that invalidate this view, not on a timer.
  useEffect(
    () =>
      onYuriEvent((ev) => {
        if (ev.type.startsWith("approval.")) void refresh("approvals");
      }),
    [onYuriEvent, refresh],
  );

  const decide = async (id: string, decision: "approve" | "deny") => {
    setBusy(id);
    setError(null);
    try {
      await ypost(`/approvals/${id}/${decision}`);
      // No local mutation: the resulting event refreshes the list.
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 409
          ? "That approval was already decided — someone answered it first."
          : `Could not record the decision: ${(e as Error).message}`,
      );
    } finally {
      setBusy(null);
    }
  };

  const pending = approvals.filter((a) => a.status === "pending");
  const decided = approvals
    .filter((a) => a.status !== "pending")
    .slice()
    .sort((a, b) => (a.resolved_at ?? "") < (b.resolved_at ?? "") ? 1 : -1)
    .slice(0, 10);

  return (
    <div className="apr-view">
      <h2 className="viewtitle">Approvals</h2>

      {loadError ? (
        <ViewError error={loadError} onRetry={() => void load()} />
      ) : (
        <>
          {error && <div className="apr-error">{error}</div>}

          {pending.length === 0 ? (
            <div className="empty">Nothing is waiting on you.</div>
          ) : (
            <div className="apr-list">
              {pending.map((a) => (
                <ApprovalCard
                  key={a.id}
                  a={a}
                  busy={busy === a.id}
                  showInput
                  onDecide={(decision) => void decide(a.id, decision)}
                />
              ))}
            </div>
          )}

          {decided.length > 0 && (
            <div className="apr-decided">
              <h3 className="apr-subhead">Recently decided</h3>
              <div className="apr-hist">
                {decided.map((a) => (
                  <div className="apr-hist-row" key={a.id}>
                    <span className={`riskchip ${RISK_CLASS[a.risk]}`}>{RISK_LABEL[a.risk]}</span>
                    <span className="apr-hist-title">{approvalTitle(a)}</span>
                    <span className="apr-hist-status">
                      {a.status}
                      {a.resolved_by ? ` · ${a.resolved_by}` : ""}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
