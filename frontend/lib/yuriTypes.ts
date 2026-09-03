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
// ProjectService.list()'s own rows (services/projects.py:94) which include
// UNREGISTERED discovered directories and use `path`, not `root_path`.
// GET /projects/{id} and POST /projects return the dataclass.
export type ProjectRow = {
  id: string;
  name: string;
  path: string;
  registered: boolean;
  slug: string;
  kind: "user" | "home";
  default_agent: string | null;
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

export type YuriEvent = {
  id: string;
  type: string;
  severity: string;
  mission_id: string | null;
  session_id: string | null;
  payload: Record<string, unknown>;
  created_at: string;
};
