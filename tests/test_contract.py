import copy
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
import os
import signal
import socket
import time
from urllib.error import HTTPError
from unittest.mock import patch

from bench.config import load
from bench.evidence import analyze, write_json
from bench.runner import campaign, command, run_child
from bench.preflight import fetch, verify
from bench.kubernetes import inspect, deployment_changes

ROOT = Path(__file__).resolve().parents[1]


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = load(ROOT / "examples/benchmark.json")
        self.config["load"].pop("max_attempts_per_repeat", None)
        self.config["load"].update(repeats=1, concurrency=[1, 2], requests=2)

    def fixture(self, args, directory, deadline, lock_fd):
        native = directory / "native"
        native.mkdir()
        write_json(native / "profile_export_aiperf.json", {
            "aiperf_version": "0.12.0", "schema_version": "1.4",
            "request_count": {"avg": 2}, "time_to_first_token": {"unit": "ms", "p95": 20}})
        row = {"metadata": {"request_start_ns": 100, "request_end_ns": 200},
               "metrics": {"request_latency": {"value": 100, "unit": "ms"},
                           "time_to_first_token": {"value": 20, "unit": "ms"}}}
        (native / "profile_export.jsonl").write_text((json.dumps(row) + "\n") * 2)
        return {"exit_code": 0, "reason": None}

    def test_plan_does_not_execute_or_write(self):
        with patch("subprocess.Popen", side_effect=AssertionError("execution")):
            args = command(self.config, 1, self.root / "unused", "aiperf")
        self.assertIn("--custom-endpoint", args)
        self.assertFalse((self.root / "unused").exists())

    def test_report_exit_code_matches_campaign_status(self):
        for status, code in (("complete", 0), ("preflight_failed", 2), ("evidence_invalid", 2), ("goal_not_met", 2)):
            write_json(self.root / "state.json", {"status": status})
            result = subprocess.run([sys.executable, "-m", "bench", "report", "--run", str(self.root)],
                                    cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, code)
            self.assertEqual(json.loads(result.stdout)["status"], status)

    @patch("bench.runner.verify", return_value=[])
    def test_first_attempt_is_numbered_one_for_each_point(self, verify):
        with patch("bench.runner.run_child", side_effect=self.fixture):
            state = campaign(self.config, self.root / "run", "aiperf")
        self.assertEqual(state["completed"], ["point-01-attempt-001", "point-02-attempt-001"])

    def test_error_timestamp_required_by_pinned_export_schema(self):
        self.fixture([], self.root, 10, 0)
        native = self.root / "native"
        summary = json.loads((native / "profile_export_aiperf.json").read_text())
        summary.update(request_count={"avg": 1}, error_request_count={"avg": 1})
        write_json(native / "profile_export_aiperf.json", summary)
        good = (native / "profile_export.jsonl").read_text().splitlines()[0]
        failed = {"error": {"code": 503}, "metadata": {"request_start_ns": 150, "request_end_ns": 150}}
        self.config["goals"] = {"max_error_fraction": .6}
        (native / "profile_export.jsonl").write_text(good + "\n" + json.dumps(failed) + "\n")
        result = analyze(self.root, self.config, {"exit_code": 0})
        self.assertEqual(result["evidence"], "complete")
        self.assertEqual(result["failed_requests"], 1)
        self.assertEqual(result["goals"][0]["status"], "met")
        del failed["metadata"]["request_end_ns"]
        (native / "profile_export.jsonl").write_text(good + "\n" + json.dumps(failed) + "\n")
        self.assertIn("invalid_request_timestamps", analyze(self.root, self.config, {"exit_code": 0})["reasons"])

    def test_invalid_timestamp_cannot_pass_as_numeric(self):
        self.fixture([], self.root, 10, 0)
        path = self.root / "native/profile_export.jsonl"
        row = json.loads(path.read_text().splitlines()[0])
        for value in (float("nan"), "200", True):
            row["metadata"]["request_end_ns"] = value
            path.write_text((json.dumps(row) + "\n") * 2)
            self.assertIn("invalid_request_timestamps", analyze(self.root, self.config, {"exit_code": 0})["reasons"])

    def test_resume_preserves_older_attempt_numbers(self):
        root = self.root / "run"
        with patch("bench.runner.verify", return_value=[{"name": "fixture", "status": "fail"}]):
            campaign(self.config, root, "aiperf")
        (root / "point-01-attempt-001").rename(root / "point-01-attempt-004")
        with patch("bench.runner.verify", return_value=[]), patch("bench.runner.run_child", side_effect=self.fixture):
            state = campaign(self.config, root, "aiperf", True)
        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["completed"][0], "point-01-attempt-005")

    def test_previews_distinguish_smoke_and_sweep_budgets(self):
        config_path = self.root / "config.json"
        write_json(config_path, self.config)
        for target, points, requests in (("plan", 2, 4), ("plan-smoke", 1, 1)):
            run = self.root / target
            args = [sys.executable, "-m", "bench", "plan", "--config", str(config_path),
                    "--run", str(run), "--aiperf", "not-installed"]
            if target == "plan-smoke":
                args.append("--smoke")
            result = subprocess.run(args,
                                    cwd=ROOT, capture_output=True, text=True, check=True)
            plan = json.loads(result.stdout)
            self.assertEqual(plan["budget"]["points"], points)
            self.assertEqual(plan["budget"]["max_requests_first_pass"], requests)
            self.assertEqual(plan["budget"]["max_requests_with_manual_retries"], 3 * requests)
            self.assertEqual(len(plan["commands"]), points)
            self.assertFalse(run.exists())

    def test_finished_parent_cannot_leave_serving_descendant(self):
        ready = self.root / "child.json"
        child = ("import json,os,signal,socket,time; from pathlib import Path; "
                 "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                 "s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); "
                 f"Path({str(ready)!r}).write_text(json.dumps([os.getpid(),s.getsockname()[1]])); "
                 "time.sleep(30)")
        parent = ("import subprocess,sys,time; from pathlib import Path; "
                  f"subprocess.Popen([sys.executable,'-c',{child!r}]); p=Path({str(ready)!r});\n"
                  "while not p.exists(): time.sleep(.01)")
        try:
            with (self.root / "lock").open("w") as lock:
                result = run_child([sys.executable, "-c", parent], self.root, 3, lock.fileno())
            self.assertEqual(result["exit_code"], 0)
            pid, port = json.loads(ready.read_text())
            deadline = time.monotonic() + 1
            while True:
                try:
                    connection = socket.create_connection(("127.0.0.1", port), timeout=0.1)
                except ConnectionRefusedError:
                    break
                else:
                    connection.close()
                if time.monotonic() >= deadline:
                    self.fail("Owned descendant still accepts connections after parent exit")
                time.sleep(0.02)
        finally:
            if ready.exists():
                pid = json.loads(ready.read_text())[0]
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_empty_dataset_fails_before_traffic(self):
        config = copy.deepcopy(self.config)
        (self.root / "empty.jsonl").write_text("")
        config["workload"] = {"type": "single_turn", "path": "empty.jsonl", "output_tokens": 10}
        write_json(self.root / "config.json", config)
        with self.assertRaisesRegex(ValueError, "empty"):
            load(self.root / "config.json")

    def test_no_secret_in_command(self):
        self.config["endpoint"]["api_key_env"] = "BENCH_API_KEY"
        args = command(self.config, 1, self.root, "aiperf")
        self.assertNotIn("--api-key", args)
        self.assertIn("--config", args)

    @patch("bench.preflight.subprocess.run")
    @patch("bench.preflight.fetch", side_effect=ValueError("HTTP 404; verify endpoint, access and service health"))
    def test_model_listing_preserves_the_failed_read_reason(self, fetch_mock, runtime):
        runtime.return_value.returncode = 0
        runtime.return_value.stdout = "0.12.0"
        checks = verify(self.config, "aiperf")
        model = next(check for check in checks if check["name"] == "model")
        self.assertEqual(model["status"], "fail")
        self.assertIn("HTTP 404", model["detail"])

    def rate_config(self):
        config = copy.deepcopy(self.config)
        config["load"].pop("concurrency")
        config["load"].update(rates=[0.5, 2], arrival="constant", max_concurrency=4)
        return config

    def test_rate_validation_and_smoke_override(self):
        config = self.rate_config()
        path = self.root / "rate.json"
        write_json(path, config)
        self.assertEqual(load(path)["load"]["rates"], [0.5, 2])
        smoke = load(path, smoke=True)
        self.assertEqual(smoke["load"]["concurrency"], [1])
        self.assertNotIn("rates", smoke["load"])
        for change in ({"concurrency": [1]}, {"rates": [0]}, {"arrival": "unknown"},
                       {"max_concurrency": 0}, {"rates": [1, 1]}):
            bad = copy.deepcopy(config)
            bad["load"].update(change)
            write_json(path, bad)
            with self.assertRaises(ValueError):
                load(path)

    @patch("bench.runner.verify", return_value=[])
    def test_rate_points_keep_cap_auth_phase_and_resume_identity(self, verify):
        config = self.rate_config()
        config["endpoint"]["api_key_env"] = "BENCH_API_KEY"
        root = self.root / "rate-run"
        with patch("bench.runner.run_child", side_effect=self.fixture) as child:
            self.assertEqual(campaign(config, root, "aiperf")["status"], "complete")
        for call, rate in zip(child.call_args_list, config["load"]["rates"]):
            args, directory = call.args[:2]
            self.assertEqual(args[args.index("--request-rate") + 1], str(rate))
            self.assertEqual(args[args.index("--concurrency") + 1], "4")
            native = json.loads((directory / "auth.yaml").read_text())["benchmark"]["phases"]
            self.assertEqual(native, {"type": "constant", "rate": rate, "concurrency": 4, "requests": 2})
        with patch("bench.runner.run_child", side_effect=AssertionError("replayed")):
            self.assertEqual(campaign(config, root, "aiperf", True)["status"], "complete")
        config["load"]["arrival"] = "poisson"
        with self.assertRaisesRegex(ValueError, "changed"):
            campaign(config, root, "aiperf", True)

    @patch("bench.runner.verify", return_value=[])
    def test_points_finish_before_next_starts_and_resume_skips(self, verify):
        calls = []
        def execute(*args):
            calls.append(args[1].name)
            if len(calls) == 2:
                self.assertTrue((args[1].parent / calls[0] / "summary.json").exists())
            return self.fixture(*args)
        root = self.root / "run"
        with patch("bench.runner.run_child", side_effect=execute):
            result = campaign(self.config, root, "aiperf")
        self.assertEqual(result["status"], "complete")
        with patch("bench.runner.run_child", side_effect=AssertionError("replayed")):
            self.assertEqual(campaign(self.config, root, "aiperf", True)["status"], "complete")
        self.assertEqual(len(calls), 2)

    @patch("bench.runner.verify", return_value=[])
    def test_goal_miss_is_kept_and_resume_advances(self, verify):
        self.config["goals"] = {"ttft_p95_ms": 10}
        root = self.root / "run"
        with patch("bench.runner.run_child", side_effect=self.fixture):
            state = campaign(self.config, root, "aiperf")
            self.assertEqual(state["status"], "goal_not_met")
            self.assertEqual(state["next_point"], 1)
            state = campaign(self.config, root, "aiperf", True)
        self.assertEqual(state["next_point"], 2)
        self.assertEqual(state["attempts"], 2)

    @patch("bench.runner.verify", return_value=[])
    def test_missing_evidence_stops_without_retry(self, verify):
        root = self.root / "run"
        with patch("bench.runner.run_child", return_value={"exit_code": 0}) as child:
            state = campaign(self.config, root, "aiperf")
        self.assertEqual(state["status"], "evidence_invalid")
        self.assertEqual(child.call_count, 1)
        saved = json.loads(next(root.glob("point-*/summary.json")).read_text())
        self.assertIsNone(saved["requests"])
        self.assertIsNone(saved["failed_requests"])
        self.assertEqual(state["next_point"], 0)

    @patch("bench.runner.verify", return_value=[{"status": "fail", "name": "model"}])
    def test_preflight_failure_sends_no_traffic(self, verify):
        with patch("bench.runner.run_child", side_effect=AssertionError("traffic")):
            state = campaign(self.config, self.root / "run", "aiperf")
        self.assertEqual(state["status"], "preflight_failed")

    @patch("bench.runner.verify", return_value=[])
    def test_modified_evidence_prevents_skip(self, verify):
        root = self.root / "run"
        with patch("bench.runner.run_child", side_effect=self.fixture):
            state = campaign(self.config, root, "aiperf")
        (root / state["completed"][0] / "summary.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "evidence changed"):
            campaign(self.config, root, "aiperf", True)

    def test_unchanged_metrics_use_fetch_timeline(self):
        self.fixture([], self.root, 10, 0)
        native = self.root / "native"
        self.config["metrics"] = [{"name": "engine", "url": "http://localhost/metrics", "required": [{"metric": "requests", "why": "Coverage"}]}]
        (native / "server_metrics_export.jsonl").write_text(json.dumps({"endpoint_url": "http://localhost/metrics", "timestamp_ns": 50, "metrics": {"requests": [{"value": 0}]}}) + "\n")
        write_json(native / "server_metrics_export.json", {"summary": {"endpoint_info": {
            "http://localhost/metrics": {"total_fetches": 5, "first_fetch_ns": 50, "last_fetch_ns": 250}}}})
        self.assertEqual(analyze(self.root, self.config, {"exit_code": 0})["evidence"], "complete")
        write_json(native / "server_metrics_export.json", {"summary": {"endpoint_info": {
            "http://localhost/metrics": {"total_fetches": 1, "first_fetch_ns": 50, "last_fetch_ns": 50}}}})
        self.assertEqual(analyze(self.root, self.config, {"exit_code": 0})["evidence"], "invalid")

    def test_required_metric_needs_a_real_exported_sample(self):
        self.fixture([], self.root, 10, 0)
        native = self.root / "native"
        url = "http://localhost/metrics"
        self.config["metrics"] = [{"name": "engine", "url": url,
                                   "required": [{"metric": "requests_total", "why": "Completion evidence"}]}]
        write_json(native / "server_metrics_export.json", {
            "summary": {"endpoint_info": {url: {"total_fetches": 5, "first_fetch_ns": 50, "last_fetch_ns": 250}}},
            "metrics": {"requests": {"type": "counter"}}})
        for metrics, expected in (({}, "invalid"), ({"unrelated": [{"value": 1}]}, "invalid"),
                                  ({"requests": [{"value": float("nan")}]}, "invalid"),
                                  ({"requests": [{"value": 0}]}, "complete")):
            with self.subTest(metrics=metrics):
                (native / "server_metrics_export.jsonl").write_text(json.dumps({
                    "endpoint_url": url, "timestamp_ns": 50, "metrics": metrics}) + "\n")
                self.assertEqual(analyze(self.root, self.config, {"exit_code": 0})["evidence"], expected)

    def test_discovery_rejects_malformed_present_aggregates(self):
        self.fixture([], self.root, 10, 0)
        path = self.root / "native/profile_export_aiperf.json"
        original = json.loads(path.read_text())
        self.config["goals"] = {}
        for metric in ({"unit": "ms", "p95": float("nan")}, {"unit": "ms", "p95": -1},
                       {"unit": "s", "p95": 1}, {"unit": "ms", "p95": True}):
            with self.subTest(metric=metric):
                write_json(path, {**original, "request_latency": metric})
                self.assertEqual(analyze(self.root, self.config, {"exit_code": 0})["evidence"], "invalid")
        write_json(path, {**original, "error_request_count": {"avg": False}})
        self.assertIn("invalid_request_counts", analyze(self.root, self.config, {"exit_code": 0})["reasons"])

    def test_same_deployment_name_in_two_namespaces_keeps_both_identities(self):
        before = [{"namespace": namespace, "name": "model", "status": "pass", "identity": [{"uid": namespace}]}
                  for namespace in ("prefill", "decode")]
        self.assertEqual(deployment_changes(before, copy.deepcopy(before)), [])
        for index in (0, 1):
            after = copy.deepcopy(before)
            after[index]["identity"][0]["uid"] = "replacement"
            self.assertIn("deployment_identity_changed", deployment_changes(before, after))

    def test_interruption_cannot_be_accepted(self):
        self.fixture([], self.root, 10, 0)
        result = analyze(self.root, self.config, {"exit_code": 130, "reason": "interrupted"})
        self.assertEqual(result["evidence"], "invalid")
        self.assertIn("interrupted", result["reasons"])

    def test_malformed_and_nonfinite_timings_are_not_results(self):
        self.fixture([], self.root, 10, 0)
        native = self.root / "native"
        original = (native / "profile_export.jsonl").read_text()
        for value in (float("nan"), -1, "100", None):
            row = json.loads(original.splitlines()[0])
            row["metrics"]["request_latency"]["value"] = value
            (native / "profile_export.jsonl").write_text((json.dumps(row) + "\n") * 2)
            self.assertIn("invalid_request_latency", analyze(self.root, self.config, {"exit_code": 0})["reasons"])
        (native / "profile_export.jsonl").write_text(original)
        (native / "profile_export_aiperf.json").write_text("[]")
        self.assertEqual(analyze(self.root, self.config, {"exit_code": 0})["evidence"], "invalid")

    def test_nonfinite_goal_is_not_a_performance_miss(self):
        self.fixture([], self.root, 10, 0)
        path = self.root / "native/profile_export_aiperf.json"
        summary = json.loads(path.read_text())
        summary["time_to_first_token"]["p95"] = float("nan")
        write_json(path, summary)
        self.config["goals"] = {"ttft_p95_ms": 100}
        result = analyze(self.root, self.config, {"exit_code": 0})
        self.assertEqual(result["evidence"], "invalid")
        self.assertEqual(result["goals"][0]["status"], "unverified")

    def test_deployment_replacement_or_restart_invalidates_identity(self):
        before = [{"name": "engine", "status": "pass", "identity": [
            {"uid": "original", "containers": [{"imageID": "sha256:one", "restartCount": 0}]}]}]
        self.assertEqual(deployment_changes(before, copy.deepcopy(before)), [])
        for field, value in (("imageID", "sha256:two"), ("restartCount", 1)):
            after = copy.deepcopy(before)
            after[0]["identity"][0]["containers"][0][field] = value
            self.assertIn("deployment_identity_changed", deployment_changes(before, after))
        after = copy.deepcopy(before)
        after[0]["identity"][0]["uid"] = "replacement"
        self.assertIn("deployment_identity_changed", deployment_changes(before, after))

    @patch("bench.runner.verify", return_value=[])
    def test_failed_postflight_stops_next_point(self, verify):
        self.config["kubernetes"] = {"context": "fixture", "deployments": []}
        with patch("bench.runner.run_child", side_effect=self.fixture) as child, patch(
                "bench.kubernetes.inspect", return_value=[{"name": "engine", "status": "fail"}]):
            state = campaign(self.config, self.root / "run", "aiperf")
        self.assertEqual(child.call_count, 1)
        self.assertEqual(state["status"], "evidence_invalid")
        attempt = self.root / "run/point-01-attempt-001"
        self.assertTrue((attempt / "postflight.json").exists())
        self.assertIn("postflight_failed:engine", json.loads((attempt / "summary.json").read_text())["reasons"])

    @patch("bench.runner.verify", return_value=[])
    def test_attempt_budget_prevents_another_launch(self, verify):
        root = self.root / "run"
        self.config["load"]["max_attempts_per_point"] = 1
        with patch("bench.runner.run_child", return_value={"exit_code": 1}) as child:
            campaign(self.config, root, "aiperf")
            state = campaign(self.config, root, "aiperf", True)
        self.assertEqual(state["status"], "attempt_budget_exhausted")
        self.assertEqual(child.call_count, 1)

    @patch("bench.runner.verify", return_value=[])
    def test_nested_manifest_is_preserved_and_verified_on_resume(self, verify):
        def with_nested_manifest(*args):
            result = self.fixture(*args)
            (args[1] / "native/manifest.json").write_text('{"source":"original"}')
            return result
        root = self.root / "run"
        with patch("bench.runner.run_child", side_effect=with_nested_manifest):
            state = campaign(self.config, root, "aiperf")
        attempt = root / state["completed"][0]
        self.assertIn("native/manifest.json", json.loads((attempt / "manifest.json").read_text()))
        (attempt / "native/manifest.json").write_text('{"source":"changed"}')
        with self.assertRaisesRegex(ValueError, "evidence changed"):
            campaign(self.config, root, "aiperf", True)

    def test_cleanup_permission_failure_is_saved_as_invalid_execution(self):
        import errno
        with (self.root / ".lock").open("w") as lock, \
             patch("bench.runner.os.killpg", side_effect=PermissionError(errno.EPERM, "fixture")), \
             patch("bench.runner.nonrunning_darwin_group", return_value=False):
            result = run_child([sys.executable, "-c", "pass"], self.root, 5, lock.fileno())
        self.assertEqual(result["reason"], "cleanup_failed")
        self.assertEqual(result["exit_code"], 125)
        self.assertIn("finished", result)

    def test_nonrunning_group_permission_error_preserves_success_and_probe(self):
        import errno
        with (self.root / ".lock").open("w") as lock, \
             patch("bench.runner.os.killpg", side_effect=PermissionError(errno.EPERM, "fixture")), \
             patch("bench.runner.nonrunning_darwin_group", return_value=True):
            result = run_child([sys.executable, "-c", "pass"], self.root, 5, lock.fileno())
        self.assertIsNone(result["reason"])
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(len(result["cleanup_observations"]), 2)
        self.assertTrue(all(item["status"] == "no_live_group_members"
                            for item in result["cleanup_observations"]))

    def test_real_child_is_bounded_by_deadline(self):
        with (self.root / ".lock").open("w") as lock:
            result = run_child([sys.executable, "-c", "import time; time.sleep(30)"], self.root, 0.1, lock.fileno())
        self.assertEqual(result["reason"], "deadline_exceeded")
        self.assertLess(result["end_unix"] - result["start_unix"], 6)

    def test_reconciled_but_premature_export_is_invalid(self):
        self.fixture([], self.root, 10, 0)
        self.config["load"]["requests"] = 20
        path = self.root / "native/profile_export_aiperf.json"
        summary = json.loads(path.read_text())
        summary.update(start_time="2026-01-01T00:00:00", end_time="2026-01-01T00:00:01")
        write_json(path, summary)
        result = analyze(self.root, self.config, {"exit_code": 0})
        self.assertIn("stopped_before_request_or_time_limit", result["reasons"])

    def test_authentication_errors_are_not_retried(self):
        with patch("bench.preflight._http.open", side_effect=HTTPError("http://localhost", 401, "Unauthorized", {}, None)) as request:
            with self.assertRaisesRegex(ValueError, "HTTP 401"):
                fetch("http://localhost")
        self.assertEqual(request.call_count, 1)

    def test_transient_read_retries_are_bounded(self):
        with patch("bench.preflight._http.open", side_effect=HTTPError("http://localhost", 503, "Unavailable", {}, None)) as request:
            with patch("bench.preflight.time.sleep") as sleep:
                with self.assertRaisesRegex(ValueError, "HTTP 503"):
                    fetch("http://localhost")
        self.assertEqual(request.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_old_and_new_rollout_pods_do_not_pass(self):
        scope = {"kubernetes": {"context": "test-context", "deployments": [{"namespace": "test", "name": "model", "replicas": 1}]}}
        deployment = {"metadata": {"generation": 2}, "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "model"}}},
                      "status": {"observedGeneration": 2, "replicas": 2, "updatedReplicas": 1, "readyReplicas": 1, "availableReplicas": 1}}
        pod = {"metadata": {"name": "model-1", "uid": "a"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
        calls = []
        def read(args, **kwargs):
            calls.append(args)
            payload = deployment if "deployment" in args else {"items": [pod, pod]}
            return subprocess.CompletedProcess(args, 0, json.dumps(payload))
        with patch("bench.kubernetes.subprocess.run", side_effect=read):
            result = inspect(scope)
        self.assertEqual(result[0]["status"], "fail")
        self.assertTrue(all("get" in args and "--context" in args for args in calls))


if __name__ == "__main__":
    unittest.main()
