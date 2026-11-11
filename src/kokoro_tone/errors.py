"""OpenAI's error envelope, shared by the middleware and the exception handlers.

One shape for every failure, so an SDK's error handling behaves the same for a
validation error, a bad bearer, an oversized body and an engine failure.
"""

from __future__ import annotations


class RequestError(ValueError):
    """A caller-visible 400 that is not an HTTP concern.

    Domain modules raise this; the app's exception handler turns it into the
    OpenAI error envelope, so `request.py` and `pipeline.py` stay framework-free.
    """


_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "invalid_request_error",
    413: "invalid_request_error",
    422: "invalid_request_error",
    429: "rate_limit_error",
}


def error_body(
    message: str, status: int, param: str | None = None, code: str | None = None
) -> dict:
    """Build the envelope, choosing the type from the status."""
    if status >= 500:
        error_type = "server_error"
    else:
        error_type = _ERROR_TYPES.get(status, "api_error")
    if status == 401:
        code = code or "invalid_api_key"
    return {
        "error": {
            "message": message,
            "type": error_type,
            "param": param,
            "code": code,
        }
    }
