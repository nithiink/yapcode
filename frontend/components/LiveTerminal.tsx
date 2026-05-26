"use client";

import { useEffect, useRef } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";

// Streams the live interactive Claude TUI (CLI backend) by bridging the
// backend's PTY-over-WebSocket terminal endpoint into an xterm.js instance.
// The backend talks to a tmux pane; closing this just detaches (the session
// keeps running). Backend runs on :8000 (the Next app is on :3000).
export default function LiveTerminal({ handle }: { handle: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);

  // Scroll the Claude TUI by sending it the keys it documents under tmux
  // (PgUp/PgDn, Ctrl+End to jump to bottom). This is the only way to scroll on
  // mobile, where there's no wheel and touch can't reach the full-screen TUI.
  const send = (seq: string) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(seq);
  };

  useEffect(() => {
    const el = ref.current;
    if (!el) return;

    const term = new Terminal({
      fontSize: 12,
      fontFamily: 'ui-monospace, "SF Mono", Menlo, monospace',
      cursorBlink: true,
      theme: { background: "#0a0d14", foreground: "#e7ebf2" },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(el);
    try {
      fit.fit();
    } catch {
      /* ignore */
    }

    // Match the page's scheme: an https page (dev:network) must use wss, or the
    // browser blocks it as mixed content. Plain http (localhost) uses ws.
    const host = window.location.hostname || "localhost";
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${host}:8000/sessions/${handle}/terminal`);
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;

    const sendResize = () => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ __resize: { cols: term.cols, rows: term.rows } }));
      }
    };
    ws.onopen = () => {
      try {
        fit.fit();
      } catch {
        /* ignore */
      }
      sendResize();
    };
    ws.onmessage = (e) => {
      if (typeof e.data === "string") term.write(e.data);
      else term.write(new Uint8Array(e.data as ArrayBuffer));
    };
    ws.onclose = () => term.write("\r\n\x1b[2m[terminal disconnected]\x1b[0m\r\n");

    const dataSub = term.onData((d) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(d);
    });
    const ro = new ResizeObserver(() => {
      try {
        fit.fit();
        sendResize();
      } catch {
        /* ignore */
      }
    });
    ro.observe(el);

    return () => {
      ro.disconnect();
      dataSub.dispose();
      ws.close();
      wsRef.current = null;
      term.dispose();
    };
  }, [handle]);

  return (
    <div className="liveterm-wrap">
      <div className="liveterm" ref={ref} />
      <div className="term-scrollbtns">
        <button onClick={() => send("\x1b[5~")} title="Scroll up" aria-label="Scroll up">▲</button>
        <button onClick={() => send("\x1b[6~")} title="Scroll down" aria-label="Scroll down">▼</button>
        <button onClick={() => send("\x1b[1;5F")} title="Jump to bottom" aria-label="Jump to bottom">⤓</button>
      </div>
    </div>
  );
}
