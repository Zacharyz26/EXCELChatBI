"""Bound raw upload request bodies before Starlette parses multipart data."""

from __future__ import annotations

from collections.abc import Callable

from packages.common.config import Settings, get_settings
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_MIB = 1024 * 1024
_UPLOAD_PATH = "/upload/excel"


class UploadBodyLimitMiddleware:
    """Cap the complete multipart envelope before UploadFile parsing.

    Content-Length is rejected without reading the body. Chunked or missing-length
    requests are counted while Starlette reads them, bounding parser temp-disk use
    to the configured file limit plus a small multipart envelope allowance.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        settings_provider: Callable[[], Settings] = get_settings,
    ) -> None:
        self.app = app
        self._settings_provider = settings_provider

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != _UPLOAD_PATH:
            await self.app(scope, receive, send)
            return

        settings = self._settings_provider()
        limit = upload_body_limit_bytes(settings)
        content_length = _content_length(scope)
        if content_length is not None and content_length > limit:
            await _send_too_large(scope, receive, send, settings.max_upload_mb)
            return

        consumed = 0
        response_started = False
        body_too_large = False

        async def limited_receive() -> Message:
            nonlocal body_too_large, consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > limit:
                    body_too_large = True
                    # Multipart parsing turns arbitrary receive exceptions into a
                    # 400 response. End the parser input instead, suppress that
                    # downstream response, and emit the authoritative 413 below.
                    return {
                        "type": "http.request",
                        "body": b"",
                        "more_body": False,
                    }
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if body_too_large:
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except Exception:
            if not body_too_large or response_started:
                raise
        if body_too_large:
            if response_started:
                raise RuntimeError(
                    "upload body exceeded its limit after the response started"
                )
            await _send_too_large(scope, receive, send, settings.max_upload_mb)


def upload_body_limit_bytes(settings: Settings) -> int:
    """Return total multipart envelope bytes accepted before parsing stops."""
    return (
        settings.max_upload_mb + settings.upload_multipart_overhead_mb
    ) * _MIB


def _content_length(scope: Scope) -> int | None:
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name.lower() != b"content-length":
            continue
        try:
            value = int(raw_value.decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            return None
        return value if value >= 0 else None
    return None


async def _send_too_large(
    scope: Scope,
    receive: Receive,
    send: Send,
    max_upload_mb: int,
) -> None:
    response = JSONResponse(
        status_code=413,
        content={"detail": f"文件过大（上限 {max_upload_mb} MB）"},
    )
    await response(scope, receive, send)
