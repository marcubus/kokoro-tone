"""Discovery of declarative plugin manifests.

There are two ways to add a filter block, and neither executes client data:

1. **A JSON file.** Point `KOKORO_TONE_FILTER_MANIFESTS` at a file or a
   directory of `*.json` files. This is the quick path for a lab experiment:
   edit the file, restart the gateway (or rerun the CLI).
2. **An installed distribution.** Advertise an entry point in the
   `kokoro_tone.filters` group. The entry point resolves to a mapping, or to a
   zero-argument callable returning one.

Both payloads are validated by `filters.parse_block`. A bad manifest is skipped
with a warning, so one typo cannot stop the gateway from starting. Nothing from
a manifest is executed per request: the template is a constant and the client
supplies numbers.

This is deliberately not a code plugin API. The security model in `AGENTS.md`
says filter expressions are built from constants; a manifest can add a constant
template and typed numbers, not a new code path.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from .log import warning

FILTER_GROUP = "kokoro_tone.filters"
MANIFEST_ENV = "KOKORO_TONE_FILTER_MANIFESTS"


def _files(raw: str) -> list[Path]:
    target = Path(raw).expanduser()
    if target.is_dir():
        return sorted(target.glob("*.json"))
    if target.is_file():
        return [target]
    warning(f"{MANIFEST_ENV}={raw!r} is not a file or directory; ignored")
    return []


def file_manifests(group: str) -> list[tuple[str, object]]:
    """Load JSON manifests from `KOKORO_TONE_FILTER_MANIFESTS`."""
    if group != FILTER_GROUP:
        return []
    raw = os.environ.get(MANIFEST_ENV, "").strip()
    if not raw:
        return []

    found: list[tuple[str, object]] = []
    for path in _files(raw):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            warning(f"filter manifest {path}: {exc}; ignored")
            continue
        found.append((str(path), payload))
    return found


def entry_point_manifests(group: str) -> list[tuple[str, object]]:
    """Load entry points in `group`, skipping ones that fail."""
    found: list[tuple[str, object]] = []
    try:
        points = entry_points(group=group)
    except Exception as exc:  # pragma: no cover - metadata backend failure
        warning(f"plugin group {group!r} could not be read: {exc}")
        return found

    for point in points:
        try:
            loaded: Any = point.load()
            payload = loaded() if callable(loaded) and not isinstance(loaded, Mapping) else loaded
        except Exception as exc:
            warning(f"plugin {group}:{point.name} failed to load: {exc}")
            continue
        found.append((f"{group}:{point.name}", payload))
    return found


def manifests(group: str) -> list[tuple[str, object]]:
    """Every manifest for `group`; local files load last so they win."""
    return entry_point_manifests(group) + file_manifests(group)
