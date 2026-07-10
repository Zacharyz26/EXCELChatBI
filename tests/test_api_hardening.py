"""API 硬化测试（R2）：上传文件名穿越、ingest 路径白名单、上传大小上限、行数上限。

补上 upload router 的测试盲区，钉住安全/健壮性修复。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.api.deps import settings_dep  # noqa: E402
from apps.api.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from packages.common.config import Settings, get_settings  # noqa: E402

_XLSX_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx_bytes() -> bytes:
    buf = io.BytesIO()
    pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]}).to_excel(buf, index=False)
    return buf.getvalue()


def test_upload_filename_traversal_is_basenamed(tmp_path: Path) -> None:
    up = tmp_path / "uploads"
    app.dependency_overrides[settings_dep] = lambda: Settings(upload_dir=str(up))
    try:
        client = TestClient(app)
        resp = client.post(
            "/upload/excel",
            files={"file": ("../../evil.xlsx", _xlsx_bytes(), _XLSX_CT)},
        )
        assert resp.status_code == 200, resp.text
        # 落盘文件在 upload_dir 内、以 basename 结尾；父目录未被写入 evil.xlsx（未穿越）
        saved = list(up.glob("*_evil.xlsx"))
        assert len(saved) == 1
        assert not (tmp_path / "evil.xlsx").exists()
        assert not (up.parent / "evil.xlsx").exists()
    finally:
        app.dependency_overrides.clear()


def test_upload_oversize_rejected(tmp_path: Path) -> None:
    up = tmp_path / "uploads"
    # 上限设为 0 → 任何非空文件都应 413，且不留半成品
    app.dependency_overrides[settings_dep] = lambda: Settings(upload_dir=str(up), max_upload_mb=0)
    try:
        client = TestClient(app)
        resp = client.post(
            "/upload/excel",
            files={"file": ("ok.xlsx", _xlsx_bytes(), _XLSX_CT)},
        )
        assert resp.status_code == 413, resp.text
        assert not up.exists() or list(up.glob("*")) == []  # 无残留半成品
    finally:
        app.dependency_overrides.clear()


def test_upload_rejects_rows_over_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 阈值压到 2，样例表 3 行数据 → 读表前按元数据拒绝（413），且不留盘（大表防护 V3）
    monkeypatch.setenv("LARGE_TABLE_ROW_THRESHOLD", "2")
    get_settings.cache_clear()
    up = tmp_path / "uploads"
    app.dependency_overrides[settings_dep] = lambda: Settings(upload_dir=str(up))
    try:
        client = TestClient(app)
        resp = client.post(
            "/upload/excel",
            files={"file": ("big.xlsx", _xlsx_bytes(), _XLSX_CT)},
        )
        assert resp.status_code == 413, resp.text
        assert "行数" in resp.json()["detail"]
        assert list(up.glob("*.xlsx")) == []  # 被拒文件不留盘
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()  # 恢复默认阈值，避免污染其他测试


def test_ingest_path_outside_whitelist_forbidden(tmp_path: Path) -> None:
    # tmp_path 在 kb_docs_dir(默认 docs/kb_samples) 之外 → 403
    outside = tmp_path / "secret.md"
    outside.write_text("敏感内容", encoding="utf-8")
    client = TestClient(app)
    resp = client.post("/kb/ingest", json={"path": str(outside)})
    assert resp.status_code == 403, resp.text
    assert "超出允许" in resp.json()["detail"]


def test_ingest_absolute_escape_forbidden() -> None:
    client = TestClient(app)
    resp = client.post("/kb/ingest", json={"path": "/etc"})
    assert resp.status_code == 403, resp.text
