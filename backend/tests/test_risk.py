import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from yuri.domain.risk import risk_for  # noqa: E402


class Risk(unittest.TestCase):
    def test_safe(self):
        self.assertEqual(risk_for("Read", {"file_path": "x"}), "safe")
        self.assertEqual(risk_for("mcp__claude-in-chrome__navigate", {}), "safe")

    def test_edits_confirm(self):
        for t in ["Edit", "Write", "MultiEdit", "NotebookEdit"]:
            self.assertEqual(risk_for(t, {}), "confirm", t)

    def test_plain_bash_confirm(self):
        self.assertEqual(risk_for("Bash", {"command": "ls -la"}), "confirm")
        self.assertEqual(risk_for("Bash", {"command": "git status"}), "confirm")

    def test_destructive_bash_dangerous(self):
        for cmd in ["rm -rf build", "git push --force origin main", "git reset --hard HEAD~1",
                    "psql -c 'DROP TABLE users'", "mkfs.ext4 /dev/sda1", "echo hi > /dev/sda",
                    "chmod -R 777 /", "sudo rm -r /var", "rm --force important.txt"]:
            self.assertEqual(risk_for("Bash", {"command": cmd}), "dangerous", cmd)

    def test_rm_short_flags_match_their_long_forms(self):
        """`rm --force f` was dangerous while `rm -f f` was only confirm —
        under-flagging destruction, the one direction this must never fail."""
        for cmd in ["rm -f important.txt", "rm -F important.txt", "rm -r dir", "rm -R dir",
                    "rm -vf important.txt", "rm -Rf dir", "rm -fv a b", "cd /tmp && rm -f x"]:
            self.assertEqual(risk_for("Bash", {"command": cmd}), "dangerous", cmd)

    def test_rm_negatives_stay_non_dangerous(self):
        for cmd in ["grep -rf pattern", "git status", "ls -la", "rm important.txt",
                    "rm -i important.txt", "warm -f x", "npm -v"]:
            self.assertEqual(risk_for("Bash", {"command": cmd}), "confirm", cmd)

    def test_patterns_stay_linear_on_a_pathological_input(self):
        """The repo has CodeQL ReDoS history — no pattern may blow up on a long
        near-match."""
        import time
        cmd = "rm " + "-" + "a" * 20000
        t = time.monotonic()
        risk_for("Bash", {"command": cmd})
        self.assertLess(time.monotonic() - t, 1.0)

    def test_unknown_risky_tool_confirm(self):
        self.assertEqual(risk_for("mcp__something__else", {}), "confirm")

    def test_question_is_safe(self):
        self.assertEqual(risk_for("AskUserQuestion", {}), "safe")


if __name__ == "__main__":
    unittest.main()
