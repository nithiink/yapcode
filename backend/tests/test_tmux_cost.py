"""Integration test for cost wiring in the CLI (tmux) runner.

Verifies TmuxClaudeRunner._update_cost reads a session transcript, sums the
API-equivalent cost, and caches by file size so a static transcript isn't
re-scanned. This is the path that fixes the "Claude $0.0000 always" bug for the
default (CLI) backend.

Requires the backend deps (claude_agent_sdk) — run with the project venv:
    backend/.venv/bin/python backend/tests/test_tmux_cost.py
Skips cleanly if the SDK isn't importable.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import tmux_runner
except ModuleNotFoundError as e:
    print(f"SKIP test_tmux_cost: {e} (run with the backend venv)")
    sys.exit(0)


def _write_transcript(path, messages):
    with open(path, "w", encoding="utf-8") as f:
        for m in messages:
            f.write(json.dumps(m) + "\n")


def _make_session(handle, transcript_path):
    s = tmux_runner._TmuxSession(handle, cwd="/tmp", model="opus")
    s.transcript_path = transcript_path  # _find_transcript returns this if it exists
    return s


def test_update_cost_sums_transcript():
    runner = tmux_runner.TmuxClaudeRunner()
    with tempfile.TemporaryDirectory() as d:
        tp = os.path.join(d, "sess.jsonl")
        _write_transcript(tp, [
            {"type": "user", "message": {"content": "hi"}},
            {"type": "assistant", "message": {"model": "claude-opus-4-8",
                                              "usage": {"output_tokens": 1_000_000}}},
        ])
        s = _make_session("h1", tp)
        runner._update_cost(s)
        assert abs(s.cost_usd - 25.0) < 1e-9, s.cost_usd


def test_update_cost_is_cached_until_file_grows():
    runner = tmux_runner.TmuxClaudeRunner()
    with tempfile.TemporaryDirectory() as d:
        tp = os.path.join(d, "sess.jsonl")
        _write_transcript(tp, [
            {"type": "assistant", "message": {"model": "claude-opus-4-8",
                                              "usage": {"output_tokens": 1_000_000}}},
        ])
        s = _make_session("h2", tp)
        runner._update_cost(s)
        first = s.cost_usd
        assert abs(first - 25.0) < 1e-9

        # Mutate cost_usd out-of-band; a same-size file must NOT trigger a re-scan.
        s.cost_usd = -1.0
        runner._update_cost(s)
        assert s.cost_usd == -1.0, "unchanged transcript should hit the size cache"

        # Append a new assistant turn → file grows → re-scan picks up both turns.
        with open(tp, "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant",
                                "message": {"model": "claude-opus-4-8",
                                            "usage": {"input_tokens": 1_000_000}}}) + "\n")
        runner._update_cost(s)
        assert abs(s.cost_usd - (25.0 + 5.0)) < 1e-9, s.cost_usd


def test_update_cost_no_transcript_is_safe():
    runner = tmux_runner.TmuxClaudeRunner()
    s = _make_session("h3", "/nonexistent/path.jsonl")
    runner._update_cost(s)
    assert s.cost_usd == 0.0


def test_list_reports_nonzero_cost():
    runner = tmux_runner.TmuxClaudeRunner()
    with tempfile.TemporaryDirectory() as d:
        tp = os.path.join(d, "sess.jsonl")
        _write_transcript(tp, [
            {"type": "assistant", "message": {"model": "claude-opus-4-8",
                                              "usage": {"output_tokens": 1_000_000}}},
        ])
        s = _make_session("h4", tp)
        runner._sessions[s.handle] = s
        listed = runner.list()
        assert listed and abs(listed[0]["cost_usd"] - 25.0) < 1e-9, listed


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
