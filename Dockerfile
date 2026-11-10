# syntax=docker/dockerfile:1

# Pinned by digest: the gateway's whole job is deterministic audio processing,
# so the interpreter and ffmpeg build must not drift between rebuilds.
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

# uv resolves and installs; pip is not used at build time.
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=0 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    PATH="/app/.venv/bin:$PATH"

# Debian's ffmpeg carries librubberband (formant-preserving pitch) and
# libmp3lame/libopus, which the prosody and encode stages both need.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

COPY scripts ./scripts

# Non-root, no shell, no home: the container is read-only apart from /tmp.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin tone
USER 10001:10001

EXPOSE 8000

CMD ["uvicorn", "kokoro_tone.main:app", "--host", "0.0.0.0", "--port", "8000"]
