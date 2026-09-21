"""Verification presentation preserves blocking checks and machine-readable evidence."""
import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from bench.cli import main

CONFIG = Path(__file__).resolve().parents[1] / "examples/benchmark.json"


class Terminal(io.StringIO):
    def isatty(self):
        return True


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
        _, text = self.invoke([{"name": "engine\x1b[2J", "status": "fail", "detail": "Bad\rPASS\x1b[32m"}], format="text")
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\r", text)
        self.assertIn("FAIL", text)
