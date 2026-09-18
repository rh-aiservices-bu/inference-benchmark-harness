# Operator guide

A campaign measures one workload across selected load points on a fixed deployment. Add goals to check acceptance limits, or leave them empty to collect a baseline.

[V1 sequence](#matrix-and-next-steps) · [Configure inputs](#configuration-ownership) · [Debug](#debugging) · [Resume](#recovery) · [Mixed load](#mixed-load)

## Run sequence

`plan → verify → smoke → inspect/drain → benchmark → report`

Each repeat follows `check → run → validate → checkpoint`. One Python supervisor invokes AIPerf child processes. These modules are not separate pods. AIPerf generates traffic and exports measurements. The supervisor decides whether to continue.

A smoke checks the request path. It does not warm every replica or establish steady state. Before measured runs, the operator establishes cache/warmup conditions and confirms earlier traffic has drained. Keep model/image, tensor parallelism, replica count, route, serving limits and run location fixed. Save the serving owner's effective configuration separately. A declared object does not prove a process loaded it.

## Matrix and next steps

Start with verify and smoke, then measure each workload alone, mixed traffic and declared policy comparisons. [Matrices](#matrix-configuration) execute explicit rows and repeats. Operators choose the next experiment.

One config expands to **load points × repeats**. The example `[1, 2, 4]` with three repeats produces nine measurements. These are mechanics values, not calibrated capacity limits. `make plan` prints the actual commands and request budget. Sending ends at the request count or duration, whichever comes first. Grace lets outstanding responses finish. The process deadline includes startup/export, but not all checks or hashing.

### Runnable now

Use the [command sequence](../README.md#run) for each campaign. Use a separate configuration copy and result directory for each workload or changed experiment. Hold the deployment, routing, cache preparation, input distribution and measurement method fixed within a sweep.

| Question | Keep unchanged | Action | Evidence / next step |
|---|---|---|---|
| Are inputs reachable? | Intended endpoint and execution location | Configure inputs. Run `verify` | Resolve failed checks before traffic |
| Does this request take the right path? | Route, model and intended identity | Run `smoke`. Inspect and drain | Corroborate classification if required. HTTP 200 alone is insufficient |
| How does interactive traffic behave? | Workload, serving configuration and cache preparation | Sweep `load.concurrency` | Compare `time_to_first_token.p95` (ms), `request_throughput.avg` (requests/s) and failed/total requests. Inspect `inter_token_latency.p95` (ms) for streaming pauses |
| How does another workload behave alone? | Deployment and measurement method | Copy config. Replace `workload` and choose its bounded range | Retain the same measurements per workload. Use `request_latency.p95` (ms) when full-response completion matters |
| Does an operating point meet the target? | Workload, route and topology | Add agreed `goals`. Refine points or choose `load.rates` | Check `time_to_first_token.p95` / `request_latency.p95` (ms) against selected goals and failed/total requests against the error limit. Inspect record start timestamps for achieved arrivals |
| What changes with more replicas? | Model, parallelism, other policies and comparison traffic | Owner changes replicas. Update expected count. Start a new campaign | Verify identities. Compare `time_to_first_token.p95` (ms), `request_throughput.avg` (requests/s), error fraction and per-replica `vllm:request_success_total` deltas |

These are AIPerf export keys. See the [measurement selectors](metrics.md#client-evidence) for summary aliases and availability. Compare each repeat separately. Do not average p95 values into a campaign p95.

### When the runner moves on

| Result | Automatic action | Operator action |
|---|---|---|
| Checks pass. Evidence valid. No request errors or declared goal misses | Save the repeat. Run the next declared repeat or row | Review results before choosing a new experiment |
| Required check/evidence fails, or mixed arrivals do not overlap enough | Stop. Preserve the failed attempt | Diagnose, then explicitly resume within the saved attempt budget |
| Process fails or exceeds its deadline | Stop and clean up owned client processes. Reject the mixed group | Confirm serving queues drained. Diagnose before resume |
| Valid evidence misses a goal or contains request errors | Finish that point/row's repeats. Stop before the next point/row | Inspect results. Resume only deliberately |
| Expected serving profile differs before traffic | Stop without sending traffic | Serving owner applies the planned profile. Confirm readiness, then resume |
| Serving profile changes during the repeat | Reject the group and stop | Restore a stable setup. Rerun the whole group through resume |
| Pause requested | Finish and validate the current matrix group, then checkpoint | Resume with unchanged inputs |

Empty goals permit discovery. Evidence checks and request-error stops remain active. No rule automatically concludes that a detector works, chooses a new limit, or changes serving policy. Those decisions require the relevant metrics and a controlled comparison. Changing the experiment requires a new campaign. Any positive replica count is supported. Replicas are not GPU counts, and the harness does not scale them.

### Configuration ownership

| Input | V1 choice / reason | Where it changes |
|---|---|---|
| Prompt shape, output limit, tokenizer | Representative generated input or local single-turn rows. Characterize separately first | `workload` |
| Offered load | Concurrency for an initial capacity curve. Constant/Poisson rate when independent arrivals are the question | `load.concurrency` **or** `load.rates`, `arrival`, `max_concurrency` |
| Repeats and budgets | Example has three repeats. Choose enough data for the claim and bound execution | `load.repeats`, request/time limits, `max_attempts_per_repeat` |
| Application goals | `{}` for discovery. Add agreed latency/error limits for suitability | `goals`. See [targets](#targets) |
| Route and class | Use the intended model/path/headers. Verify actual classification where required | `endpoint`. Optional `kubernetes.routing` checks |
| Metric evidence | Configure required producers/names before making claims that depend on them | `metrics`. See [metric reference](metrics.md) |
| Fixed topology | Verify expected replicas and identities. Record model, parallelism and cache conditions | Optional `kubernetes.deployments`. Serving owner controls deployment |
| Serving plugins | Preserve the effective detector, ceiling, ordering, filter and scoring configuration during baseline comparisons | Serving owner's version-matched config. Not harness fields |

Flow-control gate comparisons are optional, separate campaigns. Record all effective changes: disabling the gate does not establish that every detector/filter is inactive. Benchmark `goals` evaluate client results. Serving InferenceObjectives classify requests. They are separate inputs.



## Defaults and fixed behavior

The [example config](../examples/benchmark.json) explicitly supplies the experiment values. They are not an automatically selected matrix. Omitted `repeats` means one. Omitted attempt limit means three. Set both explicitly. Load points execute in the listed order, without adaptive search.

The runner fixes streaming Chat Completions, random seed 42 and server token-count reporting. Dedicated GPU telemetry and automatic plots are disabled. Native records and configured server metrics are retained. The default tokenizer is `builtin` and record-processor count is one. Smoke replaces the workload with one synthetic 16-input/16-output-token request at concurrency one, with one repeat. Configured time budgets remain.

Choose only header names supported by the deployed version. No legacy aliases are injected automatically. Use `endpoint.api_key_env` for bearer credentials. Token acquisition/refresh is outside the runner. A custom classifier may intentionally overwrite caller headers: verify its resolved objective rather than bypassing it. Neither upstream images nor running in a Job establishes the required authentication or mesh identity.

## Handoff check

- Rehearse README commands and relevant debugging steps from the intended run location. Record expected output, config edits and saved artifacts. Label fixture/lab checks separately from customer checks.
- Qualify endpoint authentication, classification where required, metric names/units/labels, replica identity and durable storage. Confirm warmup/drain and realistic measurement budgets.
- Give the operator a reviewed config, results, stop/resume instructions and the next experiment from the matrix above. Qualify the actual image/Job if that is the chosen execution path.

Passing runner tests makes a candidate reviewable. These checks establish whether an operator can use it independently. Matrix handoff also requires observed overlap, per-stream classification and reviewed serving-profile observers.

## Workload

For generated input, set `input_tokens`, `output_tokens` and `tokenizer` in the example. The built-in tokenizer needs no model download but may differ from the server. Use an approved matching local tokenizer when exact generated lengths matter. Results use server-reported token counts. Missing usage stays unknown.

For private single-turn prompts, replace the workload block:

```json
{"type":"single_turn", "path":"prompts.jsonl", "output_tokens":64, "tokenizer":"builtin"}
```

Each JSONL line is `{"text":"A prompt","output_length":64}`. Paths are relative to the config file. A row's `output_length` overrides the default. This input does not preserve conversation history, tool execution or production arrival timing.

Keep the input/output length distribution, request mix, shared prefixes and cache state representative. An output limit is a ceiling, not a guarantee that the model generates that many tokens. Reusing identical prompts can warm caches and change later results.

## Targets

Example only—replace these values with agreed application requirements:

```json
"goals": {
  "ttft_p95_ms": 500,
  "latency_p95_ms": 5000,
  "max_error_fraction": 0.01
}
```

These mean p95 time to first token at most 500 ms, p95 whole-request latency at most 5 seconds, and errors at most 1%. Latency and error goals are evaluated separately. An error is not a fast successful response. A p95 target is not a maximum-latency guarantee. Long answers need an appropriate whole-request target. Inter-token latency is not an implemented goal. Each matrix stream can supply its own goals. The harness does not classify records into additional workload classes within a stream.

Omit a goal you have not agreed. Empty goals collect measurements without asserting a target pass. Three requests or 20 requests cannot establish reliable tail behavior. Choose observation length and repeat count for the decision and observed variability. Set `load.repeats` (example: 3) and `load.max_attempts_per_repeat` (example: 3). A repeat is one measurement. A replacement attempt only repairs invalid execution.

A first discovery sweep can use `"goals": {}` (or omit the field). The evaluator then emits measurements with no goal verdicts. Configuration validation checks supported goal names and numeric limits. `bench/evidence.py` compares each supplied limit with observed values, and `bench/runner.py` controls progression. These are harness checks, not AIPerf CLI flags or Kubernetes InferenceObjectives.

Without goals, invalid evidence still stops immediately. The current runner also retains request errors, finishes repeats at that point and stops before higher load—even when an explicit error-fraction goal would tolerate those errors. That conservative progression rule is separate from target evaluation. Add agreed goals in a new campaign. Changing goals changes the configuration fingerprint and is not a same-config resume.

## Load

Choose one axis per campaign:

| Shape | Fields | Meaning |
|---|---|---|
| Closed loop | `"concurrency": [1, 2, 4]` | Maintain up to this many outstanding requests |
| Regular arrivals | `"rates": [0.5, 1, 2], "arrival": "constant", "max_concurrency": 4` | Target requests/second with regular spacing and an outstanding-request cap |
| Random arrivals | Same rate fields, `"arrival": "poisson"` | Random arrival gaps at the target average, subject to the cap |

Keep the remaining `load` limits. A binding cap can prevent the desired arrival rate. Compare actual arrivals and achieved throughput. Three Poisson requests do not validate a distribution.

Start with these steps.

1. Run the one-request smoke and establish warmup conditions.
2. Choose a bounded, increasing load range.
3. If goals are configured, inspect the first valid goal miss before increasing load. Without goals, inspect timing, throughput and request errors before extending the range.
4. If passing and failing points bracket a useful boundary, test intermediate points in a new campaign.
5. Repeat near that boundary. Results can vary, so do not assume they improve or worsen monotonically.

The runner does not choose intermediate points or warm the deployment automatically. Retain valid results even when they miss a goal.


## Kubernetes checks

Add this block when the runner has read access through kubectl:

```json
{
  "kubernetes": {
    "context": "YOUR_CONTEXT",
    "deployments": [
      {"namespace": "inference", "name": "model-server", "replicas": 1},
      {"namespace": "inference", "name": "endpoint-picker", "replicas": 1}
    ]
  }
}
```

The runner reads Deployments and selected pods, checks rollout convergence and records image identities. It never scales them. Use your deployment owner to establish the experiment's replica count. Preserve tensor parallelism, model, cache settings and other serving parameters. One replica may still use multiple GPUs.

To verify llm-d object bindings, add `routing` inside `kubernetes`:

```json
{
  "routing": {
    "namespace": "inference",
    "model_deployment": "model-server",
    "picker_deployment": "endpoint-picker",
    "pool": "model-pool",
    "route": "model-route",
    "rule_index": 0,
    "gateway": {"namespace": "gateway-system", "name": "inference-gateway"},
    "objectives": [{"name": "interactive", "priority": 10}],
    "selected_objective": "interactive"
  }
}
```

For this example, also set `endpoint.headers` to `{"x-llm-d-inference-objective":"interactive"}`. The runner verifies pool/model and picker-Service ownership, current Gateway/HTTPRoute acceptance, the selected rule's pool references, objective pool/priority bindings and the configured header. `rule_index` is zero-based. Confirm the actual request matches that rule's path, host and header conditions. The report includes its matches. All declared model/picker Deployments must also appear in the readiness list.

An objective's missing or stale controller status is `unverified`, not a failed declared binding. A current explicit rejection fails the check. When a custom application maps a client field to an objective, omit `selected_objective` unless the benchmark directly sets the objective header. Capture the actual classification separately.

These checks do not prove custom filters, effective scheduling policy or actual request classification. Use matching gateway/router evidence from smoke for those claims. The endpoint-only path remains available when the runner cannot use the Kubernetes API. Kubernetes read permissions are needed for the selected namespaces' Deployments, pods, Services, Gateways, HTTPRoutes, InferencePools and InferenceObjectives. No write permission is used.

Before comparing policies, the serving owner should record the actual picker image ID, startup arguments, mounted scheduling configuration and the version-specific loaded configuration reported by the process, when available. Compare the running pod with the deployment owner's intended configuration. Editing a ConfigMap or higher-level serving resource alone does not prove the process loaded it. If effective controls cannot be established, retain that uncertainty and do not attribute a latency change to a particular policy.


## Execution environment

| Location | Required checks |
|---|---|
| Approved Python host | Endpoint/metric access, identity, CA trust, CPU capacity and durable storage |
| Local container | Same checks from inside the container. Writable results and scratch directories |
| Kubernetes Job | Approved image, architecture, service DNS, mesh identity, volumes and Job completion |

For an authorized local tunnel, use an explicit context in a separate terminal:

```sh
kubectl --context YOUR_CONTEXT -n YOUR_NAMESPACE port-forward service/YOUR_GATEWAY 8000:80
```

Use `http://127.0.0.1:8000` from the host. Forward individual metric producers separately. A container's loopback belongs to that container. Qualify its actual endpoint path. Tunnel latency is included in client timing, so prefer a stable direct path for performance comparisons. Stop only your own tunnels.

Build the supplied multi-stage image with your approved builder:

```sh
make image IMAGE=inference-benchmark-harness:0.1.0
make test-container IMAGE=inference-benchmark-harness:0.1.0 TEST_ARTIFACTS=/path/to/test-artifacts
```

`test-container` runs unit and real AIPerf fixture tests in the built image. It sends no model traffic. Set `CONTAINER_ENGINE=podman` for an approved Podman installation. The output directory must be writable by container UID 10001. The target checks this before testing.

Results must be writable by UID 10001. Adapt [the Job example](../examples/job.yaml) to an approved image and existing input/output PVCs. It uses no API token, requires no GPU and disables Job retries. The default image contains no kubectl: enable Kubernetes checks only in an environment providing kubectl and the required read permissions. Check sidecar completion, CA trust and network policy. Preserve required mutual TLS.

If the base image is unavailable or unapproved, rebuild on an approved Python 3.11+ Linux base or install in an approved virtual environment. Match builder/runtime libraries, registry and dependency mirror. Rerun unit tests, fixture integration, verify and smoke. A different image requires qualification, not just a changed `FROM` line. The example does not imply organizational approval.

## Recovery

If a crash leaves the last matrix row saved with status `ready`, use `matrix-resume` with unchanged inputs. It verifies accepted evidence and finalizes the status without sending traffic. Earlier goal misses and request errors remain in the final status.

| Condition | Automatic behavior | Operator action |
|---|---|---|
| Temporary connection failure or HTTP 502/503/504 during a read-only check | At most three reads, with 1-second and 2-second delays | Fix the path if the check remains unavailable |
| Wrong model, rejected authentication, unready deployment or missing required metric | Stop before inference | Restore the planned precondition, then resume. Changed experiment configuration needs a new run |
| AIPerf exits unsuccessfully, exceeds its deadline or leaves incomplete exports | Stop and preserve the attempt | Inspect logs and evidence. Deliberate resume makes a new attempt |
| Interrupt or termination signal | Terminate the process group owned by this run and save the outcome | Inspect the partial attempt before resume |
| Valid measurement misses a goal or includes request errors | Retain it, finish the declared repeats at this load, then stop before higher load | Resume advances after those repeats, without rerunning accepted results. Prior goal misses/errors remain in the final campaign status |
| Previous supervisor vanished without a checkpoint | Refuse automatic recovery | Reconcile running processes and evidence. Use a new campaign after resolving ownership |
| Configuration, dataset or accepted evidence changed | Refuse to skip or resume under the old identity | Create a new experiment with the changed inputs |

For a failed smoke, use `make resume-smoke CONFIG=/path/to/benchmark.json RUN=/path/to/results/smoke`. Ordinary `resume` uses the full workload and will reject the smoke configuration identity.

Inference traffic is never automatically retried. Each repeat permits three attempts by default, including preflight failures. Configure `load.max_attempts_per_repeat` before the campaign. The legacy `max_attempts_per_point` name remains accepted when the new name is absent. Reaching this limit calls for diagnosis. The harness does not generate runtime patches, alter serving configuration or loosen goals.

The campaign lock prevents simultaneous owners on a local filesystem. Shared filesystems must support advisory locks and atomic rename. Keep one operator per output directory. Kubernetes Job retries are disabled in the example because a new pod must not silently replay traffic.

Stopping the client does not prove that every serving layer cancelled its work. After an interruption or timeout, verify that the previous traffic has drained before deliberately resuming. Use the engine and router signals appropriate to your deployment.

The free-space check catches an almost-full output filesystem. It is not a prediction of the run's artifact size. Size durable storage for your request count and payloads. If a collector or runner is evicted, incomplete evidence remains incomplete.


The client inherits the campaign lock. Do not delete a lock file to bypass ownership. Resolve surviving owned processes first. Cleanup is bounded, best-effort signaling of the launched process group. It does not cover escaped descendants or prove remote serving cancellation. `report` distinguishes incomplete work, invalid evidence and goal misses through its saved status and exit code.

## Debugging

Run `make verify CONFIG=/path/to/benchmark.json`. It reads configured targets without inference and prints named checks, discovered `metric_names`, and any `missing` requirements. Save its JSON output with your run notes. There is no separate `debug` command.


For raw diagnosis, fill these placeholders from the existing deployment. These commands read resources. They do not create a Gateway or change routing.

```sh
kubectl --context YOUR_CONTEXT --request-timeout=15s -n YOUR_NAMESPACE get httproute YOUR_ROUTE -o yaml
kubectl --context YOUR_CONTEXT --request-timeout=15s -n YOUR_NAMESPACE get service YOUR_METRICS_SERVICE -o yaml
kubectl --context YOUR_CONTEXT --request-timeout=15s -n YOUR_NAMESPACE get pods -l YOUR_MODEL_SELECTOR -o wide
curl --fail --silent --show-error --max-time 10 'http://YOUR_REACHABLE_PRODUCER:PORT/metrics'
```

Inspect route `parentRefs`, host/path matches, filters and acceptance. Use the referenced Gateway's namespace when it differs. Service ports/selectors identify the intended producer, but do not prove per-pod metric coverage. Run the metric read from the benchmark's network location using approved authentication and CA trust. Expect metric names, labels and values—not HTML or a login response. Use the same raw sample to check `metrics[].required`. An absent lazy series may need a separate triggering smoke before it can become required.

| Symptom | Inspect | Action |
|---|---|---|
| Required name missing | `metric_names`, `missing`, producer URL and actual exporter version | If the new name has equivalent meaning, edit `metrics[].required[].metric`. Keep `why`. A changed config starts a new run. |
| Metric exists for wrong model/pod | Producer identity, source labels, collection filters | Correct the target or selector. A matching name alone does not establish attribution. |
| Units or histogram changed | Source type/unit and monitoring ingestion mapping | Correct the conversion/query. Do not fix a semantic mismatch with a name-only alias. |
| HTTP 401/403 or TLS failure | Identity, certificate trust and approved route | Correct access. Inference bearer credentials do not configure metric-producer authentication. |
| Monitoring samples missing/stale | Collector status, ingestion lag, query filters and exact run window | Missing is not zero. Hold claims needing that evidence. Direct collection can be used when approved. |
| Model-list 404 | Gateway routes and `endpoint.models_path` | If listing is intentionally absent, set it to null. Verify the served model using smoke and server evidence. No KServe dependency is assumed. |
| HTTP 200 but wrong/default priority | Actual router classification. Header spelling for the deployed version. Objective-to-pool binding. Gateway header filters | Correct `endpoint.headers` for a direct classification test, or the trusted classifier mapping for an end-to-end test. A client header alone is not proof. |
| Inference 404 / no engine traffic | Request host/path, HTTPRoute `parentRefs`, selected Gateway address and route acceptance | Use the existing route's intended Gateway. Do not create another Gateway to make a benchmark pass. |
| Header changes between client and router | Route `RequestHeaderModifier` and custom filters/classifier. Intended header owner | Preserve deliberate policy overwrites. Check resolved objective/fairness at the router. Correlate IDs if the gateway replaces client request IDs. |
| Native export incomplete | `execution.json`, `aiperf.log`, native directory and storage | Preserve the attempt. Diagnose before deliberate resume. |
| AIPerf reports `ipc path` too long or proxy socket initialization fails | Native log and `TMPDIR`. Local socket paths have an OS length limit | Use a short, private scratch path: `scratch=$(mktemp -d /tmp/aip.XXXXXX)`, then prefix the command with `TMPDIR="$scratch"`. Keep results in the original durable directory. Inspect the log before treating every socket failure as a path issue. |
| Pod replaced/restarted | `preflight.json` and `postflight.json` when enabled | Reestablish a stable deployment. Preserve the invalid run and plan a fresh comparison. |

New Relic query/import, unit conversion and source-to-ingested metric mapping are not implemented adapters. Validate those externally for the exact collection path. Direct preflight currently validates metric names, not label attribution or full time-series semantics. Configuration changes do not alter historical results.


## End a session

Stop the benchmark process you launched. Confirm outstanding requests and queues have drained. Preserve results before removing only this session's Jobs, tunnels, containers and temporary inputs. Verify those resources are gone. Shared model Deployments, Gateways, nodes and clusters are not part of benchmark cleanup. Never scale them down as an implicit “shutdown.”

## Evidence and storage

Each run directory contains configuration, state, events and a provenance ledger. Each attempt retains pre/postflight checks, command, execution outcome, AIPerf log, native exports, summary and checksums. Point summaries compare accepted repeats. Their p95 range is neither a pooled percentile nor a confidence interval.

Native exports can contain prompts and operational data even when raw-response export is disabled. Retain them under your data policy. Storage grows with requests, prompt size, scrape dimensions and repeat/attempt count. Measure a small run before budgeting a large one. The 64 MiB free-space guard is not a size estimate. A New Relic dashboard is not a replacement for resumable state or native client evidence. [collection details](metrics.md#collection-and-new-relic).

| Field | Meaning |
| --- | --- |
| `started`, `finished` | When an operation began and ended on the runner |
| `recorded` | When its ledger entry was written |
| `timestamp_ns` | Integer Unix time in nanoseconds, matching AIPerf request timestamp units |
| `timestamp_utc` | The same instant as UTC text, with nine fractional digits and `Z` |
| `duration_ns` | Elapsed time from a monotonic clock. Unaffected by wall-clock adjustments |
| `artifact`, `sha256` | Relative file path and hash of the saved contents |


Acquisitions, errors and saved artifacts are recorded in `provenance.jsonl`. Acquisition times describe when the runner read a config or endpoint, not when the remote configuration changed. Native files keep their original format. Provenance records when they were observed and hashed. Use AIPerf's request/scrape times for measurement.

Use durable output storage with working advisory locks and atomic rename. Collect evidence before deleting a Job or its volumes. Hard failure can occur between a save and its ledger entry. Absent provenance is not reconstructed. Clock synchronization is required across hosts. JavaScript readers should display UTC text or use lossless integers for nanosecond timestamps. The ledger hashes prior state versions but does not archive every overwritten state file.

## Mixed load

Keep interactive demand at a chosen isolated operating point. Add one competing workload and sweep **its** request rate while holding the interactive rate, workload shapes, topology and serving policy fixed. Compare the interactive stream’s `time_to_first_token.p95` and `inter_token_latency.p95` (ms), `request_throughput.avg` (requests/s), successful completions and error fraction with its isolated reference. Inspect per-class `llm_d_epp_flow_control_queue_size` (requests), window p95 of `llm_d_epp_flow_control_request_queue_duration_seconds` (seconds), and per-replica `vllm:num_requests_waiting` (requests). Derive achieved arrivals from native record start timestamps. Completion throughput is a different measurement. Equal treatment is the control before testing differentiated priorities. [AIPerf's load reference](https://docs.nvidia.com/aiperf/benchmark-modes/load-generator-options-reference) explains rate versus concurrency scheduling. This harness remains pinned to 0.12.0 and exposes only the modes listed above.

| Experiment | Change | Why / next decision |
|---|---|---|
| Interactive + one competitor | Sweep competitor rate. Repeat separately for each relevant competitor | Identify which workload causes interference before combining them |
| Add a third/fourth workload | Fix the existing rates. Sweep the newly added rate | Find the additional effect of that workload |
| Full steady mixture | Fix workload proportions. Sweep total offered rate | Measure the representative mix's operating range |
| Detector calibration, when admission is the question | Fix a discriminating traffic mixture. Compare bounded detector limits | Require valid accounting and evidence that the configured gate actually engaged |
| Priority comparison | Hold traffic, detector, shared ceiling and routing fixed. Compare equal versus intended priorities | Measure protection and lower-priority progress. Do not infer causality from different prompt lengths |
| Later, only for a remaining question | Change holdback, fairness, endpoint filtering/scoring or burst shape one at a time | Diagnose the remaining delay or sharing problem. Retain the previous control |

Represent these recipes as explicit matrix stages below. The coordinator preserves each stream and validates a common arrival window. Request-rate mixtures still need achieved-arrival checks. Concurrency ratios do not establish request-rate ratios. It does not select detector limits or infer causality.

## Keep the guide and evidence aligned

This section owns the experiment sequence and reasons. Configuration files own runnable values. `bench/config.py` validates supported inputs, `make plan` expands them, and saved summaries/state own outcomes. Reuse those outputs in a dashboard instead of copying values into another decision graph. The public package has no dashboard or adaptive experiment selector.

For each manual next-step decision, retain the prior run/point, observed metrics with units, changed field/value, reason or rule, UTC time and decision owner beside the run evidence. “Insufficient evidence” is a valid conclusion. V1 does not automatically author these takeaways or accept decision metadata as benchmark config fields. When a supported option changes, update its validator, relevant behavior test and this guide together. Rerun the documented plan examples.


## Generate a matrix

First configure one workload file per stream: endpoint, prompts, goals, metrics and request/time budgets. The generator changes only the load axis. It keeps the other inputs in those files.

```sh
python -m bench matrix-create --output /path/mixed.json --name background-sweep \
  --question "Does background demand delay interactive responses?" \
  --stream interactive=/path/interactive.json \
  --stream background=/path/background.json \
  --sweep background --axis rates --values 0.5,1,2 \
  --hold interactive=0.5 --max-concurrency 4 --repeats 3
make matrix-plan MATRIX=/path/mixed.json RUN=/path/results/mixed
```

The Make wrapper accepts the same options: `make matrix-create ARGS='--output /path/baseline.json --name baseline --question "How does interactive load behave?" --stream interactive=/path/interactive.json --sweep interactive --axis concurrency --values 1,2,4'`.

This holds interactive traffic at 0.5 requests/s and sweeps background traffic through 0.5, 1 and 2 requests/s. It generates three rows with three repeats each. These values are examples, not capacity recommendations. Both streams inherit their own request/time limits. The shared arrival window must meet the overlap requirement.

- Add each workload with `--stream NAME=CONFIG`. Paths are relative to the current directory. The output stores references relative to the matrix file.
- Choose one `--sweep` and give every other stream a fixed `--hold NAME=VALUE`.
- Use `--axis concurrency --values 1,2,4` for closed loop. Omit rate-only options. A single stream needs no `--hold`.
- Rate mode requires `--max-concurrency`. Arrival defaults to constant; choose `--arrival poisson` for random gaps.
- Defaults: three repeats, three allowed attempts per repeat, five seconds of shared arrival overlap. Set `--max-attempts-per-repeat` and `--min-overlap-seconds` to change those limits.

The command validates all referenced configs and prints the expanded request budget. It never overwrites a file, sends traffic, runs a policy observer or chooses detector settings. The output directory must exist. Keep the matrix and workload files together when transferring them. Review the plan, then use `make matrix-run MATRIX=/path/mixed.json RUN=/path/results/mixed`.

To change the experiment, generate a new matrix and use a new result directory. Edit JSON for multiple stages, Cartesian-product sweeps or reviewed policy profiles. AI can help propose those choices, but validation and execution use the same deterministic commands. Full CLI help: `python -m bench matrix-create --help`.

## Matrix configuration

`examples/matrix.json` references ordinary workload configs. A stage has a question, a changed variable and named concurrent streams. Each stream can override `load` fields. Endpoint, headers, dataset and goals stay in its workload config.

```json
{
  "schema_version": 1,
  "name": "interactive-with-background",
  "repeats": 3,
  "max_attempts_per_repeat": 3,
  "min_overlap_seconds": 5,
  "stages": [{
    "id": "background-sweep",
    "question": "Does background demand delay interactive responses?",
    "change": "Hold interactive rate; sweep background rate.",
    "streams": {
      "interactive": {"config": "interactive.json", "load": {
        "rates": [0.5], "arrival": "constant", "max_concurrency": 4}},
      "background": {"config": "background.json", "load": {
        "rates": [0.5, 1, 2], "arrival": "constant", "max_concurrency": 4}}
    }
  }]
}
```

These numbers demonstrate configuration, not calibrated limits. This expands to three rows × three repeats × two streams. Multiple non-singleton axes form a Cartesian product. `matrix-plan` shows every row and total request budget before execution. Maximums are eight streams per row, 1,000 expanded rows and 100 repeats. Choose request/time budgets long enough to obtain the declared overlap and useful samples. No automatic warmup, midpoint search or workload selection occurs.

All streams pass checks before any starts. They launch together but are **not synchronized at the first request**. Each repeat requires a shared observed arrival window: latest first request start through earliest last request start, including both bounds. The window must meet `min_overlap_seconds`, and every stream must contribute arrivals. Missing overlap invalidates the whole repeat.

`summary.json` separates `streams` (full native runs) from `shared_window` (requests arriving in the common interval). Shared results retain full response durations, including completions after the window. Report nearest-rank p95 TTFT/request latency in milliseconds, error fraction and sample counts. This is an arrival cohort, not proof of constant contention throughout every response. Tail responses may finish after another workload stops sending. Do not use full-run throughput as common-window throughput or treat tiny fixture samples as reliable p95 estimates. Native p95 and shared nearest-rank p95 may use different estimators.

Any invalid stream or missing overlap stops the matrix. A process failure/deadline cancels all owned peers. An invalid group has **zero accepted streams**. Deliberate resume reruns every stream in a new group attempt. A valid goal miss in either full-run or shared-window results, or any request errors, completes that row's repeats then stops before the next row. Earlier misses remain visible after resume.

```sh
make matrix-pause RUN=/path/results/matrix
make matrix-resume MATRIX=/path/matrix.json RUN=/path/results/matrix
```

Pause finishes the current group and validates it before checkpointing. Interrupt cancels the group instead. Inspect serving queues before resume. Neither action proves that remote work has drained. An unfinished `running`/`checking` checkpoint after a crash refuses blind replay. Resume requires unchanged matrix, source config files, dataset identities, expected profiles and accepted evidence. Planned input changes require a new matrix directory.

### Detector and filter stages

Use `"kind": "policy"` and a named `"profile"` on a stage. Define that profile under top-level `profiles`:

```json
"profiles": {
  "candidate": {
    "expected": "expected-policy.json",
    "observe": ["/approved/read-effective-policy", "--json"],
    "deadline_seconds": 30
  }
}
```

The observer is an operator-supplied **read-only command**, passed as an argv array without a shell. Review it before execution. Matrix files are executable configuration, not safe untrusted input. It must return stable JSON describing the effective serving controls under test. `expected-policy.json` contains the exact expected JSON. Include version, detector limits, ceiling and filter wiring that matter. Exclude credentials, timestamps, counters and unrelated changing fields. A ConfigMap read alone does not prove the running process loaded it. There is no universal observer for custom serving controllers.

The runner saves the observation and checks exact JSON equality before and after each repeat. A precheck mismatch stops with `profile_mismatch` **before traffic**. The serving owner applies the planned change, confirms readiness and loaded configuration, then runs `matrix-resume`. A postcheck mismatch invalidates the group. The package never applies serving changes or trusts a filename as proof of the deployed policy.

To compare detector limits or filters, create one stage per expected profile, keep the stream configs and load points identical, and name the one changed field in `change`. To sweep two parameters, list the intended profile combinations explicitly. The runner automates all traffic rows for the current profile. Transitions requiring a serving change stop for the owner. This supports repeatable comparisons without silently changing the cluster. Policy effectiveness still requires the relevant router/engine metrics and actual classification evidence.
