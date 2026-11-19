# SPDX-License-Identifier: AGPL-3.0-only
# Pisa Core image: one image, several processes (pisa serve-api, pisa worker, pisa migrate).
# Multi-arch: linux/amd64 and linux/arm64 (a Raspberry Pi 4/5 on a 64-bit OS), built with
#   docker buildx build --platform linux/amd64,linux/arm64 core
# Nothing here is architecture-specific: both bases are multi-arch indexes and every locked wheel has an
# aarch64 build. Base images pinned by the index digest (the tag says which; bump both together, as relay/).
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS base
COPY --from=ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY pisa ./pisa
RUN uv sync --frozen --no-dev

# The user-voice gate's ONNX models (speaker embeddings, VAD, end of turn), fetched here once and
# checked against their pinned SHA-256 (pisa/connectors/voice_models.py); never at request time. ≈ 36 MB.
RUN /app/.venv/bin/python -m pisa.connectors.voice_models /app/models/voice

# a home directory: `pisa client` keeps its key in ~/.config/pisa
RUN useradd --system --uid 10001 --create-home pisa
USER pisa
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)" || exit 1
CMD ["pisa", "serve-api"]
