"""Read saved results without changing evidence; summarize accepted repeats."""

from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re

from .provenance import timestamp

ATTEMPT = re.compile(r"^(point|row)-(\d+)(?:-repeat-\d+)?-attempt-\d+$")
METRICS = ("ttft_p95_ms", "latency_p95_ms", "request_throughput_rps")
NATIVE_METRICS = (
    ("inter_token_latency", "p95", "ms", "itl_p95_ms"),
    ("output_token_throughput", "avg", "tokens/sec", "output_tokens_per_second"),
    ("input_sequence_length", "avg", "tokens", "input_tokens_mean"),
    ("output_sequence_length", "avg", "tokens", "output_tokens_mean"),
    ("benchmark_duration", "avg", "sec", "duration_seconds"),
)


def clean(value):
    """Keep saved labels from injecting terminal escapes or Markdown rows."""
    return re.sub(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069|`<>]", " ", str(value))[:120]


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def span(values):
    known = [v for v in values if number(v)]
    if not known:
        return "unknown"
    lo, hi = min(known), max(known)
    text = f"{lo:.3g}" if lo == hi else f"{lo:.3g}–{hi:.3g}"
    return text if len(known) == len(values) else text + " (partial)"


def read_report(root):
    root = Path(root)
    if not root.is_dir():
        raise ValueError("Saved run directory not found. Set RUN to an existing benchmark directory.")
    issues = []

    def read(path, expected=dict, required=True):
        if not required and not path.exists():
            return expected()
        try:
            value = json.loads(path.read_text())
            if not isinstance(value, expected):
                raise ValueError("Unexpected shape")
            return value
        except (OSError, ValueError):
            # Exception strings can contain endpoint responses, credentials or private paths.
            issues.append(f"{clean(path.relative_to(root))}: missing, unreadable or malformed")
            return expected()

    state = read(root / "state.json")
    config = read(root / "config.json", required=False)
    result = {"state": state, "status": state.get("status", "unknown"), "attempts": {}, "points": {}}
    matrix = state.get("kind") == "matrix" or isinstance(config.get("rows"), list)
    if matrix:
        result["groups"] = {}
    completed = state.get("completed", [])
    if not isinstance(completed, list) or any(not isinstance(v, str) or not ATTEMPT.fullmatch(v) for v in completed):
        issues.append("state.json: invalid completed-attempt list")
        completed = []
    if len(set(completed)) != len(completed):
        issues.append("state.json: duplicate completed attempts")
    accepted = set(completed)
    directories = sorted(p for p in root.iterdir() if p.is_dir() and ATTEMPT.fullmatch(p.name))
    existing = {p.name for p in directories}
    for name in sorted(accepted - existing):
        issues.append(f"{name}: accepted attempt directory missing")
    rows, notices, windows = {}, Counter(), []
    unaccepted = 0
    goal_counts = Counter()
    for directory in directories:
        match = ATTEMPT.fullmatch(directory.name)
        point = int(match[2])
        group = read(directory / "summary.json")
        result["groups" if matrix else "attempts"][directory.name] = group
        is_accepted = directory.name in accepted and group.get("evidence") == "complete"
        if directory.name in accepted and not is_accepted:
            issues.append(f"{directory.name}: checkpoint references incomplete evidence")
        scope_note = "accepted repeat" if is_accepted else "unaccepted attempt"
        if not is_accepted:
            unaccepted += 1
            reasons = group.get("reasons", [])
            if isinstance(reasons, list):
                for reason in reasons:
                    notices[f"{clean(reason)} (unaccepted attempt)"] += 1
        streams = group.get("streams", {}) if matrix else {"workload": group}
        if not isinstance(streams, dict):
            issues.append(f"{directory.name}: malformed streams")
            continue
        # Preflight may fail before a summary or any stream directory is written.
        check_dirs = [directory] + ([directory / name for name in streams
                                     if re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", name)] if matrix else [])
        for check_dir in check_dirs:
            for filename in ("preflight.json", "postflight.json"):
                checks = read(check_dir / filename, list, required=False)
                for check in checks:
                    if isinstance(check, dict) and check.get("status") in ("fail", "unverified"):
                        notices[f"{clean(check.get('name', 'check'))}: {clean(check['status'])} ({filename}; {scope_note})"] += 1
        for stream, summary in streams.items():
            if not isinstance(summary, dict):
                issues.append(f"{directory.name}: malformed stream summary")
                continue
            coverage = summary.get("metrics", [])
            if not isinstance(coverage, list):
                issues.append(f"{directory.name}: malformed metric coverage")
                coverage = []
            if not coverage:
                notices[f"Server metric coverage not recorded ({scope_note})"] += 1
            for producer in coverage:
                if not isinstance(producer, dict):
                    issues.append(f"{directory.name}: malformed producer coverage")
                    continue
                missing = producer.get("missing_samples", [])
                if producer.get("window_covered") is not True or missing:
                    detail = ", ".join(clean(v) for v in missing) if isinstance(missing, list) else "unknown"
                    notices[f"{clean(producer.get('producer', 'producer'))}: collection incomplete" +
                            (f"; missing {detail}" if detail else "") + f" ({scope_note})"] += 1
            if matrix and is_accepted and summary.get("evidence") != "complete":
                issues.append(f"{directory.name}: accepted group has incomplete stream evidence")
                continue
            scope = "full run" if is_accepted else "unaccepted"
            # Display-only enrichment must not change the saved validation summary.
            observation = {**summary, "native_metrics": {}}
            rows.setdefault((point, clean(stream), scope), []).append(observation)
            goals = summary.get("goals", [])
            if is_accepted and isinstance(goals, list):
                for goal in goals:
                    if isinstance(goal, dict):
                        goal_counts[clean(goal.get("status", "unverified"))] += 1
            child = directory / stream if matrix else directory
            # Never follow stream names from damaged data outside the attempt directory.
            if matrix and not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", stream):
                continue
            native = read(child / "native/profile_export_aiperf.json", required=False)
            native_metrics = {}
            for metric, statistic, unit, key in NATIVE_METRICS:
                item = native.get(metric)
                if item is None:
                    continue
                if not isinstance(item, dict) or item.get("unit") != unit or not number(item.get(statistic)):
                    notices[f"{clean(stream)}: native {metric}.{statistic} unavailable: expected finite nonnegative {unit} ({scope_note})"] += 1
                    continue
                native_metrics[key] = item[statistic]
            observation["native_metrics"] = native_metrics
            result.setdefault("native_metrics", {}).setdefault(directory.name, {})[stream] = native_metrics
            if is_accepted and native:
                try:
                    times = [datetime.fromisoformat(native[key]) for key in ("start_time", "end_time")]
                    if (times[0].tzinfo is None) != (times[1].tzinfo is None) or times[1] < times[0]:
                        raise ValueError("Invalid window")
                    # Some pinned exports omit the offset. Never silently assume UTC.
                    zone = "timezone not exported" if times[0].tzinfo is None else "UTC"
                    if zone == "UTC":
                        times = [t.astimezone(timezone.utc) for t in times]
                    windows.append((*[t.isoformat() for t in times], zone))
                except (KeyError, TypeError, ValueError):
                    notices["Native run window unavailable"] += 1
        shared = group.get("shared_window", {})
        if matrix and is_accepted and len(streams) > 1 and isinstance(shared, dict):
            for stream, summary in shared.items():
                if isinstance(summary, dict):
                    rows.setdefault((point, clean(stream), "shared arrivals"), []).append(summary)
                    for goal in summary.get("goals", []) if isinstance(summary.get("goals", []), list) else []:
                        if isinstance(goal, dict):
                            goal_counts[clean(goal.get("status", "unverified"))] += 1
    for path in sorted(root.glob("point-*-repeats.json")):
        result["points"][path.stem] = read(path)
    # A changing checkpoint cannot be presented as a consistent completed snapshot.
    if read(root / "state.json") != state:
        issues.append("Checkpoint changed while reading; regenerate the report")
    result["report_issues"] = sorted(set(issues))
    result["reported"] = timestamp()
    return result, config, rows, notices, goal_counts, windows, unaccepted


def planned(config, matrix):
    try:
        if matrix:
            return len(config["rows"]) * config["repeats"]
        load = config["load"]
        return len(load.get("rates", load.get("concurrency", []))) * load.get("repeats", 1)
    except (KeyError, TypeError, AttributeError):
        return "unknown"


def load_label(config, point, stream, matrix):
    try:
        if matrix:
            load = config["rows"][point - 1]["streams"][stream]["load"]
            value = load.get("rates", load.get("concurrency"))[0]
        else:
            load = config["load"]
            value = load.get("rates", load.get("concurrency"))[point - 1]
        if not number(value):
            return "unknown"
        return f"{value:g} req/s" if "rates" in load else f"{value:g} concurrent"
    except (KeyError, TypeError, IndexError, AttributeError):
        return "unknown"


def format_report(data):
    result, config, rows, notices, goals, windows, unaccepted = data
    state = result["state"]
    matrix = "groups" in result
    completed = state.get("completed")
    count = len(set(v for v in completed if isinstance(v, str))) if isinstance(completed, list) else "unknown"
    lines = ["# Benchmark report", "", f"Generated: {result['reported']['timestamp_utc']}",
             f"Saved status: {clean(result['status'])}",
             f"Progress: {count}/{planned(config, matrix)} checkpointed repeats; {unaccepted} unaccepted attempts",
             "Coverage: partial report" if result["report_issues"] else "Coverage: saved summaries read", ""]
    workload = config.get("workload", {})
    if not matrix and isinstance(workload, dict):
        inp, out = workload.get("input_tokens"), workload.get("output_tokens")
        if number(inp) and number(out):
            lines.append(f"Requested shape: {inp:g} input / {out:g} output tokens")
    if windows and len({w[2] for w in windows}) == 1:
        lines.append(f"Available accepted run window: {min(w[0] for w in windows)} to {max(w[1] for w in windows)} ({windows[0][2]})")
    else:
        lines.append("Accepted run window: unknown")
    lines += ["", "## Results", "",
              "| Point/row | Workload | Load | Scope | Runs | Failed/requests | TTFT p95 ms | Request p95 ms | Requests/s |",
              "|---|---|---|---|---:|---:|---:|---:|---:|"]
    for (point, stream, scope), summaries in sorted(rows.items()):
        counts = [s.get("requests") for s in summaries]
        failed = [s.get("failed_requests") for s in summaries]
        totals = (f"{sum(failed)}/{sum(counts)}" if all(type(v) is int and v >= 0 for v in counts + failed)
                  else "unknown")
        values = []
        for metric in METRICS:
            measurements = [s.get("measurements", {}) for s in summaries]
            values.append(span([m.get(metric) if isinstance(m, dict) else None for m in measurements]))
        lines.append(f"| {point} | {stream} | {load_label(config, point, stream, matrix)} | {scope} | {len(summaries)} | {totals} | " + " | ".join(values) + " |")
    if not rows:
        lines.append("\nNo measurements available. See checks and detailed JSON for saved attempts.")
    lines += ["", "Ranges show per-run min–max, not pooled percentiles. Unknown is not zero. Unaccepted observations do not count toward repeats."]
    if matrix:
        lines.append("Shared arrivals are a subset of full runs; do not add their counts. Shared throughput is not calculated.")
    native_rows = [(key, summaries) for key, summaries in sorted(rows.items()) if key[2] != "shared arrivals"]
    if native_rows:
        lines += ["", "## Streaming and workload", "",
                  "| Point/row | Workload | Scope | ITL p95 ms | Output tokens/s | Input tokens mean | Output tokens mean | Duration s |",
                  "|---|---|---|---:|---:|---:|---:|---:|"]
        for (point, stream, scope), summaries in native_rows:
            values = [span([s.get("native_metrics", {}).get(key) for s in summaries])
                      for _, _, _, key in NATIVE_METRICS]
            lines.append(f"| {point} | {stream} | {scope} | " + " | ".join(values) + " |")
        lines += ["", "Native AIPerf full-run statistics; ranges span repeats. ITL is inter-token latency. Token lengths are observed means, not requested limits. Duration is the native benchmark duration.",
                  "Cache-hit rate and server queue statistics are not calculated. Check serving metrics over the same run windows."]
    lines += ["", "## Checks", ""]
    lines.append("Goals: " + (", ".join(f"{n} {status}" for status, n in sorted(goals.items())) if goals else "none evaluated"))
    lines.extend(f"- {item} [{n} checks]" for item, n in sorted(notices.items()))
    lines.extend(f"- {item}" for item in result["report_issues"])
    if not notices and not result["report_issues"]:
        lines.append("No collection gaps recorded in the saved summaries. Labels and policy attribution still need review.")
    status = result["status"]
    if result["report_issues"]:
        action = "Inspect the listed missing/damaged artifacts; regenerate after writes finish. Do not infer a pass from partial evidence."
    elif status == "complete":
        action = "Review coverage and per-repeat results against the experiment question before choosing another bounded test."
    elif status in ("goal_not_met", "request_errors"):
        action = "Keep these valid results. Review goal misses/errors before deliberately advancing; do not retry to obtain a pass."
    elif status in ("running", "checking"):
        action = "This is a saved checkpoint, not proof of a live process. Inspect ownership before recovery."
    elif status == "paused":
        action = "Review results; resume only with unchanged inputs and intact evidence."
    else:
        action = "Diagnose the saved stop reason and checks before resuming. Changed inputs require a new experiment."
    lines += ["", "## Next", "", action,
              "", "Review labels before sharing. Detailed JSON may contain operational data."]
    return "\n".join(lines)
