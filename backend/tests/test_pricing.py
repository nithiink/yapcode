"""Tests for cost reconstruction from transcript token usage (backend/pricing.py).

Runs under pytest, or standalone: `python3 backend/tests/test_pricing.py`.
No third-party deps — pricing.py is pure stdlib.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pricing

# Per-MTok list rates the tests assert against (kept in sync with pricing._BASE_RATES).
OPUS_IN, OPUS_OUT = 5.0 / 1e6, 25.0 / 1e6
SONNET_IN, SONNET_OUT = 3.0 / 1e6, 15.0 / 1e6
HAIKU_IN, HAIKU_OUT = 1.0 / 1e6, 5.0 / 1e6


def _close(a, b, tol=1e-9):
    return abs(a - b) <= tol


def test_input_and_output_priced_per_model():
    assert _close(pricing.cost_for_usage("claude-opus-4-8", {"input_tokens": 1_000_000}), 5.0)
    assert _close(pricing.cost_for_usage("claude-opus-4-8", {"output_tokens": 1_000_000}), 25.0)
    assert _close(pricing.cost_for_usage("claude-sonnet-4-6", {"input_tokens": 1_000_000}), 3.0)
    assert _close(pricing.cost_for_usage("claude-haiku-4-5", {"output_tokens": 1_000_000}), 5.0)


def test_cache_read_is_tenth_of_input():
    cost = pricing.cost_for_usage("claude-opus-4-8", {"cache_read_input_tokens": 1_000_000})
    assert _close(cost, 5.0 * 0.1)


def test_cache_write_ttl_breakdown():
    # 5-minute writes bill at 1.25× input, 1-hour writes at 2× input.
    usage = {"cache_creation": {"ephemeral_5m_input_tokens": 1_000_000,
                                "ephemeral_1h_input_tokens": 1_000_000}}
    cost = pricing.cost_for_usage("claude-opus-4-8", usage)
    assert _close(cost, 5.0 * 1.25 + 5.0 * 2.0)


def test_cache_write_flat_total_defaults_to_5m_rate():
    # No per-TTL breakdown → treat the flat total as a 5-minute write (1.25×).
    cost = pricing.cost_for_usage("claude-opus-4-8", {"cache_creation_input_tokens": 1_000_000})
    assert _close(cost, 5.0 * 1.25)


def test_real_transcript_message():
    # A real assistant `usage` block captured from a CLI session.
    usage = {"input_tokens": 3239, "cache_creation_input_tokens": 4402,
             "cache_read_input_tokens": 16213, "output_tokens": 369,
             "cache_creation": {"ephemeral_1h_input_tokens": 4402,
                                "ephemeral_5m_input_tokens": 0}}
    expected = (3239 * OPUS_IN + 369 * OPUS_OUT
                + 16213 * OPUS_IN * 0.1 + 4402 * OPUS_IN * 2.0)
    assert _close(pricing.cost_for_usage("claude-opus-4-8", usage), expected)
    assert pricing.cost_for_usage("claude-opus-4-8", usage) > 0  # the bug was always-zero


def test_unknown_model_falls_back_to_opus_tier():
    # Never silently under-report: an unrecognized model bills at the top tier.
    assert _close(pricing.cost_for_usage("some-future-model", {"input_tokens": 1_000_000}), 5.0)


def test_missing_or_malformed_usage_is_zero():
    assert pricing.cost_for_usage("claude-opus-4-8", {}) == 0.0
    assert pricing.cost_for_usage("claude-opus-4-8", None) == 0.0
    assert pricing.cost_for_usage(None, {"input_tokens": 100}) > 0  # None model → default rate


def test_transcript_lines_sum_only_assistant_usage():
    lines = [
        json.dumps({"type": "user", "message": {"content": "hi"}}),
        json.dumps({"type": "assistant",
                    "message": {"model": "claude-opus-4-8",
                                "usage": {"output_tokens": 1_000_000}}}),
        json.dumps({"type": "assistant",
                    "message": {"model": "claude-sonnet-4-6",
                                "usage": {"input_tokens": 1_000_000}}}),
        "",                       # blank line
        "{not valid json",        # malformed
        json.dumps({"type": "assistant", "message": {"model": "claude-opus-4-8"}}),  # no usage
    ]
    # opus output 25.0 + sonnet input 3.0
    assert _close(pricing.cost_for_transcript_lines(lines), 25.0 + 3.0)


def test_env_override(monkeypatch=None):
    # VC_PRICING_JSON lets a new/negotiated rate be wired in without code change.
    os.environ["VC_PRICING_JSON"] = json.dumps({"opus": {"input": 99.0, "output": 1.0}})
    try:
        assert _close(pricing.cost_for_usage("claude-opus-4-8", {"input_tokens": 1_000_000}), 99.0)
    finally:
        del os.environ["VC_PRICING_JSON"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
