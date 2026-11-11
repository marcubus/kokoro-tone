"""Environment readers shared by the settings loaders.

Every reader logs to stderr and falls back to its default rather than raising:
a typo in `.env` must never stop the container from starting. An empty value is
treated as unset.
"""

from __future__ import annotations

import os
import sys

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def read_str(name: str, default: str) -> str:
    raw = os.environ.get(name, "").strip()
    return raw or default


def read_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(float(raw))
    except ValueError:
        print(f"{name}={raw!r} is not a number; using {default}", file=sys.stderr)
        return default
    if value <= 0:
        print(f"{name}={value} must be positive; using {default}", file=sys.stderr)
        return default
    return value


def read_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"{name}={raw!r} is not a number; using {default}", file=sys.stderr)
        return default


def read_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    print(f"{name}={raw!r} is not a boolean; using {default}", file=sys.stderr)
    return default
