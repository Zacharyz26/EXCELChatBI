"""Chart data semantics: ordered axes and legal scatter encodings."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, cast

import pandas as pd
import pytest
from mcp_servers.chart.server import build_server
from packages.common.dataset_store import delete_dataset, save_dataframe


@pytest.fixture
def dataset_factory() -> Iterator[Callable[[pd.DataFrame], str]]:
    refs: list[str] = []

    def create(frame: pd.DataFrame) -> str:
        ref = save_dataframe(frame)
        refs.append(ref)
        return ref

    yield create
    for ref in refs:
        delete_dataset(ref)


def _gen_chart(arguments: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], build_server()._tools["gen_chart"].invoke(arguments))


def _line_chart(dataset_ref: str, x: str, y: str) -> dict[str, Any]:
    return _gen_chart(
        {
            "dataset_ref": dataset_ref,
            "chart_type": "line",
            "encoding": {"x": x, "y": y, "agg": "sum"},
        }
    )


def test_line_chart_orders_numeric_axis_by_numeric_value(
    dataset_factory: Callable[[pd.DataFrame], str],
) -> None:
    dataset_ref = dataset_factory(
        pd.DataFrame(
            {"bucket": [10] * 5 + [2] * 5 + [1] * 5, "amount": [20] * 15}
        )
    )

    result = _line_chart(dataset_ref, "bucket", "amount")

    option = result["option"]
    assert option["xAxis"]["data"] == ["1", "2", "10"]
    assert option["series"][0]["data"] == [100.0, 100.0, 100.0]


def test_line_chart_orders_datetime_axis_chronologically(
    dataset_factory: Callable[[pd.DataFrame], str],
) -> None:
    dataset_ref = dataset_factory(
        pd.DataFrame(
            {
                "when": pd.to_datetime(
                    ["2025-10-02T09:00:00"] * 5
                    + ["2025-02-01T09:00:00"] * 5
                    + ["2025-01-15T09:00:00"] * 5
                ),
                "amount": [20] * 15,
            }
        )
    )

    result = _line_chart(dataset_ref, "when", "amount")

    option = result["option"]
    assert option["xAxis"]["data"] == [
        "2025-01-15 09:00:00",
        "2025-02-01 09:00:00",
        "2025-10-02 09:00:00",
    ]
    assert option["series"][0]["data"] == [100.0, 100.0, 100.0]


def test_bar_chart_orders_numeric_axis_but_keeps_top_n_by_value(
    dataset_factory: Callable[[pd.DataFrame], str],
) -> None:
    dataset_ref = dataset_factory(
        pd.DataFrame(
            {
                "bucket": [10] * 5 + [2] * 5 + [1] * 5,
                "amount": [20] * 5 + [8] * 5 + [2] * 5,
            }
        )
    )

    result = _gen_chart(
        {
            "dataset_ref": dataset_ref,
            "chart_type": "bar",
            "encoding": {"x": "bucket", "y": "amount", "agg": "sum", "top_n": 2},
        }
    )

    option = result["option"]
    assert option["xAxis"]["data"] == ["2", "10"]
    assert option["series"][0]["data"] == [40.0, 100.0]


def test_bar_chart_categorical_axis_remains_ranked_by_value(
    dataset_factory: Callable[[pd.DataFrame], str],
) -> None:
    dataset_ref = dataset_factory(
        pd.DataFrame(
            {
                "category": ["zeta"] * 5 + ["alpha"] * 5,
                "amount": [2] * 5 + [10] * 5,
            }
        )
    )

    result = _gen_chart(
        {
            "dataset_ref": dataset_ref,
            "chart_type": "bar",
            "encoding": {"x": "category", "y": "amount", "agg": "sum"},
        }
    )

    option = result["option"]
    assert option["xAxis"]["data"] == ["alpha", "zeta"]
    assert option["series"][0]["data"] == [50.0, 10.0]


def test_scatter_uses_time_axis_for_datetime_and_value_axis_for_number(
    dataset_factory: Callable[[pd.DataFrame], str],
) -> None:
    dataset_ref = dataset_factory(
        pd.DataFrame(
            {
                "when": pd.to_datetime(["2025-01-02", "2025-01-01"]),
                "amount": [2.5, 1.5],
            }
        )
    )

    result = _gen_chart(
        {
            "dataset_ref": dataset_ref,
            "chart_type": "scatter",
            "encoding": {"x": "when", "y": "amount", "agg": "none"},
        }
    )

    option = result["option"]
    assert option["xAxis"] == {"type": "time", "name": "when"}
    assert option["yAxis"] == {"type": "value", "name": "amount"}
    assert option["series"][0]["data"] == [
        ["2025-01-02 00:00:00", 2.5],
        ["2025-01-01 00:00:00", 1.5],
    ]


@pytest.mark.parametrize("column", ["category", "flag"])
def test_scatter_rejects_non_continuous_axis_fields(
    dataset_factory: Callable[[pd.DataFrame], str],
    column: str,
) -> None:
    dataset_ref = dataset_factory(
        pd.DataFrame(
            {
                "category": ["A", "B"],
                "flag": [True, False],
                "amount": [1.0, 2.0],
            }
        )
    )

    with pytest.raises(ValueError, match=rf"散点图.*{column}.*数值或日期时间"):
        _gen_chart(
            {
                "dataset_ref": dataset_ref,
                "chart_type": "scatter",
                "encoding": {"x": column, "y": "amount", "agg": "none"},
            }
        )


def test_scatter_rejects_explicit_aggregation(
    dataset_factory: Callable[[pd.DataFrame], str],
) -> None:
    dataset_ref = dataset_factory(
        pd.DataFrame({"x": [1.0, 2.0], "y": [3.0, 4.0]})
    )

    with pytest.raises(ValueError, match="散点图.*agg.*none"):
        _gen_chart(
            {
                "dataset_ref": dataset_ref,
                "chart_type": "scatter",
                "encoding": {"x": "x", "y": "y", "agg": "sum"},
            }
        )


def test_scatter_rejects_top_n_encoding(
    dataset_factory: Callable[[pd.DataFrame], str],
) -> None:
    dataset_ref = dataset_factory(
        pd.DataFrame({"x": [1.0, 2.0], "y": [3.0, 4.0]})
    )

    with pytest.raises(ValueError, match="散点图.*top_n"):
        _gen_chart(
            {
                "dataset_ref": dataset_ref,
                "chart_type": "scatter",
                "encoding": {"x": "x", "y": "y", "agg": "none", "top_n": 1},
            }
        )


def test_scatter_without_agg_remains_backward_compatible(
    dataset_factory: Callable[[pd.DataFrame], str],
) -> None:
    dataset_ref = dataset_factory(
        pd.DataFrame({"x": [1.0, 2.0], "y": [3.0, 4.0]})
    )

    result = _gen_chart(
        {
            "dataset_ref": dataset_ref,
            "chart_type": "scatter",
            "encoding": {"x": "x", "y": "y"},
        }
    )

    option = result["option"]
    assert option["xAxis"]["type"] == "value"
    assert option["yAxis"]["type"] == "value"
    assert option["series"][0]["data"] == [[1.0, 3.0], [2.0, 4.0]]
