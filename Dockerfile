FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        poppler-utils \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-nld \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.lock ./requirements.lock
RUN python -m pip install --no-cache-dir --disable-pip-version-check --require-hashes -r requirements.lock \
    && rm -rf /var/lib/apt/lists/* /root/.cache/pip

COPY src ./src
COPY config/config_template.ini ./config/config_template.ini
# Containers start from the reviewed fail-closed defaults; environment values
# still override deployment-specific paths and secrets at runtime.
COPY config/config_template.ini ./config/config.ini

RUN groupadd --system --gid 10001 fab \
    && useradd --system --uid 10001 --gid fab --home-dir /app fab \
    && mkdir -p data backups credentials tokens downloads/sort-out logs output/support \
    && chown -R fab:fab /app

USER fab

EXPOSE 5001

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD ["python", "-m", "src.run_health_probe", "api"]

CMD ["python", "-m", "src.operations.local_api"]
