FROM ghcr.io/astral-sh/uv:python3.13-alpine

WORKDIR /src/phice
COPY pyproject.toml .
RUN uv sync

COPY app.py .
COPY src ./src

# One process so the in-memory cache is shared; threads serve concurrent pages and media streams.
CMD ["uv", "run", "gunicorn", "-b", "0.0.0.0:5000", "-w", "1", "--threads", "16", "--timeout", "60", "app:app"]
