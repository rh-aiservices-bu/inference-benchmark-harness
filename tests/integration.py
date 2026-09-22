"""Exercise the installed AIPerf CLI against harmless local HTTP fixtures."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]


class Handler(BaseHTTPRequestHandler):
    requests = []
    failures = False

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/v1/models":
            body = json.dumps({"data": [{"id": "fixture-model"}]}).encode()
        elif self.path == "/metrics":
            body = ("# TYPE fixture_requests_total counter\nfixture_requests_total " + str(len(self.requests)) + "\n").encode()
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append({"path": self.path, "model": payload.get("model"),
                              "authorized": self.headers.get("Authorization") == "Bearer fixture-key",
                              "payload": payload, "at": time.time()})
        if self.failures:
            self.send_error(503)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for word in ("Hello", " world", "."):
                chunk = {"id": "fixture", "object": "chat.completion.chunk", "created": int(time.time()),
                         "model": "fixture-model", "choices": [{"index": 0, "delta": {"content": word}, "finish_reason": None}]}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.flush()
                time.sleep(0.06)
            chunk = {"id": "fixture", "object": "chat.completion.chunk", "model": "fixture-model",
                     "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11}}
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--aiperf", required=True)
    args = parser.parse_args()
    output = Path(tempfile.mkdtemp(prefix="inference-bench-integration-"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    commands = []
    env = {**os.environ, "FIXTURE_API_KEY": "fixture-key", "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        config = json.loads((ROOT / "examples/benchmark.json").read_text())
        config["endpoint"].update(url=f"http://127.0.0.1:{server.server_port}", model="fixture-model", api_key_env="FIXTURE_API_KEY")
        config["load"].update(concurrency=[1, 2], repeats=1, requests=3, duration_seconds=10,
                              request_timeout_seconds=3, grace_seconds=5, deadline_seconds=90)
        config["metrics"] = [{"name": "fixture", "url": config["endpoint"]["url"] + "/metrics", "required": [{"metric": "fixture_requests_total", "why": "Check collection during the request window"}]}]
        # Suppress nested GNU Make directory notices so stdout contains only the verify report.
        verify_config = output / "verify.json"
        for metric, expected in (("fixture_requests_total", 0), ("missing_requests_total", 2)):
            config["metrics"][0]["required"][0]["metric"] = metric
            verify_config.write_text(json.dumps(config))
            for format in ("auto", "text"):
                result = subprocess.run(
                    ["make", "--no-print-directory", "verify", f"PYTHON={sys.executable}", f"AIPERF={args.aiperf}",
                     f"CONFIG={verify_config}", f"FORMAT={format}"],
                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=40)
                assert result.returncode == expected, result.stdout + result.stderr
                if format == "auto":
                    report = json.loads(result.stdout)
                    assert report["status"] == ("ready_for_smoke" if expected == 0 else "preflight_failed")
                    assert "timestamp_utc" in report["reported"]
                    producer = next(c for c in report["checks"] if c["name"] == "fixture")
                    assert "fixture_requests_total" in producer["metric_names"]
                else:
                    assert "\x1b" not in result.stdout
                    assert ("READY FOR SMOKE" if expected == 0 else "BLOCKED") in result.stdout
                    if expected:
                        assert "Missing: missing_requests_total" in result.stdout
                assert not Handler.requests, "Verification sent inference traffic"
        config["metrics"][0]["required"][0]["metric"] = "fixture_requests_total"
        duration_requests = 0
        for case in ("generated", "file", "duration", "server-error"):
            if case == "file":
                config["workload"] = {"type": "single_turn", "path": str(ROOT / "examples/prompts.jsonl"), "output_tokens": 32, "tokenizer": "builtin"}
                config["load"]["concurrency"] = [1]
            if case == "duration":
                config["load"].update(requests=100, duration_seconds=1)
            if case == "server-error":
                config["load"].update(requests=3, duration_seconds=10)
                Handler.failures = True
            config_path = output / f"{case}.json"
            config_path.write_text(json.dumps(config))
            run = output / case
            command = [sys.executable, "-m", "bench", "run", "--config", str(config_path), "--run", str(run), "--aiperf", args.aiperf, "--execute"]
            result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=200)
            commands.append({"case": case, "command": command, "exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
            (output / "commands.json").write_text(json.dumps(commands, indent=2))
            print(json.dumps({"case": case, "exit_code": result.returncode, "output": str(run)}), flush=True)
            if case != "server-error":
                if result.returncode != 0:
                    raise AssertionError(result.stdout + result.stderr)
                state = json.loads((run / "state.json").read_text())
                assert state["status"] == "complete"
                for attempt in state["completed"]:
                    summary = json.loads((run / attempt / "summary.json").read_text())
                    assert summary["evidence"] == "complete"
                    if case == "duration":
                        duration_requests = summary["requests"]
                        assert 0 < duration_requests < 100
                    else:
                        assert summary["requests"] == 3
                if case == "generated":
                    report = subprocess.run([sys.executable, "-m", "bench", "report", "--run", str(run)],
                                            cwd=ROOT, capture_output=True, text=True, timeout=30)
                    assert report.returncode == 0, report.stdout + report.stderr
                    native_metrics = json.loads(report.stdout)["native_metrics"]
                    expected = {"itl_p95_ms", "output_tokens_per_second", "input_tokens_mean",
                                "output_tokens_mean", "duration_seconds"}
                    for attempt in state["completed"]:
                        observed = native_metrics[attempt]["workload"]
                        assert set(observed) == expected, observed
                        assert observed["input_tokens_mean"] > 0 and observed["duration_seconds"] > 0, observed
            else:
                assert result.returncode != 0
                assert len(list(run.glob("point-*"))) == 1
        assert len(Handler.requests) == 12 + duration_requests, len(Handler.requests)
        assert all(row["authorized"] for row in Handler.requests)
        assert all(row["path"] == "/v1/chat/completions" for row in Handler.requests)
        assert all(row["model"] == "fixture-model" for row in Handler.requests)
        assert all(row["payload"].get("stream") is True for row in Handler.requests)
        expected_prompts = {json.loads(line)["text"] for line in (ROOT / "examples/prompts.jsonl").read_text().splitlines() if line.strip()}
        for row in Handler.requests[6:9]:
            payload = row["payload"]
            assert any(message.get("content") in expected_prompts for message in payload["messages"]), payload
            assert payload.get("max_tokens", payload.get("max_completion_tokens")) == 32, payload
        Handler.failures = False
        sys.path.insert(0, str(ROOT))
        from bench.runner import command as build_command
        sweep_dir = output / "native-sweep"
        sweep_dir.mkdir()
        config["endpoint"].pop("api_key_env")
        cmd = build_command(config, 1, sweep_dir, args.aiperf)
        cmd[cmd.index("--concurrency") + 1] = "1,2"
        result = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=200)
        commands.append({"case": "native-sweep", "command": cmd, "exit_code": result.returncode,
                         "stdout": result.stdout, "stderr": result.stderr})
        (output / "commands.json").write_text(json.dumps(commands, indent=2))
        assert result.returncode == 0, result.stdout + result.stderr
        exports = list(sweep_dir.rglob("profile_export_aiperf.json"))
        assert len(exports) == 2, [str(p) for p in exports]
        assert len(Handler.requests) == 18 + duration_requests, len(Handler.requests)
        config["endpoint"]["api_key_env"] = "FIXTURE_API_KEY"
        config["load"].pop("concurrency")
        for arrival, rates in (("constant", [2, 4]), ("poisson", [4])):
            config["load"].update(rates=rates, arrival=arrival, max_concurrency=2)
            config_path = output / f"rate-{arrival}.json"
            config_path.write_text(json.dumps(config))
            run = output / f"rate-{arrival}"
            before = len(Handler.requests)
            cmd = [sys.executable, "-m", "bench", "run", "--config", str(config_path),
                   "--run", str(run), "--aiperf", args.aiperf, "--execute"]
            result = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=200)
            commands.append({"case": f"rate-{arrival}", "command": cmd, "exit_code": result.returncode,
                             "stdout": result.stdout, "stderr": result.stderr})
            (output / "commands.json").write_text(json.dumps(commands, indent=2))
            print(json.dumps({"case": f"rate-{arrival}", "exit_code": result.returncode, "output": str(run)}), flush=True)
            assert result.returncode == 0, result.stdout + result.stderr
            state = json.loads((run / "state.json").read_text())
            assert state["status"] == "complete"
            assert len(state["completed"]) == len(rates)
            received = Handler.requests[before:]
            assert len(received) == 3 * len(rates)
            assert all(row["authorized"] for row in received)
            if arrival == "constant":
                # A broad bound detects an unpaced burst without requiring precise host timing.
                for index, rate in enumerate(rates):
                    point = received[index * 3:(index + 1) * 3]
                    assert point[-1]["at"] - point[0]["at"] >= 0.6 * 2 / rate
        config["load"].pop("rates")
        config["load"].pop("arrival")
        config["load"].pop("max_concurrency")
        config["load"].update(concurrency=[1, 2], repeats=3, requests=2)
        path = output / "repeats.json"
        path.write_text(json.dumps(config))
        before = len(Handler.requests)
        cmd = [sys.executable, "-m", "bench", "run", "--config", str(path),
               "--run", str(output / "repeats"), "--aiperf", args.aiperf, "--execute"]
        result = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=400)
        commands.append({"case": "repeats", "command": cmd, "exit_code": result.returncode,
                         "stdout": result.stdout, "stderr": result.stderr})
        (output / "commands.json").write_text(json.dumps(commands, indent=2))
        assert result.returncode == 0, result.stdout + result.stderr
        state = json.loads((output / "repeats/state.json").read_text())
        assert len(state["completed"]) == 6
        assert len(Handler.requests) - before == 12
        windows = []
        for attempt in state["completed"]:
            rows = [json.loads(line) for line in (output / "repeats" / attempt / "native/profile_export.jsonl").read_text().splitlines() if line.strip()]
            windows.append((min(r["metadata"]["request_start_ns"] for r in rows), max(r["metadata"]["request_end_ns"] for r in rows)))
        assert all(left[1] <= right[0] for left, right in zip(windows, windows[1:])), windows
        print(json.dumps({"case": "repeats", "exit_code": 0, "valid_repeats": 6}), flush=True)
        (output / "observed-requests.json").write_text(json.dumps(Handler.requests, indent=2))
        print(json.dumps({"status": "pass", "requests": len(Handler.requests), "artifacts": str(output)}))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
