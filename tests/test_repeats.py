"""Repeat scheduling must preserve measurements and isolate replacement budgets."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bench.config import load
from bench.evidence import write_json
from bench.runner import campaign

ROOT = Path(__file__).resolve().parents[1]

class RepeatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "run"
        self.config = load(ROOT / "examples/benchmark.json")
        self.config["load"].update(concurrency=[1, 2], repeats=3, requests=1, max_attempts_per_repeat=2)
        self.calls = []

    def client(self, args, directory, *unused):
        if self.calls:
            self.assertTrue((self.calls[-1] / "summary.json").exists(), "Next repeat began before evidence validation")
        self.calls.append(directory)
        native = directory / "native"
        native.mkdir()
        write_json(native / "profile_export_aiperf.json", {
            "aiperf_version": "0.12.0", "schema_version": "1.4", "request_count": {"avg": 1},
            "time_to_first_token": {"unit": "ms", "p95": 20}})
        row = {"metadata": {"request_start_ns": 100, "request_end_ns": 200},
               "metrics": {"request_latency": {"value": 100, "unit": "ms"},
                           "time_to_first_token": {"value": 20, "unit": "ms"}}}
        (native / "profile_export.jsonl").write_text(json.dumps(row) + "\n")
        return {"exit_code": 0}

    @patch("bench.runner.verify", return_value=[])
    def test_three_valid_repeats_per_point_and_no_replay_on_resume(self, verify):
        with patch("bench.runner.run_child", side_effect=self.client):
            result = campaign(self.config, self.root, "unused")
        self.assertEqual([p.name for p in self.calls], [f"point-{point:02d}-repeat-{repeat:02d}-attempt-001" for point in (1, 2) for repeat in (1, 2, 3)])
        self.assertEqual(result["status"], "complete")
        for point in (1, 2):
            report = json.loads((self.root / f"point-{point:02d}-repeats.json").read_text())
            self.assertEqual(report["valid_repeats"], 3)
            self.assertEqual(len(report["measurements"]), 3)
        with patch("bench.runner.run_child", side_effect=AssertionError("completed repeat replayed")):
            campaign(self.config, self.root, "unused", resume=True)

    @patch("bench.runner.verify", return_value=[])
    def test_invalid_second_repeat_stops_then_replaces_only_that_repeat(self, verify):
        def invalid_second(*args):
            result = self.client(*args)
            if len(self.calls) == 2:
                (args[1] / "native/profile_export.jsonl").write_text("")
            return result
        with patch("bench.runner.run_child", side_effect=invalid_second):
            failed = campaign(self.config, self.root, "unused")
        self.assertEqual(failed["status"], "evidence_invalid")
        self.assertEqual(failed["next_repeat"], 1)
        self.assertEqual(len(self.calls), 2)
        failed_path = self.calls[-1]
        before = (failed_path / "summary.json").read_bytes()
        with patch("bench.runner.run_child", side_effect=self.client):
            done = campaign(self.config, self.root, "unused", resume=True)
        self.assertEqual(done["status"], "complete")
        self.assertEqual(len(done["completed"]), 6)
        self.assertEqual(self.calls[2].name, "point-01-repeat-02-attempt-002")
        self.assertEqual((failed_path / "summary.json").read_bytes(), before)

    @patch("bench.runner.verify", return_value=[])
    def test_goal_miss_keeps_three_repeats_and_stops_before_higher_load(self, verify):
        self.config["goals"] = {"ttft_p95_ms": 10}
        with patch("bench.runner.run_child", side_effect=self.client):
            state = campaign(self.config, self.root, "unused")
        self.assertEqual(state["status"], "goal_not_met")
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(state["next_point"], 1)
        self.assertEqual(state["next_repeat"], 0)
        report = json.loads((self.root / "point-01-repeats.json").read_text())
        self.assertEqual(report["outcome"], "goal_not_met")
        self.assertTrue(all(r["goals"][0]["status"] == "missed" for r in report["measurements"]))

    def retained_failure_client(self, outcome):
        def execute(*args):
            result = self.client(*args)
            native = args[1] / "native"
            summary = json.loads((native / "profile_export_aiperf.json").read_text())
            first_point = args[1].name.startswith("point-01-")
            if outcome == "request_errors" and first_point:
                summary.update(request_count={"avg": 0}, error_request_count={"avg": 1})
                summary.pop("time_to_first_token")
                row = {"error": {"code": 503},
                       "metadata": {"request_start_ns": 100, "request_end_ns": 200}}
                (native / "profile_export.jsonl").write_text(json.dumps(row) + "\n")
            elif not first_point:
                summary["time_to_first_token"]["p95"] = 5
                records = native / "profile_export.jsonl"
                row = json.loads(records.read_text())
                row["metrics"]["time_to_first_token"]["value"] = 5
                records.write_text(json.dumps(row) + "\n")
            write_json(native / "profile_export_aiperf.json", summary)
            return result
        return execute

    def assert_unsuccessful_report(self, root, outcome):
        import subprocess, sys
        result = subprocess.run([sys.executable, "-m", "bench", "report", "--run", str(root)],
                                capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["status"], outcome)

    @patch("bench.runner.verify", return_value=[])
    def test_resume_after_final_failure_preserves_outcome_without_traffic(self, verify):
        self.config["load"]["concurrency"] = [1]
        for outcome in ("goal_not_met", "request_errors"):
            with self.subTest(outcome=outcome):
                self.calls = []
                root = self.root / outcome
                self.config["goals"] = {"ttft_p95_ms": 10} if outcome == "goal_not_met" else {}
                with patch("bench.runner.run_child", side_effect=self.retained_failure_client(outcome)):
                    state = campaign(self.config, root, "unused")
                self.assertEqual(state["status"], outcome)
                self.assertEqual(len(self.calls), 3)
                with patch("bench.runner.run_child", side_effect=AssertionError("completed point replayed")):
                    state = campaign(self.config, root, "unused", resume=True)
                self.assertEqual(state["status"], outcome)
                self.assertEqual(state["next_point"], 1)
                self.assert_unsuccessful_report(root, outcome)

    @patch("bench.runner.verify", return_value=[])
    def test_later_success_does_not_erase_retained_failure(self, verify):
        for outcome in ("goal_not_met", "request_errors"):
            with self.subTest(outcome=outcome):
                self.calls = []
                root = self.root / outcome
                self.config["goals"] = {"ttft_p95_ms": 10} if outcome == "goal_not_met" else {}
                with patch("bench.runner.run_child", side_effect=self.retained_failure_client(outcome)):
                    stopped = campaign(self.config, root, "unused")
                    self.assertEqual(stopped["status"], outcome)
                    self.assertEqual(len(self.calls), 3)
                    state = campaign(self.config, root, "unused", resume=True)
                self.assertEqual(state["status"], outcome)
                self.assertEqual(state["next_point"], 2)
                self.assertEqual(state["point_results"], {"1": outcome, "2": "ready"})
                self.assertEqual(len(self.calls), 6)
                self.assertEqual(len(state["completed"]), 6)
                self.assert_unsuccessful_report(root, outcome)

    @patch("bench.runner.verify", return_value=[{"status": "fail"}])
    def test_repeat_attempt_budget_blocks_additional_inference(self, verify):
        with patch("bench.runner.run_child", side_effect=AssertionError("traffic")):
            campaign(self.config, self.root, "unused")
            campaign(self.config, self.root, "unused", resume=True)
            state = campaign(self.config, self.root, "unused", resume=True)
        self.assertEqual(state["status"], "attempt_budget_exhausted")
        self.assertEqual(len(list(self.root.glob("point-*-attempt-*"))), 2)

    def test_bad_repeat_count_rejected_and_smoke_remains_one(self):
        path = self.root.parent / "config.json"
        for count in (0, -1, True, 1.5):
            config = copy.deepcopy(self.config)
            config["load"]["repeats"] = count
            write_json(path, config)
            with self.assertRaises(ValueError):
                load(path)
        write_json(path, self.config)
        smoke = load(path, smoke=True)
        self.assertEqual(smoke["load"]["repeats"], 1)
        self.assertEqual(smoke["load"]["requests"], 1)

    def test_rollout_between_repeats_stops_before_more_traffic(self):
        self.config["kubernetes"] = {"context": "fixture", "deployments": []}
        def identity(uid):
            return [{"name": "model", "status": "pass", "identity": [{"uid": uid, "containers": [{"imageID": uid}]}]}]
        with patch("bench.runner.verify", side_effect=[identity("old"), identity("new")]), \
             patch("bench.kubernetes.inspect", return_value=identity("old")), \
             patch("bench.runner.run_child", side_effect=self.client):
            state = campaign(self.config, self.root, "unused")
        self.assertEqual(state["status"], "preflight_failed")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(state["next_repeat"], 1)
        checks = json.loads((self.root / "point-01-repeat-02-attempt-001/preflight.json").read_text())
        self.assertTrue(any(c["name"] == "campaign_identity" and c["status"] == "fail" for c in checks))

    def test_interrupted_first_preflight_has_resumable_checkpoint(self):
        with patch("bench.runner.verify", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                campaign(self.config, self.root, "unused")
        state = json.loads((self.root / "state.json").read_text())
        self.assertEqual(state["status"], "checking")
        self.assertEqual(state["attempts"], 1)
        with patch("bench.runner.verify", return_value=[]), patch("bench.runner.run_child", side_effect=self.client):
            result = campaign(self.config, self.root, "unused", resume=True)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.calls[0].name, "point-01-repeat-01-attempt-002")

    @patch("bench.runner.verify", return_value=[])
    def test_repeat_report_preserves_spread_without_goals(self, verify):
        def varying(*args):
            result = self.client(*args)
            path = args[1] / "native/profile_export_aiperf.json"
            native = json.loads(path.read_text())
            native["time_to_first_token"]["p95"] = len(self.calls) * 10
            records = args[1] / "native/profile_export.jsonl"
            row = json.loads(records.read_text())
            row["metrics"]["time_to_first_token"]["value"] = len(self.calls) * 10
            records.write_text(json.dumps(row) + "\n")
            write_json(path, native)
            return result
        with patch("bench.runner.run_child", side_effect=varying):
            campaign(self.config, self.root, "unused")
        report = json.loads((self.root / "point-01-repeats.json").read_text())
        self.assertEqual(report["spread"]["ttft_p95_ms"], {"per_repeat": [10, 20, 30], "available_repeats": 3, "min": 10, "max": 30})
        self.assertEqual(report["spread"]["latency_p95_ms"]["per_repeat"], [None, None, None])
        import subprocess, sys
        result = subprocess.run([sys.executable, "-m", "bench", "report", "--run", str(self.root)], capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0)
        self.assertIn("point-01-repeats", json.loads(result.stdout)["points"])

    def test_plan_expands_repeats_and_budgets_without_execution(self):
        import subprocess, sys
        result = subprocess.run([sys.executable, "-m", "bench", "plan", "--config", str(ROOT / "examples/benchmark.json"), "--run", str(self.root), "--aiperf", "not-installed"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        plan = json.loads(result.stdout)
        self.assertEqual(len(plan["commands"]), 9)
        self.assertEqual(plan["budget"]["max_requests_first_pass"], 180)
        self.assertEqual(plan["budget"]["max_requests_with_manual_retries"], 540)
        self.assertFalse(self.root.exists())

    def test_arbitrary_fixed_replica_counts_require_all_pods_ready(self):
        from bench.kubernetes import inspect
        for count in (2, 4, 5, 8):
            scope = {"kubernetes": {"context": "fixture", "deployments": [{"namespace": "test", "name": "model", "replicas": count}]}}
            deployment = {"metadata": {"generation": 1}, "spec": {"replicas": count, "selector": {"matchLabels": {"app": "model"}}},
                          "status": {"observedGeneration": 1, **{k: count for k in ("replicas", "updatedReplicas", "availableReplicas", "readyReplicas")}}}
            pods = [{"metadata": {"name": f"model-{i}", "uid": str(i)}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}} for i in range(count)]
            def read(context, namespace, kind, *args, **kwargs):
                return deployment if kind == "deployment" else {"items": pods}
            with patch("bench.kubernetes.read", side_effect=read):
                self.assertEqual(inspect(scope)[0]["status"], "pass")
                pods[-1]["status"]["conditions"][0]["status"] = "False"
                self.assertEqual(inspect(scope)[0]["status"], "fail")

    def test_metrics_outage_is_not_misreported_as_deployment_drift(self):
        self.config["kubernetes"] = {"context": "fixture", "deployments": []}
        identity = {"name": "model", "status": "pass", "identity": [{"uid": "same", "containers": []}]}
        with patch("bench.runner.verify", side_effect=[[identity], [identity, {"name": "metrics", "status": "fail"}]]), \
             patch("bench.kubernetes.inspect", return_value=[identity]), \
             patch("bench.runner.run_child", side_effect=self.client):
            state = campaign(self.config, self.root, "unused")
        self.assertEqual(state["status"], "preflight_failed")
        checks = json.loads((self.root / "point-01-repeat-02-attempt-001/preflight.json").read_text())
        self.assertFalse(any(c["name"] == "campaign_identity" for c in checks))
        self.assertEqual(len(self.calls), 1)
