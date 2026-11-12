"""Environment readers shared by the settings loaders.

Every reader logs to stderr and falls back to its default rather than raising:
a typo in `.env` must never stop the container from starting. An empty value is
treated as unset.
"""

from __future__ import annotations

import os

from .log import warning

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
        # Plain integers keep full precision; going via float first would round
        # anything above 2**53 and turn "3.9" into 3.
        value = int(raw)
    except ValueError:
        # Still accept an integral decimal such as "4096.0", but reject
        # fractional values and "inf"/"nan". `int()` on those raises
        # OverflowError/ValueError, so they must be filtered before the cast.
        try:
            number = float(raw)
        except ValueError:
            warning(f"{name}={raw!r} is not a whole number; using {default}")
            return default
        if not number.is_integer():
            warning(f"{name}={raw!r} is not a whole number; using {default}")
            return default
        value = int(number)
    if value <= 0:
        warning(f"{name}={value} must be positive; using {default}")
        return default
    return value


def read_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        warning(f"{name}={raw!r} is not a number; using {default}")
        return default


def read_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    warning(f"{name}={raw!r} is not a boolean; using {default}")
    return default
