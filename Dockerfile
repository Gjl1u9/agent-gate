FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependencies first, so editing the source does not invalidate this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY README.md ./
COPY src ./src
RUN uv sync --locked --no-dev

ENV PATH="/app/.venv/bin:$PATH" PORT=8080
EXPOSE 8080

# Cloud Run sets PORT. Bind to 0.0.0.0 so traffic from outside the container reaches the app,
# and exec so uvicorn receives SIGTERM directly and shuts down cleanly.
CMD ["sh", "-c", "exec uvicorn agent_gate.main:app --host 0.0.0.0 --port ${PORT}"]
