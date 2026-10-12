FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.18 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_DEV=1

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project
COPY README.md ./
COPY src ./src
RUN uv sync --locked

ENV PATH="/app/.venv/bin:$PATH" PORT=8000
EXPOSE 8000
USER nobody
HEALTHCHECK --interval=30s --timeout=3s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')"
CMD ["nidol"]
