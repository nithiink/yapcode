"use client";

// One approval, pending or resolved. Shared between the Approvals
// view (which passes showInput so the full tool_input is visible — the whole
// basis for deciding) and the Dashboard's "needs you" band (which omits it
// for a compact row). Purely presentational: no fetching, no local mutation
// of the approval itself — the parent owns `busy` and what onDecide does.
import { RISK_CLASS, RISK_LABEL, approvalTitle, outcomeOf, waitedFor } from "@/lib/approvals";
import { isFlatObject, fmtPayload } from "@/lib/timeline";
import type { Approval } from "@/lib/yuriTypes";

export function ApprovalCard({
  a,
  busy,
  onDecide,
  showInput,
}: {
  a: Approval;
  busy: boolean;
  onDecide: (decision: "approve" | "deny") => void;
  showInput?: boolean;
}) {
  const riskClass = RISK_CLASS[a.risk];
  // Null while it is still waiting; otherwise what became of it. A resolved
  // approval must NOT keep its Allow/Deny — the backend answers 409 and the
  // user gets "someone answered it first", which is a control that can only
  // fail. Same rule as the mission Delete button.
  const outcome = outcomeOf(a);

  return (
    <div className={`apr ${riskClass === "danger" ? "dangerous" : ""}`}>
      <div className="apr-head">
        <span className={`riskchip ${riskClass}`}>{RISK_LABEL[a.risk]}</span>
        <span className="apr-title">{approvalTitle(a)}</span>
      </div>

      <div className="apr-meta">
        {a.tool_name}
        {outcome ? "" : ` · ${waitedFor(a)}`}
        {a.session_id ? ` · session ${a.session_id.slice(0, 8)}` : ""}
        {a.mission_id ? ` · mission ${a.mission_id.slice(0, 8)}` : ""}
      </div>

      {showInput && (
        <div className="apr-input">
          {isFlatObject(a.tool_input) ? (
            <dl className="apr-kv">
              {Object.entries(a.tool_input).map(([k, v]) => (
                <div className="apr-kv-row" key={k}>
                  <dt>{k}</dt>
                  <dd>{fmtPayload(v)}</dd>
                </div>
              ))}
            </dl>
          ) : (
            <pre className="apr-code">{fmtPayload(a.tool_input)}</pre>
          )}
        </div>
      )}

      {outcome ? (
        <div className={`apr-outcome ${outcome.cls}`}>
          <span className="apr-outcome-label">{outcome.label}</span>
          {outcome.detail && <span className="apr-outcome-detail">{outcome.detail}</span>}
        </div>
      ) : (
        <div className="apr-actions">
          <button className="apr-btn allow" disabled={busy} onClick={() => onDecide("approve")}>
            Allow
          </button>
          <button className="apr-btn deny" disabled={busy} onClick={() => onDecide("deny")}>
            Deny
          </button>
        </div>
      )}
    </div>
  );
}
