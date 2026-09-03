"use client";

import { useState } from "react";
import { Icon } from "./Icon";

// Small copy-to-clipboard button with transient ✓ feedback.
export function CopyBtn({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      className={`copybtn ${done ? "done" : ""}`}
      title={done ? "Copied" : "Copy"}
      aria-label={done ? "Copied" : "Copy"}
      onClick={() => {
        navigator.clipboard?.writeText(text).catch(() => undefined);
        setDone(true);
        setTimeout(() => setDone(false), 1100);
      }}
    >
      {done ? (
        <Icon name="check" size={14} strokeWidth={2.5} />
      ) : (
        <Icon name="copy" size={14} />
      )}
    </button>
  );
}
