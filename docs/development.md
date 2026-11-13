# Development

Toolchain, tasks, the pinned ffmpeg build and the versioning scheme.

## Layout

The package is flat; each module owns one seam.

| Module | Owns |
|---|---|
| `env.py` | reading and validating individual environment variables |
| `settings.py` | `Settings`, `load_settings` and the loudness constants |
| `limits.py` | request bounds and their environment overrides |
| `log.py` | the logfmt event line and its `logging` backend |
| `errors.py` | the OpenAI error envelope and `RequestError` |
| `middleware.py` | the body-size limit and the HTTP metrics middleware |
| `telemetry.py` | the OpenTelemetry meter, instruments, views and OTLP exporter |
| `upstream.py` | the `Upstream` client: health probe, voices, PCM synthesis and passthrough streaming |
| `request.py` | the OpenAI speech body, model catalog and model check |
| `text.py` | pure text and voice shaping: sanitise, aliases, rewrite, passthrough |
| `script.py` | the script-layer facade: `ScriptInput` → `Compiled`, no HTTP or audio |
| `script_cli.py` | the standalone compiler CLI (`python -m kokoro_tone.script_cli`) |
| `filters.py` | declarative ffmpeg blocks, their schema and the `[filter:]` builder |
| `plugins.py` | JSON and entry-point discovery for declarative filter manifests |
| `context.py` | `Context` and the FastAPI dependency that supplies it |
| `pipeline.py` | `prepare` and `render`: the order of operations, without FastAPI |
| `routes.py` | the HTTP routes, passthrough streaming and SSE envelope |
| `lexicon.py` | the deployment lexicon, its schema and reload |
| `pronounce.py` | rule precedence, the scanner and the built-in layers |
| `parse.py`, `prosody.py`, `normalise.py`, `audio.py` | markup, presets, normalisation, ffmpeg |
| `main.py` | `create_app`: the composition root, lifespan, middleware, error handlers, router |

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
| `make type-check` | mypy over `src/` |
| `make check` | format check, lint, type check, `security-check`, `pin-check` |
| `make verify` | `check` then `test` |
| `make security-check` | refuse dynamic-evaluation sinks in `src/` |
| `make pin-check` | the engine tag appears only in `engine.version` |
| `make test` | pytest |
| `make version` | print the CalVer version |
| `make engine` | print the pinned engine tag |
| `make config` | validate compose interpolation (requires `.env`) |
| `make build` / `make up` / `make down` / `make logs` | container lifecycle |
| `make narrate FILE=script.txt OUT=out.mp3` | render a transcript |
| `make script FILE=script.txt` | compile a script to spoken text, no server |
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

## Releasing

1. Bump `__version__` in `src/kokoro_tone/__init__.py` and add the
   `CHANGELOG.md` entry.
2. Run `make verify` and `make build`.
3. Merge, then tag the merge commit: `git tag vYYYY.MM.MICRO` and
   `git push origin vYYYY.MM.MICRO`.

The `release` workflow re-checks that the tag matches `__version__`, builds
`linux/amd64` and `linux/arm64` images, pushes them to
`ghcr.io/<owner>/kokoro-tone` with the full version, the `YYYY.MM` line and
`latest`, scans them with Trivy (failing on unfixed HIGH or CRITICAL findings),
and signs the image index with cosign (keyless). The GitHub release is only
created after the image job succeeds.

The `YYYY.MM` tag is a floating "newest release this month" pointer, like
`python:3.12`; the full `YYYY.MM.MICRO` tag is the immutable pin. Verify a
release with:

```bash
cosign verify ghcr.io/<owner>/kokoro-tone:YYYY.MM.MICRO \
  --certificate-identity-regexp '^https://github.com/<owner>/kokoro-tone/.github/workflows/release.yml@refs/tags/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```


