"use client";

import { useState, type ReactElement } from "react";
import { type TimelineItem, type ToolItem } from "@/lib/timeline";
import { ToolCall } from "./ToolCall";

// Render the conversation timeline, grouping runs of consecutive tool calls.
// An isolated call renders as a full card; a run of 2+ condenses into light
// lines inside one grouped container, so a burst of actions reads as a single
// tidy block instead of a stack of heavy boxes. Each line stays independently
// expandable (native <details>, keyed by stable id so open state survives
// re-renders and the timeline cap).
//
// Which tool rows are expanded lives in the Timeline component (below) rather
// than inside ToolCall itself, and is threaded through here as `openIds` /
// `onToggle` so each row can still be expanded independently of the others —
// unchanged, just lifted one level up.
function renderConversation(
  items: TimelineItem[],
  openIds: Set<number>,
  onToggle: (id: number) => void,
): ReactElement[] {
  const nodes: ReactElement[] = [];
  let i = 0;
  while (i < items.length) {
    const item = items[i];
    if (item.kind === "turn") {
      nodes.push(
        <div key={`turn-${i}`} className={`bubble ${item.role}`}>
          <div className="who">{item.role === "user" ? "You" : "Assistant"}</div>
          {item.text}
        </div>,
      );
      i++;
      continue;
    }
    // Collect the run of consecutive tool calls starting here.
    const run: ToolItem[] = [];
    while (i < items.length && items[i].kind === "tool") {
      run.push(items[i] as ToolItem);
      i++;
    }
    if (run.length === 1) {
      const t = run[0];
      nodes.push(
        <ToolCall
          key={`tool-${t.id}`}
          item={t}
          variant="card"
          open={openIds.has(t.id)}
          onToggle={() => onToggle(t.id)}
        />,
      );
    } else {
      nodes.push(
        <div key={`tgroup-${run[0].id}`} className="tcall-group">
          {run.map((t) => (
            <ToolCall
              key={`tool-${t.id}`}
              item={t}
              variant="line"
              open={openIds.has(t.id)}
              onToggle={() => onToggle(t.id)}
            />
          ))}
        </div>,
      );
    }
  }
  return nodes;
}

export function Timeline({ items }: { items: TimelineItem[] }) {
  const [openIds, setOpenIds] = useState<Set<number>>(() => new Set());
  const onToggle = (id: number) => {
    setOpenIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };
  return <>{renderConversation(items, openIds, onToggle)}</>;
}
