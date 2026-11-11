"""One-line structured events on stderr, which is the container's log stream.

The shape is `event=<name> key=value …`, with a value quoted only when it holds
a space or a quote, and newlines flattened. An `=` inside a value is left bare,
so `guess=bass=music` stays greppable; both `grep` and Loki's `| logfmt` split
on the first `=`.

The backend is `logging`, so a deployment can set `LOG_LEVEL` and a test can
capture the stream. The handler resolves `sys.stderr` at emit time, which keeps
pytest's `capsys` working across tests.

Nothing here may carry request text. A field is a count, a name, a duration or a
status - never the words the caller asked to be spoken. The service is the
gateway for a script that may be private; the log is for the operator.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

LOGGER_NAME = "kokoro_tone"

_logger = logging.getLogger(LOGGER_NAME)
_configured = False


class _StderrHandler(logging.Handler):
    """Write the formatted record to whatever `sys.stderr` is right now."""

    def emit(self, record: logging.LogRecord) -> None:
        print(self.format(record), file=sys.stderr, flush=True)


def configure(level: str = "INFO") -> None:
    """Install the one handler this process uses. Safe to call more than once."""
    global _configured
    handler = _StderrHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.handlers.clear()
    _logger.addHandler(handler)
    _logger.setLevel(level.upper())
    _logger.propagate = False
    _configured = True


def _ensure_configured() -> None:
    if not _configured:
        configure()


def _format(name: str, fields: dict[str, object]) -> str:
    parts = [f"event={name}"]
    for key, value in fields.items():
        if value is None:
            continue
        text = " ".join(str(value).split())
        if not text or any(ch in text for ch in ' "'):
            text = '"' + text.replace('"', "'") + '"'
        parts.append(f"{key}={text}")
    return " ".join(parts)


def event(name: str, **fields: object) -> None:
    """Write one event line. `None` fields are omitted, not printed as `None`."""
    _ensure_configured()
    _logger.info(_format(name, fields))


def warning(message: str) -> None:
    """A configuration problem the operator should see but that is not fatal."""
    _ensure_configured()
    _logger.warning(message)


def exception(name: str, *, exc_info: Any = True, **fields: object) -> None:
    """An event with a traceback attached, for an unhandled failure."""
    _ensure_configured()
    _logger.error(_format(name, fields), exc_info=exc_info)
