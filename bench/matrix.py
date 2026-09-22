"""Expand explicit experiments; checkpoint complete mixed repeats, never individual peers."""

import copy
import fcntl
import itertools
import json
import os
from pathlib import Path
import re

from .config import file_digest, fingerprint, load_points, positive, validate
from .evidence import manifest, write_json
from .provenance import operation, record, recording
from .runner import command, event, run_child


def named(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", value):
        raise ValueError("Names must start with a lowercase letter and contain only letters, digits, - or _")
    return value


def keys(value, allowed, label):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError(f"Unsupported fields in {label}")


def load_matrix(path):
    path = Path(path).resolve()
    spec = json.loads(path.read_text())
    keys(spec, ("schema_version", "name", "repeats", "max_attempts_per_repeat", "min_overlap_seconds", "profiles", "stages"), "matrix")
    sources = {str(path): file_digest(path)}
    if spec.get("schema_version") != 1:
        raise ValueError("Expected matrix schema_version 1")
    named(spec.get("name"))
    for key in ("repeats", "max_attempts_per_repeat"):
        positive(spec.get(key), key, integer=True)
    positive(spec.get("min_overlap_seconds"), "min_overlap_seconds")
    if spec["repeats"] > 100:
        raise ValueError("Matrix supports at most 100 repeats per row")
    profiles = {}
    for name, profile in spec.get("profiles", {}).items():
        named(name)
        keys(profile, ("observe", "expected", "deadline_seconds"), "profile")
        argv = profile.get("observe")
        if not isinstance(argv, list) or not argv or not all(isinstance(v, str) and v for v in argv):
            raise ValueError("Each profile needs a reviewed read-only observe argv list")
        positive(profile.get("deadline_seconds", 30), "profile.deadline_seconds")
        expected_path = (path.parent / profile["expected"]).resolve()
        expected = json.loads(expected_path.read_text())
        sources[str(expected_path)] = file_digest(expected_path)
        profiles[name] = {"observe": argv, "expected": expected,
                          "deadline_seconds": profile.get("deadline_seconds", 30)}
    rows, ids = [], set()
    stages = spec.get("stages")
    if not isinstance(stages, list) or not stages:
        raise ValueError("Matrix needs stages")
    for stage in stages:
        keys(stage, ("id", "question", "change", "kind", "profile", "streams"), "stage")
        name = named(stage.get("id"))
        if name in ids:
            raise ValueError("Stage names must be unique")
        ids.add(name)
        for field in ("question", "change"):
            if not isinstance(stage.get(field), str) or not stage[field].strip():
                raise ValueError(f"Stage needs {field}")
        if stage.get("profile") is not None and stage["profile"] not in profiles:
            raise ValueError("Stage references an unknown serving profile")
        if stage.get("kind", "traffic") not in ("traffic", "policy"):
            raise ValueError("Stage kind must be traffic or policy")
        if stage.get("kind") == "policy" and not stage.get("profile"):
            raise ValueError("Policy stages require an observed serving profile")
        streams = stage.get("streams")
        if not isinstance(streams, dict) or not 1 <= len(streams) <= 8:
            raise ValueError("Each stage needs one to eight named streams")
        configs = {}
        for stream, source in streams.items():
            named(stream)
            keys(source, ("config", "load"), "stream")
            config_path = (path.parent / source["config"]).resolve()
            sources[str(config_path)] = file_digest(config_path)
            config = json.loads(config_path.read_text())
            overrides = source.get("load", {})
            keys(overrides, ("concurrency", "rates", "arrival", "max_concurrency", "requests", "duration_seconds", "request_timeout_seconds", "grace_seconds", "deadline_seconds"), "stream.load")
            if "rates" in overrides and "concurrency" in overrides:
                raise ValueError("Choose one stream load axis")
            if "rates" in overrides:
                config["load"].pop("concurrency", None)
            if "concurrency" in overrides:
                for field in ("rates", "arrival", "max_concurrency"):
                    config["load"].pop(field, None)
            config["load"].update(overrides)
            config["load"]["repeats"] = 1
            configs[stream] = validate(config, config_path.parent)
        count = 1
        for config in configs.values():
            count *= len(load_points(config))
        if count + len(rows) > 1000:
            raise ValueError("Matrix expands beyond 1000 rows; split the experiment")
        for values in itertools.product(*(load_points(c) for c in configs.values())):
            selected = copy.deepcopy(configs)
            for config, point in zip(selected.values(), values):
                axis = "rates" if "rates" in config["load"] else "concurrency"
                config["load"][axis] = [point]
            rows.append({"stage": name, "question": stage["question"], "change": stage["change"],
                         "profile": stage.get("profile"), "streams": selected})
    return {"schema_version": 1, "name": spec["name"], "repeats": spec["repeats"],
            "max_attempts_per_repeat": spec["max_attempts_per_repeat"],
            "min_overlap_seconds": spec["min_overlap_seconds"], "profiles": profiles, "rows": rows,
            "sources": sources}


def plan_matrix(config, root, aiperf):
    budget = sum(sum(c["load"]["requests"] for c in row["streams"].values()) for row in config["rows"]) * config["repeats"]
    return {"status": "plan_only", "name": config["name"], "rows": len(config["rows"]),
            "repeats_per_row": config["repeats"], "max_requests_first_pass": budget,
            "max_requests_with_manual_retries": budget * config["max_attempts_per_repeat"],
            "automatic_inference_retries": 0, "min_overlap_seconds": config["min_overlap_seconds"],
            "experiments": [{**{k: row[k] for k in ("stage", "question", "change", "profile")},
                             "budgets": {name: {key: c["load"][key] for key in ("requests", "duration_seconds", "request_timeout_seconds", "grace_seconds", "deadline_seconds")} for name, c in row["streams"].items()},
                             "streams": {name: command(c, load_points(c)[0], Path(root) / f"row-{i+1:03d}" / name, aiperf)
                                         for name, c in row["streams"].items()}}
                            for i, row in enumerate(config["rows"])],
            "profile_observers": {name: profile["observe"] for name, profile in config["profiles"].items()}}


def observe(profile, directory, lock_fd):
    directory.mkdir()
    write_json(directory / "command.json", profile["observe"])
    result = run_child(profile["observe"], directory, profile["deadline_seconds"], lock_fd)
    write_json(directory / "execution.json", result)
    if result["exit_code"] != 0 or result.get("reason"):
        return False
    log = directory / "aiperf.log"
    if log.stat().st_size > 1024 * 1024:
        return False
    try:
        actual = json.loads(log.read_text())
    except (ValueError, UnicodeError):
        return False
    write_json(directory / "observed.json", actual)
    return fingerprint(actual) == fingerprint(profile["expected"])


def matrix_campaign(config, root, aiperf, resume=False, config_acquisition=None):
    from .mixed import execute_group
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
            raise ValueError("A process still owns this matrix") from None
        if missing_checkpoint:
            raise ValueError(f"No saved checkpoint at {root / 'state.json'}; check RUN or start a new campaign")
        with recording(root), operation("matrix", resume=resume):
            if config_acquisition:
                record("config_acquisition", **config_acquisition, sources=config["sources"])
            identity = fingerprint(config)
            if resume:
                state = json.loads((root / "state.json").read_text())
                if state["config_hash"] != identity:
                    raise ValueError("Matrix, configuration, dataset or expected profile changed; use a new run")
                if state["status"] in ("running", "checking"):
                    raise ValueError("Previous owner vanished without a checkpoint; reconcile processes and start a new matrix")
                for accepted in state["completed"]:
                    directory = root / accepted
                    if json.loads((directory / "manifest.json").read_text()) != manifest(directory):
                        raise ValueError("Accepted group evidence changed; cannot resume")
                (root / "pause-request.json").unlink(missing_ok=True)
            else:
                write_json(root / "config.json", config)
                state = {"kind": "matrix", "config_hash": identity, "status": "ready", "next_row": 0,
                         "next_repeat": 0, "completed": [], "outcomes": {}}
            for index in range(state["next_row"], len(config["rows"])):
                row = config["rows"][index]
                for repeat in range(state["next_repeat"], config["repeats"]):
                    if (root / "pause-request.json").exists():
                        state["status"] = "paused"
                        write_json(root / "state.json", state)
                        event(root, "matrix_paused", row=index+1, repeat=repeat+1)
                        return state
                    prefix = f"row-{index+1:03d}-repeat-{repeat+1:02d}"
                    prior = list(root.glob(prefix + "-attempt-*"))
                    if len(prior) >= config["max_attempts_per_repeat"]:
                        state["status"] = "attempt_budget_exhausted"
                        write_json(root / "state.json", state)
                        return state
                    attempt = max((int(p.name.rsplit("-", 1)[1]) for p in prior), default=0) + 1
                    directory = root / f"{prefix}-attempt-{attempt:03d}"
                    directory.mkdir()
                    write_json(directory / "experiment.json", row)
                    state["status"] = "checking"
                    write_json(root / "state.json", state)
                    event(root, "group_checking", row=index+1, repeat=repeat+1, attempt=directory.name)
                    profile = config["profiles"].get(row["profile"])
                    try:
                        if profile and not observe(profile, directory / "profile-before", lock.fileno()):
                            state["status"] = "profile_mismatch"
                            write_json(directory / "summary.json", {"evidence": "invalid", "reasons": ["profile_mismatch"], "traffic_sent": False})
                            write_json(root / "state.json", state)
                            return state
                        state["status"] = "running"
                        write_json(root / "state.json", state)
                        baseline = {}
                        for accepted in state["completed"]:
                            first = root / accepted
                            experiment = json.loads((first / "experiment.json").read_text())
                            if experiment["profile"] == row["profile"]:
                                for name in row["streams"]:
                                    checks = first / name / "preflight.json"
                                    if name not in baseline and checks.exists():
                                        baseline[name] = json.loads(checks.read_text())
                        result = execute_group(row["streams"], directory, aiperf, lock.fileno(), config["min_overlap_seconds"], baseline_checks=baseline)
                        if profile and not observe(profile, directory / "profile-after", lock.fileno()):
                            result["evidence"] = "invalid"
                            result["reasons"].append("profile_changed_or_unavailable")
                            for collection in ("streams", "shared_window"):
                                for summary in result.get(collection, {}).values():
                                    for goal in summary.get("goals", []):
                                        goal["status"] = "unverified"
                        if result["evidence"] != "complete":
                            result["outcome"] = "invalid"
                        write_json(directory / "summary.json", result)
                        write_json(directory / "manifest.json", manifest(directory))
                    except KeyboardInterrupt:
                        state["status"] = "interrupted"
                        write_json(root / "state.json", state)
                        raise
                    if result["evidence"] != "complete":
                        state["status"] = "evidence_invalid"
                        write_json(root / "state.json", state)
                        event(root, "group_invalid", row=index+1, repeat=repeat+1, reasons=result["reasons"])
                        return state
                    state["completed"].append(directory.name)
                    state["next_repeat"] = repeat+1
                    previous = state["outcomes"].get(str(index), "ready")
                    outcomes = (previous, result["outcome"])
                    state["outcomes"][str(index)] = "goal_not_met" if "goal_not_met" in outcomes else "request_errors" if "request_errors" in outcomes else "ready"
                    state["status"] = "ready"
                    write_json(root / "state.json", state)
                    event(root, "group_accepted", row=index+1, repeat=repeat+1, attempt=directory.name)
                state.update(next_row=index+1, next_repeat=0, status=state["outcomes"][str(index)])
                write_json(root / "state.json", state)
                if state["status"] != "ready":
                    return state
            outcomes = state["outcomes"].values()
            status = "goal_not_met" if "goal_not_met" in outcomes else "request_errors" if "request_errors" in outcomes else "complete"
            if state["status"] != status:
                state["status"] = status
                write_json(root / "state.json", state)
                event(root, "matrix_complete", groups=len(state["completed"]))
            return state
