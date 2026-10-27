# syntax=docker/dockerfile:1
# DocMind API and indexing worker: one image, two commands.
#
#   docker build -t docmind:local .
#   API:    (default CMD)                 uvicorn on :8000
#   Worker: python -m app.worker
#   Migrations (one-shot, never in every process): alembic upgrade head
#
# Dependencies come only from requirements/runtime.txt (hash-pinned, generated in Linux
# with uv; PyTorch from the CPU index, so no CUDA libraries). No model weights are baked
# in: the embedding model lives in the HF cache volume (HF_HOME=/data/hf). No secrets are
# baked in: everything comes from the environment at run time.

ARG PYTHON_IMAGE=python:3.13-slim-bookworm@sha256:e32be0e55814a08977501ca863271d9bd02443710db8f2afaf8372e2c4508530

FROM ${PYTHON_IMAGE} AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY requirements/runtime.txt /tmp/runtime.txt
# --require-hashes: every wheel must match the lock; --no-deps: nothing unlocked sneaks in.
RUN pip install --require-hashes --no-deps \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        -r /tmp/runtime.txt \
    && find /opt/venv -name "__pycache__" -type d -prune -exec rm -rf {} +

FROM ${PYTHON_IMAGE} AS runtime
LABEL org.opencontainers.image.title="docmind" \
      org.opencontainers.image.description="DocMind API and indexing worker"
# Unprivileged user with a fixed uid, and the only writable paths: document storage and
# the model cache (volumes in Compose); /tmp is a tmpfs when the root fs is read-only.
RUN groupadd --system --gid 10001 docmind \
    && useradd --system --uid 10001 --gid docmind --home-dir /home/docmind --create-home \
       --shell /usr/sbin/nologin docmind \
    && mkdir -p /data/storage /data/hf \
    && chown -R docmind:docmind /data
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
# Code is owned by root and read-only for the runtime user.
COPY alembic.ini pyproject.toml ./
COPY alembic ./alembic
COPY app ./app
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/data/hf \
    DOCMIND_STORAGE_DIR=/data/storage \
    DOCMIND_LOG_FORMAT=json \
    DOCMIND_WORKER_HEARTBEAT_FILE=/tmp/docmind-worker-heartbeat
USER docmind:docmind
EXPOSE 8000
# Liveness of the API process (no database, no model). Compose overrides it per service.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4)"]
# Exec form: uvicorn is PID 1 and receives SIGTERM directly (graceful shutdown).
# --no-proxy-headers: client addresses come only from the app's DOCMIND_TRUSTED_PROXIES
# logic; --no-access-log: the app writes its own access log without query strings.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--no-proxy-headers", "--no-access-log", "--timeout-graceful-shutdown", "20"]
