"use client";

// Connected services: the MCP servers that give Yuri tools beyond her own.
//
// Lives in the Agents panel (the panel is already "what she can do with
// what"), as its SECOND section -- not a new rail item, which would put
// jargon in a list otherwise written in plain words.
//
// Two rules from docs/yuri/design/GUIDE.md do most of the work here:
//
//   * A control that would fail is not rendered. Reconnect appears only on a
//     service that is actually down; Save appears only when the form can be
//     saved -- which means only after a test has answered for THIS form.
//   * Empty, failed and loading never look the same.
//
// Every decision about validity lives in lib/mcp.ts so `node --test` can
// reach it; this file keeps the state and the fetches.
import { useCallback, useEffect, useState } from "react";
import { ApiError, ydelete, yget, ypost, yput } from "@/lib/api";
import {
  EMPTY_FORM, TIER_CHOICES, canSave, canTest, fingerprint, formProblem,
  requestBody, rowActions, verdictSummary,
  type ServerForm, type ServerRow, type Tier, type TestResult,
} from "@/lib/mcp";
import { ViewError } from "./ViewError";

type Listing = { servers: ServerRow[]; config_error?: string };

export function McpServers() {
  const [rows, setRows] = useState<ServerRow[] | null>(null);
  const [configError, setConfigError] = useState("");
  const [loadError, setLoadError] = useState<unknown>(null);
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState("");

  const load = useCallback(async () => {
    try {
      const data = await yget<Listing>("mcp");
      setRows(data.servers || []);
      setConfigError(data.config_error || "");
      setLoadError(null);
    } catch (e) {
      setLoadError(e);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const act = async (name: string, fn: () => Promise<unknown>) => {
    setBusy(name);
    try {
      await fn();
      await load();
    } catch (e) {
      setLoadError(e);
    } finally {
      setBusy("");
    }
  };

  return (
    <section className="mcp">
      <div className="mcp-head">
        <h3 className="sectitle">Connected services</h3>
        {!adding && rows && (
          <button className="txtoggle" onClick={() => setAdding(true)}>
            Add a service
          </button>
        )}
      </div>
      <p className="mcp-blurb">
        Each service gives Yuri tools of its own. She says where an answer came from, and
        will ask first if you tell her to.
      </p>

      {configError && (
        <div className="mcp-configerr">
          Your <code>mcp.json</code> can&rsquo;t be read, so no services are connected: {configError}
        </div>
      )}

      {adding && (
        <AddForm
          existing={(rows || []).map((r) => r.name)}
          onCancel={() => setAdding(false)}
          onSaved={async () => {
            setAdding(false);
            await load();
          }}
        />
      )}

      {loadError ? (
        <ViewError error={loadError} onRetry={() => void load()} />
      ) : rows === null ? (
        <div className="empty">Checking…</div>
      ) : rows.length === 0 ? (
        <div className="empty">No services connected. Yuri uses her own tools only.</div>
      ) : (
        <div className="mcp-list">
          {rows.map((r) => (
            <ServerCard
              key={r.name}
              row={r}
              busy={busy === r.name}
              onReconnect={() => void act(r.name, () => ypost(`mcp/${r.name}/reconnect`))}
              onToggle={(enabled) =>
                void act(r.name, () => yput(`mcp/${r.name}/enabled`, { enabled }))
              }
              onRemove={() => void act(r.name, () => ydelete(`mcp/${r.name}`))}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function ServerCard({ row, busy, onReconnect, onToggle, onRemove }: {
  row: ServerRow; busy: boolean;
  onReconnect: () => void; onToggle: (enabled: boolean) => void; onRemove: () => void;
}) {
  // Arm-then-fire, in the row, like the mission Delete flow. No confirm().
  const [armed, setArmed] = useState(false);
  const actions = rowActions(row);
  const label = row.status === "connected" ? "Connected"
    : row.status === "disabled" ? "Off" : "Not working";

  return (
    <div className="mcp-card">
      <div className="mcp-cardhead">
        <span className="mcp-name">{row.name}</span>
        <span className={`agentchip ${row.status === "connected" ? "online" : row.status === "disabled" ? "off" : "offline"}`}>
          {label}
        </span>
        {row.server_name && row.server_name !== row.name && (
          <span className="agent-version">
            {row.server_name}
            {row.server_version ? ` ${row.server_version}` : ""}
          </span>
        )}
      </div>

      {/* In full, not in a tooltip: this is the only thing the user can act on. */}
      {row.status === "failed" && row.error && <pre className="mcp-err">{row.error}</pre>}

      <div className="mcp-meta">
        {row.status === "connected"
          ? `${row.tool_count ?? 0} tool${row.tool_count === 1 ? "" : "s"}`
          : "No tools while it isn’t running"}
        {row.env_keys?.length ? ` · keys: ${row.env_keys.join(", ")}` : ""}
      </div>

      {row.tools?.length ? <div className="mcp-tools">{row.tools.join(" · ")}</div> : null}

      {/* Never silent: a dropped or shadowed tool the list doesn't mention is
          the capability map lying by omission. */}
      {row.dropped_tools ? (
        <div className="mcp-note">
          It offers {row.dropped_tools} more tool{row.dropped_tools === 1 ? "" : "s"} than Yuri
          will take on.
        </div>
      ) : null}
      {row.colliding_tools?.length ? (
        <div className="mcp-note">
          Two of its tools have names Yuri can&rsquo;t tell apart, so these are left out:{" "}
          {row.colliding_tools.join(", ")}
        </div>
      ) : null}

      <div className="mcp-actions">
        {actions.reconnect && (
          <button className="txtoggle" disabled={busy} onClick={onReconnect}>
            Try again
          </button>
        )}
        <button
          className="txtoggle"
          disabled={busy}
          onClick={() => onToggle(actions.toggle === "enable")}
        >
          {actions.toggle === "enable" ? "Turn on" : "Turn off"}
        </button>
        {armed ? (
          <>
            <button className="txtoggle danger" disabled={busy} onClick={onRemove}>
              Remove for good
            </button>
            <button className="txtoggle" disabled={busy} onClick={() => setArmed(false)}>
              Keep
            </button>
          </>
        ) : (
          <button className="txtoggle" disabled={busy} onClick={() => setArmed(true)}>
            Remove
          </button>
        )}
      </div>
    </div>
  );
}

function AddForm({ existing, onCancel, onSaved }: {
  existing: string[]; onCancel: () => void; onSaved: () => Promise<void>;
}) {
  const [form, setForm] = useState<ServerForm>(EMPTY_FORM);
  const [result, setResult] = useState<TestResult | null>(null);
  // Which form the result belongs to. Editing the command after a green test
  // has to retract it, or the user saves something that was never checked.
  const [tested, setTested] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");

  const set = <K extends keyof ServerForm>(key: K, value: ServerForm[K]) => {
    setForm((f) => ({ ...f, [key]: value }));
    setSaveError("");
  };

  const problem = formProblem(form, existing);
  const summary = result ? verdictSummary(result) : null;
  const stale = result !== null && tested !== null && fingerprint(form) !== tested;

  const test = async () => {
    setTesting(true);
    setSaveError("");
    try {
      const r = await ypost<TestResult>("mcp/test", requestBody(form));
      setResult(r);
      setTested(fingerprint(form));
    } catch (e) {
      setResult({ verdict: "failed", error: e instanceof Error ? e.message : String(e) });
      setTested(fingerprint(form));
    } finally {
      setTesting(false);
    }
  };

  const save = async () => {
    setSaving(true);
    try {
      await ypost("mcp", requestBody(form));
      await onSaved();
    } catch (e) {
      // The backend re-tests on save, so this can carry a server's own stderr.
      setSaveError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="mcp-form">
      <label className="mcp-field">
        <span>Name</span>
        <input value={form.name} onChange={(e) => set("name", e.target.value)}
               placeholder="weather" spellCheck={false} />
      </label>

      <label className="mcp-field">
        <span>Command that starts it</span>
        <input value={form.command} onChange={(e) => set("command", e.target.value)}
               placeholder="uvx" spellCheck={false} />
      </label>

      <label className="mcp-field">
        <span>Arguments</span>
        <input value={form.args} onChange={(e) => set("args", e.target.value)}
               placeholder="mcp-server-weather" spellCheck={false} />
      </label>

      <label className="mcp-field">
        <span>Settings it needs (one KEY=value per line)</span>
        <textarea value={form.env} onChange={(e) => set("env", e.target.value)}
                  rows={3} placeholder="WEATHER_API_KEY=…" spellCheck={false} />
      </label>

      {/* Required, with no default: this is a choice about what Yuri may do
          without asking, and a default here would be us making it. */}
      <div className="mcp-field">
        <span>How should Yuri use it?</span>
        <div className="mcp-tiers">
          {TIER_CHOICES.map((c) => (
            <button
              key={c.value}
              className={`mcp-tier ${form.tier === c.value ? "on" : ""}`}
              onClick={() => set("tier", c.value as Tier)}
            >
              <strong>{c.label}</strong>
              <em>{c.detail}</em>
            </button>
          ))}
        </div>
      </div>

      {problem && <div className="mcp-note">{problem}</div>}

      {summary && !stale && (
        <div className={`mcp-verdict ${summary.tone}`}>
          <pre>{summary.text}</pre>
          {result?.tools?.length ? (
            <div className="mcp-tools">{result.tools.map((t) => t.name).join(" · ")}</div>
          ) : null}
        </div>
      )}
      {stale && <div className="mcp-note">You changed something — test it again before saving.</div>}
      {saveError && <pre className="mcp-err">{saveError}</pre>}

      <div className="mcp-actions">
        <button className="txtoggle primary" disabled={!canTest(form, existing) || testing}
                onClick={() => void test()}>
          {testing ? "Testing…" : "Test it"}
        </button>
        {/* Absent, not disabled: an untested service cannot be saved at all. */}
        {canSave(form, existing, result, tested) && (
          <button className="txtoggle primary" disabled={saving} onClick={() => void save()}>
            {saving ? "Saving…" : "Save"}
          </button>
        )}
        <button className="txtoggle" onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}
