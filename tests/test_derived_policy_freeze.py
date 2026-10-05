"""Derived datasets must never loosen a source column's effective visibility."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from mcp_servers.dataset_ops.server import build_server as build_ops
from mcp_servers.excel_parser.server import build_server as build_excel
from packages.common.dataset_store import load_metadata, save_dataframe, save_metadata


def _tool(name: str):
    servers = (build_excel(), build_ops())
    return next(server._tools[name] for server in servers if name in server._tools)


def _source_ref() -> str:
    count = 24
    ref = save_dataframe(
        pd.DataFrame(
            {
                "key": np.arange(count),
                "customer": [f"customer-{index:02d}" for index in range(count)],
                "segment": ["A", "B"] * 12,
                "value": np.arange(1, count + 1, dtype=float),
            }
        )
    )
    save_metadata(ref, {"policy": {"level": "open"}})
    return ref


@pytest.mark.parametrize(
    "operation",
    [
        {"filters": [{"column": "value", "op": ">", "value": 20}]},
        {"exclude_row_indices": list(range(20))},
    ],
)
def test_transform_freezes_high_cardinality_normal_visibility(
    operation: dict[str, object],
) -> None:
    result = _tool("transform_dataset").invoke({"dataset_ref": _source_ref(), **operation})
    derived_ref = str(result["dataset_ref"])

    rows = _tool("data_preview").invoke({"dataset_ref": derived_ref, "rows": 10})["rows"]
    metadata = load_metadata(derived_ref)

    assert len(rows) == 4
    assert all(row["customer"] == "***" for row in rows)
    assert all(row["segment"] in {"A", "B"} for row in rows)
    assert all(isinstance(row["value"], float) for row in rows)
    assert metadata is not None
    assert metadata["policy"]["columns"]["customer"] == "mask"
    assert "segment" not in metadata["policy"]["columns"]

    with pytest.raises(ValueError, match="mask|受数据策略保护"):
        _tool("aggregate_preview").invoke(
            {
                "dataset_ref": derived_ref,
                "group_col": "customer",
                "value_col": "value",
                "agg": "sum",
            }
        )
    allowed = _tool("aggregate_preview").invoke(
        {
            "dataset_ref": derived_ref,
            "group_col": "segment",
            "value_col": "value",
            "agg": "sum",
        }
    )
    assert allowed["rows"]


def test_join_freezes_each_parent_effective_visibility() -> None:
    count = 24
    left_ref = save_dataframe(
        pd.DataFrame(
            {
                "left_key": np.arange(count),
                "left_customer": [f"left-{index:02d}" for index in range(count)],
                "left_segment": ["A", "B"] * 12,
            }
        )
    )
    right_ref = save_dataframe(
        pd.DataFrame(
            {
                "right_key": np.arange(20, 20 + count),
                "right_customer": [f"right-{index:02d}" for index in range(count)],
                "right_segment": ["X", "Y"] * 12,
            }
        )
    )
    save_metadata(left_ref, {"policy": {"level": "open"}})
    save_metadata(right_ref, {"policy": {"level": "open"}})

    result = _tool("join_datasets").invoke(
        {
            "left_dataset_ref": left_ref,
            "right_dataset_ref": right_ref,
            "left_key": "left_key",
            "right_key": "right_key",
            "join_type": "inner",
        }
    )
    derived_ref = str(result["dataset_ref"])
    rows = _tool("data_preview").invoke({"dataset_ref": derived_ref, "rows": 10})["rows"]
    metadata = load_metadata(derived_ref)

    assert len(rows) == 4
    assert all(row["left_customer"] == "***" for row in rows)
    assert all(row["right_customer"] == "***" for row in rows)
    assert all(row["left_segment"] in {"A", "B"} for row in rows)
    assert all(row["right_segment"] in {"X", "Y"} for row in rows)
    assert metadata is not None
    assert metadata["policy"]["columns"]["left_customer"] == "mask"
    assert metadata["policy"]["columns"]["right_customer"] == "mask"
    assert "left_segment" not in metadata["policy"]["columns"]
    assert "right_segment" not in metadata["policy"]["columns"]
