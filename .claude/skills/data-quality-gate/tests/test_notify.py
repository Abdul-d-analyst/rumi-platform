"""Tests for notify.py — sanitization (no PII/secrets/local paths ever
reach a Slack message) and the never-block guarantee. No live Slack
call is made; send_slack degrades to a reported "not sent" when no
channel/token is configured, which is exercised directly here."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import notify  # noqa: E402


class TestSanitize(unittest.TestCase):
    def test_windows_path_is_redacted(self):
        self.assertEqual(notify.sanitize(r"C:\Users\abdul\secret.txt"), "[local-path-redacted]")

    def test_unix_home_path_is_redacted(self):
        self.assertEqual(notify.sanitize("/home/abdul/secret.txt"), "[local-path-redacted]")

    def test_slack_token_is_redacted(self):
        self.assertIn("[credential-redacted]", notify.sanitize("token is xoxb-12345-abcde"))

    def test_github_token_is_redacted(self):
        self.assertIn("[credential-redacted]", notify.sanitize("token ghp_abc123def456"))

    def test_ordinary_string_is_unchanged(self):
        self.assertEqual(notify.sanitize("users.legacy_phone"), "users.legacy_phone")

    def test_sanitize_deep_handles_nested_structures(self):
        obj = {"path": r"C:\Users\abdul\x", "nested": {"list": ["ok", "xoxb-secret"]}}
        result = notify.sanitize_deep(obj)
        self.assertEqual(result["path"], "[local-path-redacted]")
        self.assertIn("[credential-redacted]", result["nested"]["list"][1])
        self.assertEqual(result["nested"]["list"][0], "ok")


class TestBuildMessage(unittest.TestCase):
    def test_unknown_event_raises(self):
        with self.assertRaises(ValueError):
            notify.build_message("not_a_real_event", {})

    def test_block_message_includes_declared_fields(self):
        text = notify.build_message("block", {"repo": "org/repo", "actor": "abdul"})
        self.assertIn("org/repo", text)
        self.assertIn("abdul", text)

    def test_empty_fields_are_omitted_not_rendered_as_blank(self):
        text = notify.build_message("block", {"repo": "org/repo", "pr": ""})
        self.assertNotIn("PR:", text)

    def test_message_sanitizes_fields_before_rendering(self):
        text = notify.build_message("block", {"repo": "org/repo", "rationale": r"see C:\Users\abdul\notes.txt"})
        self.assertNotIn("abdul", text)
        self.assertIn("[local-path-redacted]", text)


class TestSendSlackNoChannel(unittest.TestCase):
    def test_no_channel_configured_reports_not_sent_without_raising(self):
        result = notify.send_slack("block", None, {"repo": "org/repo"})
        self.assertFalse(result["sent"])
        self.assertIn("no channel configured", result["reason"])


if __name__ == "__main__":
    unittest.main()
