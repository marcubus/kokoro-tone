# Security policy

`kokoro-tone` sits in front of a speech engine and handles bearer tokens, so
please report issues privately.

## Reporting a vulnerability

Open a private security advisory on GitHub:

<https://github.com/marcubus/kokoro-tone/security/advisories/new>

If that is unavailable, contact the maintainer directly. Please do not open a
public issue for anything that could expose a token or allow remote code
execution.

Include:

- the version or commit you ran,
- the request body (with any private text removed),
- what happened, and what you expected,
- the relevant output with **all secrets removed**.

## Never paste

- `KOKORO_TONE_API_KEY` or `KOKORO_TONE_API_KEYS`,
- `KOKORO_FASTAPI_API_KEY` or `UPSTREAM_API_KEY`,
- private transcripts you do not want published.

## Supported versions

Only the latest commit on `main` is supported. Fixes are released as new
commits; pin a commit SHA if you need reproducibility.

## Scope

In scope: the gateway code in `src/kokoro_tone/`, the container image, and the
compose file.

Out of scope: Kokoro-FastAPI itself and the ffmpeg build. Report those upstream.
