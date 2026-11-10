#!/usr/bin/env python3
"""Transcript file -> mastered audio, through the local TTS gateway.

Runs with the standard library only, so it works inside the `kokoro-tone` image:

    docker compose exec -T kokoro-tone \
        python scripts/narrate.py - --out - --url http://127.0.0.1:8000 \
        > narration.mp3

The gateway publishes no host port; that is deliberate.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

FORMATS = ("mp3", "wav", "flac", "opus", "aac", "pcm")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transcript", help="transcript path, or - for stdin")
    parser.add_argument("--out", default="-", help="output path, or - for stdout")
    parser.add_argument(
        "--url",
        default=os.environ.get("KOKORO_TONE_URL", "http://kokoro-tone:8000"),
        help="gateway base URL (default: http://kokoro-tone:8000)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("KOKORO_TONE_API_KEY", ""),
        help="bearer token (default: $KOKORO_TONE_API_KEY)",
    )
    parser.add_argument("--voice", default="af_heart", help="default voice")
    parser.add_argument("--instructions", default=None, help="free-text mood")
    parser.add_argument("--format", default="mp3", choices=FORMATS)
    parser.add_argument("--speed", type=float, default=1.0)
    return parser.parse_args()


def read_transcript(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def main() -> int:
    args = parse_args()
    text = read_transcript(args.transcript)
    if not text.strip():
        print("transcript is empty", file=sys.stderr)
        return 2

    payload: dict[str, object] = {
        "model": "kokoro-v1_0",
        "input": text,
        "voice": args.voice,
        "response_format": args.format,
        "speed": args.speed,
    }
    if args.instructions:
        payload["instructions"] = args.instructions

    headers = {"Content-Type": "application/json"}
    if args.api_key:
        headers["Authorization"] = f"Bearer {args.api_key}"

    request = urllib.request.Request(
        f"{args.url.rstrip('/')}/v1/audio/speech",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=900) as response:
            audio = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        print(f"gateway returned {exc.code}: {detail}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"gateway unreachable at {args.url}: {exc.reason}", file=sys.stderr)
        return 1

    if args.out == "-":
        sys.stdout.buffer.write(audio)
        sys.stdout.buffer.flush()
    else:
        with open(args.out, "wb") as handle:
            handle.write(audio)
        print(f"wrote {args.out} ({len(audio)} bytes)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
