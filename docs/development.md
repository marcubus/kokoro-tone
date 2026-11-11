# Development

Toolchain, tasks, the pinned ffmpeg build and the versioning scheme.

## Layout

The package is flat; each module owns one seam.

| Module | Owns |
|---|---|
| `env.py` | reading and validating individual environment variables |
| `settings.py` | `Settings`, `load_settings` and the loudness constants |
| `limits.py` | request bounds and their environment overrides |
| `errors.py` | the OpenAI error envelope |
| `middleware.py` | the body-size limit |
| `upstream.py` | every engine call: health probe, voices, PCM synthesis and passthrough streaming |
| `request.py` | request validation, voice aliases, markup rewriting and passthrough detection |
| `routes.py` | the HTTP routes, speech orchestration and SSE envelope |
| `lexicon.py` | the deployment lexicon, its schema and reload |
| `pronounce.py` | rule precedence, the scanner and the built-in layers |
| `parse.py`, `prosody.py`, `normalise.py`, `audio.py` | markup, presets, normalisation, ffmpeg |
| `main.py` | app assembly: lifespan, middleware, error handlers, router |

## Tasks

mise pins the toolchain; make runs the tasks; uv manages dependencies.

```bash
mise install      # python + uv
uv sync           # dependencies from uv.lock
make check        # format, lint, security and pin checks
make test         # pytest
```

| Target | Does |
|---|---|
| `make install` | `uv sync --frozen` |
| `make lint` / `make fmt` | ruff check / ruff format |
| `make check` | format check, lint, `security-check`, `pin-check` |
| `make security-check` | refuse dynamic-evaluation sinks in `src/` |
| `make pin-check` | the engine tag appears only in `engine.version` |
| `make test` | pytest |
| `make version` | print the CalVer version |
| `make engine` | print the pinned engine tag |
| `make config` | validate compose interpolation (requires `.env`) |
| `make build` / `make up` / `make down` / `make logs` | container lifecycle |
| `make narrate FILE=script.txt OUT=out.mp3` | render a transcript |
| `make analyse FILE=out.mp3` | measure F0, syllables/s, centroid, LUFS, TP |

`ffmpeg` must be on `PATH` for the audio tests.

## The pinned ffmpeg build

Debian's `ffmpeg` package is 455 MB, and ~292 MB of that is libraries the audio
chain never loads: `libllvm15` (114 MB, pulled in by mesa), `libflite1` (27 MB of
speech-synthesis voices), `libmfx1`, `libx265`, `librsvg`, `libcodec2`,
`libplacebo`. The image therefore compiles ffmpeg from a pinned source tarball
with `--disable-everything` and enables only what `audio.py` calls: the
`rubberband`, `atempo`, `volume`, `bass`, `treble`, `lowpass`, `highpass`,
`acompressor`, `asoftclip`, `alimiter`, `loudnorm`, `aresample` and `asetrate`
filters, `pcm_s16le` in and out, and the `libmp3lame`, `flac`, `libopus`, `aac`
and `pcm_s16le` encoders. That is a 2.4 MB binary which starts in 2 ms, and the
image is 164 MB instead of 656 MB.

**A new filter or encoder in `audio.py` needs a matching `--enable-filter` /
`--enable-encoder` in the `Dockerfile`.** Without it the request fails at
runtime with `No such filter`, not at build time. `GET /health` reports
`rubberband: true`, which is the canary for the pitch path.

Alpine is not an option: its ffmpeg package is built without `librubberband`, so
the pitch path would silently fall back to `asetrate` and shift formants.

## Versioning

CalVer in the form `YYYY.MM.MICRO`. `MICRO` starts at `0` each month and
increments per release; tags are prefixed with `v`. The version lives in
`src/kokoro_tone/__init__.py`, and CI's `tag-check` fails if a tag disagrees
with it.

