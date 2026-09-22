"""Run isolated points and keep every attempt, including failures."""

import errno
import fcntl
import copy
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time

from .config import attempt_limit, attempt_prefix, file_digest, fingerprint, load_points, phase, repeat_count
from .evidence import analyze, manifest, write_json
from .preflight import verify
from .processes import nonrunning_darwin_group
from .provenance import artifact, operation, record, recording, timestamp


def command(config, point, directory, aiperf):
    endpoint, workload, bounds = config["endpoint"], config["workload"], config["load"]
    traffic = phase(config, point)
    args = [aiperf, "profile", "--url", endpoint["url"], "--custom-endpoint", endpoint["path"],
            "--model", endpoint["model"], "--endpoint-type", "chat", "--streaming",
            "--tokenizer", workload.get("tokenizer", "builtin"), "--use-server-token-count",
            "--concurrency", str(traffic["concurrency"]), "--request-count", str(bounds["requests"]),
            "--benchmark-duration", str(bounds["duration_seconds"]),
            "--benchmark-grace-period", str(bounds["grace_seconds"]),
            "--request-timeout-seconds", str(bounds["request_timeout_seconds"]),
            "--osl", str(workload["output_tokens"]), "--random-seed", "42",
            "--record-processors", str(config.get("record_processors", 1)),
            "--no-gpu-telemetry", "--ui-type", "none", "--export-level", "records", "--no-auto-plot",
            "--output-artifact-dir", str(directory / "native")]
    if "rate" in traffic:
        args += ["--request-rate", str(traffic["rate"]), "--request-rate-mode", traffic["type"]]
    if workload["type"] == "single_turn":
        args += ["--input-file", workload["path"], "--custom-dataset-type", "single_turn"]
    else:
        args += ["--isl", str(workload["input_tokens"]), "--isl-stddev", "0", "--osl-stddev", "0"]
    if endpoint.get("api_key_env"):
        args += ["--config", str(directory / "auth.yaml")]
    for key, value in sorted(endpoint.get("headers", {}).items()):
        args += ["--header", key + ":" + value]
    if config.get("metrics"):
        args += ["--server-metrics", *[producer["url"] for producer in config["metrics"]],
                 "--server-metrics-formats", "json", "csv", "jsonl"]
    else:
        args += ["--no-server-metrics"]
    return args


def event(root, code, **fields):
    when = timestamp()
    value = {"schema_version": 1, "timestamp": when["timestamp_ns"] / 1e9, **when, "event": code, **fields}
    with (root / "events.jsonl").open("a") as stream:
        stream.write(json.dumps(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    record("event", **value)


def run_child(args, directory, deadline, lock_fd):
    process = None
    code, reason = 1, None
    cleanup_observations = []
    started = timestamp()
    clock = time.monotonic_ns()
    try:
        with (directory / "aiperf.log").open("w") as log:
            process = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True, pass_fds=(lock_fd,))
            code = process.wait(timeout=deadline)
    except subprocess.TimeoutExpired:
        code, reason = 124, "deadline_exceeded"
    except KeyboardInterrupt:
        code, reason = 130, "interrupted"
    except OSError:
        code, reason = 127, "launch_failed"
    finally:
        if process is not None:
            for stop_signal in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(process.pid, stop_signal)
                except ProcessLookupError:
                    pass
                except OSError as exc:
                    probe = {}
                    inactive = exc.errno == errno.EPERM and nonrunning_darwin_group(process.pid, probe)
                    observation = {"signal": int(stop_signal), "errno": exc.errno,
                                   "group_probe": probe,
                                   "status": "no_live_group_members" if inactive else "failed"}
                    cleanup_observations.append(observation)
                    record("cleanup_observation", process_id=process.pid, **observation)
                    if not inactive:
                        code, reason = 125, "cleanup_failed"
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    if stop_signal == signal.SIGKILL:
                        code, reason = 125, "cleanup_failed"
    finished = timestamp()
    if (directory / "aiperf.log").exists():
        artifact(directory / "aiperf.log", capture_window={"started": started, "finished": finished})
    return {"start_unix": started["timestamp_ns"] / 1e9, "end_unix": finished["timestamp_ns"] / 1e9,
            "started": started, "finished": finished, "duration_ns": time.monotonic_ns() - clock,
            "exit_code": code, "reason": reason, "cleanup_observations": cleanup_observations}


def campaign(config, root, aiperf, resume=False, config_acquisition=None):
    root = Path(root).resolve()
    missing_checkpoint = resume and not (root / "state.json").is_file()
    if missing_checkpoint and not (root / ".lock").is_file():
        raise ValueError(f"No saved checkpoint at {root / 'state.json'}; check RUN or start a new campaign")
    os.umask(0o077)
    root.mkdir(parents=True, exist_ok=resume)
    # Check ownership first; read mode cannot recreate a lock removed during inspection.
    with (root / ".lock").open("r" if missing_checkpoint else "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("A process still owns this campaign") from None
        if missing_checkpoint:
            raise ValueError(f"No saved checkpoint at {root / 'state.json'}; check RUN or start a new campaign")
        with recording(root), operation("campaign", resume=resume):
            if config_acquisition:
                record("config_acquisition", **config_acquisition)
            return run_locked(config, root, aiperf, resume, lock.fileno())


def run_locked(config, root, aiperf, resume, lock_fd):
    identity = fingerprint(config)
    if resume:
        state = json.loads((root / "state.json").read_text())
        if state["config_hash"] != identity:
            raise ValueError("Configuration or dataset changed; choose a new run directory")
        if state["status"] == "running":
            raise ValueError("Previous execution ended without a checkpoint; inspect ownership and start a new campaign")
        for accepted in state["completed"]:
            directory = root / accepted
            if json.loads((directory / "manifest.json").read_text()) != manifest(directory):
                raise ValueError("Saved evidence changed; cannot skip this point on resume")
    else:
        state = {"schema_version": 1, "config_hash": identity, "status": "created", "completed": [], "next_point": 0, "attempts": 0}
        write_json(root / "config.json", config)
    if state["status"] == "complete":
        return state
    points = load_points(config)
    repeats = repeat_count(config)
    state.setdefault("next_repeat", 0)
    state.setdefault("point_results", {})
    for index in range(state["next_point"], len(points)):
        point = points[index]
        for repeat in range(state["next_repeat"], repeats):
            prefix = attempt_prefix(index, repeat, repeats)
            previous_attempts = list(root.glob(f"{prefix}-attempt-*"))
            point_attempt = max((int(path.name.rsplit("-", 1)[1]) for path in previous_attempts), default=0) + 1
            if len(previous_attempts) >= attempt_limit(config):
                state["status"] = "attempt_budget_exhausted"
                write_json(root / "state.json", state)
                event(root, "attempt_budget_exhausted", point=index + 1, repeat=repeat + 1, retryable=False,
                      action="Review the accumulated failures before planning another experiment")
                return state
            state["attempts"] += 1
            directory = root / f"{prefix}-attempt-{point_attempt:03d}"
            directory.mkdir()
            state["status"] = "checking"
            write_json(root / "state.json", state)
            with operation("verify_attempt", attempt=directory.name):
                checks = verify(config, aiperf)
            if config.get("kubernetes") and state["completed"] and not any(c["status"] == "fail" for c in checks):
                from .kubernetes import deployment_changes
                baseline = json.loads((root / state["completed"][0] / "preflight.json").read_text())
                if deployment_changes(baseline, checks):
                    checks.append({"name": "campaign_identity", "status": "fail",
                                   "detail": "Deployment identity changed since the first accepted repeat; restore the baseline or start a new campaign"})
            if shutil.disk_usage(root).free < 64 * 1024 * 1024:
                checks.append({"name": "storage", "status": "fail", "detail": "Less than 64 MiB free; choose durable storage with enough space for the planned run"})
            write_json(directory / "preflight.json", checks)
            if any(check["status"] == "fail" for check in checks):
                state["status"] = "preflight_failed"
                event(root, "preflight_failed", point=index + 1, repeat=repeat + 1, attempt=directory.name, retryable=True, action="Correct the failed check, then resume; no inference was sent")
                write_json(root / "state.json", state)
                return state
            point_config = copy.deepcopy(config)
            if config["workload"]["type"] == "single_turn":
                dataset = directory / "input.jsonl"
                with operation("dataset_acquisition", attempt=directory.name) as acquired:
                    shutil.copyfile(config["workload"]["path"], dataset)
                artifact(dataset, acquisition=acquired)
                if file_digest(dataset) != config["workload"]["sha256"]:
                    state["status"] = "dataset_changed"
                    write_json(root / "state.json", state)
                    event(root, "dataset_changed", point=index + 1, repeat=repeat + 1, attempt=directory.name, retryable=False,
                          action="Restore the planned input or create a new experiment")
                    return state
                point_config["workload"]["path"] = str(dataset)
            if config["endpoint"].get("api_key_env"):
                env_name = config["endpoint"]["api_key_env"]
                workload = point_config["workload"]
                dataset = ({"type": "file", "format": "single_turn", "path": workload["path"]}
                           if workload["type"] == "single_turn" else
                           {"type": "synthetic", "isl": {"mean": workload["input_tokens"], "stddev": 0},
                            "osl": {"mean": workload["output_tokens"], "stddev": 0}})
                write_json(directory / "auth.yaml", {"schemaVersion": "2.0", "benchmark": {
                    "endpoint": {"api_key": "${" + env_name + "}"}, "dataset": dataset,
                    "phases": phase(config, point)}})
            args = command(point_config, point, directory, aiperf)
            write_json(directory / "command.json", args)
            state["status"] = "running"
            write_json(root / "state.json", state)
            event(root, "point_started", point=index + 1, repeat=repeat + 1, attempt=directory.name, load=phase(config, point))
            with operation("client_execution", attempt=directory.name):
                execution = run_child(args, directory, config["load"]["deadline_seconds"], lock_fd)
            write_json(directory / "execution.json", execution)
            result = analyze(directory, config, execution)
            if config.get("kubernetes"):
                from .kubernetes import deployment_changes, inspect
                with operation("postflight", attempt=directory.name):
                    after = inspect(config)
                write_json(directory / "postflight.json", after)
                changes = deployment_changes(checks, after)
                if changes:
                    result["evidence"] = "invalid"
                    result["reasons"] = sorted(set(result["reasons"] + changes))
                    for goal in result["goals"]:
                        goal["status"] = "unverified"
            write_json(directory / "summary.json", result)
            files = manifest(directory)
            for name, digest in files.items():
                if name.startswith("native/"):
                    record("native_artifact_observed", artifact=str(directory.relative_to(root) / name),
                           sha256=digest, bytes=(directory / name).stat().st_size)
            write_json(directory / "manifest.json", files)
            if result["evidence"] != "complete":
                state["status"] = "evidence_invalid"
                event(root, "point_invalid", point=index + 1, repeat=repeat + 1, attempt=directory.name, retryable=False,
                      reasons=result["reasons"], action="Inspect artifacts; deliberate resume creates a new attempt")
                write_json(root / "state.json", state)
                return state
            state["completed"].append(directory.name)
            state["next_repeat"] = repeat + 1
            missed = any(goal["status"] != "met" for goal in result["goals"])
            outcome = "goal_not_met" if missed else "request_errors" if result["failed_requests"] else "ready"
            previous = state["point_results"].get(str(index + 1), "ready")
            state["point_results"][str(index + 1)] = (
                "goal_not_met" if "goal_not_met" in (previous, outcome) else
                "request_errors" if "request_errors" in (previous, outcome) else "ready")
            state["status"] = "ready"
            event(root, "repeat_completed", point=index + 1, repeat=repeat + 1,
                  attempt=directory.name, result=outcome, retryable=False)
            write_json(root / "state.json", state)
        state["next_point"] = index + 1
        state["next_repeat"] = 0
        state["status"] = state["point_results"][str(index + 1)]
        summaries = [json.loads((root / name / "summary.json").read_text())
                     for name in state["completed"] if name.startswith(f"point-{index + 1:02d}-")]
        write_json(root / f"point-{index + 1:02d}-repeats.json", {
            "point": index + 1, "load": phase(config, point), "valid_repeats": len(summaries),
            "planned_repeats": repeats, "outcome": state["status"],
            "measurements": summaries,
            "spread": repeat_spread(summaries)})
        event(root, "point_completed", point=index + 1, repeats=repeats, retryable=False,
              result=state["status"], action="Keep all repeats; resume advances to the next point" if state["status"] != "ready" else "Continue")
        write_json(root / "state.json", state)
        if state["status"] != "ready":
            return state
    outcomes = state["point_results"].values()
    state["status"] = ("goal_not_met" if "goal_not_met" in outcomes else
                       "request_errors" if "request_errors" in outcomes else "complete")
    write_json(root / "state.json", state)
    event(root, "campaign_complete", points=len(points), valid_repeats=len(state["completed"]))
    return state


def repeat_spread(summaries):
    """Describe run-to-run variation; never average percentiles into a pooled one."""
    result = {}
    for metric in ("ttft_p95_ms", "latency_p95_ms", "request_throughput_rps", "error_fraction"):
        values = [summary.get("measurements", {}).get(metric) for summary in summaries]
        known = [value for value in values if value is not None]
        result[metric] = {"per_repeat": values, "available_repeats": len(known),
                          "min": min(known) if known else None, "max": max(known) if known else None}
    return result
