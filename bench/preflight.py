"""Read-only checks with bounded retries for temporary transport failures."""

import json
import os
import re
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import AIPERF_VERSION
from .provenance import observed, operation, timestamp


class NoRedirects(HTTPRedirectHandler):
    """Keep credentials and observations on the explicitly configured endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_http = build_opener(NoRedirects())


@observed("http_acquisition")
def fetch(url, headers=None):
    for attempt in range(3):
        try:
            with _http.open(Request(url, headers=headers or {}), timeout=10) as response:
                data = response.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                raise ValueError("Read exceeded 8 MiB")
            return data.decode(), attempt + 1
        except HTTPError as exc:
            exc.close()
            if exc.code in (301, 302, 303, 307, 308):
                raise ValueError(f"HTTP {exc.code}; redirects are disabled; configure the approved direct endpoint") from None
            if exc.code not in (502, 503, 504) or attempt == 2:
                raise ValueError(f"HTTP {exc.code}; verify endpoint, access and service health") from None
        except (URLError, TimeoutError, ConnectionError):
            if attempt == 2:
                raise ValueError("Transport unavailable after 3 reads; verify network, TLS and service health") from None
        time.sleep(attempt + 1)
    raise AssertionError("unreachable")


@observed("preflight")
def verify(config, aiperf):
    checks = []
    if config.get("kubernetes"):
        from .kubernetes import inspect
        checks.extend(inspect(config))
    started = timestamp()
    try:
        process = subprocess.run([aiperf, "--version"], capture_output=True, text=True, timeout=20)
        ok = process.returncode == 0 and process.stdout.strip() == AIPERF_VERSION
        checks.append({"name": "runtime", "status": "pass" if ok else "fail", "detail": f"Requires AIPerf {AIPERF_VERSION}"})
    except (OSError, subprocess.TimeoutExpired):
        checks.append({"name": "runtime", "status": "fail", "detail": "Install AIPerf or set AIPERF to its executable"})
    checks[-1]["observation"] = {"started": started, "finished": timestamp()}
    endpoint = config["endpoint"]
    headers = dict(endpoint.get("headers", {}))
    key = endpoint.get("api_key_env")
    if key:
        if not os.environ.get(key):
            checks.append({"name": "authentication", "status": "fail", "detail": f"Set {key} in the execution environment"})
            return checks
        headers["Authorization"] = "Bearer " + os.environ[key]
    started = timestamp()
    if endpoint.get("models_path"):
        try:
            with operation("model_acquisition", source="model_list"):
                body, attempts = fetch(endpoint["url"].rstrip("/") + endpoint["models_path"], headers)
            models = [row["id"] for row in json.loads(body)["data"]]
            checks.append({"name": "model", "status": "pass" if endpoint["model"] in models else "fail", "attempts": attempts, "detail": "Served model listing; route attribution still requires server evidence"})
        except (ValueError, KeyError, TypeError) as exc:
            checks.append({"name": "model", "status": "fail", "detail": f"Cannot confirm model listing: {exc}. Check models_path, model and access"})
    else:
        checks.append({"name": "model", "status": "unverified", "detail": "No model-list API configured; smoke must check inference"})
    checks[-1]["observation"] = {"started": started, "finished": timestamp()}
    for producer in config.get("metrics", []):
        started = timestamp()
        try:
            with operation("metrics_acquisition", source=producer["name"]):
                body, attempts = fetch(producer["url"])
            names = set(re.findall(r"^([^#\s{]+)(?:\{|\s)", body, re.M))
            missing = [item for item in producer.get("required", []) if item["metric"] not in names]
            checks.append({"name": producer["name"], "status": "fail" if missing else "pass", "attempts": attempts, "missing": missing, "metric_names": sorted(names)})
        except ValueError as exc:
            checks.append({"name": producer["name"], "status": "fail" if producer.get("required") else "unverified", "detail": str(exc)})
        checks[-1]["observation"] = {"started": started, "finished": timestamp()}
    return checks
