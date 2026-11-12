# Filter plugins

A plugin adds a `[filter:…]` block. It is **data, not code**: a constant ffmpeg
template plus typed, bounded numeric parameters. The gateway formats the numbers
and checks the result against the filter allowlist, so a plugin cannot introduce
a new execution sink or a client-supplied filter string.

There are two ways to load one.

## 1. A JSON file (quickest)

Point `KOKORO_TONE_FILTER_MANIFESTS` at a file or a directory of `*.json` files:

```bash
export KOKORO_TONE_FILTER_MANIFESTS=/etc/kokoro-tone/filters
```

Each file is one manifest, or several blocks keyed by name:

```json
{
  "telephone": {
    "summary": "A narrow, mid-band voice.",
    "template": "highpass=f={low}:lowpass=f={high}",
    "params": [
      {"name": "low", "kind": "int", "min": 200, "max": 2000, "default": 500},
      {"name": "high", "kind": "int", "min": 2000, "max": 8000, "default": 3000}
    ]
  }
}
```

Then:

```json
{"input": "[filter:telephone low=400 high=3200] Can you hear me?"}
```

Files load last, so a local manifest overrides an installed plugin with the same
block name. The gateway caches the registry for the process; restart it after an
edit. The CLI is a fresh process, so it picks the file up immediately.

## 2. An installed distribution

Advertise an entry point in the `kokoro_tone.filters` group:

```toml
[project.entry-points."kokoro_tone.filters"]
my_filters = "my_filters:manifest"
```

```python
# my_filters.py
def manifest() -> dict:
    return {
        "telephone": {
            "summary": "A narrow, mid-band voice.",
            "template": "highpass=f={low}:lowpass=f={high}",
            "params": [
                {"name": "low", "kind": "int", "min": 200, "max": 2000, "default": 500},
                {"name": "high", "kind": "int", "min": 2000, "max": 8000, "default": 3000},
            ],
        }
    }
```

The entry point may resolve to a mapping or to a zero-argument callable returning
one. The callable runs once, at registry build; it is not a per-request hook.
Only install distributions you trust to run at import, exactly as with any
Python package.

## The block schema

| Field | Required | Meaning |
|---|---|---|
| `template` | yes | A constant ffmpeg filter expression. `{name}` placeholders refer to declared params. |
| `params` | yes (may be empty) | A list of parameter objects. |
| `summary` | no | One line shown by `--list-filters`. |

A parameter:

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Lowercase identifier, matching a `{name}` in the template. |
| `kind` | no | `"float"` (default) or `"int"`. An `int` rejects a fractional value. |
| `min` / `max` | yes | Inclusive bounds. A value outside them is a `400`. |
| `default` | no | Used when the tag omits the key; defaults to `min`. |

Validation rejects:

- an undeclared placeholder in the template
- a filter name outside `filters.ALLOWED_FILTERS`
- an expression outside the constants-only grammar
- a parameter whose default is outside its bounds
- duplicate parameter names

A rejected manifest is logged and skipped; the gateway still starts.

## Discovering blocks

```bash
python -m kokoro_tone.script_cli --list-filters
```

prints every block, its template and its parameter bounds as JSON. `GET /health`
lists the names in `filters`. The built-ins are defined in
[`../src/kokoro_tone/filters.py`](../src/kokoro_tone/filters.py).

## Adding a new ffmpeg filter

The template may only name a filter in `ALLOWED_FILTERS`, and the pinned ffmpeg
build must have it enabled. Adding a name means editing both:

1. `ALLOWED_FILTERS` in `filters.py`
2. the matching `--enable-filter` in the `Dockerfile`

Without the second, the request fails at runtime with `No such filter`, not at
build time. See [the pinned ffmpeg build](development.md#the-pinned-ffmpeg-build).

## Why not arbitrary filter strings?

Because the security model is the point. `audio._filters` refuses anything
outside a fixed character grammar, and `make security-check` fails the build on
dynamic-evaluation sinks. A client that can send `ffmpeg -af …` is a client that
can send anything. A typed block is the smallest extension that keeps the
invariant while still surfacing real ffmpeg capability.
