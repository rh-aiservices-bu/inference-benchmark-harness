"""Run one bounded mixed repeat and validate observed arrival overlap."""

import copy
import errno
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time

from .config import file_digest, load_points, phase
from .evidence import analyze, finite_nonnegative, manifest, write_json
from .preflight import verify
from .provenance import artifact, artifact_name, operation, record, timestamp
from .runner import command
from .processes import nonrunning_darwin_group


def arrival_overlap(directories, minimum):
    windows, starts = {}, {}
    for name, directory in directories.items():
        try:
            rows = [json.loads(line) for line in (directory / "native/profile_export.jsonl").read_text().splitlines() if line.strip()]
            values = [row["metadata"]["request_start_ns"] for row in rows]
            ends = [row["metadata"]["request_end_ns"] for row in rows]
            if not values or any(type(value) is not int or value <= 0 for value in values + ends):
                raise ValueError("Missing valid request timestamps")
            starts[name] = values
            windows[name] = {"first_start_ns": min(values), "last_start_ns": max(values),
                             "last_end_ns": max(ends), "requests": len(values)}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            windows[name] = None
    complete = len(starts) == len(directories)
    begin = max(window["first_start_ns"] for window in windows.values()) if complete else None
    end = min(window["last_start_ns"] for window in windows.values()) if complete else None
    seconds = max(0, end - begin) / 1e9 if complete else 0
    counts = {name: sum(begin <= value <= end for value in values) if complete else 0
              for name, values in starts.items()}
    return {"basis": "observed_request_arrivals", "streams": windows,
            "start_ns": begin, "end_ns": end, "seconds": seconds,
            "required_seconds": minimum,
            "start_skew_seconds": ((max(w["first_start_ns"] for w in windows.values()) -
                                    min(w["first_start_ns"] for w in windows.values())) / 1e9) if complete else None,
            "arrivals_in_common_window": counts,
            "window_covered": {name: complete and bool(counts.get(name)) for name in directories},
            "valid": complete and (len(directories) == 1 or
                                    (seconds >= minimum and end > begin and all(counts.values())))}


def shared_window_summary(directory, config, overlap):
    """Select arrivals, retaining each selected request's complete outcome."""
    rows = []
    try:
        begin, end = overlap["start_ns"], overlap["end_ns"]
        for line in (directory / "native/profile_export.jsonl").read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                if begin <= row["metadata"]["request_start_ns"] <= end:
                    rows.append(row)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        rows = []
    successful = [row for row in rows if not row.get("error") and
                  not row.get("metadata", {}).get("was_cancelled") and
                  "request_latency" in row.get("metrics", {})]
    failed = len(rows) - len(successful)
    measurements = {"error_fraction": failed / len(rows) if rows else None}
    counts = {}
    for name, key in (("latency_p95_ms", "request_latency"), ("ttft_p95_ms", "time_to_first_token")):
        values = []
        for row in successful:
            metric = row.get("metrics", {}).get(key)
            if isinstance(metric, dict) and metric.get("unit") == "ms" and finite_nonnegative(metric.get("value")):
                values.append(metric["value"])
        counts[name] = {"valid": len(values), "expected_successful": len(successful)}
        measurements[name] = (sorted(values)[math.ceil(.95 * len(values)) - 1]
                              if values and len(values) == len(successful) else None)
    goals = []
    for name, limit in config.get("goals", {}).items():
        measured = measurements["error_fraction" if name == "max_error_fraction" else name]
        goals.append({"goal": name, "limit": limit, "observed": measured,
                      "status": "unverified" if measured is None else "met" if measured <= limit else "missed"})
    return {"basis": "request_start_ns_in_inclusive_common_arrival_window",
            "percentile_method": "nearest_rank", "start_ns": overlap["start_ns"], "end_ns": overlap["end_ns"],
            "requests": len(rows), "failed_requests": failed, "measurements": measurements,
            "sample_counts": counts, "goals": goals,
            "claim": "Arrival cohort with full response durations, including completions after the common window"}


def _prepare(config, directory, aiperf):
    planned = copy.deepcopy(config)
    write_json(directory / "config.json", config)
    if config["workload"]["type"] == "single_turn":
        target = directory / "input.jsonl"
        with operation("dataset_acquisition", stream=directory.name) as acquired:
            shutil.copyfile(config["workload"]["path"], target)
        artifact(target, acquisition=acquired)
        if file_digest(target) != config["workload"]["sha256"]:
            raise ValueError("Dataset changed after configuration validation")
        planned["workload"]["path"] = str(target)
    point = load_points(config)[0]
    if config["endpoint"].get("api_key_env"):
        workload = planned["workload"]
        dataset = ({"type": "file", "format": "single_turn", "path": workload["path"]}
                   if workload["type"] == "single_turn" else
                   {"type": "synthetic", "isl": {"mean": workload["input_tokens"], "stddev": 0},
                    "osl": {"mean": workload["output_tokens"], "stddev": 0}})
        write_json(directory / "auth.yaml", {"schemaVersion": "2.0", "benchmark": {
            "endpoint": {"api_key": "${" + config["endpoint"]["api_key_env"] + "}"},
            "dataset": dataset, "phases": phase(config, point)}})
    args = command(planned, point, directory, aiperf)
    write_json(directory / "command.json", args)
    return args


def _execute(commands, streams, directories, lock_fd):
    processes, logs, executions, clocks = {}, {}, {}, {}
    failure = None
    try:
        for name, args in commands.items():
            executions[name] = {"started": timestamp(), "reason": None}
            clocks[name] = time.monotonic()
            logs[name] = (directories[name] / "aiperf.log").open("w")
            try:
                processes[name] = subprocess.Popen(args, stdout=logs[name], stderr=subprocess.STDOUT,
                                                   start_new_session=True, pass_fds=(lock_fd,))
                executions[name]["process_id"] = processes[name].pid
            except OSError:
                executions[name].update(exit_code=127, reason="launch_failed")
                failure = "launch_failed:" + name
                break
        while not failure:
            pending = False
            for name, process in processes.items():
                code = process.poll()
                if code is not None:
                    executions[name].setdefault("exit_code", code)
                    if code:
                        failure = "client_process_failed:" + name
                        break
                else:
                    pending = True
                    if time.monotonic() - clocks[name] >= streams[name]["load"]["deadline_seconds"]:
                        executions[name].update(exit_code=124, reason="deadline_exceeded")
                        failure = "deadline_exceeded:" + name
                        break
            if not pending or failure:
                break
            time.sleep(0.02)
    except KeyboardInterrupt:
        failure = "interrupted"
    except OSError:
        failure = "launch_failed"
    finally:
        # Signal every owned group, including helpers left after a leader exits.
        for stop_signal in (signal.SIGTERM, signal.SIGKILL):
            for name, process in processes.items():
                if failure and "exit_code" not in executions[name]:
                    executions[name]["reason"] = "interrupted" if failure == "interrupted" else "group_cancelled"
                try:
                    os.killpg(process.pid, stop_signal)
                except ProcessLookupError:
                    pass
                except OSError as exc:
                    probe = {}
                    inactive = exc.errno == errno.EPERM and nonrunning_darwin_group(process.pid, probe)
                    executions[name].setdefault("cleanup_observations", []).append({
                        "signal": int(stop_signal), "errno": exc.errno,
                        "group_probe": probe, "status": "no_live_group_members" if inactive else "failed"})
                    if not inactive:
                        executions[name].update(exit_code=125, reason="cleanup_failed")
                        failure = failure or "cleanup_failed:" + name
            until = time.monotonic() + 5
            for name, process in processes.items():
                try:
                    process.wait(timeout=max(0.001, until - time.monotonic()))
                except subprocess.TimeoutExpired:
                    if stop_signal == signal.SIGKILL:
                        executions[name].update(exit_code=125, reason="cleanup_failed")
                        failure = failure or "cleanup_failed:" + name
        for name, execution in executions.items():
            process = processes.get(name)
            execution.setdefault("exit_code", process.returncode if process and process.returncode is not None else 127)
            execution["finished"] = timestamp()
            execution["duration_seconds"] = time.monotonic() - clocks[name]
        for log in logs.values():
            log.close()
        for name in logs:
            artifact(directories[name] / "aiperf.log", capture_window={
                "started": executions[name]["started"], "finished": executions[name]["finished"]})
    return executions, failure


def execute_group(streams, directory, aiperf, lock_fd, min_overlap_seconds, baseline_checks=None):
    """No retries: accept or reject the whole repeat, preserving every stream."""
    if not streams or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name) for name in streams):
        raise ValueError("Streams need distinct safe directory names")
    if any(len(load_points(config)) != 1 for config in streams.values()):
        raise ValueError("Each mixed stream must contain one load point")
    directory = Path(directory)
    os.umask(0o077)
    directories, checks, commands, summaries, reasons = {}, {}, {}, {}, []
    for name, config in streams.items():
        child = directory / name
        child.mkdir()
        directories[name] = child
        try:
            commands[name] = _prepare(config, child, aiperf)
            with operation("verify_stream", stream=name):
                checks[name] = verify(config, aiperf)
            if config.get("kubernetes") and baseline_checks and name in baseline_checks:
                from .kubernetes import deployment_changes
                if deployment_changes(baseline_checks[name], checks[name]):
                    checks[name].append({"name": "campaign_identity", "status": "fail",
                                         "detail": "Deployment identity changed since the accepted group baseline"})
            if shutil.disk_usage(directory).free < 64 * 1024 * 1024:
                checks[name].append({"name": "storage", "status": "fail",
                                     "detail": "Less than 64 MiB free; choose durable storage with enough space for the planned run"})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            checks[name] = [{"name": "preparation", "status": "fail", "detail": str(exc)}]
        write_json(child / "preflight.json", checks[name])
        if any(check["status"] == "fail" for check in checks[name]):
            reasons.append("preflight_failed:" + name)
    executions = {}
    if not reasons:
        with operation("mixed_client_execution"):
            executions, failure = _execute(commands, streams, directories, lock_fd)
        if failure:
            reasons.append(failure)
    for name, config in streams.items():
        child = directories[name]
        execution = executions.get(name, {"exit_code": 127, "reason": "not_launched"})
        write_json(child / "execution.json", execution)
        summary = analyze(child, config, execution)
        if name in executions and config.get("kubernetes"):
            from .kubernetes import deployment_changes, inspect
            with operation("postflight_stream", stream=name):
                after = inspect(config)
            write_json(child / "postflight.json", after)
            changes = deployment_changes(checks[name], after)
            if changes:
                summary["evidence"] = "invalid"
                summary["reasons"] = sorted(set(summary["reasons"] + changes))
                for goal in summary["goals"]:
                    goal["status"] = "unverified"
        summaries[name] = summary
        if summary["evidence"] != "complete":
            reasons.append("invalid_stream:" + name)
        write_json(child / "summary.json", summary)
        files = manifest(child)
        for path, digest in files.items():
            if path.startswith("native/"):
                record("native_artifact_observed", artifact=artifact_name(child / path),
                       sha256=digest, bytes=(child / path).stat().st_size, stream=name)
        write_json(child / "manifest.json", files)
    overlap = arrival_overlap(directories, min_overlap_seconds)
    if len(streams) > 1 and not overlap["valid"]:
        reasons.append("insufficient_arrival_overlap")
    shared = {name: shared_window_summary(directories[name], config, overlap) for name, config in streams.items()}
    if reasons:
        for summary in shared.values():
            for goal in summary["goals"]:
                goal["status"] = "unverified"
    goal_summaries = list(summaries.values()) + (list(shared.values()) if len(streams) > 1 else [])
    outcome = ("invalid" if reasons else "goal_not_met" if any(g["status"] != "met" for s in goal_summaries for g in s["goals"]) else
               "request_errors" if any(s["failed_requests"] for s in summaries.values()) else "ready")
    result = {"evidence": "invalid" if reasons else "complete", "reasons": sorted(set(reasons)),
              "streams": summaries, "overlap": overlap, "shared_window": shared, "outcome": outcome,
              "measurement_scope": {"streams": "full_run_native", "shared_window": "common_arrival_cohort"}}
    if "insufficient_arrival_overlap" in reasons:
        result["action"] = "Review per-stream request and duration budgets against min_overlap_seconds; choose a sufficient common arrival window before planning a new run."
    write_json(directory / "summary.json", result)
    write_json(directory / "manifest.json", manifest(directory))
    return result
