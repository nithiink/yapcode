"use client";

// One agent in the roster. The card, not the form.
//
// "Agent" is the user's word for a specialist; the panel's other section
// calls the providers "engines", which is what they are. The API resource is
// still `specialists` — /yuri/agents was already the provider list.
import { ROLE_BLURB, specialistActions, type Role, type Specialist } from "@/lib/roster";

export function SpecialistCard({ s, busy, onEdit, onArchive }: {
  s: Specialist; busy: boolean; onEdit: () => void; onArchive: () => void;
}) {
  const actions = specialistActions(s);
  const caps = s.capabilities || [];

  return (
    <div className="sp-card">
      <div className="sp-head">
        {/* The specialist's own colour, which is also what marks its steps on
            the timeline — so the two views are recognisably the same agent.
            A CSS variable, not a hardcoded fill: the value comes from the
            row, and the palette it was chosen from is the token set. */}
        <span className="sp-dot" style={{ background: s.color || "var(--acc)" }} />
        <span className="sp-name">{s.name}</span>
        <span className="sp-role">{s.role}</span>
        {s.builtin && <span className="agentchip off">Built in</span>}
        {s.archived && <span className="agentchip off">Archived</span>}
      </div>

      <p className="sp-blurb">
        {s.description || ROLE_BLURB[s.role as Role] || ""}
      </p>

      <div className="sp-meta">
        Runs on {s.provider_id}
        {s.model ? ` · ${s.model}` : ""}
        {s.permission_mode && s.permission_mode !== "default" ? ` · ${s.permission_mode}` : ""}
      </div>

      {caps.length > 0 && (
        <div className="sp-caps">
          {caps.map((c) => <span className="sp-cap" key={c}>{c.replace(/_/g, " ")}</span>)}
        </div>
      )}

      {s.system_prompt && (
        <details className="sp-prompt">
          <summary>What it is told to do</summary>
          <p>{s.system_prompt}</p>
        </details>
      )}

      {/* Absent, not disabled: a builtin's persona is not the user's to
          rewrite and archiving one answers 409, so the controls are simply
          not there (docs/yuri/design/GUIDE.md §6). */}
      {(actions.edit || actions.archive) && (
        <div className="sp-actions">
          {actions.edit && (
            <button className="txtoggle" disabled={busy} onClick={onEdit}>Edit</button>
          )}
          {actions.archive && (
            <button className="txtoggle" disabled={busy} onClick={onArchive}>Retire</button>
          )}
        </div>
      )}
    </div>
  );
}
