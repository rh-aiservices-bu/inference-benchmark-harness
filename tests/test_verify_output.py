"""Verification presentation preserves blocking checks and machine-readable evidence."""
import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from bench.cli import main, verification_coverage

CONFIG = Path(__file__).resolve().parents[1] / "examples/benchmark.json"


class Terminal(io.StringIO):
    def isatty(self):
        return True


class AsciiTerminal(Terminal):
    def write(self, text):
        text.encode("ascii")
        return super().write(text)


class VerifyOutputTests(unittest.TestCase):
    def invoke(self, checks, output=None, format="auto", env=None):
        output = output if output is not None else io.StringIO()
        with patch("sys.argv", ["bench", "verify", "--config", str(CONFIG), "--format", format]), \
                patch("bench.cli.verify", return_value=checks), \
                patch.dict("os.environ", env or {}, clear=True), contextlib.redirect_stdout(output):
            code = main()
        return code, output.getvalue()

    def test_terminal_labels_failures_and_does_not_offer_smoke(self):
        checks = [
            {"name": "runtime", "status": "pass", "detail": "Requires AIPerf 0.12.0"},
            {"name": "objective_status:interactive", "status": "unverified", "detail": "Status missing"},
            {"name": "model", "namespace": "inference", "status": "fail", "expected_replicas": 4, "observed_pods": 3},
            {"name": "engine", "status": "fail", "metric_names": ["other"], "missing": [{"metric": "queue_depth"}]},
        ]
        code, text = self.invoke(checks, Terminal())
        self.assertEqual(code, 2)
        for fragment in ("\x1b[32mPASS", "\x1b[33mWARN", "\x1b[31mFAIL", "inference/model",
                         "3 active pods; expected 4", "Missing: queue_depth", "BLOCKED"):
            self.assertIn(fragment, text)
        self.assertNotIn("READY FOR SMOKE", text)

    def test_redirected_default_preserves_json_checks_and_timestamps(self):
        checks = [{"name": "engine", "status": "pass", "metric_names": ["queue_depth"],
                   "observation": {"started": {"timestamp_ns": 123}}}]
        code, text = self.invoke(checks)
        result = json.loads(text)
        self.assertEqual(code, 0)
        self.assertEqual(result["checks"], checks)
        self.assertIn("timestamp_utc", result["reported"])
        self.assertEqual({c["name"] for c in result["coverage"]}, {"kubernetes", "routing", "server_metrics"})
        self.assertTrue(all(c["status"] == "unverified" for c in result["coverage"]))

    def test_text_and_no_color_still_show_warning_labels(self):
        for output, format, env in ((io.StringIO(), "text", {}), (Terminal(), "auto", {"NO_COLOR": "1"})):
            with self.subTest(format=format, env=env):
                code, text = self.invoke([{"name": "runtime", "status": "pass"}], output, format, env)
                self.assertEqual(code, 0)
                self.assertIn("WARN  server_metrics", text)
                self.assertIn("READY FOR SMOKE", text)
                self.assertNotIn("\x1b", text)

    def test_explicit_json_is_json_even_in_a_terminal(self):
        code, text = self.invoke([{"name": "model", "status": "fail"}], Terminal(), "json")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(text)["status"], "preflight_failed")

    def test_remote_control_characters_cannot_restyle_terminal(self):
        _, text = self.invoke([{"name": "engine\x1b[2J\u202e", "status": "fail", "detail": "Bad\rPASS\x1b[32m"}], format="text")
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\r", text)
        self.assertNotIn("\u202e", text)
        self.assertIn("FAIL", text)

    def test_optional_producer_stays_visible_beside_required_producer(self):
        gaps = verification_coverage({"metrics": [
            {"name": "engine", "required": [{"metric": "queue_depth"}]},
            {"name": "gateway", "required": []},
        ]})
        names = {gap["name"] for gap in gaps}
        self.assertIn("metric_requirements:gateway", names)
        self.assertNotIn("metric_requirements:engine", names)

    def test_display_failure_preserves_checks_and_exit_status(self):
        for status, expected in (("pass", 0), ("fail", 2)):
            checks = [{"name": "model", "status": status}]
            with self.subTest(status=status), patch("bench.cli.format_verification", side_effect=KeyError("shape")), \
                    contextlib.redirect_stderr(io.StringIO()) as errors:
                code, text = self.invoke(checks, Terminal())
            self.assertEqual(code, expected)
            self.assertEqual(json.loads(text)["checks"], checks)
            self.assertIn("showing JSON", errors.getvalue())

    def test_slow_read_is_visible_without_trailing_padding(self):
        _, text = self.invoke([{"name": "model", "status": "pass", "attempts": 3}], format="text")
        self.assertIn("3 read attempts", text)
        self.assertTrue(all(line == line.rstrip() for line in text.splitlines()))

    def test_empty_no_color_does_not_disable_terminal_color(self):
        _, text = self.invoke([{"name": "model", "status": "pass"}], Terminal(), env={"NO_COLOR": ""})
        self.assertIn("\x1b[32mPASS", text)

    def test_ascii_terminal_falls_back_to_readable_json(self):
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            code, text = self.invoke([{"name": "runtime", "status": "pass"}], AsciiTerminal())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["status"], "ready_for_smoke")
        self.assertIn("UnicodeEncodeError", errors.getvalue())
