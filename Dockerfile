# Single image shared by web, worker and beat. Dev mounts the source over /app, so the
# virtualenv must live outside /app or the bind mount would hide it.
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.10.6 /uv /uvx /usr/local/bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_FROZEN=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

COPY . .

# After COPY . . so it busts only the cheap layer — it changes on every commit, and
# ahead of the copy it would rebuild `uv sync` for nothing. The deploy job asserts
# /app/RELEASE == the pushed SHA, so the `unknown` default makes a hand-build that
# forgot --build-arg fail that assertion instead of passing with a stale value.
# Dev bind-mounts source over /app and hides this file; nothing there reads it.
ARG GIT_SHA=unknown
RUN echo "$GIT_SHA" > /app/RELEASE

EXPOSE 8000
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
