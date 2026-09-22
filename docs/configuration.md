# Configuration reference

`benchmark.local.json` describes one workload. `matrix.json` optionally coordinates several workloads or experiments. Both use JSON. No matrix or latency goal is required to verify access or run a workload.

Unknown fields are rejected, including nested settings. For example, `load.repeat` fails validation; use `load.repeats`.

## Create a workload config

```sh
make configure ARGS='--url http://localhost:8000 --model served-model'
```

This writes `benchmark.local.json`, validates its settings and sends no traffic. It never overwrites a file. Edit the JSON afterward, or choose another `--output`. `python -m bench configure --help` lists the same flags. You can instead copy `examples/benchmark.json` and edit it directly.

### Connection and workload

| Setup flag | JSON field | Setup default | Meaning |
|---|---|---|---|
| `--output` | — | `benchmark.local.json` | File to create; its parent directory must exist |
| `--url` | `endpoint.url` | Required | Existing inference server base URL; no credentials, API path, query or fragment |
| `--model` | `endpoint.model` | Required | Model name accepted by the server, not a local model directory |
| `--path` | `endpoint.path` | `/v1/chat/completions` | Streaming Chat Completions API path |
| `--no-model-list` | Omit `endpoint.models_path` | `/v1/models` otherwise | Skip model-list lookup if unavailable; smoke must establish inference access |
| JSON only | `endpoint.models_path` | — | Set a different absolute model-list API path |
| `--api-key-env` | `endpoint.api_key_env` | Omitted | Environment variable containing a bearer token; do not put the token in JSON |
| `--header NAME=VALUE` | `endpoint.headers` | `{}` | Repeat for non-secret routing/classification headers. Authorization/cookie headers are rejected |
| Selected by `--prompts` | `workload.type` | `synthetic` | `synthetic` generates prompts; `single_turn` reads a local JSONL file |
| `--input-tokens` | `workload.input_tokens` | `128` | Generated input token length; used with `workload.type: "synthetic"` |
| `--prompts` | `workload.path` | Omitted | Local single-turn JSONL; selects `workload.type: "single_turn"` instead of generated input |
| `--output-tokens` | `workload.output_tokens` | `64` | Requested output length/limit; actual output may differ |
| `--tokenizer` | `workload.tokenizer` | `builtin` | Generated lengths depend on tokenizer. Choose an approved matching tokenizer when exact lengths matter |
| JSON only | `schema_version` | `1` | Required configuration format version |
| JSON only | `record_processors` | `1` when omitted | AIPerf record processing workers |

Supported inputs: generated single-turn prompts or local JSONL with one nonempty `text` string per row. An optional positive integer `output_length` on a row specifies its requested output length. Dataset paths resolve relative to the JSON config. `workload.sha256` is calculated from the dataset during validation; do not enter it yourself. Multi-turn conversations, tool calls, Responses API and timestamped arrival replay are not supported.

```json
{"text":"Explain what a cache does.","output_length":64}
```

### Load and time limits

Setup values below demonstrate the mechanics; they are not calibrated measurement budgets. In hand-written JSON, fields are required except where an omission default is stated.

| Setup flag | JSON field under `load` | Setup default | Meaning / selection |
|---|---|---|---|
| `--concurrency 1 2 4` | `concurrency` | `[1,2,4]` | Closed-loop load points, run in order. Use `[4]` for one point. Choose exactly one axis |
| `--rates 0.5 1 2` | `rates` | Omitted | Request-rate points in requests/s. Replaces `concurrency` |
| `--arrival` | `arrival` | `constant` for rates | `constant` for regular spacing; `poisson` for randomized independent arrivals. Rate mode only |
| `--max-concurrency` | `max_concurrency` | Required for rates | In-flight request cap. A cap can constrain achieved arrivals; inspect timestamps |
| `--requests` | `requests` | `20` | Request limit per repeat. Sending stops at this limit or the duration, whichever comes first |
| `--duration-seconds` | `duration_seconds` | `60` | Sending-duration limit per repeat; not total campaign duration |
| `--request-timeout-seconds` | `request_timeout_seconds` | `30` | Timeout for each request. Choose for the workload's plausible response time |
| `--grace-seconds` | `grace_seconds` | `35` | Drain period for outstanding responses after sending stops. Allow room for request completion |
| `--deadline-seconds` | `deadline_seconds` | `180` | Runner limit for the AIPerf child process, including startup/export. Must exceed duration + grace; allow startup/export margin. Does not bound all pre/post checks |
| `--repeats` | `repeats` | `3`; omitted JSON uses `1` | Measurements at each point. Repeats are not retries |
| `--max-attempts-per-repeat` | `max_attempts_per_repeat` | `3`, also when omitted | Total attempt allowance for a repeat, including the first. Invalid evidence stops; retry requires explicit resume |
| JSON only, legacy | `max_attempts_per_point` | `3` when neither limit exists | Old attempt-limit name; do not combine with `max_attempts_per_repeat` |

For a short setup check, use `make smoke`: one synthetic 16-input/16-output-token request, concurrency one and one repeat. Configured time limits, access checks, metrics and goals still apply. Smoke does not characterize the supplied prompt file or warm every replica.

### Metrics and goals

| Setup flag / JSON field | Default | Meaning |
|---|---|---|
| `--metrics-file /path/metrics.json` | Omitted | Read a JSON array of producer objects into `metrics`; file is read during configuration, not continuously |
| `metrics` | `[]` in setup; omitted JSON also disables collection | No server metrics are collected or checked. Client timing still works; do not infer queueing, routing or GPU behavior |
| `metrics[].name` | Required per producer | Your label for this source |
| `metrics[].url` | Required per producer | Direct Prometheus-format endpoint reachable from the runner; not a New Relic UI URL |
| `metrics[].required` | `[]` when omitted | Missing required series invalidates evidence; an optional producer does not provide that guarantee |
| `metrics[].required[].metric` | Required | Exact source metric name; verify against that deployment |
| `metrics[].required[].why` | Required | What claim requires this metric |
| `goals` | `{}` in setup | Optional acceptance limits. Empty means measure without a target-pass claim |
| `goals.ttft_p95_ms` | Omitted | Maximum p95 time to first token, milliseconds |
| `goals.latency_p95_ms` | Omitted | Maximum p95 complete-request latency, milliseconds |
| `goals.max_error_fraction` | Omitted | Maximum failed-request fraction, between `0` and `1` |

Example `metrics.json` for `--metrics-file`:

```json
[{"name":"engine-1","url":"http://engine-1:8000/metrics","required":[{"metric":"vllm:num_requests_waiting","why":"Measure engine queue pressure"}]}]
```

Use stable per-replica URLs. Inference bearer auth does not configure metric auth. Check names, units and labels against the serving deployment. [Metric reference and New Relic boundary](metrics.md).

### Optional Kubernetes checks (JSON only)

Omit `kubernetes` to use endpoint/metric checks without Kubernetes API access. These settings observe resources; they do not create, scale or reconfigure them.

| JSON field | Default / requirement | Meaning |
|---|---|---|
| `kubernetes.context` | Required if enabled | Explicit kubectl context |
| `kubernetes.deployments[]` | Required nonempty list | Deployments whose readiness and identities must remain stable |
| `.deployments[].namespace`, `.name` | Required | Deployment identity |
| `.deployments[].replicas` | Required positive integer | Expected fixed replica count, not GPU count |
| `.routing` | Omitted | Optional declared Gateway/pool/objective binding checks |
| `.routing.namespace` | Required with routing | Namespace of route, pool and serving objects |
| `.routing.route`, `.pool` | Required with routing | HTTPRoute and InferencePool names |
| `.routing.model_deployment` | Required with routing | Model Deployment; also list it in `deployments` |
| `.routing.picker_deployment` | Optional | Endpoint Picker Deployment; also list it in `deployments` |
| `.routing.gateway.name`, `.namespace` | Required with routing | Expected Gateway parent |
| `.routing.gateway.section` | Optional | Expected Gateway listener/section |
| `.routing.rule_index` | `0` | Zero-based HTTPRoute rule to inspect |
| `.routing.objectives[]` | `[]` | Expected objectives, each with `name` and integer `priority` |
| `.routing.selected_objective` | Omitted | Expected direct request objective; must be in `objectives` |
| `.routing.objective_header` | `x-llm-d-inference-objective` | Header checked against the selected objective |

Object checks do not prove actual request classification. Use matching router evidence. [Routing example](operator-guide.md#kubernetes-checks).

## Make variables and execution arguments

| Input | Default | Meaning |
|---|---|---|
| `CONFIG` / CLI `--config` | Make: `benchmark.local.json`; CLI: required | Make `configure` output (explicit `--output` wins); workload input for `plan`, `verify`, `run`. CLI `--config` also selects matrix input for `matrix-plan`/`matrix-run` |
| `RUN` / CLI `--run` | Make: `results/smoke` for smoke/plan-smoke/resume-smoke, `results/matrix` for matrix commands, otherwise `results/benchmark`; CLI: required except verify | Output directory. New execution refuses an existing directory; resume uses it deliberately |
| `AIPERF` / CLI `--aiperf` | `aiperf` | Pinned executable path |
| `FORMAT` / verify CLI `--format` | `auto` | Verify only: readable output in a terminal, JSON when redirected. Force `text` or `json`; text is colored only in a terminal unless `NO_COLOR` is nonempty |
| `PYTHON` | `python3` | Python used by Make; activate the supported virtualenv first |
| `MATRIX` | Required for matrix-plan/run/resume | Matrix input; these commands do not read `CONFIG` or silently use a bundled example |
| `ARGS` | Empty | Flags forwarded by Make `configure` / `matrix-create` |
| CLI `--smoke` | Off | Replace workload/load with the bounded smoke described above; Make smoke supplies this |
| CLI `--execute` | Required by run/matrix-run | Explicit traffic execution; Make run targets supply it |
| CLI `--resume` | Off | Recheck unchanged inputs and resume saved state; Make resume targets supply it |
| `CONTAINER_ENGINE` | `docker` | Builder/runtime for image/container test targets |
| `IMAGE` | `inference-benchmark-harness:0.1.0` | Container image tag |
| `TEST_ARTIFACTS` | `.container-test-results` under checkout | Writable host output mounted by container tests |
| `--help` / `make help` | — | Command usage; CLI help is available per subcommand |

`report` and `matrix-pause` take only `--run`. `plan` sends no requests; `verify` makes read requests but no inference; run commands send inference. [Run and recovery behavior](operator-guide.md#when-the-runner-moves-on).

## Matrix generator arguments

| Flag | Default / requirement | Meaning |
|---|---|---|
| `--output` | Required, new file | Destination matrix; parent directory must exist |
| `--name` | Required | Short lowercase experiment name |
| `--question` | Required | Question the experiment should answer |
| `--stream NAME=CONFIG` | Required, repeatable | Workload configs; paths relative to the current directory |
| `--sweep` | Required | One named stream whose load changes |
| `--values` | Required | Comma-separated positive load points in execution order |
| `--hold NAME=VALUE` | Required for every other stream | Fixed load for each stream that is not swept |
| `--axis` | Required | `concurrency` or `rates` |
| `--arrival` | `constant` in rate mode | `constant` or `poisson`; rate mode only |
| `--max-concurrency` | Required for rates | Cap used by the generated rate streams |
| `--repeats` | `3` | Valid measurements per expanded row |
| `--max-attempts-per-repeat` | `3` | Total whole-group attempts allowed per repeat |
| `--min-overlap-seconds` | `5` | Minimum common request-arrival window for mixed streams; one stream needs no peer overlap |

The generator writes references to your existing configs; it does not require copying sample files together or infer safe load. [Example command](../README.md#several-workloads-or-experiments).

### Custom matrix JSON

| Field | Default / requirement | Meaning |
|---|---|---|
| `schema_version`, `name` | Required: `1`, short name | Format and experiment identity |
| `repeats`, `max_attempts_per_repeat`, `min_overlap_seconds` | Required | Same meanings as generator flags; maximum 100 repeats |
| `stages[]` | Required nonempty list | Ordered experiments |
| `stages[].id`, `.question`, `.change` | Required | Unique stage name, question, and what changes while other settings remain fixed |
| `stages[].kind` | `traffic` | `traffic` or `policy`; policy requires an observed profile |
| `stages[].streams` | Required, 1–8 named streams | Workloads launched together within a row |
| `streams.<name>.config` | Required | Workload config path relative to the matrix file |
| `streams.<name>.load` | Optional | Override load axis/pacing, requests and time budgets. Multiple value lists expand as a Cartesian product, maximum 1,000 rows |
| `stages[].profile` | Optional for traffic | Named serving profile expected before/after traffic |
| `profiles.<name>.observe` | Required for a profile | Reviewed read-only command as an argv array; no shell string |
| `profiles.<name>.expected` | Required for a profile | Expected JSON file, relative to matrix; compared exactly with observed JSON |
| `profiles.<name>.deadline_seconds` | `30` | Observer process time limit |

Matrix repeats replace per-workload repeats; every row runs each stream once per group repeat. Stream load overrides accept `concurrency`, `rates`, `arrival`, `max_concurrency`, `requests`, `duration_seconds`, `request_timeout_seconds`, `grace_seconds`, and `deadline_seconds`. Serving changes are made by the operator, not by this config.
