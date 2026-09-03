// Mirrors of the backend dataclasses (yuri/domain/*.py) that the Mission
// Control views read. Kept separate from components/VoiceProvider.tsx's own
// (looser, GET-shape) Approval/Mission/Agent/YuriEvent types, which predate
// this file and are deliberately not narrowed here — see that file's header
// comment for why the provider only needs enough shape for nav badges.
export type Approval = {
  id: string;
  session_id: string;
  agent_id: string;
  mission_id: string | null;
  action: string;
  tool_name: string;
  request_id: string;
  tool_input: Record<string, unknown>;
  risk: "safe" | "confirm" | "dangerous";
  description: string;
  status: "pending" | "allowed" | "denied" | "expired" | "superseded";
  requested_at: string;
  resolved_at: string | null;
  resolved_by: "voice" | "ui" | "api" | "mode_switch" | null;
};

export type Mission = {
  id: string;
  title: string;
  project_id: string;
  goal: string | null;
  status:
    | "draft"
    | "queued"
    | "running"
    | "waiting_for_approval"
    | "paused"
    | "completed"
    | "failed"
    | "cancelled";
  priority: number;
  current_step: string | null;
  created_by: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

// TWO shapes, deliberately, and they differ. GET /projects returns
// ProjectService.list()'s own rows (services/projects.py:list(), ~line 108)
// which include UNREGISTERED discovered directories and use `path`, not
// `root_path`. GET /projects/{id} and POST /projects return the dataclass.
//
// list() builds a REGISTERED row as {name, path, registered: true, id, slug,
// kind, default_agent} (every field present) but an UNREGISTERED row as only
// {name, path, registered: false} — id/slug/kind/default_agent are genuinely
// absent for a discovered-but-not-yet-registered directory, not empty
// strings, so they're optional here rather than nullable.
export type ProjectRow = {
  name: string;
  path: string;
  registered: boolean;
  id?: string;
  slug?: string;
  kind?: "user" | "home";
  default_agent?: string | null;
};

export type Project = {
  // the dataclass, from detail and create
  id: string;
  slug: string;
  name: string;
  root_path: string;
  kind: "user" | "home";
  default_agent: string | null;
  auto_approve_edits: boolean;
  repo_url: string | null;
  created_at: string;
  updated_at: string;
};

export type Agent = {
  id: string;
  name: string;
  online: boolean;
  version: string | null;
  detail: string;
  checked_at: string;
  capabilities: Record<string, unknown>;
  active_sessions: number;
};

// Mirrors AgentSession (backend/yuri/domain/session.py) exactly — the raw
// store record, asdict()'d. NOT the same shape as lib/sessions.ts's `Sess`:
// that's SessionService.list()'s own enriched projection for the Sessions
// view (keyed by `handle`, carrying the live work-pipeline). MissionDetail's
// `sessions` come from the store directly (MissionService.detail(), services/
// missions.py:166), so `id` is the key here, not `handle`, and there is no
// `running`/`queue`/`agent_name` — those are SessionService's own additions.
export type AgentSession = {
  id: string;
  project_id: string;
  agent_id: string;
  native_session_id: string;
  backend: string;
  working_directory: string;
  mission_id: string | null;
  status: string; // starting | running | needs_permission | needs_choice | idle | stopped | lost
  name: string | null;
  mode: string;
  model: string | null;
  started_at: string;
  last_activity_at: string;
  runtime_metadata: Record<string, unknown>;
};

export type MissionStep = {
  id: string;
  mission_id: string;
  ordinal: number;
  title: string;
  agent_id: string | null;
  status: "pending" | "running" | "done" | "failed" | "skipped";
  session_id: string | null;
  result: Record<string, unknown>;
};

// Matches YuriEvent.to_dict() (backend/yuri/domain/event.py) exactly — an
// asdict() of the dataclass, so these are the real wire field names: `ts`,
// not `created_at`, and `agent_id`/`project_id`/`speakable` are real fields,
// not omissions.
export type YuriEvent = {
  id: string;
  type: string;
  ts: string;
  mission_id: string | null;
  session_id: string | null;
  agent_id: string | null;
  project_id: string | null;
  severity: string;
  speakable: boolean;
  payload: Record<string, unknown>;
};
