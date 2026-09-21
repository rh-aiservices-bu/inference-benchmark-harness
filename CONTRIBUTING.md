# Contributing

[AGENTS.md](AGENTS.md) contains instructions for AI-assisted operation and development. If your tool does not load it automatically, include it in the task context.

Run `make test` for offline checks. Changes to AIPerf execution also require `make test-integration` and `make test-matrix` against the local fixture server.

Update the affected command examples, configuration reference and behavior tests with code changes. Follow the [writing guide](docs/writing-guide.md) for documentation.
