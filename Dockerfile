# syntax=docker/dockerfile:1
# Hardened, reproducible image (PRD SEC-14):
# - base image pinned by digest (Dependabot's docker ecosystem bumps it);
# - dependencies installed from the hashed lock file (`make lock`), so two
#   builds of one commit get identical packages;
# - build tools stay in the builder stage; the runtime stage has no compiler;
# - runs as an unprivileged user (uid 10001) with a HEALTHCHECK.

ARG PYTHON_IMAGE=python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce

# ── builder: resolve and install wheels into a self-contained venv ────────────
FROM ${PYTHON_IMAGE} AS builder
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.lock .
RUN pip install --require-hashes --no-deps -r requirements.lock

# ── runtime ───────────────────────────────────────────────────────────────────
FROM ${PYTHON_IMAGE} AS runtime
# WeasyPrint (PDF reports) needs Pango/Cairo; libpq5 for the Postgres client.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libpangocairo-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 \
        shared-mime-info libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLCONFIGDIR=/tmp/matplotlib \
    XDG_CACHE_HOME=/tmp/.cache

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=app:app . .
# The only paths the app writes: its Parquet/bhavcopy caches under data/ (mount
# a volume there, or tmpfs, when running with a read-only root filesystem).
RUN mkdir -p /app/data/cache && chown -R app:app /app/data

USER app
EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:7860/healthz', timeout=4).status == 200 else 1)"]

# Release step, then the app (ARCH-01): migrations run once per container
# start, before the server accepts traffic. For several replicas, run
# `python scripts/migrate.py` as a separate one-off release job instead and
# start replicas with `python app.py`.
CMD ["sh", "-c", "python scripts/migrate.py && exec python app.py"]
