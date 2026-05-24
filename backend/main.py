"""Voice-Claude backend.

Mints OpenAI Realtime (GA) ephemeral tokens and dispatches the voice model's
function calls to Claude Code sessions via the Agent SDK.
"""
from __future__ import annotations

import hashlib
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from session_manager import get_runner
from tools import TOOL_DEFINITIONS, dispatch_tool

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("voice-claude")

VOICE_PROVIDER = os.getenv("VOICE_PROVIDER", "azure").lower()  # "azure" | "openai"
REALTIME_VOICE = os.getenv("REALTIME_VOICE", "marin")

# OpenAI direct
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-mini")
OPENAI_CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"
OPENAI_CALLS_URL = "https://api.openai.com/v1/realtime/calls"

# Azure OpenAI (GA realtime)
AZURE_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
AZURE_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "")  # realtime model deployment name

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


class ToolCallRequest(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/tools")
async def list_tools() -> dict[str, Any]:
    return {"tools": TOOL_DEFINITIONS}


def _mint_config(req: SessionRequest) -> tuple[str, dict[str, str], dict[str, Any], str, str]:
    """Returns (mint_url, headers, payload, webrtc_url, model) for the provider.

    Keys/endpoints stay server-side; the browser only gets the ek_ token and the
    WebRTC URL to POST its SDP offer to.
    """
    voice = req.voice or REALTIME_VOICE
    if VOICE_PROVIDER == "azure":
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


@app.post("/session")
async def create_session(req: SessionRequest) -> dict[str, Any]:
    """Mint a GA Realtime ephemeral token (Azure or OpenAI). The provider key is
    read from the server environment and never reaches the browser; the browser
    receives only the short-lived ek_ token plus the WebRTC URL to connect to."""
    mint_url, headers, payload, webrtc_url, model = _mint_config(req)
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            resp = await client.post(mint_url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            log.exception("client_secrets mint failed")
            raise HTTPException(status_code=502, detail=f"mint request failed: {exc}") from exc
    if resp.status_code >= 400:
        log.warning("%s mint error %s: %s", VOICE_PROVIDER, resp.status_code, resp.text)
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    data = resp.json()
    data["webrtc_url"] = webrtc_url
    data["provider"] = VOICE_PROVIDER
    data["model"] = model
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
