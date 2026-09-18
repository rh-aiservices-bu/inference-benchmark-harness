"""Separate evidence completeness, request outcomes and declared goals."""

import json
import math
import os
from datetime import datetime
from pathlib import Path

from . import AIPERF_VERSION
from .config import file_digest
from .provenance import artifact, artifact_name, operation


def finite_nonnegative(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def exported_metric_names(metrics, definitions):
    """Map valid native samples to raw scrape names using exported metric types."""
    names = set()
    for name, series in metrics.items():
        kind = definitions.get(name, {}).get("type")
        for sample in series:
            value = sample.get("value")
            if type(value) in (int, float) and math.isfinite(value):
                names.add(name)
                if kind == "counter":
                    names.add(name + "_total")
            if kind == "histogram":
                for suffix in ("sum", "count"):
                    if finite_nonnegative(sample.get(suffix)):
                        names.add(name + "_" + suffix)
                buckets = sample.get("buckets", {})
                if buckets and all(finite_nonnegative(value) for value in buckets.values()):
                    names.add(name + "_bucket")
    return names


def write_json(path, value):
    path = Path(path)
    with operation("save_json", artifact=artifact_name(path)) as saved:
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    artifact(path, save_window=saved)



def manifest(directory):
    return {str(path.relative_to(directory)): file_digest(path)
            for path in sorted(directory.rglob("*")) if path.is_file() and path != directory / "manifest.json"}


def analyze(directory, config, execution):
    invalid = []
    rows = []
    native = directory / "native"
    summary = {}
    failed = 0
    try:
        summary = json.loads((native / "profile_export_aiperf.json").read_text())
        with (native / "profile_export.jsonl").open() as stream:
            rows = [json.loads(line) for line in stream if line.strip()]
        if summary.get("aiperf_version") != AIPERF_VERSION or summary.get("schema_version") != "1.4":
            invalid.append("unsupported_export_version")
        if summary.get("was_cancelled"):
            invalid.append("native_cancelled")
        good = sum(not row.get("error") and not row.get("metadata", {}).get("was_cancelled")
                   and "request_latency" in row.get("metrics", {}) for row in rows)
        failed = len(rows) - good
        if not rows or len(rows) > config["load"]["requests"]:
            invalid.append("request_count_out_of_bounds")
        if 0 < len(rows) < config["load"]["requests"]:
            measured_seconds = (datetime.fromisoformat(summary["end_time"]) - datetime.fromisoformat(summary["start_time"])).total_seconds()
            if measured_seconds < config["load"]["duration_seconds"]:
                invalid.append("stopped_before_request_or_time_limit")
        aggregate_good = summary.get("request_count", {}).get("avg", 0)
        aggregate_bad = summary.get("error_request_count", {}).get("avg", 0)
        if any(not finite_nonnegative(value) or not float(value).is_integer()
               for value in (aggregate_good, aggregate_bad)):
            invalid.append("invalid_request_counts")
        if aggregate_good != good or aggregate_bad != failed:
            invalid.append("request_counts_disagree")
        for row in rows:
            metadata = row.get("metadata", {})
            start, end = metadata.get("request_start_ns"), metadata.get("request_end_ns")
            if type(start) is not int or type(end) is not int or start <= 0 or end < start:
                invalid.append("invalid_request_timestamps")
            if not row.get("error") and "request_latency" not in row.get("metrics", {}):
                invalid.append("incomplete_request_record")
            if not row.get("error"):
                latency = row.get("metrics", {}).get("request_latency", {})
                if not isinstance(latency, dict) or latency.get("unit") != "ms" or not finite_nonnegative(latency.get("value")):
                    invalid.append("invalid_request_latency")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        invalid.append("missing_or_malformed_client_evidence")
    if execution["exit_code"] != 0:
        invalid.append("client_process_failed")
    if execution.get("reason"):
        invalid.append(execution["reason"])
    coverage = []
    endpoint_samples, endpoint_info = {}, {}
    if config.get("metrics"):
        try:
            server = json.loads((native / "server_metrics_export.json").read_text())
            endpoint_info = server["summary"]["endpoint_info"]
            definitions = server.get("metrics", {})
            with (native / "server_metrics_export.jsonl").open() as stream:
                for line in stream:
                    if line.strip():
                        sample = json.loads(line)
                        if type(sample["timestamp_ns"]) is not int or sample["timestamp_ns"] <= 0:
                            raise ValueError("Invalid scrape timestamp")
                        names = exported_metric_names(sample["metrics"], definitions)
                        endpoint_samples.setdefault(sample["endpoint_url"], set()).update(names)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            endpoint_samples, endpoint_info = {}, {}
    for producer in config.get("metrics", []):
        covered = False
        try:
            fetches = endpoint_info[producer["url"]]
            starts = [row["metadata"]["request_start_ns"] for row in rows]
            ends = [row["metadata"]["request_end_ns"] for row in rows]
            # JSONL stores changed values; the fetch timeline includes unchanged scrapes.
            covered = (bool(endpoint_samples.get(producer["url"]))
                       and type(fetches["total_fetches"]) is int and fetches["total_fetches"] >= 2
                       and all(type(fetches[key]) is int and fetches[key] > 0
                               for key in ("first_fetch_ns", "last_fetch_ns"))
                       and fetches["first_fetch_ns"] <= min(starts)
                       and fetches["last_fetch_ns"] >= max(ends))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        missing = [item["metric"] for item in producer.get("required", [])
                   if item["metric"] not in endpoint_samples.get(producer["url"], set())]
        coverage.append({"producer": producer["name"], "window_covered": covered, "missing_samples": missing})
        if missing:
            invalid.append("required_metric_samples_missing:" + producer["name"])
        if producer.get("required") and not covered:
            invalid.append("required_metrics_window_incomplete:" + producer["name"])
    measurements = {"error_fraction": failed / len(rows) if rows else None}
    for name, key, statistic, unit in (
            ("ttft_p95_ms", "time_to_first_token", "p95", "ms"),
            ("latency_p95_ms", "request_latency", "p95", "ms"),
            ("request_throughput_rps", "request_throughput", "avg", "requests/sec")):
        metric = summary.get(key, {}) if isinstance(summary, dict) else {}
        value = metric.get(statistic) if isinstance(metric, dict) and metric.get("unit") == unit else None
        measurements[name] = value if finite_nonnegative(value) else None
        if isinstance(summary, dict) and key in summary and not finite_nonnegative(value):
            invalid.append("invalid_aggregate_measurement:" + key)
    goals = []
    for name, bound in config.get("goals", {}).items():
        if name == "max_error_fraction":
            measured = failed / len(rows) if rows else None
        else:
            key = "time_to_first_token" if name == "ttft_p95_ms" else "request_latency"
            metric = summary.get(key, {}) if isinstance(summary, dict) else {}
            measured = metric.get("p95") if isinstance(metric, dict) and metric.get("unit") == "ms" else None
        if measured is not None and not finite_nonnegative(measured):
            invalid.append("invalid_goal_measurement:" + name)
            measured = None
        goals.append({"goal": name, "limit": bound, "observed": measured,
                      "status": "unverified" if measured is None else "met" if measured <= bound else "missed"})
    if invalid:
        for goal in goals:
            goal["status"] = "unverified"
    return {"evidence": "invalid" if invalid else "complete", "reasons": sorted(set(invalid)),
            "requests": None if "missing_or_malformed_client_evidence" in invalid else len(rows),
            "failed_requests": None if "missing_or_malformed_client_evidence" in invalid else failed,
            "requested_limit": config["load"]["requests"], "metrics": coverage, "goals": goals,
            "measurements": measurements,
            "claim": "Observed client behavior for this workload and run location; no policy attribution"}
