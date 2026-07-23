"""Agent 工具注册表测试（阶段2 验收：每个工具可独立验证）。

覆盖：定义导出（schema 同源）、execute 入口语义、质量概况、kb_search 封装、
transform 血缘落库、generate_report 按工件组装。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import cast

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.orchestrator.agent_tools import (  # noqa: E402
    GENERATE_REPORT_SCHEMA,
    KB_SEARCH_SCHEMA,
    AgentContext,
    AgentToolError,
    AgentToolRegistry,
    build_registry,
)
from mcp_servers.chart.server import build_server as build_chart  # noqa: E402
from mcp_servers.dataset_ops.server import build_server as build_ops  # noqa: E402
from mcp_servers.excel_parser.server import build_server as build_excel  # noqa: E402
from mcp_servers.report.server import build_server as build_report  # noqa: E402
from mcp_servers.stats.schemas import REGRESSION_SCHEMA  # noqa: E402
from mcp_servers.stats.server import build_server as build_stats  # noqa: E402
from packages.common.dataset_store import save_dataframe  # noqa: E402
from packages.governance.policy import DEFAULT_AGENT_TOOL_ALLOWLIST  # noqa: E402
from packages.governance.schema_validator import SchemaValidationError  # noqa: E402
from packages.rag.retriever import HybridRetriever, RetrievalResult  # noqa: E402
from packages.rag.store import SearchHit  # noqa: E402
from packages.session.store import SessionStore  # noqa: E402

_EXPECTED_TOOLS = [
    "get_data_profile",
    "trend_analysis",
    "anomaly_detect",
    "regression",
    "correlation",
    "gen_chart",
    "chart_screenshot",
    "transform_dataset",
    "aggregate_preview",
    "kb_search",
    "generate_report",
]


class _FakeRetriever:
    """确定性检索替身。"""

    def __init__(self, hits: list[SearchHit] | None = None) -> None:
        self._hits = hits or []

    def retrieve(self, query: str, top_k: int = 5) -> RetrievalResult:
        return RetrievalResult(hits=self._hits[:top_k], is_empty=not self._hits)


def _registry(
    context: AgentContext | None = None, hits: list[SearchHit] | None = None
) -> AgentToolRegistry:
    return build_registry(
        excel=build_excel(),
        stats=build_stats(),
        chart=build_chart(),
        dataset_ops=build_ops(),
        report=build_report(),
        retriever=cast(HybridRetriever, _FakeRetriever(hits)),
        context=context,
    )


@pytest.fixture
def sales_ref() -> str:
    df = pd.DataFrame(
        {
            "地区": ["华东", "华南", "华东", "华北"],
            "销量": [100.0, 200.0, 100.0, 50.0],
            "常数列": [1, 1, 1, 1],
        }
    )
    df.loc[2] = df.loc[0]  # 制造 1 行整行重复
    return save_dataframe(df)


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[SessionStore, AgentContext]:
    store = SessionStore(str(tmp_path / "chatbi.db"))
    project = store.create_project("阶段2验收")
    conversation = store.create_conversation(project.id, title="工具测试")
    return store, AgentContext(
        store=store, project_id=project.id, conversation_id=conversation.id
    )


# ── 定义导出 ──


def test_registry_exports_all_tools() -> None:
    reg = _registry()
    assert reg.names == _EXPECTED_TOOLS
    assert set(reg.names) == DEFAULT_AGENT_TOOL_ALLOWLIST
    defs = reg.openai_tools()
    assert all(d["type"] == "function" for d in defs)
    assert all(d["function"]["description"] for d in defs)


def test_mcp_schema_is_shared_with_model_defs() -> None:
    """schema 同源（红线3）：喂模型的 parameters 就是 Tool.invoke 校验的 schema。"""
    defs = {d["function"]["name"]: d["function"]["parameters"] for d in _registry().openai_tools()}
    assert defs["regression"] is REGRESSION_SCHEMA
    assert defs["kb_search"] is KB_SEARCH_SCHEMA
    assert defs["generate_report"] is GENERATE_REPORT_SCHEMA
    descriptors = {item.name: item for item in _registry().mcp_descriptors()}
    assert descriptors["regression"].input_schema is REGRESSION_SCHEMA
    assert descriptors["gen_chart"].metadata.artifact_types == ("chart",)
    assert descriptors["generate_report"].output_schema["required"] == [
        "report_id",
        "md_path",
        "analysis_ids",
        "skipped_charts",
    ]


# ── execute 入口 ──


def test_execute_unknown_tool() -> None:
    with pytest.raises(AgentToolError, match="工具不存在"):
        _registry().execute("nonexistent", "{}")


def test_execute_invalid_json() -> None:
    with pytest.raises(AgentToolError, match="不是合法 JSON"):
        _registry().execute("aggregate_preview", '{"broken":')
    with pytest.raises(AgentToolError, match="JSON 对象"):
        _registry().execute("aggregate_preview", '["not", "object"]')


def test_execute_schema_violation_propagates(sales_ref: str) -> None:
    """模型幻造参数 → SchemaValidationError 上抛（阶段3 回传重试的钩子）。"""
    with pytest.raises(SchemaValidationError):
        _registry().execute(
            "aggregate_preview",
            f'{{"dataset_ref": "{sales_ref}", "group_col": "地区", "agg": "count", "幻造": 1}}',
        )


def test_execute_happy_path(sales_ref: str) -> None:
    out = _registry().execute(
        "aggregate_preview",
        f'{{"dataset_ref": "{sales_ref}", "group_col": "地区", "agg": "count"}}',
    )
    assert out["group_total"] == 3


# ── get_data_profile：画像 + 质量概况 ──


def test_profile_with_quality(sales_ref: str) -> None:
    out = _registry().execute("get_data_profile", f'{{"dataset_ref": "{sales_ref}"}}')
    assert out["profile"]["row_count"] == 4
    assert out["quality"]["duplicate_rows"] == 1
    assert out["quality"]["constant_columns"] == ["常数列"]
    assert out["quality"]["high_null_columns"] == []


# ── kb_search ──


def test_kb_search_hits_and_empty() -> None:
    hit = SearchHit(
        chunk_id="c1", text="复购率=复购用户/总购买用户。" * 60, source="指标.md", score=1.0
    )
    out = _registry(hits=[hit]).execute("kb_search", '{"query": "复购率怎么定义"}')
    assert out["is_empty"] is False
    assert out["hits"][0]["source"] == "指标.md"
    assert len(out["hits"][0]["text"]) <= 500  # 片段截断

    empty = _registry().execute("kb_search", '{"query": "不存在的主题"}')
    assert empty["is_empty"] is True and empty["hits"] == []


def test_kb_search_blank_query_rejected() -> None:
    with pytest.raises(AgentToolError, match="非空 query"):
        _registry().execute("kb_search", '{"query": "  "}')


def test_kb_search_schema_rejects_extra_and_invalid_top_k() -> None:
    """自定义封装工具也必须经模型看到的同一份 schema 校验。"""
    with pytest.raises(SchemaValidationError):
        _registry().execute("kb_search", '{"query": "复购率", "unexpected": 1}')
    with pytest.raises(SchemaValidationError):
        _registry().execute("kb_search", '{"query": "复购率", "top_k": 1.5}')


# ── transform_dataset：血缘落库 ──


def test_transform_registers_lineage(
    sales_ref: str, workspace: tuple[SessionStore, AgentContext]
) -> None:
    store, context = workspace
    store.register_dataset(
        ref=sales_ref, project_id=context.project_id, filename="销售.xlsx",
        profile={"row_count": 4},
    )
    out = _registry(context=context).execute(
        "transform_dataset",
        f'{{"dataset_ref": "{sales_ref}", "drop_duplicates": []}}',
    )
    assert out["registered"] is True
    derived = store.get_dataset(out["dataset_ref"])
    assert derived is not None
    assert derived.parent_ref == sales_ref
    assert derived.transform == {"drop_duplicates": []}
    assert derived.filename == "销售.xlsx（衍生）"
    assert derived.profile["row_count"] == 3  # 去重后画像是真实重算的


def test_transform_auto_registers_unlisted_parent(
    sales_ref: str, workspace: tuple[SessionStore, AgentContext]
) -> None:
    """源数据集未登记时自动补登记父级，血缘链不断（冒烟发现的缺口回归）。"""
    store, context = workspace
    out = _registry(context=context).execute(
        "transform_dataset", f'{{"dataset_ref": "{sales_ref}", "drop_duplicates": []}}'
    )
    parent = store.get_dataset(sales_ref)
    assert parent is not None and parent.parent_ref is None  # 父级被补登记
    derived = store.get_dataset(out["dataset_ref"])
    assert derived is not None and derived.parent_ref == sales_ref


def test_transform_without_context_skips_registration(sales_ref: str) -> None:
    out = _registry().execute(
        "transform_dataset", f'{{"dataset_ref": "{sales_ref}", "drop_duplicates": []}}'
    )
    assert "registered" not in out  # 无上下文只做变换，不登记


# ── generate_report：按工件组装 ──


def _seed_artifacts(store: SessionStore, context: AgentContext, sales_ref: str) -> dict[str, str]:
    store.register_dataset(
        ref=sales_ref, project_id=context.project_id, filename="销售.xlsx",
        profile={"row_count": 4},
    )
    msg = store.append_message(
        conversation_id=context.conversation_id, role="assistant", content="分析完成"
    )
    profile_art = store.create_artifact(
        conversation_id=context.conversation_id, message_id=msg.id, type="profile",
        payload={"profile": {"dataset_ref": sales_ref, "row_count": 4, "column_count": 3,
                             "columns": [], "sample_rows": []}},
        source_tool="infer_schema", dataset_ref=sales_ref,
    )
    stats_art = store.create_artifact(
        conversation_id=context.conversation_id, message_id=msg.id, type="stats",
        payload={"kind": "regression", "result": {"r_squared": 0.9, "n_obs": 4},
                 "interpretation": "拟合良好"},
        source_tool="regression", params={"analysis_id": "analysis-regression"},
        dataset_ref=sales_ref,
    )
    broken_chart = store.create_artifact(
        conversation_id=context.conversation_id, message_id=msg.id, type="chart",
        payload={"caption": "无 option 的图"}, source_tool="gen_chart",
    )
    return {
        "profile": profile_art.id,
        "stats": stats_art.id,
        "stats_analysis": "analysis-regression",
        "chart": broken_chart.id,
    }


def test_generate_report_from_artifacts(
    sales_ref: str, workspace: tuple[SessionStore, AgentContext]
) -> None:
    store, context = workspace
    ids = _seed_artifacts(store, context, sales_ref)
    out = _registry(context=context).execute(
        "generate_report",
        f'{{"title": "销售分析报告", "analysis_ids": ["{ids["profile"]}", '
        f'"{ids["stats_analysis"]}", '
        f'"{ids["chart"]}"], "insights": "整体向好"}}',
    )
    md = out["markdown"]
    assert "# 销售分析报告" in md
    assert "要点速览" in md and "整体向好" in md
    assert "统计分析" in md and "0.9" in md          # 数字来自工件真实结果（红线2）
    assert out["skipped_charts"] == 1                 # 无 option 的图被跳过不阻断
    assert out["analysis_ids"][1] == "analysis-regression"
    assert Path(out["md_path"]).exists()


def test_generate_report_requires_context_and_valid_ids(
    sales_ref: str, workspace: tuple[SessionStore, AgentContext]
) -> None:
    store, context = workspace
    ids = _seed_artifacts(store, context, sales_ref)
    with pytest.raises(AgentToolError, match="会话上下文"):
        _registry().execute(
            "generate_report", f'{{"title": "T", "analysis_ids": ["{ids["profile"]}"]}}'
        )
    with pytest.raises(AgentToolError, match="分析不存在"):
        _registry(context=context).execute(
            "generate_report", '{"title": "T", "analysis_ids": ["missing"]}'
        )


def test_generate_report_rejects_legacy_or_extra_model_args() -> None:
    """报告组装入口严格采用 analysis_ids，且在检查会话上下文前完成 schema 校验。"""
    with pytest.raises(SchemaValidationError):
        _registry().execute(
            "generate_report", '{"title": "T", "artifact_ids": ["legacy"]}'
        )
    with pytest.raises(SchemaValidationError):
        _registry().execute(
            "generate_report", '{"title": "T", "analysis_ids": ["a"], "extra": true}'
        )
