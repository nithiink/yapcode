-- Phase 7: the agent roster, and missions as workflows of tasks.
-- Spec: docs/superpowers/specs/2026-09-04-yuri-phase-7-design.md §5.1, §7.1

CREATE TABLE specialists (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  slug TEXT NOT NULL,
  role TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  system_prompt TEXT NOT NULL DEFAULT '',
  model TEXT,
  tools TEXT NOT NULL DEFAULT '[]',
  permission_mode TEXT NOT NULL DEFAULT 'default',
  capabilities TEXT NOT NULL DEFAULT '[]',
  color TEXT NOT NULL DEFAULT '#dd8a6a',
  builtin INTEGER NOT NULL DEFAULT 0,
  archived INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
-- Unique among the LIVE rows only. Archiving "Reviewer" frees the name again
-- without renaming the row that a finished task still points at — the same
-- reasoning that made MissionService.delete detach sessions rather than
-- delete them.
CREATE UNIQUE INDEX specialists_name_live ON specialists(name) WHERE archived = 0;
CREATE UNIQUE INDEX specialists_slug_live ON specialists(slug) WHERE archived = 0;
CREATE INDEX specialists_role ON specialists(role) WHERE archived = 0;

CREATE TABLE workflows (
  id TEXT PRIMARY KEY,
  mission_id TEXT NOT NULL REFERENCES missions(id),
  version INTEGER NOT NULL DEFAULT 1,
  template TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
-- One live workflow per mission, enforced here rather than by convention.
-- Mirrors approvals_one_pending and sessions_one_live, both of which exist
-- because the invariant was violated in practice before it was indexed.
-- The status list must stay in step with domain/workflow.py's LIVE_WORKFLOW;
-- migrate() asserts it, since sqlite cannot import a Python constant.
CREATE UNIQUE INDEX workflows_one_live ON workflows(mission_id)
  WHERE status IN ('draft','running','paused','waiting_for_human');
CREATE INDEX workflows_status ON workflows(status);

CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  workflow_id TEXT NOT NULL REFERENCES workflows(id),
  ordinal INTEGER NOT NULL,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  role TEXT,
  specialist_id TEXT REFERENCES specialists(id),
  session_id TEXT REFERENCES sessions(id),
  status TEXT NOT NULL,
  instruction TEXT NOT NULL DEFAULT '',
  requires TEXT NOT NULL DEFAULT '[]',
  verification TEXT NOT NULL DEFAULT '[]',
  read_only INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 2,
  result TEXT NOT NULL DEFAULT '{}',
  error TEXT,
  started_at TEXT,
  ended_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX tasks_workflow ON tasks(workflow_id);
CREATE INDEX tasks_specialist ON tasks(specialist_id);

-- A join table rather than a JSON column, so a cycle check is a query and
-- not a parse.
CREATE TABLE task_deps (
  task_id TEXT NOT NULL REFERENCES tasks(id),
  depends_on TEXT NOT NULL REFERENCES tasks(id),
  PRIMARY KEY (task_id, depends_on)
);
CREATE INDEX task_deps_reverse ON task_deps(depends_on);

CREATE TABLE artifacts (
  id TEXT PRIMARY KEY,
  mission_id TEXT NOT NULL REFERENCES missions(id),
  task_id TEXT REFERENCES tasks(id),
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX artifacts_mission ON artifacts(mission_id);
CREATE INDEX artifacts_task ON artifacts(task_id);

-- Every pre-Phase-7 mission has exactly one mission_steps row, titled 'work'.
-- Convert each into a one-task workflow so historical missions still render a
-- timeline instead of an empty panel.
INSERT INTO workflows (id, mission_id, version, template, status, created_at, updated_at)
SELECT lower(hex(randomblob(16))), m.id, 1, 'single',
       CASE WHEN m.status IN ('completed','failed','cancelled') THEN m.status
            ELSE 'running' END,
       m.created_at, m.updated_at
FROM missions m
WHERE EXISTS (SELECT 1 FROM mission_steps s WHERE s.mission_id = m.id);

-- Reuses each step's own id, so missions.current_step keeps pointing at a row
-- that exists.
INSERT INTO tasks (id, workflow_id, ordinal, kind, title, role, specialist_id,
                   session_id, status, instruction, requires, verification,
                   read_only, attempts, max_attempts, result, error,
                   started_at, ended_at, created_at, updated_at)
SELECT s.id, w.id, 0, 'agent_task', s.title, 'developer', NULL,
       s.session_id,
       CASE s.status WHEN 'done' THEN 'completed'
                     WHEN 'running' THEN 'running'
                     WHEN 'failed' THEN 'failed'
                     WHEN 'skipped' THEN 'skipped'
                     ELSE 'pending' END,
       '', '[]', '[]', 0, 0, 2, s.result, NULL, NULL, NULL,
       m.created_at, m.updated_at
FROM mission_steps s
JOIN missions m ON m.id = s.mission_id
JOIN workflows w ON w.mission_id = s.mission_id;

-- Drained, not dropped. The ids above are the same ids, so nothing that
-- referenced a step is now dangling. Dropping the table belongs to a later
-- migration, once nothing reads it.
DELETE FROM mission_steps;
