FROM ghcr.io/astral-sh/uv:python3.13-alpine

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/src/phice/.venv/bin:$PATH"

WORKDIR /src/phice

# Dependencies first, pinned by the lockfile, so code changes don't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project && rm -rf /root/.cache

COPY app.py .
COPY src ./src

RUN adduser -D -H -u 10001 phice
USER phice

EXPOSE 5000
HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:5000/healthz || exit 1

# One process so the in-memory cache is shared; threads serve concurrent pages and media streams.
CMD ["gunicorn", "-b", "0.0.0.0:5000", "-w", "1", "--threads", "16", "--timeout", "60", "--no-control-socket", "app:app"]
