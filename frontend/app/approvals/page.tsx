"use client";

// Approvals: refresh("approvals") now rejects on a failed fetch (see
// VoiceProvider.tsx's refreshApprovals), so this view awaits it directly for
// its one and only list — an empty `approvals` and a load failure both used
// to render as "Nothing is waiting on you," and only one of those means
// there is really nothing to do. No onYuriEvent subscription: the provider
// itself now refreshes approvals on every approval.* event, and this view
// reads the list straight off useYuri().
import { useCallback, useEffect, useState } from "react";
import { useYuri } from "@/components/VoiceProvider";
import { ApprovalCard } from "@/components/ApprovalCard";
import { ViewError } from "@/components/ViewError";
import { ypost, ApiError } from "@/lib/api";
import { RISK_CLASS, RISK_LABEL, approvalTitle } from "@/lib/approvals";

export default function ApprovalsPage() {
  const { approvals, refresh } = useYuri();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);

  const load = useCallback(async () => {
    try {
      await refresh("approvals");
      setLoadError(null);
    } catch (e) {
      setLoadError(e);
    }
  }, [refresh]);

  useEffect(() => {
    void load();
  }, [load]);

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
    .sort((a, b) => {
      const ra = a.resolved_at ?? "";
      const rb = b.resolved_at ?? "";
      return ra < rb ? 1 : ra > rb ? -1 : 0;
    })
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
