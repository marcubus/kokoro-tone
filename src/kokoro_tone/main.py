"""App assembly: lifespan, middleware, error handlers and the router."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry.sdk.metrics.export import MetricReader

from . import __version__, upstream
from .context import Context
from .errors import AmbiguityError, RequestError, error_body
from .lexicon import Lexicon
from .log import configure, event, exception
from .middleware import BodyLimitMiddleware, TelemetryMiddleware
from .routes import router
from .settings import Settings, load_settings
from .telemetry import build_telemetry


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    app.state.ctx.upstream.close()
    app.state.ctx.telemetry.shutdown()


def _record_error(request: Request, error_type: str) -> None:
    ctx = getattr(request.app.state, "ctx", None)
    if ctx is not None:
        ctx.telemetry.errors.add(1, {"error.type": error_type})


def _http_error_type(status: int, detail: str) -> str:
    if status == 401:
        return "auth"
    if status == 400:
        return "invalid_request"
    if status == 413:
        return "body_too_large"
    if status == 502:
        return "audio" if detail.startswith("audio processing") else "upstream"
    if status >= 500:
        return "internal"
    return f"http_{status}"


async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    body = error_body(str(exc.detail), exc.status_code)
    _record_error(request, _http_error_type(exc.status_code, str(exc.detail)))
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
    _record_error(request, "invalid_request")
    event("error", path=request.url.path, status=400, param=param, detail=message[:160])
    return JSONResponse(
        error_body(message, 400, param=param, code="invalid_value"),
        status_code=400,
    )


async def request_error_handler(request: Request, exc: RequestError) -> JSONResponse:
    _record_error(request, "invalid_request")
    event("error", path=request.url.path, status=400, detail=str(exc)[:160])
    body = error_body(str(exc), 400)
    if isinstance(exc, AmbiguityError):
        body["error"]["code"] = "ambiguous_heteronym"
        body["error"]["param"] = "input"
        body["error"]["ambiguities"] = exc.ambiguities
    return JSONResponse(body, status_code=400)


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    _record_error(request, type(exc).__name__)
    exception(
        "error",
        path=request.url.path,
        status=500,
        detail=type(exc).__name__,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return JSONResponse(error_body("internal server error", 500), status_code=500)


def create_app(config: Settings, *, metric_reader: MetricReader | None = None) -> FastAPI:
    """Build one app from one configuration. No module-level state is read."""
    configure(config.log_level)
    telemetry = build_telemetry(config, metric_reader=metric_reader)
    telemetry.set_info(config)

    app = FastAPI(title="kokoro-tone", version=__version__, lifespan=lifespan)
    app.state.ctx = Context(
        settings=config,
        lexicon=Lexicon(
            config.lexicon_path,
            config.limits,
            config.lexicon_overlay_path,
            telemetry=telemetry,
        ),
        upstream=upstream.Upstream(config, telemetry=telemetry),
        telemetry=telemetry,
    )
    app.add_middleware(
        BodyLimitMiddleware,
        max_bytes=config.limits.body_bytes,
        telemetry=telemetry,
    )
    # Added last, so it is outermost and sees the body limit's 413 as well.
    app.add_middleware(TelemetryMiddleware, telemetry=telemetry)
    app.add_exception_handler(HTTPException, cast(Any, http_error_handler))
    app.add_exception_handler(RequestError, cast(Any, request_error_handler))
    app.add_exception_handler(RequestValidationError, cast(Any, validation_error_handler))
    app.add_exception_handler(Exception, cast(Any, unhandled_error_handler))
    app.include_router(router)
    return app


app = create_app(load_settings())
