# syntax=docker/dockerfile:1

# Pinned by digest: the gateway's whole job is deterministic audio processing,
# so the interpreter must not drift between rebuilds.
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS ffmpeg

# Debian's ffmpeg package is 455 MB, and ~292 MB of that is libraries the audio
# chain never loads: libllvm15 (114 MB, via mesa), libflite1 (27 MB of TTS
# voices), libmfx1, libx265, librsvg, libcodec2, libplacebo. So build one with
# exactly the filters and encoders below. It is 2.4 MB and starts in 2 ms.
#
# `--disable-everything` is the point: every enabled entry is a line the audio
# module actually calls. Adding a filter or encoder to audio.py means adding it
# here, or the request fails at runtime with "No such filter".
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential pkg-config ca-certificates xz-utils \
        librubberband-dev libmp3lame-dev libopus-dev \
    && rm -rf /var/lib/apt/lists/*

ADD --checksum=sha256:8646515b638a3ad303e23af6a3587734447cb8fc0a0c064ecdb8e95c4fd8b389 \
    https://ffmpeg.org/releases/ffmpeg-7.0.2.tar.xz /tmp/ffmpeg.tar.xz

RUN cd /tmp \
    && tar xf ffmpeg.tar.xz \
    && cd ffmpeg-7.0.2 \
    && ./configure \
        --prefix=/usr/local \
        --enable-gpl \
        --disable-everything \
        --disable-doc --disable-debug --disable-network --disable-autodetect \
        --disable-x86asm \
        --disable-ffplay --disable-ffprobe \
        --enable-librubberband --enable-libmp3lame --enable-libopus \
        --enable-filter=aresample,atempo,volume,bass,treble,lowpass,highpass,acompressor,asoftclip,alimiter,loudnorm,rubberband,asetrate,anull,aformat,format,null \
        --enable-decoder=pcm_s16le \
        --enable-encoder=pcm_s16le,libmp3lame,flac,libopus,aac \
        --enable-demuxer=pcm_s16le \
        --enable-muxer=pcm_s16le,mp3,wav,flac,ogg,adts,null \
        --enable-protocol=file,pipe \
        --extra-cflags="-Os" --extra-ldflags="-s" \
    && make -j"$(nproc)" \
    && make install \
    && strip /usr/local/bin/ffmpeg \
    && rm -rf /tmp/ffmpeg*


FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS build

# uv resolves and installs; pip is not used at build time, and uv does not ship.
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv

ENV UV_PYTHON_DOWNLOADS=0 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

LABEL org.opencontainers.image.title="kokoro-tone" \
      org.opencontainers.image.description="OpenAI-compatible TTS gateway for Kokoro-FastAPI, with prosody presets and a tone chain." \
      org.opencontainers.image.source="https://github.com/marcubus/kokoro-tone" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# Only the shared libraries the minimal ffmpeg links against, not the -dev
# packages: ~6 MB total instead of 455 MB.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        librubberband2 libmp3lame0 libopus0 libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=ffmpeg /usr/local/bin/ffmpeg /usr/local/bin/ffmpeg
COPY --from=build /app/.venv /app/.venv
COPY scripts ./scripts
COPY LICENSE docker/THIRD_PARTY_NOTICES.md /usr/share/doc/kokoro-tone/

# Non-root, no shell, no home: the container is read-only apart from /tmp.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin tone
USER 10001:10001

EXPOSE 8000

CMD ["uvicorn", "kokoro_tone.main:app", "--host", "0.0.0.0", "--port", "8000"]
