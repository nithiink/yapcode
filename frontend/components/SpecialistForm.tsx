"use client";

// Creating or editing an agent. Every rule lives in lib/roster.ts so
// `node --test` can reach it; this keeps the state.
//
// The persona field is the reason this is UI-only: a system prompt is what
// the agent will actually do, and one dictated through a speech recogniser is
// a persona nobody reviewed. No voice tool can reach this form.
import { useState } from "react";
import {
  PERMISSION_MODES, ROLES, ROLE_BLURB, SPECIALIST_COLORS, TASK_CAPABILITIES,
  canSaveSpecialist, slugPreview, validateSpecialist,
  type Capability, type Role, type SpecialistForm as Form,
} from "@/lib/roster";

export function SpecialistForm({ form, setForm, existingNames, editing, busy, error,
                                 onSave, onCancel }: {
  form: Form;
  setForm: (f: Form) => void;
  existingNames: string[];
  editing: boolean;
  busy: boolean;
  error: string;
  onSave: () => void;
  onCancel: () => void;
}) {
  // Errors only after a field has been touched: a form that turns red before
  // anyone has typed reads as broken rather than as guidance.
  const [touched, setTouched] = useState<Record<string, boolean>>({});
  const errors = validateSpecialist(form, existingNames);
  const set = <K extends keyof Form>(key: K, value: Form[K]) => {
    setForm({ ...form, [key]: value });
    setTouched((t) => ({ ...t, [key]: true }));
  };
  const show = (key: keyof Form) => (touched[key] ? errors[key] : undefined);
  const slug = slugPreview(form.name);

  const toggleCap = (cap: Capability) => {
    const has = form.capabilities.includes(cap);
    set("capabilities", has ? form.capabilities.filter((c) => c !== cap)
                            : [...form.capabilities, cap]);
  };

  return (
    <div className="sp-form">
      <label className="mcp-field">
        <span>Name</span>
        <input value={form.name} onChange={(e) => set("name", e.target.value)}
               placeholder="Code Reviewer" spellCheck={false} />
        {/* What the backend will actually store: the slug becomes the
            provider's agent id and a filename, so showing it here means no
            surprises later. */}
        {form.name.trim() && (
          <em className="sp-slug">{slug ? `stored as ${slug}` : "a name will be generated"}</em>
        )}
        {show("name") && <em className="sp-err">{show("name")}</em>}
      </label>

      <div className="mcp-field">
        <span>What is it for?</span>
        <div className="sp-roles">
          {ROLES.map((role) => (
            <button key={role} className={`mcp-tier ${form.role === role ? "on" : ""}`}
                    onClick={() => set("role", role as Role)}>
              <strong>{role}</strong>
              <em>{ROLE_BLURB[role]}</em>
            </button>
          ))}
        </div>
        {show("role") && <em className="sp-err">{show("role")}</em>}
      </div>

      <label className="mcp-field">
        <span>Which engine runs it</span>
        <input value={form.provider_id} onChange={(e) => set("provider_id", e.target.value)}
               placeholder="claude-code" spellCheck={false} />
        {show("provider_id") && <em className="sp-err">{show("provider_id")}</em>}
      </label>

      <label className="mcp-field">
        <span>One line about it (optional)</span>
        <input value={form.description} onChange={(e) => set("description", e.target.value)}
               placeholder="Reads the diff and says what is wrong with it." />
      </label>

      <label className="mcp-field">
        <span>What it is told to do</span>
        <textarea value={form.system_prompt} rows={5}
                  onChange={(e) => set("system_prompt", e.target.value)}
                  placeholder="You review the diff for correctness. Give a concrete failing scenario for each finding." />
      </label>

      <div className="mcp-field">
        <span>What it is set up for (optional)</span>
        <div className="sp-capgrid">
          {TASK_CAPABILITIES.map((cap) => (
            <button key={cap}
                    className={`sp-captoggle ${form.capabilities.includes(cap) ? "on" : ""}`}
                    onClick={() => toggleCap(cap)}>
              {cap.replace(/_/g, " ")}
            </button>
          ))}
        </div>
        <em className="sp-hint">
          A step that asks for one of these can only go to an agent that has it.
        </em>
      </div>

      <div className="sp-row">
        <div className="mcp-field">
          <span>Colour</span>
          <div className="sp-colors">
            {SPECIALIST_COLORS.map((c) => (
              <button key={c} className={`sp-swatch ${form.color === c ? "on" : ""}`}
                      style={{ background: c }} aria-label={c}
                      onClick={() => set("color", c)} />
            ))}
          </div>
        </div>
        <label className="mcp-field">
          <span>Permission mode</span>
          <select value={form.permission_mode}
                  onChange={(e) => set("permission_mode", e.target.value)}>
            {PERMISSION_MODES.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
        </label>
      </div>

      <label className="mcp-field">
        <span>Model (optional — blank means the engine&rsquo;s default)</span>
        <input value={form.model} onChange={(e) => set("model", e.target.value)}
               placeholder="" spellCheck={false} />
      </label>

      {error && <pre className="mcp-err">{error}</pre>}

      <div className="mcp-actions">
        {/* Absent until it can succeed, like everywhere else in this shell. */}
        {canSaveSpecialist(form, existingNames) && (
          <button className="txtoggle primary" disabled={busy} onClick={onSave}>
            {busy ? "Saving…" : editing ? "Save changes" : "Create agent"}
          </button>
        )}
        <button className="txtoggle" onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}
