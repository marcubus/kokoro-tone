"""App assembly: lifespan, middleware, error handlers and the router."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import __version__, upstream
from .context import Context
from .errors import RequestError, error_body
from .lexicon import Lexicon
from .log import configure, event, exception
from .middleware import BodyLimitMiddleware
from .routes import router
from .settings import Settings, load_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    app.state.ctx.upstream.close()


async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    body = error_body(str(exc.detail), exc.status_code)
    event(
        "error",
        path=request.url.path,
        status=exc.status_code,
        detail=str(exc.detail)[:160],
    )
    return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    param = None
    message = "invalid request"
    if errors:
        first = errors[0]
        location = [str(part) for part in first.get("loc", ()) if part != "body"]
        param = ".".join(location) or None
        message = str(first.get("msg", message))
    event("error", path=request.url.path, status=400, param=param, detail=message[:160])
    return JSONResponse(
        error_body(message, 400, param=param, code="invalid_value"),
        status_code=400,
    )


async def request_error_handler(request: Request, exc: RequestError) -> JSONResponse:
    event("error", path=request.url.path, status=400, detail=str(exc)[:160])
    return JSONResponse(error_body(str(exc), 400), status_code=400)


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    exception(
        "error",
        path=request.url.path,
        status=500,
        detail=type(exc).__name__,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return JSONResponse(error_body("internal server error", 500), status_code=500)


def create_app(config: Settings) -> FastAPI:
    """Build one app from one configuration. No module-level state is read."""
    configure(config.log_level)

    app = FastAPI(title="kokoro-tone", version=__version__, lifespan=lifespan)
    app.state.ctx = Context(
        settings=config,
        lexicon=Lexicon(config.lexicon_path, config.limits, config.lexicon_overlay_path),
        upstream=upstream.Upstream(config),
    )
    app.add_middleware(BodyLimitMiddleware, max_bytes=config.limits.body_bytes)
    app.add_exception_handler(HTTPException, cast(Any, http_error_handler))
    app.add_exception_handler(RequestError, cast(Any, request_error_handler))
    app.add_exception_handler(RequestValidationError, cast(Any, validation_error_handler))
    app.add_exception_handler(Exception, cast(Any, unhandled_error_handler))
    app.include_router(router)
    return app


app = create_app(load_settings())
