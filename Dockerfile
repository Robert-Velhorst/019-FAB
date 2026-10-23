FROM python:3.15.1-slim-trixie@sha256:08275f764e022331c6d3e3ca55d8cce01fa4684b02111adf0445d71806fe7941 AS sqlite-builder

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY sqlite-runtime.json fab_sqlite_runtime.py ./
COPY scripts/setup_sqlite_runtime.py ./scripts/setup_sqlite_runtime.py
RUN python scripts/setup_sqlite_runtime.py

FROM python:3.15.1-slim-trixie@sha256:08275f764e022331c6d3e3ca55d8cce01fa4684b02111adf0445d71806fe7941

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

ARG FAB_ENABLE_ML=false

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        poppler-utils \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-nld \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.lock requirements-ml.lock ./
RUN python -m pip install --no-cache-dir --disable-pip-version-check --require-hashes -r requirements.lock \
    && if [ "$FAB_ENABLE_ML" = "true" ]; then python -m pip install --no-cache-dir --disable-pip-version-check --require-hashes -r requirements-ml.lock; fi \
    && rm -rf /var/lib/apt/lists/* /root/.cache/pip

COPY --from=sqlite-builder /app/.fab-runtime ./.fab-runtime
COPY sqlite-runtime.json fab_sqlite_runtime.py ./
COPY runtime_bootstrap ./runtime_bootstrap
ENV PYTHONPATH=/app/runtime_bootstrap:/app
RUN python -c "import sys, sqlite3; assert sys.version_info[:3] == (3, 15, 1); assert sqlite3.sqlite_version == '3.54.1'"

COPY src ./src
COPY config/config_template.ini ./config/config_template.ini
# Containers start from the reviewed fail-closed defaults; environment values
# still override deployment-specific paths and secrets at runtime.
COPY config/config_template.ini ./config/config.ini

RUN groupadd --system --gid 10001 fab \
    && useradd --system --uid 10001 --gid fab --home-dir /app fab \
    && mkdir -p data backups credentials tokens downloads/sort-out logs output/support \
    && chown -R fab:fab data backups credentials tokens downloads logs output config

USER fab

RUN python -c "import os, sqlite3; assert os.getuid() == 10001; assert sqlite3.sqlite_version == '3.54.1'"

EXPOSE 5001

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD ["python", "-m", "src.run_health_probe", "api"]

CMD ["python", "-m", "src.operations.local_api"]
