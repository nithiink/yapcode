"""Web search (spec §2).

Gemini's `google_search` grounding rather than a dedicated search API, for
three measured reasons: the key is already in the environment and already read
server-side (`main.py` mints an ephemeral browser token precisely so "the real
GEMINI_API_KEY never leaves the server"); it returns a SYNTHESISED answer,
which is what a voice assistant needs rather than ten links it must summarise
itself; and being a backend tool it works whichever voice provider is
connected — a provider-native search would be a capability that silently
disappeared when the user switched voice.

The parsing is deliberately split from the request so it can be tested
without a network: `parse_grounded` is pure and takes the raw response body.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import httpx

# She reads the answer out loud. A 2,000-word essay is not an answer.
ANSWER_MAX = 600
# Enough to say "Wikipedia and two others" without becoming a bibliography.
SOURCES_MAX = 4
# A voice assistant that goes quiet for a minute has failed regardless of what
# eventually comes back.
SEARCH_TIMEOUT_S = 20.0
QUERY_MAX = 400

MODEL = "gemini-2.5-flash"
_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class SearchUnavailable(RuntimeError):
    """Search cannot run, with a message saying what to do about it.

    A named type so `/tools/execute` surfaces the reason instead of replacing
    it with "the tool failed unexpectedly" — the same reasoning as
    ProviderUnavailable.
    """


@dataclass
class SearchResult:
    answer: str
    sources: list[dict[str, str]] = field(default_factory=list)
    searched: list[str] = field(default_factory=list)
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"answer": self.answer, "sources": self.sources,
                "searched": self.searched, "truncated": self.truncated,
                # Explicit rather than left for the model to infer from an
                # empty list: an ungrounded answer is indistinguishable from
                # the model's own memory, which is the one thing this tool
                # exists to avoid. Her instruction is to say she could not
                # find a source rather than present it as looked-up.
                "grounded": bool(self.sources)}


def _clip(text: str, cap: int) -> tuple[str, bool]:
    flat = " ".join((text or "").split())
    if len(flat) <= cap:
        return flat, False
    # Cut at a sentence end where one is near the limit, so the answer ends as
    # a sentence rather than mid-word.
    window = flat[:cap]
    stop = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
    if stop > cap * 0.6:
        return window[:stop + 1], True
    # cap-1 so the ellipsis fits INSIDE the cap rather than making it cap+1.
    return flat[:cap - 1].rstrip() + "…", True


def parse_grounded(data: Any) -> SearchResult:
    """Turn a generateContent response into something speakable.

    Tolerant by construction: every field below is optional in practice, and a
    shape change upstream must degrade to "no answer" rather than raise inside
    a voice turn.
    """
    if not isinstance(data, dict):
        return SearchResult(answer="")
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return SearchResult(answer="")
    cand = candidates[0] if isinstance(candidates[0], dict) else {}

    parts = ((cand.get("content") or {}).get("parts") or []) if isinstance(cand.get("content"), dict) else []
    raw = "".join(str(p.get("text") or "") for p in parts if isinstance(p, dict))
    answer, truncated = _clip(raw, ANSWER_MAX)

    meta = cand.get("groundingMetadata") if isinstance(cand.get("groundingMetadata"), dict) else {}
    sources: list[dict[str, str]] = []
    seen: set[str] = set()
    for chunk in (meta.get("groundingChunks") or []):
        web = chunk.get("web") if isinstance(chunk, dict) else None
        if not isinstance(web, dict):
            continue
        title = " ".join(str(web.get("title") or "").split())
        uri = str(web.get("uri") or "")
        # Titles are what she says out loud; a URL read aloud is unusable and
        # cannot be checked anyway. Deduped on title so three pages from one
        # site do not become three spoken citations.
        if not title or title.lower() in seen:
            continue
        seen.add(title.lower())
        sources.append({"title": title, "url": uri})
        if len(sources) >= SOURCES_MAX:
            break

    searched = [" ".join(str(q).split()) for q in (meta.get("webSearchQueries") or [])
                if str(q).strip()]
    return SearchResult(answer=answer, sources=sources, searched=searched[:3],
                        truncated=truncated)


async def search(query: str, *, api_key: str | None = None,
                 timeout: float = SEARCH_TIMEOUT_S) -> SearchResult:
    """One search. No follow-ups, no agentic loop.

    Each call costs money and time, so the model's instruction is not to search
    for something it knows and not to re-search to double-check itself.
    """
    q = " ".join((query or "").split())[:QUERY_MAX]
    if not q:
        raise SearchUnavailable("I need something to search for.")

    key = (api_key if api_key is not None else os.getenv("GEMINI_API_KEY", "")).strip()
    if not key:
        raise SearchUnavailable(
            "I can't search — GEMINI_API_KEY isn't set in backend/.env. "
            "Add it and restart the backend.")

    body = {
        "contents": [{"parts": [{"text": q}]}],
        "tools": [{"google_search": {}}],
        # She speaks the answer, so ask for one worth speaking. The cap in
        # _clip is the backstop, not the plan.
        "systemInstruction": {"parts": [{"text":
            "Answer in at most three short sentences, in plain prose. No lists, "
            "no markdown, no headings — this will be read aloud. If the sources "
            "disagree or you are unsure, say so."}]},
    }
    url = _URL.format(model=MODEL)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, params={"key": key}, json=body)
    except httpx.TimeoutException as exc:
        raise SearchUnavailable(
            f"The search took longer than {int(timeout)} seconds, so I gave up.") from exc
    except httpx.HTTPError as exc:
        raise SearchUnavailable("I couldn't reach the search service just now.") from exc

    if resp.status_code == 429:
        raise SearchUnavailable("I've hit the search quota for now.")
    if resp.status_code >= 400:
        # The upstream body can carry a key or a long trace; neither belongs in
        # a spoken reply or the event log.
        raise SearchUnavailable(
            f"The search service refused the request (HTTP {resp.status_code}).")
    try:
        return parse_grounded(resp.json())
    except ValueError as exc:
        raise SearchUnavailable("The search service sent something I couldn't read.") from exc
