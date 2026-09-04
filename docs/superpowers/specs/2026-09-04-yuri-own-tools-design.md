# Yuri's Own Tools — Design Spec

**Date:** 2026-09-04
**Base:** `feat/yuri-phase-7` at `0994a99`
**Predecessor:** `2026-09-04-yuri-personality-design.md` — this fills the seam that spec deliberately left open
**Touches:** `backend/tools.py`, new `backend/yuri/own/`, `frontend/lib/persona.ts`

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

## 4. Surfaces

**Tools** (`backend/tools.py`, implementations in `backend/yuri/own/`):
`web_search`, `open_app`, `music`, `set_volume`, `notify`.

**Her persona** gains the honest list. The current text — "Talk, remember
things, keep a journal, and run the coding agents. That's the honest list
today" — is updated, and the point of the sentence is preserved: it must stay
an accurate list, because a persona that claims capabilities she lacks is the
same lie in the other direction.

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
