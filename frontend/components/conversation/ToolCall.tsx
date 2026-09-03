"use client";

import { Icon } from "../ui/Icon";
import { isFlatObject, fmtPayload, toolState, toolSummary, type ToolItem } from "@/lib/timeline";

export function PayloadView({ value }: { value: unknown }) {
  if (value === undefined || value === null || value === "") return <div className="tc-empty">—</div>;
  if (isFlatObject(value)) {
    const entries = Object.entries(value).filter(([, v]) => v !== undefined && v !== "");
    if (entries.length === 0) return <div className="tc-empty">—</div>;
    return (
      <dl className="tc-kv">
        {entries.map(([k, v]) => (
          <div className="tc-kv-row" key={k}>
            <dt>{k}</dt>
            <dd>{typeof v === "string" ? v : String(v)}</dd>
          </div>
        ))}
      </dl>
    );
  }
  return <pre className="tc-code">{fmtPayload(value)}</pre>;
}

// One tool call as an expandable inline "action card": collapsed shows a status
// dot, the mono tool name, and a human summary; expanded reveals structured
// input/output. Open/closed state is controlled by the parent (see Timeline),
// so a run of tool rows can each be independently expanded and that state
// survives re-renders without living inside this component.
export function ToolCall({
  item,
  variant = "card",
  open,
  onToggle,
}: {
  item: ToolItem;
  variant?: "card" | "line";
  open: boolean;
  onToggle: () => void;
}) {
  const state = toolState(item);
  const summary = toolSummary(item.name, item.args, item.result);
  return (
    <details
      className={`tcall ${variant} ${state}`}
      open={open}
      onToggle={() => onToggle()}
    >
      <summary>
        <span className={`tc-dot ${state}`} aria-hidden />
        <span className="tc-name">{item.name}</span>
        {summary && <span className="tc-summary">{summary}</span>}
        <Icon name="chevron-down" size={13} strokeWidth={1.5} className="tc-chev" />
      </summary>
      <div className="tc-body">
        <section className="tc-sec">
          <div className="tc-label">Input</div>
          <PayloadView value={item.args} />
        </section>
        <section className="tc-sec">
          <div className="tc-label">Output</div>
          <PayloadView value={item.result} />
        </section>
      </div>
    </details>
  );
}
