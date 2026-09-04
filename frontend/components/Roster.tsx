"use client";

// Your agents: the roster. The Agents panel's first section — the coding
// engines move below it, relabelled as what they are.
//
// One panel, two sections, and no ninth rail icon: the user's word for a
// specialist is "agent", so shipping "Agents" beside "Specialists" would put
// two rail items with the same name on different things.
import { useCallback, useEffect, useState } from "react";
import { ApiError, ydelete, yget, ypost, yput } from "@/lib/api";
import {
  EMPTY_SPECIALIST, formFrom, specialistBody,
  type Specialist, type SpecialistForm as Form,
} from "@/lib/roster";
import { SpecialistCard } from "./SpecialistCard";
import { SpecialistForm } from "./SpecialistForm";
import { ViewError } from "./ViewError";

export function Roster() {
  const [rows, setRows] = useState<Specialist[] | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [form, setForm] = useState<Form | null>(null);
  const [editingId, setEditingId] = useState<string>("");
  const [busy, setBusy] = useState("");
  const [saveError, setSaveError] = useState("");

  const load = useCallback(async () => {
    try {
      const data = await yget<{ specialists: Specialist[] }>("specialists");
      setRows(data.specialists || []);
      setLoadError(null);
    } catch (e) {
      setLoadError(e);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async () => {
    if (!form) return;
    setBusy("save");
    setSaveError("");
    try {
      if (editingId) await yput(`specialists/${editingId}`, specialistBody(form));
      else await ypost("specialists", specialistBody(form));
      setForm(null);
      setEditingId("");
      await load();
    } catch (e) {
      setSaveError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy("");
    }
  };

  const archive = async (s: Specialist) => {
    setBusy(s.id);
    try {
      await ydelete(`specialists/${s.id}`);
      await load();
    } catch (e) {
      // A 409 here is meaningful — a live step is holding it — so it goes
      // where the user is looking rather than into the console.
      setSaveError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy("");
    }
  };

  // Only names OTHER than the one being edited, or renaming nothing would
  // collide with itself.
  const otherNames = (rows || [])
    .filter((s) => s.id !== editingId)
    .map((s) => s.name);

  return (
    <section className="roster">
      <div className="mcp-head">
        <h3 className="sectitle">Your agents</h3>
        {!form && rows && (
          <button className="txtoggle" onClick={() => { setForm(EMPTY_SPECIALIST); setEditingId(""); }}>
            Add an agent
          </button>
        )}
      </div>
      <p className="mcp-blurb">
        Specialists Yuri hands work to. Each one has a job, an engine that runs it, and its own
        instructions. Yuri can&rsquo;t create or change these by voice — a set of instructions
        nobody read is not something to hand tools to.
      </p>

      {form && (
        <SpecialistForm
          form={form}
          setForm={setForm}
          existingNames={otherNames}
          editing={Boolean(editingId)}
          busy={busy === "save"}
          error={saveError}
          onSave={() => void save()}
          onCancel={() => { setForm(null); setEditingId(""); setSaveError(""); }}
        />
      )}

      {!form && saveError && <pre className="mcp-err">{saveError}</pre>}

      {loadError ? (
        <ViewError error={loadError} onRetry={() => void load()} />
      ) : rows === null ? (
        <div className="empty">Loading…</div>
      ) : rows.length === 0 ? (
        <div className="empty">No agents yet.</div>
      ) : (
        <div className="sp-list">
          {rows.map((s) => (
            <SpecialistCard
              key={s.id}
              s={s}
              busy={busy === s.id}
              onEdit={() => { setForm(formFrom(s)); setEditingId(s.id); setSaveError(""); }}
              onArchive={() => void archive(s)}
            />
          ))}
        </div>
      )}
    </section>
  );
}
