FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /workspace

COPY pyproject.toml README.md ./
COPY orch ./orch
COPY scripts ./scripts
COPY tests ./tests
COPY examples ./examples

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -e ".[dev]"
