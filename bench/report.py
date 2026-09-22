"""Read saved results without changing evidence; summarize accepted repeats."""

from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re

from .provenance import timestamp

ATTEMPT = re.compile(r"^(point|row)-(\d+)(?:-repeat-(\d+))?-attempt-\d+$")
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
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def span(values):
    known = [v for v in values if number(v)]
    if not known:
        return "unknown"
    lo, hi = min(known), max(known)
    def display(value):
        # Preserve ordinary token counts and millisecond values without exponent notation.
        return f"{value:,.3f}".rstrip("0").rstrip(".") if .001 <= value < 1e9 else f"{value:.3g}"
    low, high = display(lo), display(hi)
    text = low if low == high else f"{low}–{high}"
    return text if len(known) == len(values) else text + " (partial)"


def read_report(root):
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("Saved run directory not found. Set RUN to an existing benchmark directory.")
    issues = []

    def read(path, expected=dict, required=True):
        try:
            if not path.resolve().is_relative_to(root):
                raise ValueError("Artifact points outside the run directory")
            if not required and not path.exists():
                return expected()
            value = json.loads(path.read_text())
            if not isinstance(value, expected):
                raise ValueError("Unexpected shape")
            return value
        except (OSError, ValueError, RuntimeError):
            # Exception strings can contain endpoint responses, credentials or private paths.
            issues.append(f"{clean(path.relative_to(root))}: missing, unreadable or malformed (including external artifact paths)")
            return expected()

    state = read(root / "state.json")
    config = read(root / "config.json")
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
    slots = {name: (int(ATTEMPT.fullmatch(name)[2]), int(ATTEMPT.fullmatch(name)[3] or 1)) for name in accepted}
    slot_counts = Counter(slots.values())
    load = config.get("load", {})
    repeats = config.get("repeats") if matrix else load.get("repeats", 1) if isinstance(load, dict) else None
    expected_count = planned(config, matrix)
    for name, (point, repeat) in slots.items():
        wrong_slot = point < 1 or repeat < 1 or ATTEMPT.fullmatch(name)[1] != ("row" if matrix else "point")
        if type(expected_count) is int and type(repeats) is int:
            wrong_slot |= point > expected_count // repeats or repeat > repeats
        if wrong_slot or slot_counts[(point, repeat)] > 1:
            issues.append(f"{name}: duplicate or out-of-range repeat slot")
            accepted.remove(name)
    if state.get("status") == "complete" and type(expected_count) is int and len(accepted) != expected_count:
        issues.append("state.json: complete status disagrees with planned repeat count")
    directories = sorted(p for p in root.iterdir() if p.is_dir() and ATTEMPT.fullmatch(p.name))
    existing = {p.name for p in directories}
    for name in sorted(accepted - existing):
        issues.append(f"{name}: accepted attempt directory missing")
    rows, notices, windows = {}, Counter(), []
    unaccepted, usable = 0, 0
    goal_counts = Counter()
    for directory in directories:
        match = ATTEMPT.fullmatch(directory.name)
        point = int(match[2])
        group = read(directory / "summary.json")
        result["groups" if matrix else "attempts"][directory.name] = group
        streams = group.get("streams", {}) if matrix else {"workload": group}
        is_accepted = directory.name in accepted and group.get("evidence") == "complete"
        if matrix and is_accepted:
            # A checkpoint accepts the whole mixed repeat, never one successful peer.
            coherent = (isinstance(streams, dict) and bool(streams)
                        and all(isinstance(s, dict) and s.get("evidence") == "complete" for s in streams.values()))
            expected = row_config(config, point).get("streams")
            if isinstance(expected, dict) and isinstance(streams, dict):
                coherent = coherent and set(streams) == set(expected)
            if not coherent:
                issues.append(f"{directory.name}: accepted group has missing or incomplete stream evidence")
                is_accepted = False
        if directory.name in accepted and not is_accepted:
            issues.append(f"{directory.name}: checkpoint references incomplete evidence")
        scope_note = "accepted repeat" if is_accepted else "unaccepted attempt"
        usable += int(is_accepted)
        if not is_accepted:
            unaccepted += 1
            reasons = group.get("reasons", [])
            if isinstance(reasons, list):
                for reason in reasons:
                    notices[f"{clean(reason)} (unaccepted attempt)"] += 1
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
        if matrix and is_accepted and len(streams) > 1:
            if (not isinstance(shared, dict) or set(shared) != set(streams)
                    or any(not isinstance(s, dict) for s in shared.values())):
                issues.append(f"{directory.name}: missing or malformed shared-arrival evidence")
            for stream, summary in (shared.items() if isinstance(shared, dict) else []):
                if stream in streams and isinstance(summary, dict):
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
    result["usable_checkpointed_repeats"] = usable
    result["reported"] = timestamp()
    return result, config, rows, notices, goal_counts, windows, unaccepted


def planned(config, matrix):
    try:
        if matrix:
            points, repeats = config["rows"], config["repeats"]
        else:
            load = config["load"]
            points, repeats = load.get("rates", load.get("concurrency")), load.get("repeats", 1)
        if isinstance(points, list) and points and type(repeats) is int and repeats > 0:
            return len(points) * repeats
    except (KeyError, TypeError, AttributeError):
        pass
    return "unknown"


def row_config(config, point):
    rows = config.get("rows")
    if isinstance(rows, list) and 1 <= point <= len(rows) and isinstance(rows[point - 1], dict):
        return rows[point - 1]
    return {}


def load_label(config, point, stream, matrix):
    try:
        if point < 1:
            return "unknown"
        if matrix:
            load = config["rows"][point - 1]["streams"][stream]["load"]
            value = load.get("rates", load.get("concurrency"))[0]
        else:
            load = config["load"]
            value = load.get("rates", load.get("concurrency"))[point - 1]
        if not number(value):
            return "unknown"
        if "rates" in load:
            cap = load.get("max_concurrency")
            return (f"{value:g} req/s ({clean(load.get('arrival', 'unknown'))}; "
                    f"cap {cap:g})" if number(cap) else f"{value:g} req/s (cap unknown)")
        return f"{value:g} concurrent"
    except (KeyError, TypeError, IndexError, AttributeError):
        return "unknown"


def goal_table(rows):
    lines = []
    for (point, stream, scope), summaries in sorted(rows.items()):
        goals = {}
        for summary in summaries:
            entries = summary.get("goals", [])
            for goal in entries if isinstance(entries, list) else []:
                if isinstance(goal, dict) and isinstance(goal.get("goal"), str):
                    goals.setdefault(clean(goal["goal"]), []).append(goal)
        for name, values in sorted(goals.items()):
            statuses = Counter(clean(g.get("status", "unverified")) for g in values)
            status = ", ".join(f"{n} {s}" for s, n in sorted(statuses.items()))
            lines.append(f"| {point} | {stream} | {scope} | {name} | "
                         f"{span([g.get('limit') for g in values])} | {span([g.get('observed') for g in values])} | {status} |")
    if not lines:
        return []
    return ["", "## Goal evaluations", "",
            "| Point/row | Workload | Scope | Metric | Limit (≤) | Observed | Evaluations |",
            "|---|---|---|---|---:|---:|---|", *lines, "",
            "Latency goals use ms; max_error_fraction is failed/total requests (0–1). Unaccepted evaluations do not establish a pass."]


def format_report(data):
    result, config, rows, notices, goals, windows, unaccepted = data
    state = result["state"]
    matrix = "groups" in result
    count = result["usable_checkpointed_repeats"]
    lines = ["# Benchmark report", "", f"Generated: {result['reported']['timestamp_utc']}",
             f"Saved status: {clean(result['status'])}",
             f"Progress: {count}/{planned(config, matrix)} checkpointed repeats; {unaccepted} unaccepted attempts",
             "Coverage: partial report" if result["report_issues"] else "Coverage: saved summaries read", ""]
    digest = state.get("config_hash")
    if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
        lines.append(f"Saved configuration SHA-256: {digest}")
    if matrix:
        lines += [f"Campaign: {clean(config.get('name', 'unknown'))}", "", "## Experiment", "",
                  "| Row | Stage | Serving profile | Question | Change |",
                  "|---:|---|---|---|---|"]
        for point in sorted({key[0] for key in rows}):
            row = row_config(config, point)
            lines.append(f"| {point} | {clean(row.get('stage', 'unknown'))} | "
                         f"{clean(row.get('profile') or 'not recorded')} | "
                         f"{clean(row.get('question', 'unknown'))} | {clean(row.get('change', 'unknown'))} |")
        lines.append("")
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
    lines += goal_table(rows)
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
