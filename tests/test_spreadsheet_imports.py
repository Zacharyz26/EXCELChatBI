"""真实 XLSX、legacy BIFF XLS 与 CSV 统一导入契约。"""

from __future__ import annotations

import io
import struct
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
from apps.api.deps import settings_dep
from apps.api.main import app
from fastapi.testclient import TestClient
from mcp_servers.excel_parser import tools as spreadsheet_tools
from openpyxl import Workbook
from packages.common.config import Settings, get_settings
from packages.common.dataset_store import delete_dataset, load_dataframe

_XLSX_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_XLS_CT = "application/vnd.ms-excel"
_CSV_CT = "text/csv"


def _record(code: int, payload: bytes = b"") -> bytes:
    return struct.pack("<HH", code, len(payload)) + payload


def _biff8_bof(kind: int) -> bytes:
    return _record(
        0x0809,
        struct.pack(
            "<HHHHII",
            0x0600,
            kind,
            0x0DBB,
            0x07CC,
            0x00000041,
            0x00000006,
        ),
    )


def _biff8_label(row: int, column: int, value: str, *, xf: int = 0) -> bytes:
    raw = value.encode("latin-1")
    return _record(
        0x0204,
        struct.pack("<HHHHB", row, column, xf, len(raw), 0) + raw,
    )


def _biff8_number(row: int, column: int, value: float, *, xf: int = 0) -> bytes:
    return _record(0x0203, struct.pack("<HHHd", row, column, xf, value))


def _cfb_directory_entry(
    name: str,
    *,
    entry_type: int,
    child: int,
    start_sector: int,
    size: int,
) -> bytes:
    encoded_name = name.encode("utf-16le") + b"\0\0"
    entry = bytearray(128)
    entry[: len(encoded_name)] = encoded_name
    struct.pack_into(
        "<HBBIII",
        entry,
        64,
        len(encoded_name),
        entry_type,
        1,
        0xFFFFFFFF,
        0xFFFFFFFF,
        child,
    )
    struct.pack_into("<I", entry, 116, start_sector)
    struct.pack_into("<Q", entry, 120, size)
    return bytes(entry)


def _ole2_workbook_stream(biff: bytes) -> bytes:
    """把 BIFF8 流封装为标准 OLE2 Compound File `.xls`。"""
    stream = biff.ljust(4096, b"\0")
    header = bytearray(512)
    header[:8] = bytes.fromhex("D0CF11E0A1B11AE1")
    struct.pack_into("<HHHHHH", header, 24, 0x003E, 0x0003, 0xFFFE, 9, 6, 0)
    struct.pack_into(
        "<IIIIIIIII",
        header,
        40,
        0,
        1,
        8,
        0,
        4096,
        0xFFFFFFFE,
        0,
        0xFFFFFFFE,
        0,
    )
    struct.pack_into("<I", header, 76, 9)
    for index in range(1, 109):
        struct.pack_into("<I", header, 76 + index * 4, 0xFFFFFFFF)
    directory = (
        _cfb_directory_entry(
            "Root Entry",
            entry_type=5,
            child=1,
            start_sector=0xFFFFFFFE,
            size=0,
        )
        + _cfb_directory_entry(
            "Workbook",
            entry_type=2,
            child=0xFFFFFFFF,
            start_sector=0,
            size=len(stream),
        )
    ).ljust(512, b"\0")
    fat_entries = [index + 1 for index in range(7)]
    fat_entries += [0xFFFFFFFE, 0xFFFFFFFE, 0xFFFFFFFD]
    fat_entries += [0xFFFFFFFF] * (128 - len(fat_entries))
    fat = struct.pack("<128I", *fat_entries)
    return bytes(header) + stream + directory + fat


def _xls_bytes() -> bytes:
    """构造最小 OLE2/BIFF8 `.xls`，无需测试期 XLS 写入依赖。"""
    globals_prefix = b"".join(
        [
            _biff8_bof(0x0005),
            _record(0x0042, struct.pack("<H", 1252)),
            _record(0x0022, struct.pack("<H", 0)),
            _record(0x00E0, struct.pack("<HH", 0, 0) + b"\0" * 16),
            _record(0x00E0, struct.pack("<HH", 0, 14) + b"\0" * 16),
        ]
    )
    sheet_name = b"data"
    boundsheet_size = 4 + 4 + 1 + 1 + 1 + 1 + len(sheet_name)
    sheet_offset = len(globals_prefix) + boundsheet_size + 4
    boundsheet = _record(
        0x0085,
        struct.pack("<IBBB", sheet_offset, 0, 0, len(sheet_name)) + b"\0" + sheet_name,
    )
    globals_stream = globals_prefix + boundsheet + _record(0x000A)
    sheet_stream = b"".join(
        [
            _biff8_bof(0x0010),
            _record(0x0200, struct.pack("<IIHHH", 0, 3, 0, 4, 0)),
            _biff8_label(0, 0, "date"),
            _biff8_label(0, 1, "amount"),
            _biff8_label(0, 2, "code"),
            _biff8_label(0, 3, "note"),
            _biff8_number(1, 0, 45293.0, xf=1),
            _biff8_number(1, 1, 12.5),
            _biff8_label(1, 2, "00123"),
            _biff8_label(1, 3, "alpha"),
            _biff8_number(2, 0, 45294.0, xf=1),
            _biff8_label(2, 2, "00456"),
            _record(0x000A),
        ]
    )
    return _ole2_workbook_stream(globals_stream + sheet_stream)


def _xlsx_bytes() -> bytes:
    buffer = io.BytesIO()
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "data"
    sheet.append(["date", "amount", "code", "note"])
    sheet.append([datetime(2024, 1, 2), 12.5, "00123", "alpha"])
    sheet.append([datetime(2024, 1, 3), None, "00456", None])
    workbook.save(buffer)
    return buffer.getvalue()


def _csv_bytes(*, encoding: str, separator: str) -> bytes:
    content = separator.join(["date", "amount", "code", "note"]) + "\n"
    content += separator.join(["2024-01-02", "12.5", "00123", "中文"]) + "\n"
    content += separator.join(["2024-01-03", "", "00456", ""]) + "\n"
    return content.encode(encoding)


@pytest.fixture
def isolated_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    settings = Settings(
        upload_dir=str(tmp_path / "uploads"),
        dataset_dir=str(tmp_path / "datasets"),
    )
    monkeypatch.setenv("DATASET_DIR", settings.dataset_dir)
    get_settings.cache_clear()
    app.dependency_overrides[settings_dep] = lambda: settings
    try:
        yield settings
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def _assert_standard_dataset(dataset_ref: str, *, first_note: str) -> None:
    frame = load_dataframe(dataset_ref)
    assert frame["date"].tolist() == [
        pd.Timestamp("2024-01-02"),
        pd.Timestamp("2024-01-03"),
    ]
    assert frame["amount"].iloc[0] == 12.5
    assert pd.isna(frame["amount"].iloc[1])
    assert frame["code"].tolist() == ["00123", "00456"]
    assert frame["note"].iloc[0] == first_note
    assert pd.isna(frame["note"].iloc[1])

    profile = spreadsheet_tools.infer_schema({"dataset_ref": dataset_ref})
    dtypes = {column.name: column.dtype for column in profile.columns}
    assert dtypes == {
        "date": "datetime",
        "amount": "float",
        "code": "str",
        "note": "str",
    }


@pytest.mark.parametrize(
    ("filename", "content", "content_type", "first_note"),
    [
        ("primary.xlsx", _xlsx_bytes(), _XLSX_CT, "alpha"),
        ("legacy.xls", _xls_bytes(), _XLS_CT, "alpha"),
        ("utf8-bom.csv", _csv_bytes(encoding="utf-8-sig", separator=","), _CSV_CT, "中文"),
        ("gb18030.csv", _csv_bytes(encoding="gb18030", separator=";"), _CSV_CT, "中文"),
        ("tab.csv", _csv_bytes(encoding="utf-8", separator="\t"), _CSV_CT, "中文"),
    ],
)
def test_real_spreadsheet_uploads_share_dataset_and_profile_flow(
    isolated_storage: Settings,
    filename: str,
    content: bytes,
    content_type: str,
    first_note: str,
) -> None:
    response = TestClient(app).post(
        "/upload/excel",
        files={"file": (filename, content, content_type)},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["profile"]["row_count"] == 2
    assert body["profile"]["column_count"] == 4
    _assert_standard_dataset(body["dataset_ref"], first_note=first_note)
    delete_dataset(body["dataset_ref"])
    assert list(Path(isolated_storage.upload_dir).glob("*")) == []


@pytest.mark.parametrize(
    ("filename", "content", "content_type"),
    [
        ("broken.xlsx", b"not an OOXML workbook", _XLSX_CT),
        ("broken.xls", b"not a BIFF workbook", _XLS_CT),
        ("broken.csv", b"\x81", _CSV_CT),
    ],
)
def test_invalid_supported_format_has_consistent_user_prompt_and_no_residue(
    isolated_storage: Settings,
    filename: str,
    content: bytes,
    content_type: str,
) -> None:
    response = TestClient(app).post(
        "/upload/excel",
        files={"file": (filename, content, content_type)},
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"].startswith("表格解析失败：")
    assert list(Path(isolated_storage.upload_dir).glob("*")) == []
    dataset_dir = Path(isolated_storage.dataset_dir)
    assert not dataset_dir.exists() or list(dataset_dir.glob("*")) == []


def test_unsupported_spreadsheet_format_lists_supported_contract(
    isolated_storage: Settings,
) -> None:
    response = TestClient(app).post(
        "/upload/excel",
        files={"file": ("macro.xlsm", _xlsx_bytes(), _XLSX_CT)},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "仅支持 .xlsx / .xls / .csv 文件；.xlsx 为主格式，.xls 仅用于旧文件只读导入"
    )


def test_csv_rejects_workbook_sheet_selection(tmp_path: Path) -> None:
    path = tmp_path / "single-table.csv"
    path.write_bytes(_csv_bytes(encoding="utf-8", separator=","))

    with pytest.raises(ValueError, match="CSV.*单表"):
        spreadsheet_tools.parse_excel({"file_ref": str(path), "sheet": "data"})
