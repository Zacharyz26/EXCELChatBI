"""Helpers for building deterministic OOXML workbook fixtures."""

from __future__ import annotations

import io
from collections.abc import Mapping
from xml.etree import ElementTree
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import load_workbook

_SPREADSHEET_NAMESPACE = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _qualified_name(local_name: str) -> str:
    return f"{{{_SPREADSHEET_NAMESPACE}}}{local_name}"


def inject_formula_caches(
    workbook_bytes: bytes,
    *,
    worksheet_name: str,
    cached_values: Mapping[str, int | float],
    worksheet_path: str = "xl/worksheets/sheet1.xml",
) -> bytes:
    """Set formula caches by cell coordinate and verify the data-only view.

    The openpyxl lxml and stdlib XML writers serialize empty ``<v>`` elements
    differently. Parsing the worksheet XML makes this fixture independent of
    that representation while still producing a real cached-formula workbook.
    """
    output = io.BytesIO()
    worksheet_found = False
    ElementTree.register_namespace("", _SPREADSHEET_NAMESPACE)

    with ZipFile(io.BytesIO(workbook_bytes)) as source, ZipFile(
        output,
        "w",
        ZIP_DEFLATED,
    ) as target:
        for member in source.infolist():
            data = source.read(member.filename)
            if member.filename == worksheet_path:
                worksheet_found = True
                root = ElementTree.fromstring(data)
                cells = {
                    cell.get("r"): cell for cell in root.iter(_qualified_name("c"))
                }
                for coordinate, cached_value in cached_values.items():
                    cell = cells.get(coordinate)
                    if cell is None:
                        raise AssertionError(f"missing cell {coordinate} in {worksheet_path}")
                    if cell.find(_qualified_name("f")) is None:
                        raise AssertionError(f"cell {coordinate} does not contain a formula")
                    value = cell.find(_qualified_name("v"))
                    if value is None:
                        value = ElementTree.SubElement(cell, _qualified_name("v"))
                    value.text = str(cached_value)
                data = ElementTree.tostring(root, encoding="utf-8")
            target.writestr(member, data)

    if not worksheet_found:
        raise AssertionError(f"missing worksheet XML {worksheet_path}")

    result = output.getvalue()
    cached_workbook = load_workbook(io.BytesIO(result), read_only=True, data_only=True)
    try:
        worksheet = cached_workbook[worksheet_name]
        for coordinate, expected in cached_values.items():
            actual = worksheet[coordinate].value
            if actual != expected:
                raise AssertionError(
                    f"cached value for {worksheet_name}!{coordinate} is {actual!r}, "
                    f"expected {expected!r}"
                )
    finally:
        cached_workbook.close()
    return result
