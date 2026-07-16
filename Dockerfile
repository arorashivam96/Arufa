# Multi-stage image for the Arufa FDEBench service.
# Build context: the repository root (Arufa/).
#
#   docker build -t arufa:local .
#   docker run --rm -p 8000:8000 --env-file py/apps/arufa/.env arufa:local

FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

# Install uv (matches the version pinned in py/pyproject.toml [tool.uv])
RUN pip install --no-cache-dir "uv>=0.9.5"

WORKDIR /app

# Copy the whole workspace so uv can resolve every member the lock file
# references (sample + eval + arufa + common libs). .dockerignore keeps
# .venv, caches, and .git out of the image.
COPY py ./py

WORKDIR /app/py

# Resolve into a venv at /app/py/.venv. --frozen ensures the container
# uses the same versions we tested against.
RUN uv sync --all-packages --frozen --no-dev

# ---- runtime ---------------------------------------------------------

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/py/.venv/bin:${PATH}"

# Non-root user
RUN groupadd --system --gid 1000 appuser \
 && useradd --system --uid 1000 --gid appuser --home /app appuser

WORKDIR /app

COPY --from=builder --chown=appuser:appuser /app/py /app/py

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health').read()"

# uvicorn --app-dir ensures the arufa package is importable regardless of cwd.
CMD ["uvicorn", "arufa.main:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "/app/py/apps/arufa"]
