# Build from the repo root (the backend also serves ../frontend):
#   docker build -f backend/Dockerfile -t flash-sale-backend .
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    PYTHONUNBUFFERED=1 \
    PATH="/srv/backend/.venv/bin:$PATH"

WORKDIR /srv/backend

# Install the exact versions from uv.lock (no dev tools).
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

COPY backend/ .
COPY frontend/ /srv/frontend/

EXPOSE 8000
# Migrations, then the server; demo data too when SEED_DEMO=true.
CMD ["sh", "scripts/start.sh"]
