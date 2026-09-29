"""Content-based resource limits, including missing and forged XLSX dimensions."""

from __future__ import annotations

import io
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pandas as pd
import pytest
from mcp_servers.excel_parser import tools as excel_tools
from packages.common.config import Settings, get_settings


def _workbook(*, dimension: str = "original", rows: int = 5, columns: int = 2) -> bytes:
    buffer = io.BytesIO()
    pd.DataFrame({f"c{i}": list(range(rows)) for i in range(columns)}).to_excel(
        buffer,
        index=False,
    )
    output = io.BytesIO()
    with ZipFile(buffer) as source, ZipFile(output, "w", ZIP_DEFLATED) as target:
        for member in source.infolist():
            data = source.read(member.filename)
            if member.filename == "xl/worksheets/sheet1.xml" and dimension != "original":
                replacement = b"" if dimension == "missing" else b'<dimension ref="A1:A1"/>'
                data = re.sub(rb"<dimension\b[^>]*/>", replacement, data)
            target.writestr(member.filename, data)
    return output.getvalue()


@pytest.fixture
def limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    settings = Settings(large_table_row_threshold=2, dataset_dir=str(tmp_path / "datasets"))
    monkeypatch.setattr(excel_tools, "get_settings", lambda: settings)

    def must_not_save(*args: object, **kwargs: object) -> None:
        raise AssertionError("An oversized workbook must never publish a dataset")

    monkeypatch.setattr(excel_tools, "save_dataframe", must_not_save)
    return settings


@pytest.mark.parametrize("suffix", [".xlsx", ".xls", ".bin"])
@pytest.mark.parametrize("dimension", ["original", "missing", "forged"])
@pytest.mark.parametrize("nrows", [None, 999999999])
def test_actual_rows_rejected_regardless_of_filename_or_dimension(
    tmp_path: Path,
    limits: Settings,
    suffix: str,
    dimension: str,
    nrows: int | None,
) -> None:
    path = tmp_path / f"input{suffix}"
    path.write_bytes(_workbook(dimension=dimension))
    with pytest.raises(excel_tools.TableTooLargeError, match="行数"):
        excel_tools.parse_excel({"file_ref": str(path), "nrows": nrows})


@pytest.mark.parametrize("dimension", ["missing", "forged"])
def test_explicit_sampling_and_exact_limit_are_not_truncated(
    tmp_path: Path,
    limits: Settings,
    monkeypatch: pytest.MonkeyPatch,
    dimension: str,
) -> None:
    saved: list[pd.DataFrame] = []

    def save(frame: pd.DataFrame) -> str:
        saved.append(frame)
        return "test-dataset"

    monkeypatch.setattr(excel_tools, "save_dataframe", save)
    path = tmp_path / "renamed.xls"
    path.write_bytes(_workbook(dimension=dimension))
    sample = excel_tools.parse_excel({"file_ref": str(path), "nrows": 2})
    assert sample["row_count"] == 2 and saved[-1]["c0"].tolist() == [0, 1]
    path.write_bytes(_workbook(dimension=dimension, rows=2))
    assert excel_tools.parse_excel({"file_ref": str(path)})["row_count"] == 2


def test_compressed_workbook_budget_applies_even_to_samples(
    tmp_path: Path,
    limits: Settings,
) -> None:
    limits.excel_max_uncompressed_mb = 1
    path = tmp_path / "compressed.xls"
    path.write_bytes(_workbook(rows=1))
    with ZipFile(path, "a", ZIP_DEFLATED) as archive:
        archive.writestr("oversized_shared_strings", b"0" * (2 * 1024 * 1024))
    assert path.stat().st_size < 20_000
    with pytest.raises(excel_tools.TableTooLargeError, match="解压"):
        excel_tools.parse_excel({"file_ref": str(path), "nrows": 1})


@pytest.mark.parametrize("budget", ["columns", "cells"])
def test_width_and_rectangular_cell_budget_before_dataframe(
    tmp_path: Path,
    limits: Settings,
    budget: str,
) -> None:
    path = tmp_path / "wide.xlsx"
    path.write_bytes(_workbook(rows=2, columns=5, dimension="forged"))
    if budget == "columns":
        limits.excel_max_columns = 4
    else:
        limits.excel_max_cells = 10  # header plus two data rows is 15 cells
    with pytest.raises(excel_tools.TableTooLargeError, match="列数或单元格"):
        excel_tools.parse_excel({"file_ref": str(path)})


def test_sparse_trailing_rows_cannot_be_silently_cut_off(tmp_path: Path, limits: Settings) -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    worksheet = workbook.active
    worksheet["A1"] = "header"
    worksheet["A2"] = 1
    worksheet["A50"] = 2
    path = tmp_path / "sparse.xlsx"
    workbook.save(path)
    with pytest.raises(excel_tools.TableTooLargeError, match="行数"):
        excel_tools.parse_excel({"file_ref": str(path)})


def test_api_rejects_disguised_oversized_upload_and_cleans_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.api.deps import settings_dep
    from apps.api.main import app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("LARGE_TABLE_ROW_THRESHOLD", "2")
    monkeypatch.setenv("DATASET_DIR", str(tmp_path / "datasets"))
    get_settings.cache_clear()
    uploads = tmp_path / "uploads"
    app.dependency_overrides[settings_dep] = lambda: Settings(upload_dir=str(uploads))
    try:
        response = TestClient(app).post(
            "/upload/excel",
            files={
                "file": (
                    "disguised.xls",
                    _workbook(dimension="missing"),
                    "application/vnd.ms-excel",
                ),
            },
        )
        assert response.status_code == 413, response.text
        assert "行数" in response.json()["detail"]
        assert list(uploads.iterdir()) == []
        assert not list((tmp_path / "datasets").glob("*.parquet"))
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
