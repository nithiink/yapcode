"""Yapcode backend.

Mints realtime voice tokens (OpenAI / Azure OpenAI over WebRTC, or Google
Gemini Live over WebSocket) and dispatches the voice model's function calls to
Claude Code sessions via the Agent SDK. Provider keys stay server-side; the
browser only ever receives a short-lived ephemeral token.
"""
from __future__ import annotations

import asyncio
import datetime
import fcntl
import hashlib
import json
import logging
import os
import pty
import re
import signal
import struct
import termios
import time
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

import config
import event_log
from cost_log import COST_LOG_PATH, append_cost_event
from tmux_runner import validate_session_id
from tools import TOOL_DEFINITIONS, dispatch_tool
from yuri import app as yuri_app
from yuri.api.routes import build_router
from yuri.providers.base import ProviderUnavailable

# .env is loaded once, by `import config` above. No second load here: a CWD-based
# re-read would restore the VC_AUTH_TOKEN a run mode intentionally left unset.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("yapcode")


class _RedactTokenFilter(logging.Filter):
    """Strip `token=...` from uvicorn log lines. The shared secret rides the query
    string on SSE (/debug/stream) and WebSocket (/sessions/.../terminal) connects
    because those transports can't set headers — keep it out of the logs, which
    are routinely shipped to stdout / aggregators / shown on screen-shares."""
    _TOKEN_RE = re.compile(r"(token=)[^&\s\"']+")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                self._TOKEN_RE.sub(r"\1[redacted]", a) if isinstance(a, str) else a
                for a in record.args
            )
        return True


# uvicorn.error emits the WebSocket handshake line; a filter only runs on the
# logger that emits the record, so register on both, not just uvicorn.access.
for _uvicorn_logger in ("uvicorn.access", "uvicorn.error"):
    logging.getLogger(_uvicorn_logger).addFilter(_RedactTokenFilter())

# Default provider when the request doesn't specify one. "azure" | "openai" | "gemini"
VOICE_PROVIDER = os.getenv("VOICE_PROVIDER", "azure").lower()
REALTIME_VOICE = os.getenv("REALTIME_VOICE", "marin")

# OpenAI direct
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-mini")
OPENAI_CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"
OPENAI_CALLS_URL = "https://api.openai.com/v1/realtime/calls"

# Azure OpenAI (GA realtime)
AZURE_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
AZURE_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "")  # default realtime deployment name
# All deployments the client may choose between (comma-separated, best first).
# Falls back to the single default so existing setups need no new env.
AZURE_DEPLOYMENTS: list[str] = [
    d.strip() for d in os.getenv("AZURE_OPENAI_DEPLOYMENTS", AZURE_DEPLOYMENT).split(",") if d.strip()
]


def _assert_allowed_mint_host(url: str) -> None:
    """Refuse to POST a realtime-token mint request anywhere but the known provider
    endpoints. The mint URL is built from the request-selected provider, so pin it
    to https + an allowlisted host before the outbound call."""
    parsed = urlsplit(url)
    allowed = {"api.openai.com"}
    if AZURE_ENDPOINT:
        azure_host = urlsplit(AZURE_ENDPOINT).hostname
        if azure_host:
            allowed.add(azure_host)
    if parsed.scheme != "https" or parsed.hostname not in allowed:
        raise HTTPException(status_code=500, detail="realtime mint endpoint is not allowed")

# Google Gemini Live (WebSocket). Native-audio preview is the cheapest viable model.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-native-audio-preview-12-2025")
GEMINI_VOICE = os.getenv("GEMINI_VOICE", "Kore")
# v1alpha constrained endpoint: the browser appends ?access_token=<ephemeral token>.
GEMINI_WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContentConstrained"
)

# Claude turns can run for minutes; tool dispatch must not time out early.
TOOL_TIMEOUT_S = float(os.getenv("TOOL_TIMEOUT_S", "600"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Config provenance banner: which .env supplied what (names only, no
    # secrets) — turns "API key is not there" mysteries into one log line.
    log.info("config: %s", config.summary())
    if not config.voice_keys_found():
        log.warning(
            "no voice provider key found (%s) — voice sessions WILL fail to start. "
            "Looked in %s. Fix: run `yapcode config`, or re-run the setup wizard "
            "(`yapcode up`).",
            " / ".join(config.VOICE_KEY_VARS), config.env_files_checked())
    # The debug bus first, so the Yuri events published while the container is
    # being built (project.registered, …) are mirrored by a live writer.
    event_log.start_writer()
    # Build Yuri's object graph: home, store, event bus, providers, services.
    # This also installs the one ClaudeCodeProvider in session_manager's provider
    # slot, so nothing in the process ever owns a second runner.
    # Degrade, don't refuse to boot: an unwritable ~/Yuri (or one that exists as
    # a FILE) used to take the whole voice app down, where before this branch
    # only bad config could. The block below already treats rehydration failure
    # as non-fatal; storage failure gets the same treatment. Everything that
    # needs the Yuri layer then reports YuriUnavailable — a 503 from /yuri/*, a
    # soft {ok: false, error} from a voice tool — instead of a stack trace.
    c = None
    try:
        c = await yuri_app.startup()
    except Exception as exc:
        yuri_app.note_startup_failure(exc)
        log.exception(
            "yuri: STARTUP FAILED — serving without the Yuri layer. Voice sessions cannot "
            "start until this is fixed: nothing can be recorded (no missions, no approvals, "
            "no journal) and /yuri/* returns 503. Check that YURI_HOME (%s) is a writable "
            "DIRECTORY, not a file, then restart the backend.", config.YURI_HOME)
    if c is not None:
        try:
            restored = await c.sessions.rehydrate()
            if restored:
                log.info("rehydrated %d CLI session(s): %s", len(restored),
                         [s.get("name") or s["handle"][:8] for s in restored])
        except Exception:
            log.exception("CLI session rehydration failed (continuing without it)")
    yield
    # Reverse order: Yuri's shutdown publishes/bridges its last events, so the
    # debug writer has to outlive it.
    await yuri_app.shutdown()
    await event_log.stop_writer()


# Routine poll heartbeats (every ~1.5s per active session) are pure noise; by
# default they're suppressed from the debug stream. Set VC_DEBUG_POLLS=1 to emit
# every poll tick (still default-hidden in the UI filter).
DEBUG_POLLS = os.getenv("VC_DEBUG_POLLS", "0") == "1"


# Interactive API docs are disabled: they'd disclose the full route/schema map
# of a command-executing backend to anyone who can reach the port.
app = FastAPI(title="Yapcode", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
# Origins restricted to the trusted frontend (localhost + private-LAN dev) instead
# of "*": a malicious web page in the user's browser can no longer read responses
# from — or make non-simple cross-origin calls to — the backend it can reach at
# localhost. See config.ALLOWED_ORIGINS / VC_ALLOWED_ORIGINS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_origin_regex=config.ALLOWED_ORIGIN_REGEX,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Hosts treated as "local" when no VC_AUTH_TOKEN is configured.
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _token_from(headers, query) -> str | None:
    """Pull the shared-secret token from an Authorization: Bearer header, an
    X-VC-Token header, or a ?token= query param (EventSource/WebSocket can't set
    headers, so they use the query param)."""
    auth = headers.get("authorization") or ""
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    xt = headers.get("x-vc-token")
    if xt:
        return xt.strip()
    return query.get("token")


def _access_ok(client_host: str | None, token: str | None) -> tuple[bool, str]:
    """Core access decision shared by HTTP and WebSocket paths.

    - If VC_AUTH_TOKEN is configured: a matching token is required from everyone
      (including loopback — so the same-origin Next proxy can't launder a remote
      request into a trusted localhost call).
    - Otherwise: only loopback clients are allowed; remote callers are refused
      until a token is set.
    """
    if config.AUTH_TOKEN:
        if config.token_matches(token):
            return True, ""
        return False, "missing or invalid auth token"
    if client_host in _LOOPBACK_HOSTS:
        return True, ""
    return False, "remote access requires VC_AUTH_TOKEN to be set on the server"


async def require_auth(request: Request) -> None:
    """FastAPI dependency guarding every sensitive HTTP endpoint."""
    # Origin allowlist, enforced in-app (not just via CORS response headers). A
    # cross-origin POST is still delivered to the app even when CORS hides the
    # response — and FastAPI parses a JSON body regardless of Content-Type — so a
    # malicious page could otherwise fire a side-effecting "simple" request from
    # a loopback-trusted browser. The same-origin Next proxy and native clients
    # send no Origin; only real cross-origin browser requests carry one.
    origin = request.headers.get("origin")
    if origin and not config.origin_allowed(origin):
        raise HTTPException(status_code=403, detail="origin not allowed")
    token = _token_from(request.headers, request.query_params)
    ok, reason = _access_ok(request.client.host if request.client else None, token)
    if not ok:
        raise HTTPException(status_code=401 if config.AUTH_TOKEN else 403, detail=reason)


# Mounted right after require_auth's own definition so the dependency it
# passes in is unambiguously the one just above -- build_router() takes the
# guard as a parameter (rather than importing main.require_auth itself)
# purely to avoid yuri.api.routes <-> main circularity.
app.include_router(build_router(require_auth))


@app.exception_handler(yuri_app.YuriUnavailable)
async def _yuri_unavailable(request: Request, exc: yuri_app.YuriUnavailable) -> JSONResponse:
    """Yuri's storage is down (see the lifespan's degraded path). 503 with the
    actionable reason — never a 500 and a stack trace."""
    log.warning("%s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=503, content={"detail": str(exc)})


def _ws_access_ok(ws: WebSocket) -> tuple[bool, int]:
    """Authorize a WebSocket handshake (CORS middleware does not apply to WS).
    Returns (ok, close_code). Enforces the Origin allowlist for browser clients
    and the same token/loopback rule as HTTP."""
    origin = ws.headers.get("origin")
    if origin and not config.origin_allowed(origin):
        return False, 4403  # forbidden origin
    token = _token_from(ws.headers, ws.query_params)
    ok, _reason = _access_ok(ws.client.host if ws.client else None, token)
    return (True, 0) if ok else (False, 4401)  # unauthorized


class SessionRequest(BaseModel):
    model: str | None = None
    voice: str | None = None
    provider: str | None = None  # "azure" | "openai" | "gemini"; falls back to VOICE_PROVIDER
    instructions: str | None = None  # baked into the Azure mint (see _mint_config)


class ToolCallRequest(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class CostLogRequest(BaseModel):
    # Free-form record from the UI — schema is enforced by the writer's docstring
    # rather than pydantic so we can evolve fields without bumping the API. The
    # backend stamps `ts` if the client omits it.
    record: dict[str, Any]


class DebugLogRequest(BaseModel):
    # Browser-only pipeline events the backend can't see otherwise (voice
    # transcripts, [Claude update] injections, voice errors, connect/disconnect).
    source: str = "voice"
    dest: str = "backend"
    kind: str
    summary: str
    session: str | None = None
    detail: dict[str, Any] | None = None


class HandoffRequest(BaseModel):
    # Terminal -> voice handoff: a Claude Code session the user is running in
    # their own terminal asks yapcode to take it over (the /voice-handoff
    # plugin command POSTs this).
    session_id: str
    cwd: str
    tmux: str | None = None     # the $TMUX env of the caller (informational)
    name: str | None = None


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/tools", dependencies=[Depends(require_auth)])
async def list_tools() -> dict[str, Any]:
    return {"tools": TOOL_DEFINITIONS}


@app.get("/voice/models", dependencies=[Depends(require_auth)])
async def voice_models() -> dict[str, Any]:
    """Provider model options that are server infra rather than client knowledge:
    Azure's choices are the env-configured deployment names."""
    return {"azure": AZURE_DEPLOYMENTS}


def _mint_config(
    provider: str, req: SessionRequest
) -> tuple[str, dict[str, str], dict[str, Any], str, str]:
    """Returns (mint_url, headers, payload, webrtc_url, model) for an OpenAI-shaped
    provider (azure | openai). Keys/endpoints stay server-side; the browser only
    gets the ek_ token and the WebRTC URL to POST its SDP offer to.
    """
    voice = req.voice or REALTIME_VOICE
    if provider == "azure":
        if not AZURE_ENDPOINT or not AZURE_DEPLOYMENT:
            raise HTTPException(status_code=500,
                                detail=config.missing_key_detail("AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_DEPLOYMENT"))
        key = os.getenv("AZURE_OPENAI_API_KEY", "").strip()
        if not key:
            raise HTTPException(status_code=500, detail=config.missing_key_detail("AZURE_OPENAI_API_KEY"))
        # Honor the client's deployment pick only when allowlisted — deployment
        # names are server infra; anything else falls back to the default.
        model = req.model if req.model in AZURE_DEPLOYMENTS else (AZURE_DEPLOYMENT or AZURE_DEPLOYMENTS[0])
        # Azure's ephemeral WebRTC session locks its config at mint time and
        # ignores the browser's later session.update — so tools/instructions
        # have to be baked in here, or the model connects with no tools (it can
        # never call Claude) and no system prompt (generic-assistant behavior).
        # OpenAI, by contrast, honors the client session.update, so its mint
        # stays minimal.
        session_cfg: dict[str, Any] = {
            "type": "realtime", "model": model,
            "tools": TOOL_DEFINITIONS, "tool_choice": "auto",
            # Lock output to audio so the model never emits a text-only message
            # item that goes unspoken. Azure binds config at mint time and
            # ignores the client session.update, so it MUST be set here (the
            # OpenAI-native path also sets it in lib/realtime.ts).
            "output_modalities": ["audio"],
            "audio": {
                "output": {"voice": voice},
                # Input VAD must be set here too: Azure ignores the client
                # session.update, so without this it runs on Azure's bare
                # default and barges in on the model's own audio
                # (self-interruption). Set the SAME server_vad the frontend
                # configures for OpenAI-native (lib/realtime.ts) so both
                # providers behave identically.
                "input": {"turn_detection": {"type": "server_vad"}},
            },
        }
        if req.instructions:
            session_cfg["instructions"] = req.instructions
        payload = {"session": session_cfg}
        headers = {"api-key": key, "Content-Type": "application/json"}
        mint_url = f"{AZURE_ENDPOINT}/openai/v1/realtime/client_secrets"
        # No ?webrtcfilter=on: it strips the function-call argument events
        # (response.done, *.function_call_arguments.done, …), so tool calls
        # arrive with empty args. It only hides the prompt, which we don't need.
        webrtc_url = f"{AZURE_ENDPOINT}/openai/v1/realtime/calls"
        return mint_url, headers, payload, webrtc_url, model

    # OpenAI direct
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise HTTPException(status_code=500, detail=config.missing_key_detail("OPENAI_API_KEY"))
    model = req.model or OPENAI_REALTIME_MODEL
    # output_modalities: ["audio"] locks output to speech so no text-only item
    # goes unspoken. The client session.update (lib/realtime.ts) reasserts it,
    # but setting it at mint covers the first response before that lands.
    payload = {"session": {"type": "realtime", "model": model,
                           "output_modalities": ["audio"],
                           "audio": {"output": {"voice": voice}}}}
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "OpenAI-Safety-Identifier": hashlib.sha256(key.encode()).hexdigest()[:32],
    }
    return OPENAI_CLIENT_SECRETS_URL, headers, payload, OPENAI_CALLS_URL, model


def _mint_gemini_token(model: str) -> str:
    """Mint a single-use Gemini Live ephemeral token via the google-genai SDK.

    The token is pinned to this model and the AUDIO modality and expires quickly,
    so it's safe to hand to the browser; the real GEMINI_API_KEY never leaves the
    server. Synchronous SDK call — run it in a threadpool from the async route.
    """
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        raise HTTPException(status_code=500, detail=config.missing_key_detail("GEMINI_API_KEY"))
    from google import genai  # imported lazily so the rest of the app loads without it

    client = genai.Client(api_key=key, http_options={"api_version": "v1alpha"})
    now = datetime.datetime.now(datetime.timezone.utc)
    # Intentionally NO live_connect_constraints: locking a config makes the
    # constrained endpoint use the token's config and ignore the browser's full
    # setup message — which is where our systemInstruction and tools live. The
    # token is still single-use with a short window, so it's safe to let the
    # client supply the session config.
    token = client.auth_tokens.create(
        config={
            "uses": 1,
            "expire_time": now + datetime.timedelta(minutes=30),
            "new_session_expire_time": now + datetime.timedelta(minutes=2),
            "http_options": {"api_version": "v1alpha"},
        }
    )
    return token.name


@app.post("/session", dependencies=[Depends(require_auth)])
async def create_session(req: SessionRequest) -> dict[str, Any]:
    """Mint a realtime ephemeral token for the chosen provider. The provider key
    is read from the server environment and never reaches the browser; the browser
    receives only the short-lived token plus the URL to connect to.

    Response always includes: value (token), provider, model, transport. For
    WebRTC providers it adds webrtc_url; for Gemini it adds ws_url."""
    provider = (req.provider or VOICE_PROVIDER).lower()

    if provider == "gemini":
        model = req.model or GEMINI_MODEL
        try:
            token = await run_in_threadpool(_mint_gemini_token, model)
        except HTTPException:
            raise
        except Exception as exc:
            log.exception("gemini token mint failed")
            raise HTTPException(status_code=502, detail=f"gemini mint failed: {exc}") from exc
        return {
            "value": token,
            "provider": "gemini",
            "model": model,
            "transport": "websocket",
            "ws_url": GEMINI_WS_URL,
            "voice": req.voice or GEMINI_VOICE,
        }

    mint_url, headers, payload, webrtc_url, model = _mint_config(provider, req)
    _assert_allowed_mint_host(mint_url)
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            resp = await client.post(mint_url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            log.exception("client_secrets mint failed")
            raise HTTPException(status_code=502, detail=f"mint request failed: {exc}") from exc
    if resp.status_code >= 400:
        log.warning("%s mint error %s: %s", provider, resp.status_code, resp.text)
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    data = resp.json()
    data["webrtc_url"] = webrtc_url
    data["provider"] = provider
    data["model"] = model
    data["transport"] = "webrtc"
    return data


@app.post("/session/handoff", dependencies=[Depends(require_auth)])
async def handoff_session(req: HandoffRequest) -> dict[str, Any]:
    """Adopt a Claude Code session the user started in their own terminal so the
    voice agent can co-drive it. If the id is already a live yapcode session
    (e.g. started via the `yapcode` launcher), this is a no-op that just hands
    back the attach target. Otherwise the session is reopened in a hooked tmux pane
    via `claude --resume` — the caller should then exit the original process and
    `tmux attach` to the returned target (single writer per session)."""
    sid = (req.session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="session_id is required")
    # session_id becomes a directory name under the session store; reject path
    # traversal before it is used (see tmux_runner.validate_session_id).
    try:
        sid = validate_session_id(sid)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # SessionService.adopt decides which of the two paths this is (already a
    # live session -> hand back the attach target; a bare one -> reopen it in a
    # hooked pane) and records the mission/session rows either way. It resolves
    # req.cwd through resolve_project_path, which realpath + containment-checks
    # it against ALLOWED_PROJECT_ROOTS (fail closed).
    try:
        out = await yuri_app.container().sessions.adopt(sid, req.cwd, req.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # `attach` is None on a backend with no tmux pane (the SDK runner). It goes
    # straight into a message the user is told to paste, so never fabricate one
    # — say there is nothing to attach to instead.
    attach = out["attach"]
    if out["already"]:
        return {"session_id": out["session_id"], "name": out["name"], "attach": attach,
                "message": (f"Voice is live on this session. Keep typing here, or attach "
                            f"another terminal with: {attach}") if attach else
                           ("Voice is live on this session. Keep typing here — this backend "
                            "has no terminal to attach a second view to.")}
    return {"session_id": out["session_id"], "name": out["name"], "cwd": out["cwd"], "attach": attach,
            "message": (f"Reopened '{out['name']}' under yapcode. Exit your old session "
                        f"(Ctrl-D), then run: {attach}") if attach else
                       (f"Reopened '{out['name']}' under yapcode, but this backend has no "
                        "terminal to attach to — drive it by voice.")}


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


@app.websocket("/sessions/{handle}/terminal")
async def session_terminal(ws: WebSocket, handle: str) -> None:
    """Stream the live interactive Claude TUI (CLI backend) to a browser terminal.

    Bridges a PTY running `tmux attach-session` to the WebSocket: pane output ->
    ws bytes; ws text -> keystrokes; a {"__resize":{cols,rows}} message resizes.
    Closing the socket detaches the tmux client without killing the session."""
    # Authorize the handshake BEFORE accepting: this socket injects raw keystrokes
    # into the live Claude TUI, so an unauthenticated/cross-origin client must not
    # reach the PTY. Rejecting before accept() denies the handshake outright.
    ok, close_code = _ws_access_ok(ws)
    if not ok:
        await ws.close(code=close_code)
        return
    await ws.accept()
    # container_or_none, not container(): the handshake is already accepted, so
    # a not-yet-built container should reach the client as the same "no live
    # terminal" message rather than an unhandled RuntimeError.
    c = yuri_app.container_or_none()
    pane = c.sessions.native_pane(handle) if c is not None else None
    if not pane:
        await ws.send_text("\r\n[no live terminal — this is not a running CLI session]\r\n")
        await ws.close()
        return

    pid, fd = pty.fork()
    if pid == 0:  # child: become the tmux client
        os.environ["TERM"] = "xterm-256color"
        try:
            os.execvp("tmux", ["tmux", "attach-session", "-t", pane])
        except Exception:
            os._exit(1)

    loop = asyncio.get_event_loop()
    _set_winsize(fd, 30, 100)

    def on_readable() -> None:
        try:
            data = os.read(fd, 65536)
        except OSError:
            data = b""
        if not data:
            loop.remove_reader(fd)
            return
        asyncio.create_task(ws.send_bytes(data))

    loop.add_reader(fd, on_readable)
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            text = msg.get("text")
            if text is not None:
                if text.startswith('{"__resize"'):
                    try:
                        r = json.loads(text)["__resize"]
                        _set_winsize(fd, int(r["rows"]), int(r["cols"]))
                    except Exception:
                        pass
                else:
                    os.write(fd, text.encode())
            elif msg.get("bytes") is not None:
                os.write(fd, msg["bytes"])
    except WebSocketDisconnect:
        pass
    finally:
        loop.remove_reader(fd)
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


@app.post("/cost/log", dependencies=[Depends(require_auth)])
async def log_cost(req: CostLogRequest) -> dict[str, Any]:
    """Append one cost record to the JSONL log. The UI calls this on connection
    start, periodically while connected, and on disconnect."""
    await append_cost_event(req.record)
    return {"ok": True, "path": str(COST_LOG_PATH)}


@app.get("/debug/recent", dependencies=[Depends(require_auth)])
async def debug_recent(limit: int = 500) -> dict[str, Any]:
    """Snapshot of the most recent pipeline events (ring buffer). Fallback for
    clients that can't hold an SSE connection."""
    return {"events": event_log.recent(limit)}


@app.post("/debug/log", dependencies=[Depends(require_auth)])
async def debug_log(req: DebugLogRequest) -> dict[str, str]:
    """Ingest a browser-only event into the unified bus so it lands in the file
    and streams to every other connected panel alongside the backend events."""
    event_log.log_event(req.source, req.dest, req.kind, req.summary,
                        session=req.session, detail=req.detail)
    return {"ok": "true"}


@app.get("/debug/stream", dependencies=[Depends(require_auth)])
async def debug_stream(limit: int = 200) -> StreamingResponse:
    """Server-Sent Events stream of the full pipeline. Replays the last `limit`
    buffered events on connect (so the panel isn't empty), then live-streams new
    ones. Emits a comment ping every 15s to keep the connection open."""
    async def gen():
        q = event_log.subscribe()
        try:
            for rec in event_log.recent(limit):
                yield f"data: {json.dumps(rec, default=str)}\n\n"
            while True:
                try:
                    rec = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {json.dumps(rec, default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            event_log.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"},
    )


@app.post("/tools/execute", dependencies=[Depends(require_auth)])
async def execute_tool(req: ToolCallRequest) -> dict[str, Any]:
    """Run a function call dispatched from the voice session."""
    start = time.monotonic()
    # poll_session fires every ~1.5s per active session — too noisy to log every
    # call, but the moment it returns something interesting (not 'working' / not
    # 'idle') we want to SEE it. Same for the result side below.
    sid = req.arguments.get("session_id")
    # poll_session and list_sessions are UI heartbeats (every ~1.5-2s per the
    # frontend's poll + session-list refresh). They drown out real communication,
    # so keep them out of the INFO log and the debug stream unless VC_DEBUG_POLLS=1.
    is_poll = req.name == "poll_session"
    is_quiet = req.name in ("poll_session", "list_sessions")
    if not is_quiet or DEBUG_POLLS:
        if not is_quiet:
            log.info("tool call: %s args=%s", req.name, req.arguments)
        event_log.log_event("voice", "backend", "tool_call", req.name,
                            session=sid, detail={"name": req.name, "arguments": req.arguments})
    try:
        result = await dispatch_tool(req.name, req.arguments)
        elapsed = time.monotonic() - start
        if is_poll:
            status = (result or {}).get("status")
            rsid = (result or {}).get("session_id")
            if status not in (None, "working", "idle"):
                log.info("poll_session -> %s (sid=%s)", status, rsid)
                event_log.log_event("backend", "voice", "poll", f"poll → {status}",
                                    session=rsid, detail=result)
            elif DEBUG_POLLS:
                event_log.log_event("backend", "voice", "poll", f"poll → {status}",
                                    session=rsid, detail=result)
        elif is_quiet:
            if DEBUG_POLLS:
                event_log.log_event("backend", "voice", "tool_result", f"{req.name} → ok",
                                    session=sid, detail=result)
        else:
            log.info("tool done: %s in %.2fs", req.name, elapsed)
            event_log.log_event("backend", "voice", "tool_result",
                                f"{req.name} → {(result or {}).get('status', 'ok')}",
                                session=sid, detail=result)
        return {"ok": True, "result": result}
    except yuri_app.YuriUnavailable as exc:
        # Degraded boot: every tool needs the Yuri layer. Hand the voice model
        # the actionable reason as a SOFT error so it can tell the user what to
        # fix, instead of the generic "failed unexpectedly" below.
        log.warning("tool %s unavailable: %s", req.name, exc)
        event_log.log_event("backend", "voice", "error", f"{req.name}: {exc}", session=sid)
        return {"ok": False, "error": str(exc)}
    except NotImplementedError as exc:
        # A capability this provider does not have -- set_mode on OpenCode,
        # send_keys on the SDK backend. The message names the provider and what
        # it cannot do, which is exactly what the model should relay; the
        # generic handler below would replace all of it with "failed
        # unexpectedly" and the user would hear nothing useful.
        log.info("tool %s unsupported: %s", req.name, exc)
        event_log.log_event("backend", "voice", "error", f"{req.name}: {exc}", session=sid)
        return {"ok": False, "error": str(exc)}
    except ProviderUnavailable as exc:
        # A provider that cannot serve wrote an actionable message about how to
        # fix it; the generic handler below would throw that away. Soft, like
        # YuriUnavailable above, so the model relays it instead of retrying.
        log.warning("tool %s: provider unavailable: %s", req.name, exc)
        event_log.log_event("backend", "voice", "error", f"{req.name}: {exc}", session=sid)
        return {"ok": False, "error": str(exc)}
    except KeyError as exc:
        event_log.log_event("backend", "voice", "error", f"{req.name}: {exc}", session=sid)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        log.info("tool failed: %s in %.2fs (%s)", req.name, time.monotonic() - start, exc)
        event_log.log_event("backend", "voice", "error", f"{req.name}: {exc}", session=sid)
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        # Unexpected failures can carry internal detail, so log the traceback
        # server-side but return a generic message rather than str(exc).
        log.exception("tool error after %.2fs", time.monotonic() - start)
        event_log.log_event("backend", "voice", "error", f"{req.name}: {exc}", session=sid)
        return {"ok": False, "error": "the tool failed unexpectedly — see the backend logs"}
