PYTHON ?= python3
CONFIG ?= benchmark.local.json
ifeq ($(origin RUN),undefined)
RUN = results/benchmark
smoke plan-smoke resume-smoke: RUN = results/smoke
matrix-plan matrix-run matrix-resume matrix-pause: RUN = results/matrix
endif
AIPERF ?= aiperf
FORMAT ?= auto
CONTAINER_ENGINE ?= docker
IMAGE ?= inference-benchmark-harness:0.1.0
TEST_ARTIFACTS ?= $(CURDIR)/.container-test-results
MATRIX ?=

.PHONY: benchmark help plan plan-smoke verify smoke sweep resume report test test-integration resume-smoke image test-container
help:
	@echo 'configure        Write a workload config; pass --url and --model in ARGS'
	@echo 'matrix-create    Generate a validated matrix; pass CLI options in ARGS'
	@echo 'matrix-plan      Preview named experiments, concurrent streams and total budget'
	@echo 'matrix-run       Run MATRIX, checkpointing whole mixed repeats'
	@echo 'matrix-resume    Resume MATRIX after diagnosing the saved stop reason'
	@echo 'matrix-pause     Finish the current group, then pause at a checkpoint'
	@echo 'plan             Print commands without network requests or writes'
	@echo 'plan-smoke       Preview the one-request smoke and its budget'
	@echo 'verify           Read endpoint and metrics; send no inference'
	@echo 'smoke            Send one short request'
	@echo 'sweep/benchmark  Run configured points and repeats sequentially'
	@echo 'resume           Continue a stopped campaign after reviewing its reason'
	@echo 'resume-smoke     Resume a failed smoke with its original smoke configuration'
	@echo 'report           Summarize saved results; FORMAT=json for details'
	@echo 'test             Run contract tests without AIPerf or a cluster'
	@echo 'test-integration Run AIPerf against a local test server; no GPU needed'
	@echo 'image            Build IMAGE with CONTAINER_ENGINE (default: docker)'
	@echo 'test-container   Test the built image; save fixtures to TEST_ARTIFACTS'
	@echo 'Default config: benchmark.local.json. Results: results/smoke, results/benchmark or results/matrix.'
	@echo 'Override CONFIG, RUN or AIPERF when needed. Existing runs are never overwritten.'
plan:
	$(PYTHON) -m bench plan --config "$(CONFIG)" --run "$(RUN)" --aiperf "$(AIPERF)"
plan-smoke:
	$(PYTHON) -m bench plan --config "$(CONFIG)" --run "$(RUN)" --aiperf "$(AIPERF)" --smoke
verify:
	@$(PYTHON) -m bench verify --config "$(CONFIG)" --aiperf "$(AIPERF)" --format "$(FORMAT)"
smoke:
	$(PYTHON) -m bench run --config "$(CONFIG)" --run "$(RUN)" --aiperf "$(AIPERF)" --smoke --execute
sweep:
	$(PYTHON) -m bench run --config "$(CONFIG)" --run "$(RUN)" --aiperf "$(AIPERF)" --execute
resume:
	$(PYTHON) -m bench run --config "$(CONFIG)" --run "$(RUN)" --aiperf "$(AIPERF)" --resume --execute
resume-smoke:
	$(PYTHON) -m bench run --config "$(CONFIG)" --run "$(RUN)" --aiperf "$(AIPERF)" --smoke --resume --execute
report:
	@$(PYTHON) -m bench report --run "$(RUN)" --format "$(FORMAT)"
test:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v
test-integration:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) tests/integration.py --aiperf "$(AIPERF)"

benchmark: sweep

.PHONY: matrix-plan matrix-run matrix-resume matrix-pause test-matrix require-matrix
require-matrix:
	@test -n "$(MATRIX)" || { echo 'Set MATRIX=/path/to/matrix.json; matrix commands do not use CONFIG.' >&2; exit 2; }
matrix-plan matrix-run matrix-resume: require-matrix
matrix-plan:
	$(PYTHON) -m bench matrix-plan --config "$(MATRIX)" --run "$(RUN)" --aiperf "$(AIPERF)"
matrix-run:
	$(PYTHON) -m bench matrix-run --config "$(MATRIX)" --run "$(RUN)" --aiperf "$(AIPERF)" --execute
matrix-resume:
	$(PYTHON) -m bench matrix-run --config "$(MATRIX)" --run "$(RUN)" --aiperf "$(AIPERF)" --execute --resume
matrix-pause:
	$(PYTHON) -m bench matrix-pause --run "$(RUN)"
test-matrix:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) tests/integration_matrix.py --aiperf "$(AIPERF)"

image:
	$(CONTAINER_ENGINE) build -f Containerfile -t "$(IMAGE)" .

test-container:
	@mkdir -p "$(TEST_ARTIFACTS)"
	$(CONTAINER_ENGINE) run --rm --read-only --tmpfs /tmp:rw,size=1g \
	  -v "$(CURDIR)/tests:/opt/harness/tests:ro" \
	  -v "$(CURDIR)/examples:/opt/harness/examples:ro" \
	  -v "$(abspath $(TEST_ARTIFACTS)):/results:rw" -e TMPDIR=/results \
	  --entrypoint python "$(IMAGE)" -c 'import os, subprocess, sys; os.access("/results", os.W_OK) or sys.exit("TEST_ARTIFACTS must be writable by container UID 10001; choose an approved writable directory"); subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"], check=True); subprocess.run([sys.executable, "tests/integration.py", "--aiperf", "/opt/venv/bin/aiperf"], check=True); subprocess.run([sys.executable, "tests/integration_matrix.py", "--aiperf", "/opt/venv/bin/aiperf"], check=True)'

.PHONY: matrix-create
matrix-create:
	$(PYTHON) -m bench matrix-create $(ARGS)

.PHONY: configure
configure:
	$(PYTHON) -m bench configure --output "$(CONFIG)" $(ARGS)
