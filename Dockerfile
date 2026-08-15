# Multi-stage: build the venv with uv, then copy only the venv + app into a slim runtime
# image. Render's free tier is 512MB RAM -- the build stage's weight (uv, the wheel cache,
# fastembed's build dependencies) never reaches the image that actually runs.

FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Dependencies first, separate from app code, so an app-only change doesn't invalidate the
# (slow: fastembed pulls in onnxruntime) dependency-install layer.
#
# why README.md is copied here, not with app/ below: hatchling reads `readme = "README.md"`
# from pyproject.toml and refuses to build the project metadata without the file present --
# found by actually running this build, not by inspecting the Dockerfile. `uv sync
# --no-install-project` still triggers that validation even though it isn't installing the
# project package itself yet.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY app/ ./app/
RUN uv sync --frozen --no-dev

FROM python:3.12-slim AS runtime

# why: fastembed's ONNX runtime needs libgomp at runtime, not just at build time -- omitting
# it here produces an image that builds cleanly and fails on the first embedding call.
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# why: locally, GitHubMcpClient shells out to `docker run ghcr.io/github/github-mcp-server`
# (QUORUM_GITHUB_MCP_COMMAND=docker) because a dev machine already has Docker. A container
# running on Render has no docker binary and no socket to talk to one -- confirmed by actually
# running this image without one: `FileNotFoundError: [Errno 2] No such file or directory:
# 'docker'`, not a vague timeout. Vendoring the server's own binary into this image and
# invoking it directly as a plain subprocess avoids needing Docker-in-Docker at all.
# QUORUM_GITHUB_MCP_COMMAND/QUORUM_GITHUB_MCP_ARGS are overridden at deploy time (render.yaml)
# to point at it; local dev is untouched.
COPY --from=ghcr.io/github/github-mcp-server:latest /server/github-mcp-server /usr/local/bin/github-mcp-server

RUN useradd --create-home --uid 1000 quorum
WORKDIR /app
COPY --from=builder --chown=quorum:quorum /app/.venv /app/.venv
COPY --chown=quorum:quorum app/ ./app/

USER quorum
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "app.interface.composition:app", "--host", "0.0.0.0", "--port", "8000"]

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1
