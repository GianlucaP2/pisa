# SPDX-License-Identifier: AGPL-3.0-only
# Pisa push relay: one small process. The SQLite file lives in /data (a volume); the APNs key is mounted.
# Multi-arch: linux/amd64 and linux/arm64 (a Raspberry Pi 4/5 on a 64-bit OS); both bases are multi-arch
# indexes, so `docker buildx build --platform linux/amd64,linux/arm64 relay` needs nothing else.
# Base images pinned by the index digest (the tag says which; bump both together).
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
COPY --from=ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c /uv /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 RELAY_DATABASE=/data/relay.sqlite3
WORKDIR /app

COPY pyproject.toml uv.lock LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project

COPY pisa_relay ./pisa_relay
RUN uv sync --frozen --no-dev

RUN useradd --system --uid 10001 relay && mkdir /data && chown relay /data
USER relay
VOLUME /data
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz').status==200 else 1)" || exit 1
# one worker (SQLite, one writer); no access log: it would record callers' addresses and every request
CMD ["uvicorn", "--factory", "pisa_relay.app:app_from_env", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log", "--no-server-header"]
