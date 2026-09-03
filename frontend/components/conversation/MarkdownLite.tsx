"use client";

import type { ReactElement, ReactNode } from "react";

// Minimal markdown rendering (headings, lists, bold, inline code, fences) as
// React elements — no innerHTML, so prompt content can't inject markup.
export function MarkdownLite({ md }: { md: string }) {
  let key = 0;
  const inline = (s: string): ReactNode[] => {
    const nodes: ReactNode[] = [];
    const re = /(`[^`]+`|\*\*[^*]+\*\*)/g;
    let last = 0;
    for (let m = re.exec(s); m; m = re.exec(s)) {
      if (m.index > last) nodes.push(s.slice(last, m.index));
      const t = m[0];
      nodes.push(t.startsWith("`") ? <code key={key++}>{t.slice(1, -1)}</code> : <b key={key++}>{t.slice(2, -2)}</b>);
      last = m.index + t.length;
    }
    if (last < s.length) nodes.push(s.slice(last));
    return nodes;
  };
  const out: ReactElement[] = [];
  const lines = md.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i];
    if (l.startsWith("```")) {
      const buf: string[] = [];
      while (++i < lines.length && !lines[i].startsWith("```")) buf.push(lines[i]);
      out.push(<pre key={key++}>{buf.join("\n")}</pre>);
      continue;
    }
    const h = l.match(/^(#{1,4})\s+(.*)/);
    if (h) {
      out.push(<div key={key++} className={`mdh mdh${h[1].length}`}>{inline(h[2])}</div>);
      continue;
    }
    const li = l.match(/^\s*([-*]|\d+\.)\s+(.*)/);
    if (li) {
      out.push(<div key={key++} className="mdli"><span className="mdb">{li[1] === "-" || li[1] === "*" ? "•" : li[1]}</span>{inline(li[2])}</div>);
      continue;
    }
    out.push(l.trim() ? <div key={key++} className="mdp">{inline(l)}</div> : <div key={key++} className="mdgap" />);
  }
  return <div className="planmd">{out}</div>;
}
