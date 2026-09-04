# Yuri's Own Tools — Design Spec

**Date:** 2026-09-04
**Base:** `feat/yuri-phase-7` at `0994a99`
**Predecessor:** `2026-09-04-yuri-personality-design.md` — this fills the seam that spec deliberately left open
**Touches:** `backend/tools.py`, new `backend/yuri/own/`, `frontend/lib/persona.ts`, `frontend/lib/instructions.ts`
**Borrows from:** `~/projects/project-yuri` — the declared permission tier (§3.6) and the derived capability map (§4). Both were surveyed at `c11aed3`; where this spec departs from that implementation, it says why.

---

## 1. What this is for

Every one of Yuri's 23 tools drives a coding agent, a mission or a project.
So when she was asked for anything else she reached for the only thing she
had, and starting a Claude Code session to open Music is the result. The
personality spec removed the instruction that told her to do that ("route it
to an agent instead of explaining limitations") and replaced it with *use the
smallest thing that answers the question* — but until now there was no
smaller thing. Her persona says, honestly:

> WHAT YOU CAN DO YOURSELF: Talk, remember things, keep a journal, and run
> the coding agents. That's the honest list today.

This spec grows that list, in two directions: **finding things out**, and
**touching this computer**. They are one spec because they share a category
(tools that are hers, never the agents') and because the second is where all
the risk is, so it is better reviewed beside something benign than alone.

---

## 2. Web search

### 2.1 Provider — measured, not chosen on preference

Verified live against the key already in `backend/.env`: Gemini's
`generateContent` with the `google_search` tool returns a synthesised answer
plus grounding metadata. A real query came back with a one-sentence answer,
three sources, and the search string it actually ran.

That settles it over the alternatives:

| | |
|---|---|
| **Gemini `google_search` grounding** | Uses `GEMINI_API_KEY`, already present. Returns a *synthesised answer*, which is what voice needs. **Chosen.** |
| A dedicated search API (Brave, Tavily, Exa) | Another key to obtain and store, and returns links she would have to read and summarise herself — a second model call to fix the shape. |
| The realtime model's own native search | Only exists on some providers, so behaviour would differ depending on which voice is connected. |

The decisive property is that it is a **backend tool**: it works whether the
voice is OpenAI, Azure or Gemini. A provider-native search tool would give
her a capability that silently disappears when the user switches voice.

It also costs nothing in security posture. `main.py` already reads
`GEMINI_API_KEY` server-side and mints an ephemeral token for the browser
precisely so "the real GEMINI_API_KEY never leaves the server" — its own
words. A backend search tool uses the key exactly where it already lives.

### 2.2 Shape

```python
web_search(query: str, freshness: str | None = None) -> dict
# -> {"answer": str, "sources": [{"title": str, "url": str}], "searched": [str]}
```

- **`answer` is capped for speech** (`ANSWER_MAX = 600` chars). She reads this
  aloud; a 2,000-word essay is not an answer, it is a hostage situation.
- **`sources` carry titles, and she cites by title, never by URL.** Reading
  "h-t-t-p-s colon slash slash" out loud is unusable, and a spoken URL cannot
  be checked anyway. Her instruction is to name the source in words
  ("Wikipedia says…"), and the URLs are there for the transcript.
- **`searched` is what the model actually queried.** Returned so the user can
  hear that their question was reinterpreted before it was answered — which
  is exactly the difference between a search result and a claim.
- **No sources means say so.** An ungrounded answer from a search tool is
  indistinguishable from the model's own memory, which is the one thing the
  tool exists to avoid. If `groundingChunks` is empty, the tool returns the
  answer with `sources: []` and her instruction is to say she could not find a
  source rather than presenting it as looked-up.

### 2.3 Bounds

- `SEARCH_TIMEOUT_S = 20`. A voice assistant that goes quiet for a minute has
  failed regardless of what comes back.
- One search per tool call. No agentic loop, no follow-up queries she decides
  on her own — §7 of the Phase 7 spec's reasoning about bounded work applies
  here too.
- Each call costs money. Her instruction is not to search for something she
  already knows, and not to re-search to double-check an answer she just gave.

---

## 3. Touching this computer

This is the part with teeth. "Control macOS" as a capability is a remote
shell with a microphone attached, so the design is an **enumerated allowlist
of named actions**, not a passthrough.

### 3.1 The actions

| Tool | Does | Needs macOS consent? |
|---|---|---|
| `open_app(app)` | Launches an app by name via `open -a` | No — launching is not scripting |
| `music(action)` | `play` / `pause` / `next` / `previous` / `now_playing` | **Yes**, once, for Music |
| `set_volume(level or step)` | Output volume, 0–100, or up/down/mute | No |
| `notify(title, text)` | A macOS notification | No |

`open_app` is allowlisted from config (`YURI_ALLOWED_APPS`), not arbitrary —
the same posture as `ALLOWED_PROJECT_ROOTS` (`config.py:137`), which already
exists, is already mandatory, and already realpaths and contains every
candidate before returning it. An unset allowlist means **no apps**, failing closed, for
the same reason `resolve_project_path` raises when no roots are configured.

### 3.2 What she may never do, by construction

Not "is discouraged from" — there is no code path:

- **No arbitrary shell, and no `osascript` passthrough.** If a capability is
  not in the table above, it does not exist. A tool that takes a script is
  the whole vulnerability in one parameter.
- **No keystrokes or clicks into other apps.** `send_keys` exists and is
  deliberately scoped to a coding session's own terminal; a general one is a
  different thing entirely.
- **No screenshots.** She has no reason to see the screen, and a voice
  assistant that can photograph the display is a surveillance device.
- **No quitting apps** — unsaved work.
- **Nothing on the filesystem.** She has agents for that, sandboxed to the
  project roots.

### 3.3 AppleScript injection — the design, verified

An app name reaches AppleScript. Interpolating it into a script string is the
bug, and this repo has shipped shell-escaping bugs twice
(`5149db7`, and the persona work's `--agents` JSON).

**Every value is passed as `argv`, never interpolated.** Verified live: a name
of `Safari"; do shell script "echo pwned` came back as the literal string,
not executed:

```
osascript - 'HOSTILE' <<'SCRIPT'
on run argv
  ... item 1 of argv ...
end run
SCRIPT
```

`subprocess` with an argument list, never `shell=True` — the same rule
`services/verify.py` already has a source-grep test for, and this module gets
the same test.

### 3.4 macOS automation consent (TCC)

Measured, not assumed: an Apple event to another app from an unconsented
process fails with

```
execution error: Not authorized to send Apple events to Finder. (-1743)
```

`display notification` and `set volume` do **not** target another app and
need no consent — both verified. Only `music` does.

Consent is granted per (calling process, target app), the first time, via a
system dialog. Two consequences the design must handle rather than discover:

- **The tool detects `-1743` and returns an actionable message**, not a
  stack trace: "macOS hasn't given me permission to control Music yet —
  approve the dialog, or turn it on in System Settings → Privacy & Security →
  Automation." Her instruction is to say that and stop, not retry.
- **The consenting process is Yuri's backend**, whatever launched it. The
  permission state observed while developing says nothing about the state at
  runtime, so this cannot be tested into a pass — the live check in §5 is how
  it gets confirmed.

### 3.5 What does NOT get a confirmation gate, and why

`cancel_mission` was given a two-call confirm because it destroys work. None
of these do: opening an app, pausing music and changing the volume are all
things the user watches happen and can undo in one gesture. A confirmation on
"pause the music" is friction protecting nothing, and friction spent where it
is not needed is what teaches someone to say yes without reading.

The line is *irreversibility*, not *reaching outside the browser*.

---

### 3.6 A declared permission tier, enforced in one place

Adopted from `~/projects/project-yuri`, which declares `permissionTier`
(`safe | approval | sensitive`) on every one of its ~35 tools
(`packages/tool-system/src/registry.ts:5-17`). Making the confirmation posture
a **property of the tool** rather than logic scattered per tool is the right
call, and this spec takes it.

It also takes the lesson from how that repo implements it, which is that **it
does not**. The tier is checked nowhere; the entire gate is a `console.log`
reminding the daemon that Yuri *should* have asked
(`apps/daemon/src/agents/tool-agent.ts:728-732`), and
`ToolRegistry.getByPermission()` has zero callers. The only real effect is
`" (asks first)"` appended to a line in the system prompt. So the gate is a
sentence the model is asked to obey — which is exactly the mechanism that
failed here on 2026-09-04, when `cancel_mission`'s description said "confirm
with the user first" and Yuri cancelled an unrelated mission two seconds
after an unrelated one was cancelled from the UI.

The asymmetry there is worth recording too, because it is the failure mode of
gating by intuition rather than by rule: `run_shell_command` has a real
allowlist with genuinely good anti-chaining (splits on `&& || ; |` and
requires every segment allowed, `apps/daemon/src/tools/shell.ts:33-49`) —
while `write_file` and `execute_applescript` are `Sensitive`, meaning
unrestricted. **`execute_applescript` therefore defeats the shell allowlist
outright** via `do shell script`. One policed door, an unlocked window beside
it.

#### The design here

Each entry in `TOOL_DEFINITIONS` declares a tier:

```python
"tier": "safe"      # runs immediately. The default; most tools.
"tier": "confirm"   # first call arms and reports; only a second call with the
                    # token runs it.
```

`dispatch_tool` reads it and applies the gate **once, centrally**, before the
handler — which generalises the arm-then-confirm mechanism `cancel_mission`
already has into a property any tool can declare. `cancel_mission` becomes
the first *user* of a general gate rather than a hand-wired special case, and
its bespoke `_arm_cancel`/`_cancel_is_confirmed` pair collapses into it.

Two rules the generalisation must keep:

- **The arm is keyed on the tool AND its resolved target.** `cancel_mission`'s
  token is bound to one mission id precisely so a token armed for one cannot
  cancel another — there is a test for that exact shape. A generic gate keyed
  only on the tool name would lose it, which would be a regression dressed as
  a refactor.
- **Single use, consumed on a wrong guess**, so a token cannot be
  brute-forced against a still-valid arm.

None of the tools in this spec is `confirm`. That is §3.5's point restated:
the line is irreversibility, not reaching outside the browser. The tier exists
so that when something irreversible does arrive, the gate is already there and
declared rather than argued about again.

---

## 4. Surfaces

**Tools** (`backend/tools.py`, implementations in `backend/yuri/own/`):
`web_search`, `open_app`, `music`, `set_volume`, `notify`.

**Her persona stops carrying a hand-written list at all.** The current text —
"Talk, remember things, keep a journal, and run the coding agents. That's the
honest list today" — is honest right now and goes stale the moment this spec
lands. It is replaced by a **derived capability map**, the second idea taken
from project-yuri (`apps/daemon/src/system-instruction.ts:148-195`), whose own
comment states the reason: so she *"never claims a tool she doesn't have"*.

Both voice transports already `fetch("/api/tools")` for the definitions they
hand the model (`lib/realtime.ts:106`, `lib/gemini.ts:163`). The map is built
from **that same payload**, in `lib/instructions.ts`, so the prose describing
her abilities and the function declarations enabling them cannot disagree —
they are one list rendered twice. That is stronger than deriving both from a
shared registry in one process, because there is no second source to drift.

Rendering rules, each with a reason:
- **Grouped by category** (`orchestration`, `own`, `macos`), so she can tell
  "things I do to your machine" from "things I do to your code".
- **First sentence of each description only.** The personality work moved
  1,517 words of guidance onto those descriptions; rendering them in full
  would put it straight back into the prompt and undo that. The full text
  still reaches the model through the function declarations — the map only has
  to be scannable.
- **`confirm`-tier tools are marked** as asking first, derived from the tier
  rather than written out, so the marking cannot drift from the enforcement.
- **Empty means empty.** If the fetch fails, the block is omitted rather than
  rendered as a guess. `yuriContextBlock` already has this contract and there
  is a test for it.

`TOOL_DEFINITIONS` gains a `category` alongside `tier`.

**Config** (`backend/config.py`): `YURI_ALLOWED_APPS` (comma-separated, empty
= none). No new key for search — `GEMINI_API_KEY` is already read.

**Not built:** a UI for any of this. These are voice-first by nature and the
Agents panel is about coding agents. Adding a "media controls" panel would be
building a worse version of the one macOS ships.

---

## 5. Testing

**Unit, no network, no AppleScript:**
- The `google_search` response parser: an answer with sources, an answer with
  **none** (must not present it as grounded), a malformed response, a timeout.
- The answer cap, and that a truncation is marked rather than silent.
- `open_app` against an allowlist: an allowed name, a disallowed one, an empty
  allowlist (refuses everything), and names containing quotes, semicolons,
  backslashes and newlines.
- `set_volume` clamps 0–100 and rejects non-numbers.
- A source grep asserting no `shell=True`, no `create_subprocess_shell`, no
  `os.system`, and **no f-string or `%` or `.format` reaching an
  `osascript -e`** — the injection rule, enforced mechanically.
- The `-1743` branch returns the actionable message.
- **The tier gate**: a `safe` tool runs on the first call; a `confirm` tool
  does not, arms, and runs on a second call with the token; a token armed for
  one target cannot act on another; a wrong guess burns the arm; a stale arm
  expires. These are `cancel_mission`'s existing eight tests, re-pointed at
  the general gate — if the refactor loses one, it says which.
- **The capability map**: every tool in the payload appears exactly once; a
  `confirm` tool is marked; only the first sentence of a description is
  rendered; a failed fetch renders nothing rather than a guess; and — the one
  that matters — **every name in the map exists in the payload, and every
  name in the payload appears in the map.** A map that can omit a tool is a
  map that makes her deny an ability she has.

**Live, and recorded in `docs/yuri/own-tools-verification.md`:**
One real search, and one real `music` call — which is the only way to see the
consent dialog appear and confirm the `-1743` path fires before consent and
stops firing after. What was *measured*, not what was expected: Phase 5's
live run corrected four things that were wrong in production and right in
the fake.

---

## 6. Deliberately not in this spec

- **Anything that reads the user's data** — calendar, mail, messages,
  contacts, browser history. Each is a separate consent surface and a
  separate privacy decision, and none of them is "open Music".
- **Play a specific song or playlist by name.** Searching a music library is
  a real feature with its own failure modes (no match, many matches, the
  wrong Sarah Vaughan); play/pause/skip is the request that was actually
  made.
- **Home automation, HomeKit, Shortcuts.** `open_app` covers launching the
  Shortcuts app; running a shortcut by name is a passthrough to arbitrary
  user-authored automation, which is §3.2's rule wearing a hat.
