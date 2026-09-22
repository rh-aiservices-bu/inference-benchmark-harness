"""Command-line entry point for the benchmark harness."""

import argparse
import json
import os
from pathlib import Path
import re
import signal
import sys
import textwrap

from .config import attempt_limit, attempt_prefix, load, load_points, repeat_count
from .preflight import verify
from .runner import campaign, command
from .provenance import timestamp


def verification_coverage(config):
    """Expose skipped inspection without changing the runner's acceptance rules."""
    gaps = []
    if not config.get("kubernetes"):
        gaps.append(("kubernetes", "Not configured; deployment readiness and identity are unverified."))
    if not config.get("kubernetes", {}).get("routing"):
        gaps.append(("routing", "Not configured; gateway, pool and objective bindings are unverified."))
    if not config.get("metrics"):
        gaps.append(("server_metrics", "No producers configured; server metrics will not be collected."))
    for producer in config.get("metrics", []):
        if not producer.get("required"):
            gaps.append((f"metric_requirements:{producer['name']}",
                         "No required series configured; missing evidence from this producer will not block acceptance."))
    return [{"name": name, "status": "unverified", "detail": detail} for name, detail in gaps]


def format_verification(result, color=False):
    """Render the same checks as JSON; warnings never turn failures into passes."""
    styles = {"pass": ("PASS", 32), "unverified": ("WARN", 33), "fail": ("FAIL", 31)}

    def clean(value):
        # Endpoint output is untrusted: do not let control characters style the terminal.
        return re.sub(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]", " ", str(value))

    rows = result["checks"] + result["coverage"]
    names = [clean(f"{c['namespace']}/{c['name']}" if c.get("namespace") else c["name"]) for c in rows]
    lines = [f"Verification · {result['reported']['timestamp_utc']}", ""]
    for check, name in zip(rows, names):
        label, code = styles.get(check["status"], ("WARN", 33))
        tag = f"\033[{code}m{label}\033[0m" if color else label
        detail = check.get("detail", "")
        if isinstance(detail, dict):
            detail = json.dumps(detail, sort_keys=True)
        details = [clean(detail)] if detail else []
        if "expected_replicas" in check:
            details.insert(0, f"{check['observed_pods']} active pods; expected {check['expected_replicas']}")
        if "metric_names" in check:
            details.insert(0, f"{len(check['metric_names'])} metric names discovered")
        if check.get("missing"):
            details.append("Missing: " + ", ".join(clean(item["metric"]) for item in check["missing"]))
        if check.get("attempts", 1) > 1:
            details.append(f"{check['attempts']} read attempts")
        lines.append(f"{tag}  {name}")
        lines.extend(textwrap.wrap(". ".join(details), width=100, initial_indent="      ", subsequent_indent="      "))
    failed = result["status"] == "preflight_failed"
    lines.extend(["", "BLOCKED — resolve FAIL checks, then run verify again." if failed else
                  "READY FOR SMOKE — review WARN checks before sending one request.",
                  "No inference sent. PASS covers the named check only; request classification still needs smoke evidence."])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    from .configure import add_parser, create_config
    add_parser(sub)
    for name in ("plan", "verify", "run"):
        child = sub.add_parser(name)
        child.add_argument("--config", required=True)
        child.add_argument("--aiperf", default="aiperf")
        child.add_argument("--smoke", action="store_true")
        if name == "verify":
            child.add_argument("--format", choices=("auto", "text", "json"), default="auto",
                               help="auto: readable terminal output, JSON when redirected")
        if name != "verify":
            child.add_argument("--run", required=True)
        if name == "run":
            child.add_argument("--execute", action="store_true", required=True)
            child.add_argument("--resume", action="store_true")
    report = sub.add_parser("report")
    report.add_argument("--run", required=True)
    pause = sub.add_parser("matrix-pause")
    pause.add_argument("--run", required=True)
    for name in ("matrix-plan", "matrix-run"):
        child = sub.add_parser(name)
        child.add_argument("--config", required=True)
        child.add_argument("--run", required=True)
        child.add_argument("--aiperf", default="aiperf")
        if name == "matrix-run":
            child.add_argument("--execute", action="store_true", required=True)
            child.add_argument("--resume", action="store_true")
    create = sub.add_parser("matrix-create", help="Generate and validate one load sweep without traffic")
    create.add_argument("--output", required=True, help="New matrix file; never overwrites")
    create.add_argument("--name", required=True, help="Short lowercase experiment name")
    create.add_argument("--question", required=True, help="Question this experiment answers")
    create.add_argument("--stream", action="append", required=True, metavar="NAME=CONFIG", help="Repeat for each workload config; paths relative to current directory")
    create.add_argument("--sweep", required=True, help="Stream whose load changes")
    create.add_argument("--values", required=True, help="Comma-separated load points, in execution order")
    create.add_argument("--hold", action="append", default=[], metavar="NAME=VALUE", help="One fixed load for every other stream")
    create.add_argument("--axis", choices=("concurrency", "rates"), required=True)
    create.add_argument("--arrival", choices=("constant", "poisson"))
    create.add_argument("--max-concurrency", type=int)
    create.add_argument("--repeats", type=int, default=3)
    create.add_argument("--max-attempts-per-repeat", type=int, default=3)
    create.add_argument("--min-overlap-seconds", type=float, default=5)
    args = parser.parse_args()
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        if args.action == "configure":
            result = create_config(args)
        elif args.action == "matrix-create":
            from .generate import create_matrix
            result = create_matrix(args)
        elif args.action == "matrix-pause":
            from .evidence import write_json
            root = Path(args.run).resolve()
            state = json.loads((root / "state.json").read_text())
            if state.get("kind") != "matrix" or state["status"] == "complete":
                raise ValueError("Choose an unfinished matrix")
            write_json(root / "pause-request.json", {"requested": timestamp()})
            result = {"status": "pause_requested", "detail": "Finish and validate the current group, then pause before starting another"}
        elif args.action in ("matrix-plan", "matrix-run"):
            from .matrix import load_matrix, matrix_campaign, plan_matrix
            acquired = {"started": timestamp()}
            config = load_matrix(args.config)
            acquired["finished"] = timestamp()
            result = (plan_matrix(config, args.run, args.aiperf) if args.action == "matrix-plan" else
                      matrix_campaign(config, args.run, args.aiperf, args.resume, acquired))
        elif args.action == "report":
            root = Path(args.run)
            result = {"state": json.loads((root / "state.json").read_text()),
                      "attempts": {str(path.parent.name): json.loads(path.read_text()) for path in sorted(root.glob("point-*/summary.json"))}}
            result["points"] = {path.stem: json.loads(path.read_text()) for path in sorted(root.glob("point-*-repeats.json"))}
            result["status"] = result["state"]["status"]
            if result["state"].get("kind") == "matrix":
                result["groups"] = {p.parent.name: json.loads(p.read_text()) for p in sorted(root.glob("row-*/summary.json"))}
        else:
            acquired = {"started": timestamp()}
            config = load(args.config, args.smoke)
            acquired["finished"] = timestamp()
            if args.action == "plan":
                points, bounds = load_points(config), config["load"]
                repeats = repeat_count(config)
                requests = len(points) * repeats * bounds["requests"]
                result = {
                    "status": "plan_only",
                    "budget": {
                        "points": len(points), "repeats_per_point": repeats,
                        "requests_per_repeat": bounds["requests"], "requests_per_point": bounds["requests"] * repeats,
                        "max_requests_first_pass": requests,
                        "max_requests_with_manual_retries": requests * attempt_limit(config),
                        "process_deadline_seconds_per_attempt": bounds["deadline_seconds"],
                        "startup_export_margin_seconds": bounds["deadline_seconds"] - bounds["duration_seconds"] - bounds["grace_seconds"],
                        "automatic_inference_retries": 0,
                    },
                    "commands": [command(config, point, Path(args.run).resolve() / f"{attempt_prefix(index, repeat, repeats)}-attempt-001", args.aiperf)
                                 for index, point in enumerate(points) for repeat in range(repeats)],
                }
            elif args.action == "verify":
                result = {"checks": verify(config, args.aiperf), "coverage": verification_coverage(config)}
                result["status"] = "preflight_failed" if any(c["status"] == "fail" for c in result["checks"]) else "ready_for_smoke"
            else:
                result = campaign(config, args.run, args.aiperf, args.resume, config_acquisition=acquired)
        result = {**result, "reported": timestamp()}
        if args.action == "verify" and (args.format == "text" or args.format == "auto" and sys.stdout.isatty()):
            try:
                print(format_verification(result, color=sys.stdout.isatty() and not os.environ.get("NO_COLOR")))
            except (KeyError, TypeError, ValueError) as exc:
                # A presentation failure must not discard completed checks or change their outcome.
                print(f"Text display failed ({type(exc).__name__}); showing JSON.", file=sys.stderr)
                print(json.dumps(result, indent=2))
        else:
            print(json.dumps(result, indent=2))
        return 0 if result.get("status", "complete") in ("complete", "ready_for_smoke", "plan_only", "pause_requested", "configured") else 2
    except (OSError, ValueError, KeyError, TypeError) as exc:
        detail = str(exc)
        if isinstance(exc, FileExistsError):
            detail = "Results already exist. Choose a new RUN directory, or inspect the saved report and deliberately resume the same experiment."
        elif isinstance(exc, FileNotFoundError):
            if getattr(args, 'config', None) and not Path(args.config).is_file():
                detail = ("Matrix config not found. Set MATRIX=/path/to/matrix.json." if args.action.startswith('matrix-') else
                          "Workload config not found. Run make configure with --url and --model, or set CONFIG=/path/to/config.json.")
            elif args.action in ('report', 'matrix-pause'):
                detail = "Saved run file not found. Set RUN to the output directory of an existing benchmark."
        print(f"{timestamp()['timestamp_utc']} Cannot continue: {detail}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(f"{timestamp()['timestamp_utc']} Interrupted; inspect the saved campaign state before resuming", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
