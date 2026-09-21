"""Validate the supported experiment contract before any traffic."""

import hashlib
import json
import math
from pathlib import Path
import re
from urllib.parse import urlsplit


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def file_digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def positive(value, name, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a positive number")
    if not math.isfinite(value) or value <= 0 or (integer and type(value) is not int):
        raise ValueError(f"Invalid {name}")


def check_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Use an HTTP(S) URL without credentials, query or fragment")


def load_points(config):
    bounds = config["load"]
    return bounds["rates"] if "rates" in bounds else bounds["concurrency"]


def repeat_count(config):
    return config["load"].get("repeats", 1)


def attempt_limit(config):
    bounds = config["load"]
    return bounds.get("max_attempts_per_repeat", bounds.get("max_attempts_per_point", 3))


def attempt_prefix(index, repeat, repeats):
    point = f"point-{index + 1:02d}"
    return f"{point}-repeat-{repeat + 1:02d}" if repeats > 1 else point


def phase(config, point):
    bounds = config["load"]
    if "rates" in bounds:
        return {"type": bounds["arrival"], "rate": point,
                "concurrency": bounds["max_concurrency"], "requests": bounds["requests"]}
    return {"type": "concurrency", "concurrency": point, "requests": bounds["requests"]}


def load(path, smoke=False):
    return validate(json.loads(Path(path).read_text()), Path(path).resolve().parent, smoke)


def fields(value, allowed, path):
    """Reject misspellings before an optional setting can silently use its default."""
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    unknown = sorted(set(value) - set(allowed.split()))
    if unknown:
        raise ValueError("Unsupported field: " + ", ".join(f"{path}.{key}" for key in unknown))


def objects(value, path):
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{path} must be an array of objects")
    return enumerate(value)


def validate_fields(config):
    fields(config, "schema_version endpoint workload load metrics goals kubernetes record_processors", "config")
    fields(config.get("endpoint"), "url path models_path model api_key_env headers", "endpoint")
    fields(config.get("workload"), "type path sha256 input_tokens output_tokens tokenizer", "workload")
    fields(config.get("load"), "concurrency rates arrival max_concurrency requests duration_seconds "
           "request_timeout_seconds grace_seconds deadline_seconds repeats max_attempts_per_repeat max_attempts_per_point", "load")
    fields(config.get("goals", {}), "ttft_p95_ms latency_p95_ms max_error_fraction", "goals")
    for i, producer in objects(config.get("metrics", []), "metrics"):
        path = f"metrics[{i}]"
        fields(producer, "name url required", path)
        for j, requirement in objects(producer.get("required", []), path + ".required"):
            fields(requirement, "metric why", f"{path}.required[{j}]")
    if "kubernetes" in config:
        scope = config["kubernetes"]
        fields(scope, "context deployments routing", "kubernetes")
        for i, target in objects(scope.get("deployments", []), "kubernetes.deployments"):
            fields(target, "name namespace replicas", f"kubernetes.deployments[{i}]")
        if "routing" in scope:
            routing = scope["routing"]
            fields(routing, "namespace route pool model_deployment picker_deployment gateway "
                   "rule_index objectives selected_objective objective_header", "kubernetes.routing")
            fields(routing.get("gateway", {}), "name namespace section", "kubernetes.routing.gateway")
            for i, objective in objects(routing.get("objectives", []), "kubernetes.routing.objectives"):
                fields(objective, "name priority", f"kubernetes.routing.objectives[{i}]")


def validate(config, base, smoke=False):
    """Validate an in-memory config; resolve dataset paths from its source folder."""
    validate_fields(config)
    if config.get("schema_version") != 1:
        raise ValueError("Expected schema_version 1")
    positive(config.get("record_processors", 1), "record_processors", integer=True)
    if config.get("kubernetes"):
        scope = config["kubernetes"]
        if not isinstance(scope.get("context"), str) or not scope["context"] or not scope.get("deployments"):
            raise ValueError("kubernetes needs an explicit context and deployments")
        for target in scope["deployments"]:
            if not target.get("name") or not target.get("namespace"):
                raise ValueError("Each Deployment needs name and namespace")
            positive(target["replicas"], "replicas", integer=True)
        if scope.get("routing"):
            routing = scope["routing"]
            for key in ("namespace", "route", "pool", "model_deployment"):
                if not isinstance(routing.get(key), str) or not routing[key]:
                    raise ValueError(f"Set kubernetes.routing.{key}")
            if not routing.get("gateway", {}).get("name") or not routing["gateway"].get("namespace"):
                raise ValueError("routing.gateway needs name and namespace")
            verified = {(item["namespace"], item["name"]) for item in scope["deployments"]}
            for key in ("model_deployment", "picker_deployment"):
                if routing.get(key) and (routing["namespace"], routing[key]) not in verified:
                    raise ValueError(f"Include routing.{key} in kubernetes.deployments for readiness checks")
            if type(routing.get("rule_index", 0)) is not int or routing.get("rule_index", 0) < 0:
                raise ValueError("rule_index must be a nonnegative integer")
            names = set()
            for objective in routing.get("objectives", []):
                if not objective.get("name") or type(objective.get("priority")) is not int or objective["name"] in names:
                    raise ValueError("Objectives need distinct names and integer priorities")
                names.add(objective["name"])
            if routing.get("selected_objective") and routing["selected_objective"] not in names:
                raise ValueError("selected_objective must be listed in routing.objectives")
    endpoint = config["endpoint"]
    check_url(endpoint["url"])
    if urlsplit(endpoint["url"]).path not in ("", "/"):
        raise ValueError("Use endpoint.path for the API path")
    if not isinstance(endpoint["model"], str) or not endpoint["model"].strip():
        raise ValueError("Set endpoint.model to the served model name")
    for name in ("path", "models_path"):
        value = endpoint.get(name)
        if name == "models_path" and value is None:
            continue
        if not isinstance(value, str) or not value.startswith("/") or any(c in value for c in "?#\r\n") or value.startswith("//"):
            raise ValueError(f"Set endpoint.{name} to an absolute API path")
    if endpoint.get("api_key_env") and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", endpoint["api_key_env"]):
        raise ValueError("api_key_env must name an environment variable")
    for name, value in endpoint.get("headers", {}).items():
        if not re.fullmatch(r"[A-Za-z0-9-]+", name) or not isinstance(value, str) or "\n" in value or "\r" in value:
            raise ValueError("Invalid request header")
        if name.lower() in ("authorization", "cookie", "proxy-authorization"):
            raise ValueError("Use api_key_env for credentials")
    workload = config["workload"]
    if workload["type"] == "single_turn":
        data = Path(base) / workload["path"]
        lines = 0
        with data.open() as stream:
            for number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Dataset line {number}: {exc.msg}") from None
                if not isinstance(row, dict) or not isinstance(row.get("text"), str) or not row["text"]:
                    raise ValueError(f"Dataset line {number}: expected an object with nonempty text")
                if "output_length" in row:
                    positive(row["output_length"], "output_length", integer=True)
                lines += 1
        if not lines:
            raise ValueError("Dataset is empty")
        workload["path"] = str(data.resolve())
        workload["sha256"] = file_digest(data)
    elif workload["type"] == "synthetic":
        positive(workload["input_tokens"], "input_tokens", integer=True)
    else:
        raise ValueError("Supported workload types: synthetic, single_turn")
    positive(workload["output_tokens"], "output_tokens", integer=True)
    bounds = config["load"]
    if ("concurrency" in bounds) == ("rates" in bounds):
        raise ValueError("Choose exactly one load axis: concurrency or rates")
    rate_mode = "rates" in bounds
    axis = "rates" if rate_mode else "concurrency"
    points = bounds[axis]
    if not isinstance(points, list) or not points or len(points) != len(set(points)):
        raise ValueError(f"{axis} must be a nonempty list of distinct values")
    for point in points:
        positive(point, axis, integer=not rate_mode)
    if rate_mode:
        positive(bounds.get("max_concurrency"), "max_concurrency", integer=True)
        if bounds.get("arrival") not in ("constant", "poisson"):
            raise ValueError("Rate mode needs arrival: constant or poisson")
    elif "arrival" in bounds or "max_concurrency" in bounds:
        raise ValueError("arrival and max_concurrency apply only to rate mode")
    positive(bounds["requests"], "requests", integer=True)
    positive(repeat_count(config), "repeats", integer=True)
    if "max_attempts_per_repeat" in bounds and "max_attempts_per_point" in bounds:
        raise ValueError("Use max_attempts_per_repeat, not both attempt-limit names")
    positive(attempt_limit(config), "max_attempts_per_repeat", integer=True)
    for key in ("duration_seconds", "request_timeout_seconds", "grace_seconds", "deadline_seconds"):
        positive(bounds[key], key)
    if bounds["deadline_seconds"] <= bounds["duration_seconds"] + bounds["grace_seconds"]:
        raise ValueError("deadline_seconds must exceed duration plus grace; choose startup/export margin for your runtime")
    for metric in config.get("metrics", []):
        if not isinstance(metric, dict) or not isinstance(metric.get("url"), str) or not metric["url"]:
            raise ValueError("Each metrics producer needs a URL in metrics[].url")
        check_url(metric["url"])
        if not metric.get("name"):
            raise ValueError("Give each metrics producer a name")
        for requirement in metric.get("required", []):
            if not requirement.get("metric") or not requirement.get("why"):
                raise ValueError("Required metrics need metric and why")
    for key, value in config.get("goals", {}).items():
        if key not in ("ttft_p95_ms", "latency_p95_ms", "max_error_fraction"):
            raise ValueError(f"Unsupported goal: {key}")
        if key == "max_error_fraction":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                raise ValueError("max_error_fraction must be between 0 and 1")
        else:
            positive(value, key)
    if smoke:
        config["workload"] = {"type": "synthetic", "input_tokens": 16, "output_tokens": 16, "tokenizer": "builtin"}
        for key in ("rates", "arrival", "max_concurrency"):
            config["load"].pop(key, None)
        config["load"].update(concurrency=[1], requests=1)
        if "repeats" in config["load"]:
            config["load"]["repeats"] = 1
    return config
