FROM python:3.13-slim

LABEL org.opencontainers.image.title="congress-mcp"
LABEL org.opencontainers.image.source="https://github.com/barryw/congress-mcp"

COPY --from=ghcr.io/astral-sh/uv:0.9.21 /uv /bin/uv

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project && rm -rf /root/.cache

COPY server.py ./

USER 10001:10001
EXPOSE 8080
CMD ["/app/.venv/bin/python", "server.py"]
