# Inference benchmark harness

Run bounded AIPerf tests against an existing streaming Chat Completions endpoint. Preserve each attempt and validate it before continuing.

## Where to start

Benchmark to find a workload's usable capacity, check latency targets and compare serving policies before sharing GPUs across applications.

| Need | Start here |
|---|---|
| Get a first test running | [Daniel's standalone AIPerf guide](https://github.com/dandawg/llm-d-flow-control-demo/blob/main/benchmarks/standalone-guide.md). His [demo repository](https://github.com/dandawg/llm-d-flow-control-demo) also provides a deployment scaffold |
| Run repeatable experiments | This harness checks inputs, runs isolated or mixed workloads, repeats measurements and preserves evidence |
| Understand the shared-GPU business case and prior experiments | [Alexa's flow-control benchmarks](https://github.com/alexagriffith/flow-control-benchmarks) document scenarios, results and their evidence limits |
| Understand or configure the serving stack | [llm-d](https://github.com/llm-d/llm-d), [documentation](https://llm-d.ai/docs) and [router / flow control](https://github.com/llm-d/llm-d-router) |

## Install and configure

Python 3.11+ on Linux or macOS. AIPerf is pinned to 0.12.0.

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

From the checkout, replace `/path/...` with your config and durable output paths. Complete each check before the next step. Preserve failures and use [debugging](docs/operator-guide.md#debugging) if a command fails.

1. Preview the smoke command and budget. Confirm the endpoint and inputs.

   ```sh
   make plan-smoke CONFIG=/path/benchmark.json RUN=/path/results/smoke
   ```

2. Verify access, runtime and required metrics. Continue only when the result is `ready_for_smoke`.

   ```sh
   make verify CONFIG=/path/benchmark.json
   ```

3. Send one short smoke request. Check its evidence. Establish [cache/warmup and drain conditions](docs/operator-guide.md#run-sequence) before measuring.

   ```sh
   make smoke CONFIG=/path/benchmark.json RUN=/path/results/smoke
   ```

4. Preview the measured sweep, then execute the reviewed budget in a separate directory.

   ```sh
   make plan CONFIG=/path/benchmark.json RUN=/path/results/sweep
   make benchmark CONFIG=/path/benchmark.json RUN=/path/results/sweep
   ```

5. Read the saved status and repeat summaries. An incomplete or unsuccessful campaign returns nonzero. `complete` without goals does not establish suitability.

   ```sh
   make report RUN=/path/results/sweep
   ```

`make help` lists commands. Plans send no traffic or network requests. `verify` reads endpoints but sends no inference. `benchmark` is an alias for `sweep`.

The example uses **three valid repeats per point**. Invalid evidence stops immediately. A valid goal miss or request error is retained. Remaining repeats at that point finish before higher load is stopped. Inference is never automatically retried. See [recovery](docs/operator-guide.md#recovery) before using `make resume CONFIG=/path/benchmark.json RUN=/path/results/sweep`.

## Several workloads or experiments

Copy `examples/matrix.json`, `benchmark.json` and `background.json` together. Edit their endpoint, workload and budgets. The matrix runs each row's streams together and checkpoints whole repeats.

```sh
make matrix-plan MATRIX=/path/matrix.json RUN=/path/results/matrix
make matrix-run MATRIX=/path/matrix.json RUN=/path/results/matrix
make report RUN=/path/results/matrix
```

`matrix-pause` finishes the current group before pausing. `matrix-resume` rechecks inputs and continues after accepted groups. Use the same `MATRIX` and `RUN`. Invalid peers or insufficient traffic overlap invalidate the whole repeat. See [matrix configuration and policy comparisons](docs/operator-guide.md#matrix-configuration). After a crash at the final row checkpoint, resume finalizes the saved outcomes without replaying accepted groups.

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
