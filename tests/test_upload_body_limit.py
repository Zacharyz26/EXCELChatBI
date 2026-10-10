"""Pre-parser upload body limits and file-size boundary regressions."""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from apps.api.main import app
from apps.api.routers.upload import _save_within_limit
from fastapi.testclient import TestClient
from packages.common.config import get_settings
from starlette.datastructures import UploadFile

_MIB = 1024 * 1024


def _set_one_megabyte_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_UPLOAD_MB", "1")
    monkeypatch.setenv("UPLOAD_MULTIPART_OVERHEAD_MB", "1")
    get_settings.cache_clear()


def test_content_length_is_rejected_before_multipart_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_one_megabyte_limit(monkeypatch)
    client = TestClient(app)
    try:
        response = client.post(
            "/upload/excel",
            content=b"not-a-valid-multipart-body",
            headers={
                "content-type": "multipart/form-data; boundary=missing",
                "content-length": str(2 * _MIB + 1),
            },
        )
    finally:
        get_settings.cache_clear()

    assert response.status_code == 413
    assert response.json() == {"detail": "文件过大（上限 1 MB）"}


async def test_chunked_body_is_bounded_while_multipart_parser_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_one_megabyte_limit(monkeypatch)
    boundary = b"bounded-upload"

    async def body() -> AsyncIterator[bytes]:
        yield b"--" + boundary + b"\r\n"
        yield b'Content-Disposition: form-data; name="file"; filename="large.xlsx"\r\n'
        yield b"Content-Type: application/octet-stream\r\n\r\n"
        for _ in range(3):
            yield b"x" * _MIB
        yield b"\r\n--" + boundary + b"--\r\n"

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/upload/excel",
                content=body(),
                headers={
                    "content-type": (
                        "multipart/form-data; boundary=" + boundary.decode("ascii")
                    ),
                },
            )
    finally:
        get_settings.cache_clear()

    assert response.status_code == 413
    assert response.json() == {"detail": "文件过大（上限 1 MB）"}


async def test_exact_file_limit_is_accepted_by_streaming_guard(tmp_path: Path) -> None:
    destination = tmp_path / "exact.xlsx"
    upload = UploadFile(
        io.BytesIO(b"x" * _MIB),
        size=_MIB,
        filename="exact.xlsx",
    )

    await _save_within_limit(upload, destination, max_mb=1)

    assert destination.stat().st_size == _MIB
