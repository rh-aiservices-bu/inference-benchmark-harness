"""A mixed repeat is accepted only as a complete, overlapping group."""
import copy
import errno
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from bench.config import load
from bench.evidence import manifest, write_json
from bench.mixed import _execute, execute_group, shared_window_summary
from bench.processes import nonrunning_darwin_group
from bench.provenance import recording

ROOT = Path(__file__).resolve().parents[1]


class MixedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        config = load(ROOT / "examples/benchmark.json")
        config["load"].update(concurrency=[1], requests=3, repeats=1)
        self.streams = {name: copy.deepcopy(config) for name in ("interactive", "batch")}
        self.lock = (self.root / ".lock").open("w")
        self.addCleanup(self.lock.close)

    def exports(self, commands, streams, directories, lock_fd, starts=None):
        for index, (name, directory) in enumerate(directories.items()):
            native = directory / "native"
            native.mkdir()
            values = starts[name] if starts else [1_000_000_000 + index * 100_000_000,
                                                  2_000_000_000, 3_000_000_000]
            write_json(native / "profile_export_aiperf.json", {
                "aiperf_version": "0.12.0", "schema_version": "1.4", "request_count": {"avg": 3},
                "time_to_first_token": {"unit": "ms", "p95": 20}})
            rows = [{"metadata": {"request_start_ns": start, "request_end_ns": start + 10_000_000_000},
                     "metrics": {"request_latency": {"value": 10000, "unit": "ms"},
                                 "time_to_first_token": {"value": 20, "unit": "ms"}}} for start in values]
            (native / "profile_export.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        return {name: {"exit_code": 0} for name in streams}, None

    @patch("bench.mixed.verify", return_value=[])
    def test_valid_overlap_retains_streams_and_group_manifests(self, verify):
        with patch("bench.mixed._execute", side_effect=self.exports):
            result = execute_group(self.streams, self.root, "unused", self.lock.fileno(), 1)
        self.assertEqual(result["evidence"], "complete")
        self.assertEqual(result["overlap"]["seconds"], 1.9)
        self.assertEqual(result["overlap"]["start_skew_seconds"], .1)
        self.assertEqual(result["overlap"]["arrivals_in_common_window"], {"interactive": 2, "batch": 3})
        self.assertEqual(json.loads((self.root / "manifest.json").read_text()), manifest(self.root))
        self.assertTrue((self.root / "batch/config.json").exists())
        self.assertEqual(verify.call_count, 2)

    @patch("bench.mixed.verify", side_effect=[[{"status": "fail"}], []])
    def test_every_preflight_precedes_any_launch(self, verify):
        with patch("bench.mixed._execute", side_effect=AssertionError("traffic")):
            result = execute_group(self.streams, self.root, "unused", self.lock.fileno(), 1)
        self.assertEqual(verify.call_count, 2)
        self.assertIn("preflight_failed:interactive", result["reasons"])
        self.assertTrue((self.root / "batch/preflight.json").exists())

    @patch("bench.mixed.verify", return_value=[])
    def test_low_storage_prevents_every_launch(self, verify):
        from types import SimpleNamespace
        with patch("bench.mixed.shutil.disk_usage", return_value=SimpleNamespace(free=64 * 1024 * 1024 - 1)), \
                patch("bench.mixed._execute", side_effect=AssertionError("traffic")):
            result = execute_group(self.streams, self.root, "unused", self.lock.fileno(), 1)
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(result["evidence"], "invalid")
        for name in self.streams:
            checks = json.loads((self.root / name / "preflight.json").read_text())
            self.assertTrue(any(c.get("name") == "storage" and c["status"] == "fail" for c in checks))

    @patch("bench.mixed.verify", return_value=[])
    def test_long_responses_cannot_fake_arrival_overlap(self, verify):
        starts = {"interactive": [1_000_000_000, 2_000_000_000, 3_000_000_000],
                  "batch": [5_000_000_000, 6_000_000_000, 7_000_000_000]}
        def execute(*args):
            return self.exports(*args, starts=starts)
        with patch("bench.mixed._execute", side_effect=execute):
            result = execute_group(self.streams, self.root, "unused", self.lock.fileno(), .1)
        self.assertEqual(result["evidence"], "invalid")
        self.assertIn("insufficient_arrival_overlap", result["reasons"])
        self.assertEqual(result["outcome"], "invalid")
        self.assertIn("min_overlap_seconds", result["action"])
        self.assertTrue(all(s["evidence"] == "complete" for s in result["streams"].values()))

    @patch("bench.mixed.verify", return_value=[])
    def test_empty_common_arrival_cohort_is_invalid(self, verify):
        starts = {"interactive": [1_000_000_000, 1_500_000_000, 10_000_000_000],
                  "batch": [3_000_000_000, 4_000_000_000, 6_000_000_000]}
        with patch("bench.mixed._execute", side_effect=lambda *args: self.exports(*args, starts=starts)):
            result = execute_group(self.streams, self.root, "unused", self.lock.fileno(), 1)
        self.assertEqual(result["overlap"]["seconds"], 3)
        self.assertEqual(result["evidence"], "invalid")

    @patch("bench.mixed.verify", return_value=[])
    def test_singleton_does_not_require_overlap_and_goals_are_preserved(self, verify):
        self.streams = {"interactive": self.streams["interactive"]}
        self.streams["interactive"]["goals"] = {"ttft_p95_ms": 10}
        with patch("bench.mixed._execute", side_effect=self.exports):
            result = execute_group(self.streams, self.root, "unused", self.lock.fileno(), 100)
        self.assertEqual(result["evidence"], "complete")
        self.assertEqual(result["outcome"], "goal_not_met")

    @patch("bench.mixed.verify", return_value=[{"name": "engine", "namespace": "test", "status": "pass", "identity": [{"uid": "new"}]}])
    def test_baseline_drift_prevents_every_launch(self, verify):
        self.streams["interactive"]["kubernetes"] = {"context": "fixture", "deployments": []}
        baseline = {"interactive": [{"name": "engine", "namespace": "test", "status": "pass", "identity": [{"uid": "old"}]}]}
        with patch("bench.mixed._execute", side_effect=AssertionError("traffic")):
            result = execute_group(self.streams, self.root, "unused", self.lock.fileno(), 1, baseline_checks=baseline)
        self.assertIn("preflight_failed:interactive", result["reasons"])
        self.assertEqual(verify.call_count, 2)

    @patch("bench.mixed.verify", return_value=[])
    def test_auth_and_dataset_are_preserved_without_secret(self, verify):
        config = self.streams["interactive"]
        data = self.root / "prompts.jsonl"
        data.write_text('{"text":"test"}\n')
        from bench.config import file_digest
        config["workload"] = {"type": "single_turn", "path": str(data), "sha256": file_digest(data), "output_tokens": 16}
        config["endpoint"]["api_key_env"] = "MIXED_TEST_KEY"
        with recording(self.root), patch.dict(os.environ, {"MIXED_TEST_KEY": "not-saved-secret"}), patch("bench.mixed._execute", side_effect=self.exports):
            execute_group(self.streams, self.root, "unused", self.lock.fileno(), 1)
        child = self.root / "interactive"
        self.assertEqual((child / "input.jsonl").read_bytes(), data.read_bytes())
        auth = (child / "auth.yaml").read_text()
        self.assertIn("${MIXED_TEST_KEY}", auth)
        self.assertNotIn("not-saved-secret", auth)
        self.assertEqual((child / "auth.yaml").stat().st_mode & 0o777, 0o600)
        ledger = [json.loads(line) for line in (self.root / "provenance.jsonl").read_text().splitlines()]
        dataset = next(row for row in ledger if row.get("artifact") == "interactive/input.jsonl")
        self.assertEqual(dataset["sha256"], config["workload"]["sha256"])
        self.assertLessEqual(dataset["acquisition"]["started"]["timestamp_ns"],
                             dataset["acquisition"]["finished"]["timestamp_ns"])
        native = [row for row in ledger if row["operation"] == "native_artifact_observed"]
        self.assertEqual(len(native), 4)
        self.assertTrue(all(row["bytes"] > 0 and len(row["sha256"]) == 64 for row in native))

    def test_shared_cohort_retains_tail_completions_and_nearest_rank(self):
        directory = self.root / "interactive"
        native = directory / "native"
        native.mkdir(parents=True)
        rows = []
        for start, latency, ttft in ((1, 1000, 900), (2, 10, 3), (3, 100, 7), (4, 2000, 999)):
            rows.append({"metadata": {"request_start_ns": start, "request_end_ns": 1000},
                         "metrics": {"request_latency": {"unit": "ms", "value": latency},
                                     "time_to_first_token": {"unit": "ms", "value": ttft}}})
        rows.append({"metadata": {"request_start_ns": 2, "request_end_ns": 1000}, "error": {"code": 503}})
        (native / "profile_export.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
        summary = shared_window_summary(directory, self.streams["interactive"], {"start_ns": 2, "end_ns": 3})
        self.assertEqual(summary["requests"], 3)
        self.assertEqual(summary["failed_requests"], 1)
        self.assertEqual(summary["measurements"], {"error_fraction": 1 / 3, "latency_p95_ms": 100, "ttft_p95_ms": 7})
        self.assertEqual(summary["sample_counts"]["ttft_p95_ms"]["valid"], 2)

    @patch("bench.mixed.verify", return_value=[])
    def test_missing_cohort_ttft_cannot_satisfy_goal_from_native_p95(self, verify):
        for config in self.streams.values():
            config["goals"] = {"ttft_p95_ms": 100}
        def missing(*args):
            executions = self.exports(*args)
            for directory in args[2].values():
                path = directory / "native/profile_export.jsonl"
                rows = [json.loads(line) for line in path.read_text().splitlines()]
                for row in rows:
                    del row["metrics"]["time_to_first_token"]
                path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            return executions
        with patch("bench.mixed._execute", side_effect=missing):
            result = execute_group(self.streams, self.root, "unused", self.lock.fileno(), 1)
        self.assertEqual(result["evidence"], "invalid")
        self.assertEqual(result["streams"]["interactive"]["goals"][0]["status"], "unverified")
        self.assertEqual(result["shared_window"]["interactive"]["goals"][0]["status"], "unverified")
        self.assertEqual(result["outcome"], "invalid")

    def process_setup(self):
        directories = {name: self.root / name for name in self.streams}
        for path in directories.values():
            path.mkdir()
        return directories

    def assert_gone(self, pid):
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_real_failure_cancels_and_reaps_peer(self):
        directories = self.process_setup()
        pidfile = self.root / "peer.pid"
        commands = {"interactive": [sys.executable, "-c", "import time;time.sleep(.3);raise SystemExit(7)"],
                    "batch": [sys.executable, "-c",
                              f"import os,time;from pathlib import Path;Path({str(pidfile)!r}).write_text(str(os.getpid()));time.sleep(30)"]}
        start = time.monotonic()
        execution, reason = _execute(commands, self.streams, directories, self.lock.fileno())
        self.assertLess(time.monotonic() - start, 3)
        self.assertEqual(reason, "client_process_failed:interactive")
        self.assertEqual(execution["batch"]["reason"], "group_cancelled")
        self.assert_gone(int(pidfile.read_text()))

    def test_real_deadline_cancels_and_reaps_all(self):
        directories = self.process_setup()
        commands = {}
        for name, config in self.streams.items():
            config["load"]["deadline_seconds"] = .2 if name == "interactive" else 20
            pidfile = self.root / f"{name}.pid"
            commands[name] = [sys.executable, "-c",
                              f"import os,time;from pathlib import Path;Path({str(pidfile)!r}).write_text(str(os.getpid()));time.sleep(30)"]
        start = time.monotonic()
        execution, reason = _execute(commands, self.streams, directories, self.lock.fileno())
        self.assertLess(time.monotonic() - start, 3)
        self.assertEqual(reason, "deadline_exceeded:interactive")
        self.assertEqual(execution["interactive"]["exit_code"], 124)
        for name in commands:
            self.assert_gone(int((self.root / f"{name}.pid").read_text()))

    def test_launch_failure_cancels_already_started_peer(self):
        directories = self.process_setup()
        commands = {"interactive": [sys.executable, "-c", "import time;time.sleep(30)"],
                    "batch": ["/nonexistent-mixed-client"]}
        execution, reason = _execute(commands, self.streams, directories, self.lock.fileno())
        self.assertEqual(reason, "launch_failed:batch")
        self.assertEqual(execution["batch"]["exit_code"], 127)
        self.assertIsNotNone(execution["interactive"]["exit_code"])

    def test_interrupt_cancels_every_real_child(self):
        directories = self.process_setup()
        commands = {name: [sys.executable, "-c", "import time;time.sleep(30)"] for name in self.streams}
        original_sleep = time.sleep
        interrupted = False
        def interrupt_once(seconds):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            return original_sleep(seconds)
        with patch("bench.mixed.time.sleep", side_effect=interrupt_once):
            execution, reason = _execute(commands, self.streams, directories, self.lock.fileno())
        self.assertEqual(reason, "interrupted")
        for result in execution.values():
            self.assert_gone(result["process_id"])

    def test_successful_leader_cannot_leave_ignoring_helper(self):
        directories = self.process_setup()
        ready = self.root / "helper.json"
        helper = ("import os,signal,socket,time,json;from pathlib import Path;"
                  "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                  "s=socket.socket();s.bind(('127.0.0.1',0));s.listen();"
                  f"Path({str(ready)!r}).write_text(json.dumps([os.getpid(),s.getsockname()[1]]));time.sleep(30)")
        parent = ("import subprocess,sys,time;from pathlib import Path;"
                  f"subprocess.Popen([sys.executable,'-c',{helper!r}]);p=Path({str(ready)!r});\n"
                  "while not p.exists():time.sleep(.01)")
        commands = {"interactive": [sys.executable, "-c", parent]}
        try:
            execution, reason = _execute(commands, self.streams, directories, self.lock.fileno())
            self.assertIsNone(reason)
            self.assertEqual(execution["interactive"]["exit_code"], 0)
            pid, port = json.loads(ready.read_text())
            until = time.monotonic() + 1
            while True:
                try:
                    connection = socket.create_connection(("127.0.0.1", port), timeout=.1)
                except ConnectionRefusedError:
                    break
                except ConnectionResetError:
                    pass  # The killed listener may reset a queued connection; require refusal next.
                else:
                    connection.close()
                if time.monotonic() > until:
                    self.fail("Owned helper still serves after group cleanup")
                time.sleep(.01)
        finally:
            if ready.exists():
                try:
                    os.kill(json.loads(ready.read_text())[0], signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_darwin_group_probe_requires_no_live_members(self):
        with patch("bench.processes.sys.platform", "darwin"):
            for output, expected in (("123 Z\n123 Z+\n", True), ("124 S\n", True),
                                     ("123 Z\n123 S\n", False)):
                with self.subTest(output=output), patch("bench.processes.subprocess.run", return_value=subprocess.CompletedProcess([], 0, output)):
                    self.assertEqual(nonrunning_darwin_group(123), expected)
            with patch("bench.processes.subprocess.run", side_effect=subprocess.TimeoutExpired("ps", 2)):
                self.assertFalse(nonrunning_darwin_group(123))

    def test_permission_denied_cleanup_remains_fatal_for_live_group(self):
        directories = self.process_setup()
        process = unittest.mock.Mock(pid=123, returncode=0)
        process.poll.return_value = 0
        with patch("bench.mixed.subprocess.Popen", return_value=process), \
             patch("bench.mixed.os.killpg", side_effect=PermissionError(errno.EPERM, "denied")), \
             patch("bench.mixed.nonrunning_darwin_group", return_value=False):
            execution, reason = _execute({"interactive": ["unused"]}, self.streams, directories, self.lock.fileno())
        self.assertEqual(reason, "cleanup_failed:interactive")
        self.assertEqual(execution["interactive"]["exit_code"], 125)
        self.assertEqual(execution["interactive"]["cleanup_observations"][0]["errno"], errno.EPERM)

    def test_permission_denied_cleanup_of_nonrunning_group_is_audited(self):
        directories = self.process_setup()
        process = unittest.mock.Mock(pid=123, returncode=0)
        process.poll.return_value = 0
        with patch("bench.mixed.subprocess.Popen", return_value=process), \
             patch("bench.mixed.os.killpg", side_effect=PermissionError(errno.EPERM, "denied")), \
             patch("bench.mixed.nonrunning_darwin_group", return_value=True):
            execution, reason = _execute({"interactive": ["unused"]}, self.streams, directories, self.lock.fileno())
        self.assertIsNone(reason)
        self.assertEqual(execution["interactive"]["exit_code"], 0)
        self.assertEqual(execution["interactive"]["cleanup_observations"][0]["status"], "no_live_group_members")
