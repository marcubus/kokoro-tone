"""One-line structured events on stderr, which is the container's log stream.

The shape is `event=<name> key=value …`, with a value quoted only when it holds
a space or a quote, and newlines flattened. An `=` inside a value is left bare,
so `guess=bass=music` stays greppable; both `grep` and Loki's `| logfmt` split
on the first `=`.

Nothing here may carry request text. A field is a count, a name, a duration or a
status — never the words the caller asked to be spoken. The service is the
gateway for a script that may be private; the log is for the operator.
"""

from __future__ import annotations

import sys


def event(name: str, **fields: object) -> None:
    """Write one event line. `None` fields are omitted, not printed as `None`."""
    parts = [f"event={name}"]
    for key, value in fields.items():
        if value is None:
            continue
        text = " ".join(str(value).split())
        if not text or any(ch in text for ch in ' "'):
            text = '"' + text.replace('"', "'") + '"'
        parts.append(f"{key}={text}")
    print(" ".join(parts), file=sys.stderr, flush=True)
