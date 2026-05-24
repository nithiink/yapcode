#!/usr/bin/env python3
"""Notification hook: backstop signal for idle/permission/elicitation events."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import append_event, read_input  # noqa: E402


def main() -> None:
    data = read_input()
    append_event({
        "event": "notification",
        "notification_type": data.get("notification_type", ""),
        "message": data.get("message", ""),
        "transcript_path": data.get("transcript_path", ""),
        "session_id": data.get("session_id", ""),
    })


if __name__ == "__main__":
    main()
