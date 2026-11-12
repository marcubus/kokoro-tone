"""The composition root: everything one request needs, in one object.

`main.create_app` builds this once and stores it on `app.state`. Routes take it
through a FastAPI dependency, so no domain module reads settings or an HTTP
client at import time. `main` itself builds the ASGI app from the environment
at import, as an entrypoint does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from fastapi import Request

from .lexicon import Lexicon
from .settings import Settings
from .upstream import Upstream


@dataclass(frozen=True)
class Context:
    settings: Settings
    lexicon: Lexicon
    upstream: Upstream


def get_context(request: Request) -> Context:
    return cast(Context, request.app.state.ctx)
