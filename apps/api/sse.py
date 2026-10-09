"""SSE response lifecycle helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

import anyio
from sse_starlette.sse import EventSourceResponse
from starlette.types import Receive, Scope, Send


async def close_async_iterator(iterator: object) -> None:
    """Close an async iterator when it exposes ``aclose``."""
    close = getattr(iterator, "aclose", None)
    if not callable(close):
        return
    await cast(Callable[[], Awaitable[None]], close)()


class ClosingEventSourceResponse(EventSourceResponse):
    """Always close the body iterator, including send-stage disconnects."""

    def __init__(
        self,
        content: Any,
        *args: Any,
        cleanup: object | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(content, *args, **kwargs)
        self._cleanup = cleanup

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # sse-starlette cancels its send task when the disconnect listener wins.
            # At that point async-for is suspended in ``send`` rather than ``__anext__``,
            # so the iterator otherwise never receives cancellation or ``aclose``.
            with anyio.CancelScope(shield=True):
                try:
                    await close_async_iterator(self.body_iterator)
                finally:
                    if self._cleanup is not self.body_iterator:
                        await close_async_iterator(self._cleanup)
