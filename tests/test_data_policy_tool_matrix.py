"""End-to-end column-policy matrix for direct Tool and governed MCP execution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from apps.orchestrator.agent_tools import build_registry
from mcp_servers.chart.server import build_server as build_chart
from mcp_servers.common.contracts import MCPRequestContext
from mcp_servers.dataset_ops.server import build_server as build_ops
from mcp_servers.excel_parser.server import build_server as build_excel
from mcp_servers.report.server import build_server as build_report
from mcp_servers.stats.server import build_server as build_stats
from packages.common.dataset_store import load_metadata, save_dataframe, save_metadata
from packages.rag.embedding import HashingEmbedder
from packages.rag.rerank import LexicalReranker
from packages.rag.retriever import HybridRetriever
from packages.rag.store import LocalKnowledgeStore


@pytest.fixture
def governed_ref() -> str:
    count = 24
    frame = pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=count, freq="D"),
            "group": ["A"] * 12 + ["B"] * 12,
            "value": np.arange(1, count + 1, dtype=float),
            "mask_group": ["masked-A"] * 12 + ["masked-B"] * 12,
            "mask_value": np.linspace(100, 200, count),
            "secret_group": ["secret-A"] * 12 + ["secret-B"] * 12,
            "secret_value": np.linspace(1000, 2000, count),
        }
    )
    ref = save_dataframe(frame)
    save_metadata(
        ref,
        {
            "policy": {
                "columns": {
                    "mask_group": "mask",
                    "mask_value": "mask",
                    "secret_group": "exclude",
                    "secret_value": "exclude",
                }
            }
        },
    )
    return ref


def _tool(name: str):
    servers = (build_excel(), build_stats(), build_chart(), build_ops())
    return next(server._tools[name] for server in servers if name in server._tools)


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        (
            "trend_analysis",
            {"value_col": "secret_value", "time_col": "date"},
        ),
        (
            "forecast",
            {"value_col": "secret_value", "time_col": "date", "horizon": 3},
        ),
        ("anomaly_detect", {"value_col": "secret_value"}),
        (
            "regression",
            {"target": "value", "features": ["secret_value"]},
        ),
        (
            "correlation",
            {"columns": ["value", "secret_value"]},
        ),
        (
            "dimension_contribution",
            {"dimension_col": "secret_group", "value_col": "value"},
        ),
        (
            "group_compare",
            {"group_col": "secret_group", "value_col": "value"},
        ),
        (
            "aggregate_preview",
            {"group_col": "group", "value_col": "secret_value", "agg": "sum"},
        ),
        (
            "gen_chart",
            {
                "chart_type": "bar",
                "encoding": {"x": "group", "y": "secret_value", "agg": "sum"},
            },
        ),
        (
            "transform_dataset",
            {"filters": [{"column": "secret_value", "op": ">", "value": 0}]},
        ),
    ],
)
def test_excluded_columns_are_rejected_by_every_data_tool(
    governed_ref: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    with pytest.raises(ValueError, match="exclude|\u53d7\u4fdd\u62a4"):
        _tool(tool_name).invoke({"dataset_ref": governed_ref, **arguments})


def test_preview_redacts_mask_and_removes_excluded_values(governed_ref: str) -> None:
    result = _tool("data_preview").invoke({"dataset_ref": governed_ref, "rows": 2})

    assert result["rows"]
    for row in result["rows"]:
        assert row["mask_group"] == "***"
        assert row["mask_value"] == "***"
        assert "secret_group" not in row
        assert "secret_value" not in row


def test_masked_summaries_remain_usable_but_raw_values_are_blocked(
    governed_ref: str,
) -> None:
    aggregate = _tool("aggregate_preview").invoke(
        {
            "dataset_ref": governed_ref,
            "group_col": "group",
            "value_col": "mask_value",
            "agg": "sum",
        }
    )
    assert aggregate["rows"]

    correlation = _tool("correlation").invoke(
        {"dataset_ref": governed_ref, "columns": ["value", "mask_value"]}
    )
    assert correlation["matrix"][0][1] is not None

    with pytest.raises(ValueError, match="mask|\u53d7\u4fdd\u62a4"):
        _tool("gen_chart").invoke(
            {
                "dataset_ref": governed_ref,
                "chart_type": "scatter",
                "encoding": {"x": "value", "y": "mask_value", "agg": "none"},
            }
        )
    with pytest.raises(ValueError, match="mask|\u53d7\u4fdd\u62a4"):
        _tool("dimension_contribution").invoke(
            {
                "dataset_ref": governed_ref,
                "dimension_col": "mask_group",
                "value_col": "value",
            }
        )


def test_transform_preserves_policy_on_derived_dataset(governed_ref: str) -> None:
    result = _tool("transform_dataset").invoke(
        {
            "dataset_ref": governed_ref,
            "filters": [{"column": "value", "op": ">", "value": 12}],
        }
    )
    derived_ref = str(result["dataset_ref"])
    metadata = load_metadata(derived_ref)

    assert metadata is not None
    assert metadata["policy"]["columns"] == {
        "mask_group": "mask",
        "mask_value": "mask",
        "secret_group": "exclude",
        "secret_value": "exclude",
    }
    preview = _tool("data_preview").invoke({"dataset_ref": derived_ref, "rows": 2})
    assert all("secret_value" not in row for row in preview["rows"])
    assert all(row["mask_value"] == "***" for row in preview["rows"])


def _request_context() -> MCPRequestContext:
    return MCPRequestContext(
        subject_id="policy-user",
        project_id="policy-project",
        conversation_id="policy-conversation",
        run_id="policy-run",
        plan_version=1,
        step_id="aggregate",
        invocation_id="policy-invocation",
        idempotency_key="policy-idempotency",
        permission_snapshot_id="policy-permission",
        memory_snapshot_id="0" * 32,
        evidence_ledger_version=0,
        data_version_hash="0" * 64,
        cancellation_node_id="0" * 32,
        trace_id="policy-trace",
        deadline_at=(datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
    )


async def test_excluded_column_is_rejected_through_governed_mcp_entry(
    governed_ref: str,
    tmp_path: Path,
) -> None:
    retriever = HybridRetriever(
        HashingEmbedder(),
        LocalKnowledgeStore(str(tmp_path / "kb")),
        LexicalReranker(),
    )
    registry = build_registry(
        excel=build_excel(),
        stats=build_stats(),
        chart=build_chart(),
        dataset_ops=build_ops(),
        report=build_report(),
        retriever=retriever,
    )
    try:
        with pytest.raises(Exception, match="exclude|\u53d7\u4fdd\u62a4"):
            await registry.execute_mcp(
                "aggregate_preview",
                {
                    "dataset_ref": governed_ref,
                    "group_col": "group",
                    "value_col": "secret_value",
                    "agg": "sum",
                },
                _request_context(),
                timeout_seconds=5,
            )
    finally:
        await registry.aclose()
