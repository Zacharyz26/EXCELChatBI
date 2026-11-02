"""Frozen local producer pair; no Agent/browser/model evidence. Root schedules execution."""

from __future__ import annotations

import socket

import pandas as pd
import pytest
from mcp_servers.dataset_ops.tools import aggregate_preview
from packages.common.config import get_settings
from packages.common.dataset_store import save_dataframe


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    def no_network(*_args, **_kwargs):
        raise AssertionError("Nullable-count producer fixtures are offline only")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setenv("DATASET_DIR", str(tmp_path / "datasets"))
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


def test_explicit_nullable_metric_count_is_not_row_count():
    # Independent oracle: three rows, exactly two non-null metric entries.
    source = save_dataframe(pd.DataFrame({"组别": ["A", "A", "A"], "金额（元）": [10, None, 20]}))
    result = aggregate_preview(
        {"dataset_ref": source, "group_col": "组别", "value_col": "金额（元）", "agg": "count"}
    )
    assert result["rows"] == [
        {"group": "A", "value": 2, "count": 2, "row_count": 3, "valid_value_count": 2}
    ]
    assert result["value_col"] == "金额（元）"
    assert "total_value" not in result


def test_omitted_metric_counts_nonnull_group_not_all_rows():
    # Independent oracle: null group has two rows but zero non-null group entries.
    source = save_dataframe(pd.DataFrame({"组别": [None, None, "A"], "金额（元）": [10, None, 20]}))
    result = aggregate_preview(
        {"dataset_ref": source, "group_col": "组别", "agg": "count", "sort": "value_asc"}
    )
    assert result["rows"] == [
        {"group": None, "value": 0, "count": 0, "row_count": 2, "valid_value_count": 0},
        {"group": "A", "value": 1, "count": 1, "row_count": 1, "valid_value_count": 1},
    ]
    assert result["value_col"] is None
    assert "total_value" not in result
