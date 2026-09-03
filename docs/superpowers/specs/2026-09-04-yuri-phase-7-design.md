# Yuri Phase 7 — Multi-Agent Mission Orchestration — Design Spec

**Date:** 2026-09-04
**Base:** `main` at `0af6fa3` (Phase 6 + the Yuri OS re-shell merged)
**Source plan:** `~/Downloads/Phase 7 — Multi-Agent Mission Orchestration & Collaboration.md` (§7.1–7.51)
**Predecessor specs:** `2026-09-02-yuri-foundation-design.md`, `2026-09-02-yuri-orchestration-narration-design.md`, `2026-09-03-yuri-opencode-provider-design.md`, `2026-09-03-yuri-mission-control-ui-design.md`
**Carries:** `docs/yuri/follow-ups.md`, `docs/yuri/design/README.md`

---

## 1. What this increment is

Today a mission is one session with one provider and exactly one step, titled
`"work"`. `MissionService.create()` writes that step and nothing ever advances
past it. Phase 7 turns a mission into a **workflow of tasks**, each dispatched
to a **named specialist** backed by a provider, with dependencies, handoffs,
verification and bounded retry.

Two things arrive together because they are coupled through one field:

- **The roster (7A).** An *agent* stops meaning "a provider binary" and starts
  meaning "a specialist with a role, a persona and a set of tools". You create
  them, Yuri knows what each is for.
- **The workflow engine (7B).** A mission gains a task graph. Yuri decides what
  runs next; agents decide how to do their assigned work.

They are one spec because a task's agent reference has to mean *specialist*
from the first line of code. Building the engine against provider ids first
would bake in the wrong meaning and make the roster a migration.

**Out of scope, deliberately:** autonomous workflow generation and any
LLM-based planner (§7.47), a visual DAG editor (§7.4 says so explicitly),
unbounded parallelism, git worktree isolation (§7.15 marks it future),
automatic git merge, distributed workers, vector memory, cost-aware routing
(Phase 9), the Hermes provider (Phase 8).

---

## 2. Decisions taken before design

| Decision | Choice | Why |
|---|---|---|
| Personas | **Delegated to each provider's native agent system, not reimplemented** | Measured live: `claude --agents <json>` defines agents inline at launch, and OpenCode's `POST /session` takes `agent` while its Agent schema already carries `prompt`, `color`, `permission`, `model`. Both tools own this concept; a second prompt-injection layer inside Yuri would fight them. |
| Where a task's agent comes from | **Task names a `role`; the roster resolves role → specialist → provider at dispatch** | §7.31 workflows are written in roles, §7.32 maps roles to agents. Late binding is what makes "use Claude for the whole mission" a mission-level override rather than a workflow rewrite. |
| Agent-to-agent communication | **Mediated by Yuri. No direct agent→agent control.** | Resolves a conflict in the source material — see §3. |
| Orchestrator | **Yes, a real one now: `WorkflowEngine`** | Phase 4 ruled one out because `SessionService`/`MissionService` already did its job, and said "revisit in phase 7". A task graph with dependencies, verification and retry is work no existing service does. |
| What ends a task | **The orchestrator's verification, never the agent's word** | §7.5's `VERIFYING` state and §7.19's declared checks. An agent saying "Done" is an input to the decision, not the decision. |
| Workflow authoring | **Templates + spoken composition. No inference.** | §7.30 declarative templates, §7.48's acceptance test is spoken ("use OpenCode to implement and Claude Code to review"). §7.47 excludes an LLM planner. |
| Parallelism | **Read-only tasks only, capped** | §7.7 and §7.47 ("limited parallel read-only tasks"). Two agents writing the same tree without worktrees corrupts it; §7.14's lock enforces this. |
| Specialist deletion | **Soft (archived), never hard** | A completed task records which specialist ran it. Hard-deleting rewrites history — the same reasoning that made `MissionService.delete` detach sessions rather than delete them. |

---

## 3. Two conflicts in the source material, resolved

**Agents talking to each other.** The request said *"agents also can talk to
each other for solving much more complex problems"*. The plan document §7.16
says the opposite: *"Agents should NOT directly control each other… Yuri
remains the authority. This prevents circular orchestration"*, and §7.47 lists
"agents launching agents directly" as not required.

**Resolution: agents exchange context, they do not invoke each other.** A task
finishing publishes artifacts and a handoff summary into shared mission
context; the next task's specialist receives them as part of its input. So
the reviewer really does read what the implementer found, and can send
findings back by failing verification with notes that open a retry task —
but the call to start work is always Yuri's.

This keeps §7.38's hard requirement (no autonomous infinite loop) enforceable:
if agents could start each other, no bound in Yuri could hold. Direct
agent-to-agent messaging is a coherent follow-on once this is proven; it is
not in this increment.

**Personas.** `docs/yuri/design/README.md` recorded, one day earlier, that a
per-agent persona editor was deliberately *not* taken from the reference UI:
*"Yuri's agents are providers, not personas — there is one persona, hers."*
Phase 7 reverses that decision on request. The reversal is narrower than it
looks: Yuri keeps the only *voice* and the only personality the user talks to.
A specialist's "persona" is a system prompt and a toolset — a job description,
not a character. The README will be updated to say so rather than left
contradicting this spec.

---

## 4. Vocabulary

Nine concepts, not interchangeable (§7.2). The three that are new:

| Concept | Definition | Where it lives |
|---|---|---|
| **Mission** | The user's goal. "Fix the auth bug." | `missions` (exists) |
| **Workflow** | How the mission gets done: a DAG of tasks. One per mission, versioned. | `workflows` (new) |
| **Task** | One unit of work with a role, dependencies, and a lifecycle. | `tasks` (new, replaces `mission_steps`) |
| **Role** | What a task needs done: `researcher`, `developer`, `tester`, `reviewer`, `verifier`, `documenter`. | `ROLES` constant |
| **Specialist** | A named agent: role + persona + tools + provider + model. Created by the user. | `specialists` (new) |
| **Provider** | The execution engine: `claude-code`, `opencode`. | `AgentRegistry` (exists) |
| **Session** | A running instance of a provider. | `sessions` (exists) |
| **Artifact** | Something a task produced or consumed. | `artifacts` (new) |
| **Approval** | A decision only the human can make. | `approvals` (exists) |

The rename from `mission_steps` to `tasks` is deliberate: a "step" implies a
line, and this is a graph. `mission_steps` today holds one row per mission
with `title="work"`; migration 0003 converts each into a single-task workflow
so every existing mission stays readable.

---

## 5. The roster

### 5.1 Specialist

```python
@dataclass
class Specialist:
    name: str                      # "Reviewer" — unique, user-facing
    role: str                      # one of ROLES
    provider_id: str               # "claude-code" | "opencode"
    id: str = field(default_factory=new_id)
    slug: str = ""                 # derived from name; the id given to the provider
    description: str = ""          # one line, read aloud when Yuri explains her roster
    system_prompt: str = ""        # the job description handed to the provider
    model: str | None = None       # None = the provider's default
    tools: tuple[str, ...] = ()    # () = the provider's default toolset
    permission_mode: str = "default"
    capabilities: frozenset[str] = frozenset()   # task capabilities, §6
    color: str = "#dd8a6a"         # roster and timeline identity
    builtin: bool = False          # shipped, not user-created
    archived: bool = False         # soft delete
```

`slug` is what reaches the provider (`--agent <slug>`, `POST /session
{"agent": slug}`). It is derived once at creation and then immutable, because
a running session holds it: recomputing it on rename would orphan the link.

### 5.2 Materialisation — how a persona reaches the agent

Measured, not assumed:

| Provider | Mechanism | Timing |
|---|---|---|
| `claude-code` | `--agents <json>` at launch, carrying `{slug: {description, prompt, tools, model}}`, plus `--agent <slug>` | Inline, per launch. Nothing written to disk. |
| `opencode` | Write `~/.config/opencode/agent/<slug>.md` (frontmatter: `description`, `mode`, `model`, `temperature`, `tools`, `color`; body: the prompt), then `POST /session {"agent": slug}` | The file must exist **before** session creation. |

This asymmetry is the one real cost of delegating, and it gets a named seam:
`SpecialistMaterialiser`, one implementation per provider, with a single
method:

```python
async def ensure(self, spec: Specialist) -> dict:
    """Make `spec` usable by this provider. Returns launch kwargs
    (e.g. {"agents_json": ...} or {"agent": slug}). Idempotent."""
```

Called on specialist create/update and again before every dispatch, because
a config file can be deleted behind Yuri's back and a stale `--agent <slug>`
would fail the launch with a provider error the user cannot act on.

`AgentCapabilities` gains `supports_personas: bool`. A provider that answers
False gets its specialists' prompts prepended to the first message of the
task instead — degraded but honest, and the roster UI says which providers
carry a persona natively.

### 5.3 Built-in specialists

Six ship, one per role, so a workflow runs on a fresh install with nothing
authored. They are `builtin=True`, editable, and not deletable. Their
provider assignment follows §7.32's defaults: researcher/developer →
`opencode`, tester/reviewer/verifier/documenter → `claude-code`, falling back
to whichever provider is configured when one is absent — a roster that points
at a provider the user does not run is a roster of broken buttons.

---

## 6. Capabilities

Today's `AgentCapabilities` is entirely mechanical — `interactive_terminal`,
`send_keys`, `supports_resume`. §7.9 wants to route on what a task *needs*.
These are different axes and must not be merged into one flag set: a provider
either can or cannot send keys, whereas whether it is any good at code review
is a property of the specialist's prompt and tools.

- **Provider capabilities** (`AgentCapabilities`, exists): mechanical, reported
  by the provider, not editable. Gains `supports_personas`.
- **Task capabilities** (new, `TASK_CAPABILITIES`): `coding`, `code_review`,
  `research`, `testing`, `terminal`, `browser`, `git`, `docs`. Declared on the
  **specialist**, because they describe the job it is set up for.

Routing is then a set test — a task declares `requires`, and a candidate
specialist must satisfy it:

```python
def candidates(self, role: str, requires: frozenset[str]) -> list[Specialist]:
    """Specialists that can take this task, best first. Never raises:
    an empty list is a real answer and the caller must say so out loud."""
```

Order: exact role match first, then capability superset, then the role's
configured preference from §7.32, then most recently used. Deterministic and
testable — no model call in the routing path.

---

## 7. Workflow and task model

### 7.1 Schema (migration 0003)

```sql
CREATE TABLE workflows (
  id TEXT PRIMARY KEY,
  mission_id TEXT NOT NULL REFERENCES missions(id),
  version INTEGER NOT NULL DEFAULT 1,
  template TEXT,                       -- the template name, if made from one
  status TEXT NOT NULL,                -- draft|running|paused|waiting_for_human
                                       -- |completed|failed|cancelled
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX workflows_one_live ON workflows(mission_id)
  WHERE status IN ('draft','running','paused','waiting_for_human');

CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  workflow_id TEXT NOT NULL REFERENCES workflows(id),
  ordinal INTEGER NOT NULL,            -- authoring order; NOT execution order
  kind TEXT NOT NULL,                  -- agent_task|approval|verification|human_input
  title TEXT NOT NULL,
  role TEXT,                           -- resolved to a specialist at dispatch
  specialist_id TEXT REFERENCES specialists(id),  -- pinned, or chosen at dispatch
  session_id TEXT REFERENCES sessions(id),
  status TEXT NOT NULL,                -- see 7.2
  instruction TEXT,                    -- what the specialist is told
  requires TEXT NOT NULL DEFAULT '[]', -- task capabilities, JSON array
  verification TEXT NOT NULL DEFAULT '[]',  -- check names, JSON array
  read_only INTEGER NOT NULL DEFAULT 0,     -- eligible for parallel execution
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 2,
  result TEXT NOT NULL DEFAULT '{}',
  error TEXT,
  started_at TEXT, ended_at TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE task_deps (
  task_id TEXT NOT NULL REFERENCES tasks(id),
  depends_on TEXT NOT NULL REFERENCES tasks(id),
  PRIMARY KEY (task_id, depends_on)
);

CREATE TABLE specialists (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  slug TEXT NOT NULL,
  role TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  system_prompt TEXT NOT NULL DEFAULT '',
  model TEXT,
  tools TEXT NOT NULL DEFAULT '[]',        -- JSON array
  permission_mode TEXT NOT NULL DEFAULT 'default',
  capabilities TEXT NOT NULL DEFAULT '[]', -- JSON array
  color TEXT NOT NULL DEFAULT '#dd8a6a',
  builtin INTEGER NOT NULL DEFAULT 0,
  archived INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
-- Unique among the LIVE ones only, so archiving "Reviewer" leaves the name
-- free again without renaming the row a finished task still points at.
CREATE UNIQUE INDEX specialists_name_live ON specialists(name) WHERE archived = 0;
CREATE UNIQUE INDEX specialists_slug_live ON specialists(slug) WHERE archived = 0;
CREATE TABLE artifacts (
  id TEXT PRIMARY KEY,
  mission_id TEXT NOT NULL REFERENCES missions(id),
  task_id TEXT REFERENCES tasks(id),
  kind TEXT NOT NULL,                 -- finding|patch|test_report|review|summary|file_list
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at TEXT NOT NULL
);
```

`task_deps` is a join table rather than a JSON column so a cycle check is a
query and not a parse. `workflows_one_live` mirrors the existing
`approvals_one_pending` and `sessions_one_live` partial indexes — one live
workflow per mission, enforced by the store rather than by convention.

### 7.2 Task lifecycle

§7.5's states, with the transition table as the single source of truth (the
pattern `domain/mission.py` already uses):

```
pending → ready → dispatched → running → verifying → completed
                                  ↓          ↓
                            waiting_approval  failed → (retry) → ready
                                  ↓                       ↓
                                running                blocked
```

- `pending` → `ready` when every dependency is `completed` (or `skipped`).
- `dispatched` is not cosmetic: it is the window between "we asked a provider
  to start" and "the provider confirmed" — the state a crash can land in, and
  the one reconciliation (§13) has to resolve.
- `verifying` runs the declared checks. No checks declared = pass, but the
  task still enters the state, so the timeline never shows work completing
  without the step that decides it.
- `blocked` is terminal-for-now: attempts exhausted, human needed.
- `skipped` exists for a dependency of a cancelled branch.

### 7.3 Templates

Declarative, in `yuri/workflows/templates/*.yaml`, matching §7.31's schema:

```yaml
name: bug-fix
description: Investigate, fix, test and review a bug.
tasks:
  - id: investigate
    role: researcher
    read_only: true
    instruction: "Find the root cause of: {goal}. Do not change any files."
  - id: implement
    role: developer
    depends_on: [investigate]
    instruction: "Fix the cause described in the findings. {goal}"
  - id: test
    role: tester
    depends_on: [implement]
    verification: [tests_pass]
  - id: review
    role: reviewer
    depends_on: [test]
    read_only: true
    verification: [review_approved]
```

Six ship: `bug-fix`, `feature`, `code-review`, `refactor`, `research`,
`single` (one task — what every mission gets today, so the existing implicit
path becomes a template rather than a special case).

`{goal}` is the only interpolation. A template language is a language to
maintain; one substitution covers every shipped template.

---

## 8. The orchestrator

`yuri/services/workflow.py` — `WorkflowEngine`. It owns exactly one decision:
**what happens next**. It never talks to a provider directly; it asks
`SessionService` to start and message sessions, the way voice tools already do.

```python
class WorkflowEngine:
    async def create(self, mission, template, overrides) -> Workflow
    async def advance(self, workflow_id) -> list[Task]   # the core loop
    async def on_task_finished(self, task_id, outcome) -> None
    async def retry(self, task_id, by) -> Task
    async def switch_agent(self, task_id, specialist_id, by) -> Task
    async def pause(self, workflow_id, by) -> None
    async def resume(self, workflow_id, by) -> None
    async def cancel(self, workflow_id, by, reason) -> None
```

`advance()` is a pure-ish scheduler, and the whole engine's correctness lives
in it:

1. Refuse if the workflow is not `running` (a paused workflow that still
   dispatches is the worst bug this design can have).
2. Move every `pending` task whose dependencies are satisfied to `ready`.
3. Take `ready` tasks in dependency-then-ordinal order, subject to the
   concurrency rule: **at most one writing task at a time**, plus up to
   `MAX_PARALLEL_READONLY` (2) read-only tasks.
4. For each, resolve role → specialist, materialise the persona, start or
   reuse a session, send the instruction, mark `dispatched`.
5. If nothing is ready and nothing is running and tasks remain, the workflow
   is deadlocked: status `waiting_for_human`, event `workflow.deadlocked`, and
   the blocking task set named in the payload. **Not `failed`** — the same rule
   as a bound in §12: nothing is broken, a decision is needed. A silent stall
   is indistinguishable from work in progress, which is the failure the
   presence line already taught us to avoid.
6. If every task is terminal → complete the workflow and the mission.

`advance()` is called after every task state change and on rehydrate. It is
**idempotent**: calling it twice must not double-dispatch. That is guaranteed
by the `pending → ready → dispatched` transition being the only path to a
provider call, and by transitions going through the store in one transaction.

### 8.1 Who drives it

Nothing polls. The engine is woken by the events `SessionService` already
publishes:

| Event | Engine's response |
|---|---|
| `session.turn_completed` | the running task's agent has spoken → `verifying` |
| `approval.requested` | task → `waiting_approval` |
| `approval.resolved` | task → `running`, and `advance()` |
| `agent.error` | task → `failed`, retry policy decides |
| `session.lost` | task → `failed` with a reason the user can act on |

This reuses the Phase 4 narration-ownership split rather than adding a second
subscriber that double-speaks: the engine subscribes to the **bus**, and
narration continues to own what is said. Engine-emitted events get their own
narration owners (§11).

---

## 9. Shared context, artifacts, handoff

§7.10 is explicit that agents must **not** automatically receive every other
agent's history. So the handoff is a constructed object, not a transcript
dump:

```python
@dataclass
class Handoff:
    mission_goal: str
    previous: list[ArtifactRef]     # artifacts from satisfied dependencies only
    summary: str                    # what the upstream tasks concluded
    files_touched: tuple[str, ...]
    notes: str                      # a failed review's notes, on retry
```

A task's dispatched instruction is `Handoff.render()` + the task's own
`instruction`. Rendering is capped (`HANDOFF_MAX` characters, §7.35's context
budget) and drops oldest-first, because a handoff that overflows the
provider's context makes the task fail for a reason the user cannot see.

Artifacts are produced two ways:
- **Explicitly**, by a `write_artifact` tool the specialist can call (the
  provider's own tool surface; for both providers this means the agent writes
  a file under `.yuri/artifacts/` which the engine ingests on task finish).
- **Implicitly**, from the turn's assistant text as a `summary` artifact.

The implicit one is the honest default: it always exists, so a handoff never
comes back empty just because a specialist did not know about a tool.

---

## 10. Verification

§7.19's checks, each a named, independently-testable unit:

| Check | How | Verdict |
|---|---|---|
| `tests_pass` | run the project's test command in the mission's cwd | exit code |
| `typecheck_pass` | run the project's typecheck command | exit code |
| `diff_scoped` | `git diff --name-only` ⊆ the task's expected paths | set test |
| `review_approved` | the reviewer specialist's artifact contains an explicit verdict | parsed |
| `human_ok` | an approval the user answers | approval |

The commands come from project config (`projects.metadata.verify`), not
guessed. **A project with no test command configured cannot claim
`tests_pass`** — the check reports `unavailable`, which fails the task rather
than passing it. Reporting a pass for a check that never ran is the single
most dangerous thing this feature could do, and it is exactly what a
convenient default would produce.

`VerificationResult` carries `check, verdict ∈ {pass, fail, unavailable},
detail` — and the detail is what Yuri reads out, so "tests failed" is always
followed by which.

---

## 11. Events and narration

New types in `domain/event.py`, each with a narration owner (the existing
test enforces exactly one owner per type, which is how `mission.deleted` was
caught yesterday):

| Event | Owner | Why |
|---|---|---|
| `workflow.created` | `stream` | the plan exists; Yuri reads it back |
| `task.dispatched` | `stream` | "OpenCode is investigating now" |
| `task.completed` | `stream_verbose` | texture in a long workflow |
| `task.failed` | `poll` | needs the user, carries the reason |
| `task.blocked` | `poll` | attempts exhausted, human needed |
| `task.verifying` | `none` | internal; the outcome is what matters |
| `verification.failed` | `poll` | this is the sentence that saves the user time |
| `workflow.completed` | `stream` | the mission is done |
| `workflow.deadlocked` | `poll` | never let this be silent |
| `specialist.created` | `none` | the user just did it |
| `handoff.passed` | `stream_verbose` | "passing the findings to Claude" |

---

## 12. Bounds (§7.38, hard requirement)

Enforced in `WorkflowEngine`, each with a named constant and a test that
proves the bound holds rather than that the code was written:

```python
MAX_TASK_ATTEMPTS = 2          # per task, then blocked
MAX_TASKS_PER_WORKFLOW = 40    # a template or spoken plan cannot exceed this
MAX_PARALLEL_READONLY = 2
MAX_WRITERS = 1                # until worktrees exist (§7.15)
MAX_MISSION_RUNTIME_S = 4 * 3600
MAX_SESSIONS_PER_MISSION = 4
```

On any bound: the workflow goes to `waiting_for_human`, not `failed`. A bound
is not an error, it is a decision point — and §7.38 names that state.

No task may create tasks. Only `WorkflowEngine.create` and the explicit
"append a task" API write to `tasks`, and neither is reachable from a
provider. That is what makes the bounds meaningful.

---

## 13. Recovery and reconciliation

§7.42/7.43. On startup, after the existing session rehydrate:

For every live workflow, for each non-terminal task:
- `dispatched` with no session → back to `ready` (it never started).
- `dispatched`/`running` with a session that rehydrated → stays; `advance()`.
- `running` with a session that did not survive → `failed`, reason "the
  session did not survive a restart", retry policy applies.
- `verifying` → re-run the checks. They are declared and side-effect-free by
  construction, so re-running is safe and is the only way to learn the verdict
  that was lost.

Then `advance()` once per workflow. A mission that was mid-flight when the
backend restarted therefore resumes, which is the whole point of having
persisted it.

---

## 14. Surfaces

### 14.1 Voice tools

| Tool | Purpose |
|---|---|
| `start_mission` | goal + template (or spoken task list) + role overrides |
| `describe_roster` | "who do you have?" — reads back specialists and roles |
| `assign_task` | "give the review to Claude" — pins a specialist to a task |
| `mission_status` | where the workflow is, spoken |
| `retry_task` / `skip_task` | after a failure |
| `add_task` | append to a running workflow (bounded by `MAX_TASKS_PER_WORKFLOW`) |

**No voice tool creates or edits a specialist.** Same reasoning as the mission
delete: a system prompt dictated through a speech recogniser is a persona
nobody reviewed. Creation is UI/API only.

`start_mission` **reads the plan back before running it** ("OpenCode
investigates, then implements, then Claude reviews — starting?") and waits.
This is the mitigation for the one real risk of spoken authoring: a misheard
plan that runs unseen.

### 14.2 HTTP API

```
# The RESOURCE stays `specialists` even though the UI calls them agents:
# /yuri/agents is already the provider list, and reusing that path would break
# the Agents view and the registry it reads from.
GET    /yuri/specialists                 POST   /yuri/specialists
GET    /yuri/specialists/{id}            PUT    /yuri/specialists/{id}
DELETE /yuri/specialists/{id}            # archive; 409 if a live task holds it
GET    /yuri/roles                       # roles + their preferred providers
GET    /yuri/templates
GET    /yuri/missions/{id}/workflow
POST   /yuri/missions/{id}/workflow      # create from template or explicit tasks
POST   /yuri/workflows/{id}/{pause|resume|cancel}
POST   /yuri/tasks/{id}/{retry|skip}
POST   /yuri/tasks/{id}/assign           # {specialist_id}
GET    /yuri/missions/{id}/artifacts
```

Every method used must have its Next proxy export. The `DELETE` 405 found
yesterday came from exactly this gap; `PUT` had the same bug before it.

### 14.3 UI

Two new panels in the shell, plus one extension:

- **`/agents` becomes the roster** — and does *not* become a ninth rail icon.
  The user's own word for a specialist is "agent"; today `/agents` shows
  providers. Shipping both would put "Agents" and "Specialists" side by side in
  the rail meaning two different things, which is the kind of confident-sounding
  ambiguity this shell keeps having to fix. So one panel, two sections: **Your
  agents** (the roster — a card per specialist: colour, name, role, provider,
  model, capabilities, persona, plus the create/edit form) and **Engines** (the
  existing provider health and capability table, unchanged, relabelled as what
  it is: the things that run your agents). Rail count stays at eight.
- **`/missions/[id]`** gains the **workflow timeline** (§7.27/7.44): tasks as
  a dependency-ordered list, each with its specialist's colour, status, the
  verification verdicts, and retry/skip/assign controls.
- **The dock** gains a tab per *live task session*, which it already does per
  session — `dockTabs` orders by urgency, so a task needing a decision surfaces
  without new UI.

The orb's state machine needs no change: `orbState` keys off approvals and
`s.running`, both of which a workflow drives through sessions.

---

## 15. Testing

Following the established split — pure logic under unit tests, provider
behaviour under the contract, HTTP under `TestClient`.

**New pure-logic suites** (the bulk, and where the design's correctness lives):
- `test_task_domain.py` — the transition table, every legal and illegal edge.
- `test_workflow_dag.py` — dependency satisfaction, cycle rejection at create
  time, deadlock detection, `skipped` propagation.
- `test_workflow_engine.py` — `advance()`: idempotence (called twice,
  dispatches once), the writer/reader concurrency rule, refusal while paused,
  bounds, the deadlock path.
- `test_roster.py` — slug derivation and immutability, `candidates()` ordering,
  empty-candidate honesty, archive-with-history.
- `test_handoff.py` — dependency-only scoping, budget truncation oldest-first.
- `test_verification.py` — **`unavailable` fails, never passes**.
- `test_reconcile.py` — each of §13's four cases.

**Provider-facing:**
- `provider_contract.py` gains `supports_personas` and, for providers that
  answer True, a materialise-then-launch round trip.
- `test_materialiser.py` — the OpenCode file writer (frontmatter shape,
  idempotence, recovery when the file is deleted behind us) and the Claude
  `--agents` JSON builder (shell-quoting, since §L1/L2 shell-escaping bugs
  have bitten this repo before).

**Live verification** (the Phase 5 lesson — four things were wrong in
production and right in the fake): one scripted run of the §7.48 acceptance
test against real `claude` and real `opencode serve`, recorded in
`docs/yuri/phase-7-verification.md` with what was measured, not asserted.

**Frontend:** `lib/workflow.ts` (timeline ordering, status classes),
`lib/roster.ts` (form validation, slug preview) under `node --test`.

---

## 16. Build order

Each stage leaves the app working and is independently reviewable. The
ordering rule is that nothing is built before the thing it reads from.

1. **Domain + store.** `Specialist`, `Workflow`, `Task`, `Artifact`, the
   transition table, migration 0003 including the `mission_steps → tasks`
   conversion. No behaviour yet.
2. **Roster service + API + materialisers.** Create a specialist, see it
   listed, watch it appear in OpenCode's config and in a `--agents` payload.
   Usable on its own: a single-task mission can be run by a named specialist.
3. **Roster UI.** The ninth rail icon.
4. **Workflow model + templates + creation.** Build a workflow from a
   template, see it in the API. Nothing dispatches yet.
5. **The engine's scheduler.** `advance()`, dependencies, concurrency, bounds,
   deadlock — against a fake provider, no real sessions.
6. **Dispatch + handoff.** Wire `advance()` to `SessionService`; artifacts and
   the handoff object. The acceptance test's happy path now runs.
7. **Verification + retry + failure.** Checks, `unavailable`, retry, `blocked`.
8. **Events + narration + voice tools.** Yuri can be asked and can tell.
9. **Recovery + reconciliation.** Restart mid-mission and resume.
10. **Workflow timeline UI.**
11. **Live acceptance run** against both real providers; write up what was
    measured.

Stages 1–3 are 7A. Stages 4–11 are 7B. A stop after any stage leaves a
coherent system, which matters given the size.

---

## 17. What this spec deliberately leaves for later

Git worktree isolation for parallel writers (§7.15) — the `MAX_WRITERS = 1`
bound is what makes its absence safe rather than silent. Direct agent-to-agent
messaging (§3). Cost-aware routing and automatic agent selection by learned
preference (Phase 9). A visual DAG editor (§7.4). Conditional edges — `edges`
carry a `condition` field in §7.4, but no shipped template needs one, and an
unused expression evaluator is a liability; tasks get dependencies, not
branches, until something needs a branch.
