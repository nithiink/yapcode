#!/usr/bin/env python3
"""Tests for model-default population in the first-run env template.

Scope: the wizard in ``bin/yapcode`` writes ``~/.config/yapcode/.env`` and calls
it the single source of truth. These tests pin two properties:

  1. The model-default knobs (OpenAI realtime model, Gemini live model, the
     Azure multi-deployment list) are present in the generated template so
     they're discoverable/editable from that file.
  2. The default values documented in the template do not drift from the
     ``os.getenv(..., <default>)`` fallbacks the backend actually uses in
     ``backend/main.py``.

Runnable standalone (``python3 tests/test_model_defaults.py``) or via pytest.
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
YAPCODE = os.path.join(ROOT, "bin", "yapcode")
MAIN_PY = os.path.join(ROOT, "backend", "main.py")


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _backend_getenv_default(text, var):
    """The default in ``os.getenv("<var>", "<default>")`` from main.py."""
    m = re.search(
        r'os\.getenv\(\s*["\']' + re.escape(var) + r'["\']\s*,\s*["\']([^"\']*)["\']\s*\)',
        text,
    )
    assert m, f"could not find os.getenv default for {var} in backend/main.py"
    return m.group(1)


def _wizard_local_default(text, name):
    """A ``local NAME="value"`` assignment from bin/yapcode."""
    m = re.search(r'local\s+' + re.escape(name) + r'="([^"]*)"', text)
    assert m, f"could not find `local {name}=` in bin/yapcode"
    return m.group(1)


def _render_template():
    """Expand the wizard's env heredoc with stub inputs, return the text.

    Extracts the exact ``cat <<EOF ... EOF`` body and lets bash perform the
    real variable expansion, so the test exercises the same substitution the
    wizard does at runtime.
    """
    text = _read(YAPCODE)
    start = text.index('cat > "$ENV_FILE" <<EOF\n') + len('cat > "$ENV_FILE" <<EOF\n')
    end = text.index("\nEOF", start)
    body = text[start:end]
    preamble = "\n".join(
        [
            'provider=openai',
            'az_endpoint=""',
            'az_key=""',
            'az_deploy=""',
            'openai_key="sk-test"',
            'gemini_key=""',
            'roots="$HOME/projects"',
            'token="tok-test"',
            'DEFAULT_OPENAI_REALTIME_MODEL="gpt-realtime-mini"',
            'DEFAULT_GEMINI_MODEL="gemini-2.5-flash-native-audio-preview-12-2025"',
        ]
    )
    script = preamble + "\ncat <<EOF\n" + body + "\nEOF\n"
    out = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    )
    return out.stdout


def test_no_drift_between_template_and_backend_defaults():
    wiz = _read(YAPCODE)
    backend = _read(MAIN_PY)
    assert _wizard_local_default(wiz, "DEFAULT_OPENAI_REALTIME_MODEL") == \
        _backend_getenv_default(backend, "OPENAI_REALTIME_MODEL")
    assert _wizard_local_default(wiz, "DEFAULT_GEMINI_MODEL") == \
        _backend_getenv_default(backend, "GEMINI_MODEL")


def test_template_populates_model_defaults():
    rendered = _render_template()
    # OpenAI-native realtime model knob, documented at the backend default.
    assert "# OPENAI_REALTIME_MODEL=gpt-realtime-mini" in rendered
    # Gemini live model knob, documented at the backend default.
    assert "# GEMINI_MODEL=gemini-2.5-flash-native-audio-preview-12-2025" in rendered
    # Azure: deployment name is the model selector; multi-deployment list is
    # documented too.
    assert "AZURE_OPENAI_DEPLOYMENT=" in rendered
    assert "# AZURE_OPENAI_DEPLOYMENTS=" in rendered


def test_model_knobs_are_commented_to_track_backend_default():
    """Model defaults are commented so a backend bump propagates to existing
    configs that left them untouched (no pinning of version-stamped names)."""
    rendered = _render_template()
    for line in rendered.splitlines():
        s = line.strip()
        if s.startswith("OPENAI_REALTIME_MODEL=") or s.startswith("GEMINI_MODEL="):
            raise AssertionError(f"model knob should be commented, got active: {line!r}")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
