"""Regression test: realtime mint payloads pin output_modalities to ["audio"].

Left unset, the model can emit a text-only message item inside a response (an
audio preamble item followed by a text answer item); it's transcribed but never
spoken, so the user hears only the first item. _mint_config must lock audio
output for both OpenAI-shaped providers — Azure especially, since it binds
config at mint time and ignores the client's later session.update.

    python -m unittest discover -s backend/tests
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main  # noqa: E402


class MintOutputModalities(unittest.TestCase):
    def _session(self, provider):
        _url, _headers, payload, _webrtc, _model = main._mint_config(
            provider, main.SessionRequest(provider=provider))
        return payload["session"]

    def test_openai_locks_audio_output(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            s = self._session("openai")
        self.assertEqual(s["output_modalities"], ["audio"])

    def test_azure_locks_audio_output(self):
        with mock.patch.multiple(
            main,
            AZURE_ENDPOINT="https://example.openai.azure.com",
            AZURE_DEPLOYMENT="gpt-realtime",
            AZURE_DEPLOYMENTS=["gpt-realtime"],
        ), mock.patch.dict(os.environ, {"AZURE_OPENAI_API_KEY": "az-test"}):
            s = self._session("azure")
        self.assertEqual(s["output_modalities"], ["audio"])


if __name__ == "__main__":
    unittest.main()
