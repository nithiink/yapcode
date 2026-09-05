-- Yuri state store v1 (spec §4). JSON columns are TEXT holding json.dumps().
CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  root_path TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL DEFAULT 'user',
  default_agent TEXT,
  auto_approve_edits INTEGER NOT NULL DEFAULT 0,
  repo_url TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS missions (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  goal TEXT,
  project_id TEXT NOT NULL REFERENCES projects(id),
  status TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  current_step TEXT,
  created_by TEXT NOT NULL,
  metadata TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS missions_status ON missions(status);

CREATE TABLE IF NOT EXISTS mission_steps (
  id TEXT PRIMARY KEY,
  mission_id TEXT NOT NULL REFERENCES missions(id),
  ordinal INTEGER NOT NULL,
  title TEXT NOT NULL,
  agent_id TEXT,
  status TEXT NOT NULL,
  session_id TEXT,
  result TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS mission_steps_mission ON mission_steps(mission_id, ordinal);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  mission_id TEXT REFERENCES missions(id),
  project_id TEXT NOT NULL REFERENCES projects(id),
  agent_id TEXT NOT NULL,
  native_session_id TEXT NOT NULL,
  backend TEXT NOT NULL,
  status TEXT NOT NULL,
  name TEXT,
  mode TEXT NOT NULL DEFAULT 'default',
  model TEXT,
  working_directory TEXT NOT NULL,
  started_at TEXT NOT NULL,
  last_activity_at TEXT NOT NULL,
  runtime_metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS sessions_mission ON sessions(mission_id);
CREATE INDEX IF NOT EXISTS sessions_native ON sessions(native_session_id);

CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY,
  mission_id TEXT,
  session_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  action TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  tool_input TEXT NOT NULL DEFAULT '{}',
  risk TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  request_id TEXT NOT NULL UNIQUE,
  requested_at TEXT NOT NULL,
  resolved_at TEXT,
  resolved_by TEXT
);
CREATE INDEX IF NOT EXISTS approvals_session_status ON approvals(session_id, status);
-- one decision per prompt (encodes the fix in commit 14bc293)
CREATE UNIQUE INDEX IF NOT EXISTS approvals_one_pending ON approvals(session_id) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  ts TEXT NOT NULL,
  type TEXT NOT NULL,
  mission_id TEXT,
  session_id TEXT,
  agent_id TEXT,
  project_id TEXT,
  severity TEXT NOT NULL,
  speakable INTEGER NOT NULL DEFAULT 0,
  payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS events_mission_ts ON events(mission_id, ts);
CREATE INDEX IF NOT EXISTS events_session_ts ON events(session_id, ts);
