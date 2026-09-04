"use client";

// Projects: GET /projects returns ProjectService.list()'s own row shape --
// NOT the Project dataclass -- so the field is `path`, not `root_path`, and
// the list includes unregistered directories discovered under the allowed
// roots alongside registered ones (registered rows carry id/slug/kind/
// default_agent; discovered rows genuinely omit them). Each row shows its
// name, an abbreviated path, and a Registered/Discovered chip; a discovered
// row gets a Register button.
//
// There are no session or mission counts in this response, so none are
// rendered -- deriving them would mean cross-referencing two other lists,
// a feature, not a label.
//
// Projects aren't part of useYuri()'s shared state (nothing else needs them
// continuously), so this view owns its full list itself and follows the
// same fetch-then-adopt shape as app/page.tsx: a failed fetch renders an
// error + retry, never an empty list.
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useYuri } from "@/components/VoiceProvider";
import { ViewError } from "@/components/ViewError";
import { yget, ypost } from "@/lib/api";
import { abbrevHome } from "@/lib/format";
import type { ProjectRow } from "@/lib/yuriTypes";

export default function Page() {
  const { onYuriEvent } = useYuri();
  const [rows, setRows] = useState<ProjectRow[]>([]);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busyPath, setBusyPath] = useState<string | null>(null);

  const [newPath, setNewPath] = useState("");
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await yget<{ projects: ProjectRow[]; roots: string[] }>("projects");
      setRows(Array.isArray(r?.projects) ? r.projects : []);
      setLoadError(null);
    } catch (e) {
      setLoadError(e);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Refresh when a project is registered elsewhere (e.g. by starting a
  // session in an unregistered folder), not on a timer.
  useEffect(
    () =>
      onYuriEvent((ev) => {
        if (ev.type.startsWith("project.")) void load();
      }),
    [onYuriEvent, load],
  );

  const register = async (row: ProjectRow) => {
    setBusyPath(row.path);
    setActionError(null);
    try {
      await ypost("/projects", { path: row.path });
      await load();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "Could not register that project.");
    } finally {
      setBusyPath(null);
    }
  };

  const createProject = async (e: FormEvent) => {
    e.preventDefault();
    const path = newPath.trim();
    if (!path) return;
    setCreating(true);
    setCreateError(null);
    try {
      await ypost("/projects", { path, name: newName.trim() || undefined });
      setNewPath("");
      setNewName("");
      await load();
    } catch (e) {
      // On a 400 the backend's own message names the actual constraint (the
      // path sits outside ALLOWED_PROJECT_ROOTS) -- show it verbatim rather
      // than inventing our own.
      setCreateError(e instanceof Error ? e.message : "Could not create that project.");
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="projects-view">
      <h2 className="viewtitle">Projects</h2>

      <form className="proj-form" onSubmit={(e) => void createProject(e)}>
        <input
          className="proj-input proj-input-path"
          placeholder="/path/to/project"
          value={newPath}
          onChange={(e) => setNewPath(e.target.value)}
          disabled={creating}
        />
        <input
          className="proj-input proj-input-name"
          placeholder="name (optional)"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          disabled={creating}
        />
        <button className="dash-btn" type="submit" disabled={creating || !newPath.trim()}>
          Add project
        </button>
      </form>
      {/* This does not create a folder -- resolve_project_path (backend) requires
          the path to already exist under an allowed root and rejects it otherwise.
          The rows below already list every such folder with its own Register
          button; typing the path here instead is only worth it to set the name at
          the same moment you register, which is why the field order is path then
          name. Say that plainly rather than let the form imply it makes folders. */}
      <p className="togglehint">
        The folder must already exist under an allowed project root. Registering
        doesn&apos;t create it — it names it and lets you set its agent and
        verification commands.
      </p>
      {createError && <div className="apr-error">{createError}</div>}

      {loadError ? (
        <ViewError error={loadError} onRetry={() => void load()} />
      ) : (
        <>
          {actionError && <div className="apr-error">{actionError}</div>}

          {rows.length === 0 ? (
            <div className="empty">No projects registered or discovered.</div>
          ) : (
            <div className="dash-list">
              {rows.map((row) => (
                <div className="dash-row" key={row.path}>
                  <div className="dash-row-top">
                    <span className="dash-row-title">{row.name}</span>
                    <span className={`projchip ${row.registered ? "registered" : "discovered"}`}>
                      {row.registered ? "Registered" : "Discovered"}
                    </span>
                  </div>
                  <span className="dash-row-task">{abbrevHome(row.path)}</span>
                  {!row.registered && (
                    <div className="dash-row-actions">
                      <button
                        className="dash-btn"
                        disabled={busyPath === row.path}
                        onClick={() => void register(row)}
                      >
                        Register
                      </button>
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
