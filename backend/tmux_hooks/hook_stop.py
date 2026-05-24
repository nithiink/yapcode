#!/usr/bin/env python3
"""Stop hook: signal turn completion. The runner reads new assistant text from
the transcript when it sees this event."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import append_event, read_input  # noqa: E402


def main() -> None:
    data = read_input()
    append_event({
        "event": "turn_complete",
        "transcript_path": data.get("transcript_path", ""),
        "session_id": data.get("session_id", ""),
    })


if __name__ == "__main__":
    main()
