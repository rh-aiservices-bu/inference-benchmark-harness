FROM python:3.12-slim AS builder
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
WORKDIR /opt/harness
COPY pyproject.toml README.md LICENSE ./
COPY bench ./bench
RUN python -m pip install --no-cache-dir '.[runtime]'

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends make && rm -rf /var/lib/apt/lists/*
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
WORKDIR /opt/harness
COPY Makefile ./
ENV PYTHONDONTWRITEBYTECODE=1
ENV HF_HOME=/tmp/huggingface
USER 10001:10001
ENTRYPOINT ["python", "-m", "bench"]
