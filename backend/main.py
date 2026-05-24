"""Voice-Claude backend.

Mints realtime voice tokens (OpenAI / Azure OpenAI over WebRTC, or Google
Gemini Live over WebSocket) and dispatches the voice model's function calls to
Claude Code sessions via the Agent SDK. Provider keys stay server-side; the
browser only ever receives a short-lived ephemeral token.
"""
from __future__ import annotations

import datetime
import hashlib
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from session_manager import get_runner
from tools import TOOL_DEFINITIONS, dispatch_tool

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("voice-claude")

# Default provider when the request doesn't specify one. "azure" | "openai" | "gemini"
VOICE_PROVIDER = os.getenv("VOICE_PROVIDER", "azure").lower()
REALTIME_VOICE = os.getenv("REALTIME_VOICE", "marin")

# OpenAI direct
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-mini")
OPENAI_CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"
OPENAI_CALLS_URL = "https://api.openai.com/v1/realtime/calls"

# Azure OpenAI (GA realtime)
AZURE_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
AZURE_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "")  # realtime model deployment name

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
    yield
    await get_runner().shutdown()


app = FastAPI(title="Voice-Claude", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class SessionRequest(BaseModel):
    model: str | None = None
    voice: str | None = None
    provider: str | None = None  # "azure" | "openai" | "gemini"; falls back to VOICE_PROVIDER


class ToolCallRequest(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/tools")
async def list_tools() -> dict[str, Any]:
    return {"tools": TOOL_DEFINITIONS}


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
            raise HTTPException(status_code=500, detail="AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_DEPLOYMENT not set")
        key = os.getenv("AZURE_OPENAI_API_KEY", "").strip()
        if not key:
            raise HTTPException(status_code=500, detail="AZURE_OPENAI_API_KEY is not set on the server")
        model = AZURE_DEPLOYMENT
        payload = {"session": {"type": "realtime", "model": model,
                               "audio": {"output": {"voice": voice}}}}
        headers = {"api-key": key, "Content-Type": "application/json"}
        mint_url = f"{AZURE_ENDPOINT}/openai/v1/realtime/client_secrets"
        webrtc_url = f"{AZURE_ENDPOINT}/openai/v1/realtime/calls?webrtcfilter=on"
        return mint_url, headers, payload, webrtc_url, model

    # OpenAI direct
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY is not set on the server")
    model = req.model or OPENAI_REALTIME_MODEL
    payload = {"session": {"type": "realtime", "model": model,
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
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY is not set on the server")
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


@app.post("/session")
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


@app.post("/tools/execute")
async def execute_tool(req: ToolCallRequest) -> dict[str, Any]:
    """Run a function call dispatched from the voice session."""
    log.info("tool call: %s args=%s", req.name, req.arguments)
    try:
        result = await dispatch_tool(req.name, req.arguments)
        return {"ok": True, "result": result}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        log.exception("tool error")
        return {"ok": False, "error": str(exc)}
