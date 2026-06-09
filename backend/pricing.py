"""Token-usage → USD cost estimation for Claude models.

The interactive CLI backend runs on a Max subscription, so the session JSONL
never reports a dollar figure (`total_cost_usd` is SDK-only). But each assistant
message carries a full `usage` block, so we reconstruct the *API-equivalent*
cost the same usage would bill at list rates — that's the UI's "Claude $…"
readout for CLI sessions.

Rates are list prices per 1M tokens. Override the table via `VC_PRICING_JSON`
(a JSON object mapping a model-id substring to `{"input": x, "output": y}`).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

log = logging.getLogger("yapcode.pricing")

# Per-million-token list prices (USD). Keyed by a substring matched against the
# transcript's model id (e.g. "claude-opus-4-8"). Order matters: the first key
# found as a substring wins, so list more specific keys before generic ones.
_BASE_RATES: list[tuple[str, dict[str, float]]] = [
    ("opus",   {"input": 5.0,  "output": 25.0}),
    ("sonnet", {"input": 3.0,  "output": 15.0}),
    ("haiku",  {"input": 1.0,  "output": 5.0}),
]

# Cache multipliers relative to the input rate (documented, model-independent).
_CACHE_READ_MULT = 0.1
_CACHE_WRITE_5M_MULT = 1.25
_CACHE_WRITE_1H_MULT = 2.0

# Fallback when the model can't be classified — assume the most expensive tier
# so we never silently under-report. Opus is the CLI default model.
_DEFAULT_RATE = {"input": 5.0, "output": 25.0}


def _load_overrides() -> dict[str, dict[str, float]]:
    raw = os.getenv("VC_PRICING_JSON")
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except Exception:
        log.warning("ignoring malformed VC_PRICING_JSON")
    return {}


def _rate_for(model: str | None) -> dict[str, float]:
    """Per-MTok {input, output} rate for a model id, or the Opus-tier default."""
    m = (model or "").lower()
    for key, rate in _load_overrides().items():
        if key.lower() in m and "input" in rate and "output" in rate:
            return rate
    for key, rate in _BASE_RATES:
        if key in m:
            return rate
    return _DEFAULT_RATE


def cost_for_usage(model: str | None, usage: dict[str, Any]) -> float:
    """USD cost for one assistant message's `usage` block.

    Recognized token fields (all optional, default 0):
      input_tokens                 — uncached input, billed at full input rate
      output_tokens                — billed at the output rate
      cache_read_input_tokens      — billed at 0.1× input
      cache_creation_input_tokens  — cache writes; split by TTL when the
                                     `cache_creation` breakdown is present, else
                                     billed at the 5-minute write rate
    """
    if not isinstance(usage, dict):
        return 0.0
    rate = _rate_for(model)
    in_rate = rate["input"] / 1_000_000
    out_rate = rate["output"] / 1_000_000

    def _int(v: Any) -> int:
        return v if isinstance(v, int) else 0

    cost = _int(usage.get("input_tokens")) * in_rate
    cost += _int(usage.get("output_tokens")) * out_rate
    cost += _int(usage.get("cache_read_input_tokens")) * in_rate * _CACHE_READ_MULT

    # Cache writes: prefer the per-TTL breakdown (1-hour writes cost 2× vs the
    # 1.25× of 5-minute writes); fall back to the flat total as a 5-minute write.
    breakdown = usage.get("cache_creation")
    if isinstance(breakdown, dict):
        w5 = _int(breakdown.get("ephemeral_5m_input_tokens"))
        w1 = _int(breakdown.get("ephemeral_1h_input_tokens"))
        cost += w5 * in_rate * _CACHE_WRITE_5M_MULT
        cost += w1 * in_rate * _CACHE_WRITE_1H_MULT
    else:
        cost += _int(usage.get("cache_creation_input_tokens")) * in_rate * _CACHE_WRITE_5M_MULT

    return cost


def cost_for_transcript_lines(lines: list[str]) -> float:
    """Sum API-equivalent cost across assistant messages in JSONL lines.

    Each assistant line is `{"type": "assistant", "message": {"model", "usage"}}`.
    Non-assistant lines, malformed JSON, and lines without usage are skipped.
    """
    total = 0.0
    for raw in lines:
        if not raw.strip():
            continue
        try:
            o = json.loads(raw)
        except Exception:
            continue
        if o.get("type") != "assistant":
            continue
        msg = o.get("message") or {}
        usage = msg.get("usage")
        if isinstance(usage, dict):
            total += cost_for_usage(msg.get("model"), usage)
    return total
