#!/usr/bin/env python3
"""Tests for model-default handling in the first-run env template.

Intended behavior: the wizard in ``bin/yapcode`` does NOT pre-populate model
knobs in the generated ``~/.config/yapcode/.env``. Model settings stay absent
so configs use the backend's ``os.getenv(..., <default>)`` fallbacks in
``backend/main.py`` unless a user explicitly adds the variable themselves.

These tests pin two properties:

  1. The generated template contains no model-selection knobs
     (``OPENAI_REALTIME_MODEL``, ``GEMINI_MODEL``, ``AZURE_OPENAI_DEPLOYMENTS``)
     — neither active nor commented.
  2. The backend still defines fallback defaults for those knobs, so an env
     file without them resolves to a working model.

Runnable standalone (``python3 tests/test_model_defaults.py``) or via pytest.
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
YAPCODE = os.path.join(ROOT, "bin", "yapcode")
MAIN_PY = os.path.join(ROOT, "backend", "main.py")

# Model-selection knobs that must NOT be pre-populated by the wizard.
MODEL_KNOBS = ("OPENAI_REALTIME_MODEL", "GEMINI_MODEL", "AZURE_OPENAI_DEPLOYMENTS")


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _backend_getenv_default(text, var):
    """The default in ``os.getenv("<var>", "<default>")`` from main.py, or None."""
    m = re.search(
        r'os\.getenv\(\s*["\']' + re.escape(var) + r'["\']\s*,\s*["\']([^"\']*)["\']\s*\)',
        text,
    )
    return m.group(1) if m else None


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
        ]
    )
    script = preamble + "\ncat <<EOF\n" + body + "\nEOF\n"
    out = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    )
    return out.stdout


def test_template_omits_model_knobs():
    """Model knobs are absent entirely — not active, not commented."""
    rendered = _render_template()
    for knob in MODEL_KNOBS:
        for line in rendered.splitlines():
            s = line.lstrip("# ").strip()
            if s.startswith(knob + "="):
                raise AssertionError(
                    f"{knob} should be absent from the template, found: {line!r}"
                )


def test_backend_provides_model_fallbacks():
    """With the knobs absent, the backend's os.getenv fallbacks must resolve
    to a non-empty model so a fresh config still works."""
    backend = _read(MAIN_PY)
    for var in ("OPENAI_REALTIME_MODEL", "GEMINI_MODEL"):
        default = _backend_getenv_default(backend, var)
        assert default, f"backend/main.py must define an os.getenv fallback for {var}"


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
