"""The HTTP seam to an OpenCode server.

Three jobs, each learned the hard way from the live probe recorded in the
design spec section 2:

  * unwrap the {"data": ...} envelope every /api/* response carries — nothing
    in the source plan mentions it, and it silently broke the first probes;
  * translate OpenCode's errors into the two shapes the rest of Yuri already
    knows — a ValueError for "you asked wrongly" (which tools.py turns into a
    soft error the voice model recovers from) and a RuntimeError for "the
    server is broken or gone" (which the provider reports as unhealthy);
  * carry the server password, if one is configured, without ever logging it.

Nothing above this file should know what an envelope is.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("yuri.opencode.client")

# OpenCode's own error envelope uses _tag for the discriminator. Only
# InvalidRequestError belongs here: it is the one tag OpenCode itself sends
# for "you asked wrongly" (design spec section 2). A 404 for an endpoint this
# client has no business calling is a client/provider bug, not a recoverable
# user error, and must stay an OpenCodeError so it fails loudly rather than
# being silently absorbed by tools.py's ValueError-to-soft-error mapping.
_REQUEST_ERROR_TAGS = frozenset({"InvalidRequestError"})


class OpenCodeError(RuntimeError):
    """The server is unreachable, broken, or refused us. Provider-level."""


class OpenCodeRequestError(ValueError):
    """We asked wrongly (OpenCode's InvalidRequestError). A ValueError on
    purpose: tools.py already maps ValueError to a soft error the voice model
    can recover from, so a bad request reaches the user as words rather than a
    stack trace."""


class OpenCodeClient:
    def __init__(self, base_url: str, password: str | None = None,
                 timeout: float = 30.0) -> None:
        self._base = base_url.rstrip("/")
        # Held privately and never rendered: __repr__ is the default, which
        # shows the class and address, so the secret cannot leak through a log
        # line that interpolates the client.
        self._password = password or None
        self._client = httpx.AsyncClient(timeout=timeout)

    @property
    def base_url(self) -> str:
        return self._base

    def _headers(self) -> dict[str, str]:
        # The OpenAPI declares no security scheme (spec section 9), so the
        # mechanism is empirical. This header is what the fake enforces and
        # what Task 8 verifies against a real password-protected server; if
        # that check shows otherwise, this one method changes.
        return {"x-opencode-password": self._password} if self._password else {}

    async def get(self, path: str, **params: Any) -> Any:
        return await self._call("GET", path, params=params or None)

    async def post(self, path: str, body: dict | None = None) -> Any:
        return await self._call("POST", path, json=body if body is not None else {})

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, **kw: Any) -> Any:
        url = f"{self._base}{path}"
        try:
            resp = await self._client.request(method, url, headers=self._headers(), **kw)
        except httpx.HTTPError as exc:
            # Unreachable, DNS, timeout: the server's problem, not the caller's.
            raise OpenCodeError(f"OpenCode at {self._base} is unreachable: {exc}") from exc

        if resp.status_code >= 400:
            raise self._error_for(resp)
        if not resp.content:
            return None
        try:
            payload = resp.json()
        except ValueError as exc:
            raise OpenCodeError(f"OpenCode returned non-JSON from {path}") from exc
        # The envelope. Some endpoints (and the error shape) are unwrapped, so
        # only unwrap when the key is actually there.
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    def _error_for(self, resp: httpx.Response) -> Exception:
        tag, message = "", resp.text[:300]
        try:
            body = resp.json()
            if isinstance(body, dict):
                tag = str(body.get("_tag") or "")
                message = str(body.get("message") or message)
        except ValueError:
            pass
        if resp.status_code == 400 or tag in _REQUEST_ERROR_TAGS:
            return OpenCodeRequestError(message)
        return OpenCodeError(f"OpenCode returned {resp.status_code}: {message}")
