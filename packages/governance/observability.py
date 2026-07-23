"""Bounded structured trace spans for model and tool calls."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from packages.common.logging import get_logger

_log = get_logger("governance.trace")


@dataclass(slots=True)
class TraceSpan:
    trace_id: str
    span_id: str
    name: str
    attributes: dict[str, object] = field(default_factory=dict)

    def set_attributes(self, **attributes: object) -> None:
        self.attributes.update(attributes)


@contextmanager
def trace_span(
    name: str,
    *,
    trace_id: str | None = None,
    **attrs: object,
) -> Iterator[TraceSpan]:
    """Record start/end/error with duration and bounded caller-owned metadata."""
    span = TraceSpan(
        trace_id=trace_id or uuid.uuid4().hex,
        span_id=uuid.uuid4().hex,
        name=name,
        attributes=dict(attrs),
    )
    started = time.perf_counter()
    _log.info(
        "trace.started",
        trace_id=span.trace_id,
        span_id=span.span_id,
        span_name=span.name,
        **span.attributes,
    )
    try:
        yield span
    except BaseException as exc:
        _log.error(
            "trace.failed",
            trace_id=span.trace_id,
            span_id=span.span_id,
            span_name=span.name,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            error_type=exc.__class__.__name__,
            **span.attributes,
        )
        raise
    else:
        _log.info(
            "trace.completed",
            trace_id=span.trace_id,
            span_id=span.span_id,
            span_name=span.name,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            **span.attributes,
        )
