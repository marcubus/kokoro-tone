"""App assembly: lifespan, middleware, error handlers and the router."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import __version__, settings, upstream
from .errors import error_body
from .log import event
from .middleware import BodyLimitMiddleware
from .routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    upstream.http_client()
    yield
    upstream.close_client()


app = FastAPI(title="kokoro-tone", version=__version__, lifespan=lifespan)
app.add_middleware(BodyLimitMiddleware, max_bytes=settings.SETTINGS.limits.body_bytes)


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    body = error_body(str(exc.detail), exc.status_code)
    event(
        "error",
        path=request.url.path,
        status=exc.status_code,
        detail=str(exc.detail)[:160],
    )
    return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(RequestValidationError)
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


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    event("error", path=request.url.path, status=500, detail=type(exc).__name__)
    return JSONResponse(error_body("internal server error", 500), status_code=500)


app.include_router(router)
