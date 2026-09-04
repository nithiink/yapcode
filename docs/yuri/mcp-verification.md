# MCP connectors — live verification

**Date:** 2026-09-04 · **Spec:** `docs/superpowers/specs/2026-09-04-yuri-mcp-connectors-design.md`

What was measured, against a real third-party server, through the real API.
Not what was expected.

**Setup:** a throwaway `YURI_HOME`, the backend on `127.0.0.1:8077`, and
`uvx mcp-server-time` — a server nobody here wrote — as the subject. Every
call below is the same HTTP path the UI uses.

---

## The server

`initialize` returned its own name and version, which is what the UI shows so
the user can confirm the thing they configured is the thing that answered:

```
server_name: mcp-time      server_version: 1.29.1
tools:       get_current_time, convert_time
```

Its `inputSchema` passed through untranslated into `parameters` — it is
already JSON Schema, which is the shape `TOOL_DEFINITIONS` uses.

## Test before save

| step | result |
|---|---|
| `POST /yuri/mcp/test` on the real server | `ok`, 2 tools, no stderr |
| the file afterwards | **no `mcp.json` at all** — the test persisted nothing |
| `POST /yuri/mcp` with a command that does not resolve | **HTTP 400**, `"the server did not start, so it was not saved"`, with uv's own stderr tail attached |
| the file afterwards | still absent — a failing server cannot reach config |
| `POST /yuri/mcp/test` with no `tier` | **HTTP 400**: *server 'x': needs "tier": "safe" or "confirm" — there is no default, because that would be choosing for you.* |
| `POST /yuri/mcp` for a name already saved | **HTTP 409**, and the existing entry's API key was still in the file afterwards |

## Saving, and what the API will not say

Saved with `env: {"SOME_TOKEN": "planted-secret-abc123"}`. The value is in
`~/Yuri/mcp.json` and **nowhere else**:

```
file mode:                     -rw-------   (0600)
grep for the secret in /yuri/mcp:   not present
grep for the secret in /tools:      not present
what /yuri/mcp does report:         env_keys: ["SOME_TOKEN"]
```

## The child process does not inherit this backend's secrets

`child_env()` is an allowlist, and the running child was inspected rather
than the code being trusted:

```
env vars in the parent (uvicorn):  53
env vars in the child:              7
key-shaped vars in the child:       SOME_TOKEN   (the one we gave it)
                                    — no GEMINI_API_KEY, no VC_AUTH_TOKEN
```

## Calling a tool

Through `POST /tools/execute`, the same endpoint the voice model uses:

```json
{"ok": true, "result": {
  "tool": "mcp_time_get-current-time", "server": "time", "ran": true,
  "message": "mcp-time says: {\"timezone\": \"Asia/Kolkata\", \"datetime\": \"2026-09-04T20:12:34+05:30\", …}"
}}
```

Note `mcp-time says:` — the server's own name, because a third party's answer
is something that was **said**, not something verified.

An unknown tool is a soft error the model can read back, not an exception:
`{"ok": false, "error": "mcp_time_nope isn't available right now"}`.

## The gate, at `confirm` tier

The same server, re-added with `tier: "confirm"`:

| call | `ran` |
|---|---|
| first, with no token | `false` — *"Nothing has happened yet — tell the user exactly that, and only call … again with `yuri_confirm=c4d359` once they agree."* |
| second, with `yuri_confirm=c4d359` | `true` |
| third, with the **same** token | `false` — single use |

## Lifecycle

| action | status | tools advertised |
|---|---|---|
| `PUT /yuri/mcp/time/enabled {"enabled": false}` | `disabled` | 0 — and the API key stayed in the file |
| `PUT … {"enabled": true}` | `connected` | 2 |
| `POST /yuri/mcp/time/reconnect` | `connected` | 2 |
| `DELETE /yuri/mcp/time` | gone from the file | 0, and the child process was gone |

## Startup, with one good server and one broken one

Both written to `mcp.json`, then the backend restarted:

```
nosuch: failed    | 0 tools | the server exited: × No solution found when resolving…
time:   connected | 2 tools |
/tools advertises: mcp_time_get-current-time, mcp_time_convert-time
```

The broken server is visible with its reason and advertises nothing. That is
the point of deriving the tool list: she cannot promise a capability that
isn't there.

A malformed `mcp.json` is reported rather than read as "no servers":

```
config_error: mcp.json isn't valid JSON: Expecting property name enclosed in
              double quotes: line 1 column 2 (char 1)
```

Shutting the backend down left no `mcp-server-time` process behind.

---

## What this does not verify

- **Only `stdio`.** The `sse` and `http` transports are rejected with *"isn't
  built yet"*, which is honest; nothing here drove them.
- **Not injection safety.** A server's descriptions and results still reach
  the model. The mitigations measured above are the user choosing the server,
  the tier being the user's declaration, the namespace making a native tool
  unreachable by name, and the gate. None of that is a claim that a hostile
  tool result cannot influence her — see the spec's §2.
- **The UI was not driven by hand here.** Its logic is covered by
  `frontend/lib/mcp.test.ts` (23 tests); the endpoints it calls are the ones
  exercised above.
