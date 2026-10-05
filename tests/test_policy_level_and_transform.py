"""Regression tests for level-aware NORMAL policy and row-level transforms."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest
from mcp_servers.chart.server import build_server as build_chart
from mcp_servers.dataset_ops.server import build_server as build_ops
from mcp_servers.excel_parser.server import build_server as build_excel
from packages.common.dataset_store import (
    load_dataframe,
    load_metadata,
    save_dataframe,
    save_metadata,
)


def _tool(name: str):
    servers = (build_excel(), build_chart(), build_ops())
    return next(server._tools[name] for server in servers if name in server._tools)


def _level_ref(level: str) -> str:
    count = 24
    ref = save_dataframe(
        pd.DataFrame(
            {
                "low_text": ["A", "B"] * 12,
                "high_text": [f"customer-{index:02d}" for index in range(count)],
                "number": np.arange(count, dtype=float),
                "when": pd.date_range("2025-01-01", periods=count, freq="D"),
            }
        )
    )
    save_metadata(ref, {"policy": {"level": level}})
    return ref


@pytest.mark.parametrize(
    ("level", "visible", "masked"),
    [
        ("open", ("low_text", "number", "when"), ("high_text",)),
        ("internal", ("low_text", "number", "when"), ("high_text",)),
        ("restricted", ("number",), ("low_text", "high_text", "when")),
    ],
)
def test_normal_preview_follows_level_type_and_cardinality(
    level: str,
    visible: tuple[str, ...],
    masked: tuple[str, ...],
) -> None:
    rows = _tool("data_preview").invoke({"dataset_ref": _level_ref(level), "rows": 2})["rows"]

    assert rows
    for row in rows:
        for column in visible:
            assert row[column] != "***"
        for column in masked:
            assert row[column] == "***"


@pytest.mark.parametrize("level", ["open", "internal", "restricted"])
def test_normal_non_value_text_is_blocked_from_grouping_and_charting(level: str) -> None:
    ref = _level_ref(level)

    with pytest.raises(ValueError, match="normal|受数据策略保护"):
        _tool("aggregate_preview").invoke(
            {
                "dataset_ref": ref,
                "group_col": "high_text",
                "value_col": "number",
                "agg": "sum",
            }
        )
    with pytest.raises(ValueError, match="normal|受数据策略保护"):
        _tool("gen_chart").invoke(
            {
                "dataset_ref": ref,
                "chart_type": "bar",
                "encoding": {"x": "high_text", "y": "number", "agg": "sum"},
            }
        )


@pytest.fixture
def transform_ref() -> str:
    count = 24
    ref = save_dataframe(
        pd.DataFrame(
            {
                "row_id": [f"row-{index:02d}" for index in range(count)],
                "value": np.arange(1, count + 1, dtype=float),
                "mask_value": np.linspace(100, 200, count),
                "secret": [f"secret-{index:02d}" for index in range(count)],
            }
        )
    )
    save_metadata(
        ref,
        {"policy": {"columns": {"mask_value": "mask", "secret": "exclude"}}},
    )
    return ref


@pytest.mark.parametrize(
    "operation",
    [
        {"filters": [{"column": "mask_value", "op": ">", "value": 120}]},
        {"sort": [{"column": "mask_value", "order": "asc"}]},
        {"drop_nulls": ["mask_value"]},
        {"drop_duplicates": ["mask_value"]},
        {"drop_duplicates": []},
    ],
)
def test_row_level_transforms_cannot_infer_masked_values(
    transform_ref: str,
    operation: dict[str, Any],
) -> None:
    with pytest.raises(ValueError, match="mask|受数据策略保护"):
        _tool("transform_dataset").invoke({"dataset_ref": transform_ref, **operation})


def test_normal_row_level_transform_remains_available_and_inherits_policy(
    transform_ref: str,
) -> None:
    result = _tool("transform_dataset").invoke(
        {
            "dataset_ref": transform_ref,
            "filters": [{"column": "value", "op": ">", "value": 12}],
        }
    )
    derived_ref = str(result["dataset_ref"])
    derived = load_dataframe(derived_ref)
    metadata = load_metadata(derived_ref)
    preview = _tool("data_preview").invoke({"dataset_ref": derived_ref, "rows": 2})

    assert all(row["row_id"] == "***" for row in preview["rows"])

    assert derived["value"].min() == 13
    assert list(derived["row_id"])[0] == "row-12"
    assert metadata is not None
    assert metadata["policy"]["columns"] == {
        "row_id": "mask",
        "mask_value": "mask",
        "secret": "exclude",
    }
