"""Delivery regression: domain-neutral clarification and provider error containment."""
from __future__ import annotations

from dataclasses import replace
from typing import cast

import httpx
import pytest
from apps.orchestrator.agent_loop import _role_selection_clarification
from apps.orchestrator.control.data_role_guard import validate_data_role_preconditions
from apps.orchestrator.control.report_claims import validate_report_narratives
from openai import APIConnectionError
from packages.models.adapters.base import ModelAdapter
from packages.models.gateway import ModelGateway
from packages.models.types import Message, Scenario
from packages.session.models import Artifact

from tests.test_data_role_guard import _dataset
from tests.test_model_gateway import _FailingAdapter, _registry, _seed


@pytest.mark.parametrize("names", [
    ("播出时长", "覆盖人数"), ("停机分钟", "耗电量"),
    ("参加人数", "活动时长"), ("销售额", "利润"),
])
def test_incomplete_profile_never_guesses_roles_from_domain_vocabulary(names):
    dataset = replace(_dataset(), profile={
        "columns": [{"name": name, "dtype": "float"} for name in names],
    })
    result = _role_selection_clarification(
        "分析趋势", dataset, role="metric", question_id="metric", about="metric",
        label="本次分析指标", reason="指标需要确认",
    )
    assert result is not None
    assert result["about"] == "data_profile"
    assert "data_role_request" not in result


@pytest.mark.asyncio
async def test_provider_error_body_never_reaches_log_or_public_error(monkeypatch):
    canary = "SYNTHETIC_PRIVATE_CONTENT_DO_NOT_LOG"
    captured = []

    class Logger:
        def info(self, event, **fields):
            pass

        def warning(self, event, **fields):
            captured.append((event, fields))

    monkeypatch.setattr("packages.models.gateway._log", Logger())
    gateway = ModelGateway(_registry())
    error = APIConnectionError(
        message=canary, request=httpx.Request("POST", "http://synthetic.invalid"),
    )
    _seed(gateway, {"primary": _FailingAdapter(error), "backup": _FailingAdapter(error)})
    with pytest.raises(RuntimeError) as caught:
        await gateway.complete(Scenario.CORE_REASONING, [Message(role="user", content="test")])
    assert canary not in repr(captured)
    assert canary not in str(caught.value)
    assert len(captured) == 2
    assert all(fields["error_type"] == "APIConnectionError" for _, fields in captured)


def _table(field, identity="a"):
    return Artifact(
        id=identity, conversation_id="c", message_id="m", type="table",
        payload={"total_value": 1350, "rows": [], "group_total": 6},
        file_ref=None, source_tool="aggregate_preview",
        params={"value_col": field, "group_col": "月份", "agg": "sum"},
        dataset_ref="d" * 32, created_at="2026-10-15T00:00:00Z",
    )


def test_report_keeps_full_unit_bearing_metric_identity():
    result = validate_report_narratives(
        [_table("播出时长（分钟）")],
        {"title": "节目播出报告", "insights": "播出时长（分钟）总计 1350。"},
    )
    assert result["status"] == "passed"
    refs = result["claims"][0]["value_refs"]
    assert refs[0]["semantic_binding"]["metric"] == "播出时长（分钟）"


@pytest.mark.parametrize("artifacts,text", [
    ([_table("播出时长（分钟）")], "播出时长（小时）总计 1350。"),
    ([_table("时长（分钟）"), _table("时长(分钟)", "b")], "时长(分钟)总计 1350。"),
])
def test_report_rejects_wrong_unit_and_normalized_field_collision(artifacts, text):
    with pytest.raises(ValueError, match="缺少所选分析的证据"):
        validate_report_narratives(artifacts, {"title": "报告", "insights": text})


@pytest.mark.parametrize("chart_type", ["line", "bar", "scatter"])
def test_chart_cannot_treat_identifier_as_numeric_measure(chart_type):
    result = validate_data_role_preconditions(
        tool_name="gen_chart",
        arguments={"dataset_ref": "d" * 32, "chart_type": chart_type,
                   "encoding": {"x": "发生时间", "y": "记录ID", "agg": "sum"}},
        dataset=_dataset(), confirmations=(), data_version_hash="a" * 64,
    )
    assert result is not None and not result.allowed


def test_chart_accepts_real_metric_and_count_does_not_sum_identifier():
    for encoding in [
        {"x": "发生时间", "y": "金额", "agg": "sum"},
        {"x": "发生时间", "y": "记录ID", "agg": "count"},
    ]:
        result = validate_data_role_preconditions(
            tool_name="gen_chart",
            arguments={"dataset_ref": "d" * 32, "chart_type": "line", "encoding": encoding},
            dataset=_dataset(), confirmations=(), data_version_hash="a" * 64,
        )
        assert result is not None and result.allowed


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["stream", "stream_turn"])
@pytest.mark.parametrize("started", [False, True])
async def test_stream_error_is_sanitized_without_restarting_partial_answer(method, started):
    calls = []
    canary = "SYNTHETIC_PROVIDER_BODY"

    class Adapter:
        def __init__(self, name):
            self.name = name

        async def stream(self, *args, **kwargs):
            calls.append(self.name)
            if started:
                yield "已经输出"
            raise APIConnectionError(
                message=canary, request=httpx.Request("POST", "http://synthetic.invalid"),
            )

        stream_turn = stream

    gateway = ModelGateway(_registry())
    gateway._adapters = {name: cast(ModelAdapter, Adapter(name)) for name in ["primary", "backup"]}
    output = []
    with pytest.raises(RuntimeError) as caught:
        stream = getattr(gateway, method)(
            Scenario.CORE_REASONING, [Message(role="user", content="test")]
        )
        async for item in stream:
            output.append(item)
    assert canary not in str(caught.value)
    assert calls == (["primary"] if started else ["primary", "backup"])
    assert output == (["已经输出"] if started else [])
