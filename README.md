# Inference benchmark harness

Run bounded AIPerf tests against an existing streaming Chat Completions endpoint. Preserve each attempt and validate it before continuing.

## Where to start

Benchmark to find a workload's usable capacity, check latency targets and compare serving policies before sharing GPUs across applications.

| Need | Start here |
|---|---|
| Get a first test running | [Daniel's standalone AIPerf guide](https://github.com/dandawg/llm-d-flow-control-demo/blob/main/benchmarks/standalone-guide.md). His [demo repository](https://github.com/dandawg/llm-d-flow-control-demo) also provides a deployment scaffold |
| Run repeatable experiments | This harness checks inputs, runs isolated or mixed workloads, repeats measurements and preserves evidence |
| Understand the shared-GPU business case and prior experiments | [Alexa's decision guide](https://alexagriffith.github.io/flow-control-benchmarks/benchmark-decision-map/) and [benchmark repository](https://github.com/alexagriffith/flow-control-benchmarks) cover experiment choices, prior results and their evidence limits |
| View saved progress or replay an experiment | [Flow Control Flight Recorder](https://github.com/alexagriffith/flow-control-visualizer) is a separate, read-only companion; see [saved output formats](docs/operator-guide.md#viewer-inputs) and its README for supported inputs |
| Understand or configure the serving stack | [llm-d](https://github.com/llm-d/llm-d), [documentation](https://llm-d.ai/docs) and [router / flow control](https://github.com/llm-d/llm-d-router) |

## Install and configure

Python **3.11–3.13**, Git and Make on Linux or macOS. AIPerf is pinned to 0.12.0 and does not support Python 3.14. Use a supported interpreter for `python3` below. Replace `/path/benchmark.json` with a filename in an existing private directory.

```sh
git clone https://github.com/rh-aiservices-bu/inference-benchmark-harness.git
cd inference-benchmark-harness
python3 -m venv .venv
. .venv/bin/activate
python -m pip install '.[runtime]'
cp examples/benchmark.json /path/benchmark.json
```

Edit the copied configuration before running.

| Input | You supply | Example / omission behavior |
|---|---|---|
| `endpoint` | URL, served model, API path and required authentication/headers | Example uses localhost and a placeholder model. No headers or token |
| `workload` | Representative token lengths or local single-turn JSONL | Synthetic 128 input / 64 output tokens. Built-in tokenizer |
| `load` | Load points, repeats and execution limits | Concurrency 1, 2, 4 with three repeats. Omitting `repeats` uses **one** |
| `goals` | Agreed latency/error limits, when known | `{}` collects a baseline without a target pass |
| `metrics` | Required producer URLs, names and purpose | `[]` collects no server metrics, so it cannot support flow-control claims |
| `kubernetes` | Context, expected replicas and routing objects if inspecting the cluster | Omitting this block skips Kubernetes identity/routing checks |

The example allows **20 requests or 60 seconds per repeat**, with a 30-second request timeout, 35-second grace and 180-second process deadline. Three attempts per repeat are allowed through deliberate recovery. Inference is not automatically retried. These values demonstrate the workflow. Choose measurement budgets for your workload. See [defaults and fixed behavior](docs/operator-guide.md#defaults-and-fixed-behavior).

## Run

Use **verify → smoke → benchmark**. One configuration file is enough; no matrix or latency goal is required. `CONFIG` selects the file you edited above. `RUN` selects a new directory for saved results.

1. **Verify the setup.** Validate the config and check AIPerf, authentication, the configured model listing and metric endpoints. No inference traffic is sent.

   ```sh
   make verify CONFIG=/path/benchmark.json
   ```

   The config tells verification what to check. Continue at `ready_for_smoke`, after reviewing any `unverified` checks. Empty `metrics` skips collection checks; without a model-list API, smoke must confirm inference access.

2. **Send one test request.** Smoke repeats the setup checks, sends one short synthetic request and validates its saved evidence. Read the result before continuing.

   ```sh
   make smoke CONFIG=/path/benchmark.json RUN=/path/results/smoke
   make report RUN=/path/results/smoke
   ```

   This checks the request path, not capacity or full warmup. Establish [cache/warmup and drain conditions](docs/operator-guide.md#run-sequence) before measuring.

3. **Run the benchmark.** Confirm the configured load points, repeats and request/time limits. Use a separate results directory.

   ```sh
   make benchmark CONFIG=/path/benchmark.json RUN=/path/results/sweep
   make report RUN=/path/results/sweep
   ```

   Each attempt checks the setup, runs AIPerf, validates evidence and saves its outcome. An incomplete or unsuccessful campaign returns nonzero. `complete` without goals does not establish suitability. Preserve failures and use [debugging](docs/operator-guide.md#debugging) before retrying.

**Optional preview:** `make plan CONFIG=/path/benchmark.json RUN=/path/results/sweep` prints the benchmark commands and maximum request budget without running anything. `make plan-smoke` does the same for the one-request smoke. Neither is a required execution step; smoke saves its actual command automatically.

`make help` lists commands. `benchmark` is an alias for `sweep`. Preflight requires direct endpoint URLs and refuses redirects, including login redirects.

The example uses **three valid repeats per point**. Invalid evidence stops immediately. A valid goal miss or request error is retained. Remaining repeats at that point finish before higher load is stopped. Inference is never automatically retried. See [recovery](docs/operator-guide.md#recovery) before using `make resume CONFIG=/path/benchmark.json RUN=/path/results/sweep`.

## Several workloads or experiments

Generate a matrix with [`make matrix-create`](docs/operator-guide.md#generate-a-matrix) using named workload configs and explicit load values. No AI or JSON editing is required for a single-axis sweep. The generator validates the file, prints its request budget and sends no traffic.

For custom multi-stage or policy experiments, copy `examples/matrix.json`, `benchmark.json` and `background.json` together. Edit their endpoint, workload and budgets. The matrix runs each row's streams together and checkpoints whole repeats.

```sh
make matrix-plan MATRIX=/path/matrix.json RUN=/path/results/matrix
make matrix-run MATRIX=/path/matrix.json RUN=/path/results/matrix
make report RUN=/path/results/matrix
```

Every stream uses the same verify/smoke preparation before coordinated load.

`matrix-pause` finishes the current group before pausing. `matrix-resume` rechecks inputs and continues after accepted groups. Use the same `MATRIX` and `RUN`. Invalid peers or insufficient traffic overlap invalidate the whole repeat. See [matrix configuration and policy comparisons](docs/operator-guide.md#matrix-configuration). If a crash interrupts finalization after the last row, resume verifies accepted evidence and finalizes the status without replaying traffic.

## Scope and evidence

One workload config describes one stream. A matrix combines streams and explicit experiments, with optional observed serving-profile gates for detector/filter comparisons. The harness does not deploy, scale or choose serving parameters. No KServe, OpenShift or Prometheus database is required. Multi-turn, tools, Responses API, arrival-time replay and New Relic querying are outside this package.

Results retain native AIPerf files, config, commands, checks, summaries and timestamped provenance. Native files may contain prompts and operational data. Keep them in your environment and select what to share. [Storage and timestamps](docs/operator-guide.md#evidence-and-storage).

Before handoff, follow the [qualification checklist](docs/operator-guide.md#handoff-check). Keep this README as the command entry point, the [operator guide](docs/operator-guide.md) for decisions/configuration/recovery, and the [metric reference](docs/metrics.md) for names, units and purpose.

## Test

```sh
make test
make test-integration AIPERF=/path/to/aiperf
make test-matrix AIPERF=/path/to/aiperf
```

Integration uses real AIPerf against a local simulated server, with no GPU or cluster. It checks payloads, authentication, pacing, repeats, metric collection and failure handling. It does not establish model performance or deployment compatibility. [Container and Job instructions](docs/operator-guide.md#execution-environment).

[Writing walkthroughs](docs/writing-guide.md)

Apache-2.0. AIPerf is a separate dependency with its own license.
