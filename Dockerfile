FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

WORKDIR /app
ENV UV_LINK_MODE=copy \
    BACKLOGGD_SYNC_CONFIG=/app/config.toml \
    BACKLOGGD_SYNC_DATA=/app/data

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
COPY . .
RUN uv sync --frozen --no-dev

ENTRYPOINT ["uv", "run", "backloggd-sync"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8787"]
