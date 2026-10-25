"""MANUAL-01：真实合成画像的字段、统计角色、版本与显示值必须同时一致。"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from apps.orchestrator.control.claims import build_evidence_summary, extract_numeric_claims
from apps.orchestrator.control.contracts import build_minimal_contract
from apps.orchestrator.control.verifier import verify_completion
from mcp_servers.excel_parser.advisor import diagnose_data_quality, infer_data_roles
from mcp_servers.excel_parser.tools import infer_schema
from packages.common.dataset_store import save_dataframe
from packages.session.task_models import EvidenceRecord

CAPTURE = json.loads((Path(__file__).parent / "fixtures/manual01_profile.json").read_text())
FIELD = "播出时长（分钟）"
# MANUAL-02: retain CAPTURE byte-for-byte; explicit-basis control is separate.
EXPLICIT_ANSWER = CAPTURE["answer"].replace(
    "记录ID 存在 1 个重复值（75 行中 distinct 为 74）",
    "记录ID重复多余记录数为1，记录ID distinct为74",
).replace("另有 1 行疑似完全重复的记录", "整行重复多余记录数为1")


def evidence(
    result: dict[str, Any] | None = None,
    *,
    ref: str = "d" * 32,
    version: str = "a" * 64,
    ident: str = "profile",
) -> EvidenceRecord:
    """从实际工具形状建立新 Evidence，保留精确的字段来源。"""
    return EvidenceRecord(
        ident,
        "run",
        "invocation",
        None,
        "tool_result",
        {"tool": "get_data_profile"},
        "hash",
        build_evidence_summary(
            summary="画像",
            result=copy.deepcopy(result or CAPTURE["tool_result"]),
            artifact_id=None,
            arguments={"dataset_ref": ref, "data_version_hash": version},
        ),
        "now",
    )


def refs(text: str, records: list[EvidenceRecord] | None = None) -> list[dict[str, Any]]:
    return [
        r
        for c in extract_numeric_claims(
            final_text=text,
            goal="复核画像",
            evidence=records or [evidence()],
        )
        for r in c.value_refs
    ]


@pytest.mark.parametrize(
    "text,paths",
    [
        (f"{FIELD}最大值为999.0。", ["max"]),
        (f"{FIELD}最大为999.0。", ["max"]),
        (f"{FIELD}最小值为10.0。", ["min"]),
        (f"{FIELD}最小为10.0。", ["min"]),
        (f"{FIELD}均值约50.12。", ["mean"]),
        (f"{FIELD}中位数37.5。", ["median"]),
        (
            f"{FIELD}最小值10.0、最大值999.0、均值约50.12、中位数37.5。",
            ["min", "max", "mean", "median"],
        ),
        (
            f"{FIELD}最小10.0、最大999.0、均值约50.12、中位数37.5。",
            ["min", "max", "mean", "median"],
        ),
        ("月份有6个不同取值。", ["distinct_count"]),
        ("记录ID有74个不同取值。", ["distinct_count"]),
        (f"{FIELD}的空值率约为1.33%。", ["null_ratio"]),
    ],
)
def test_isolated_profile_phrases(text: str, paths: list[str]) -> None:
    actual = refs(text)
    assert actual and all(r["supported"] for r in actual), actual
    assert [r["semantic_binding"]["statistic"] for r in actual] == paths
    assert not any("sample_rows" in r["path"] for r in actual)


@pytest.mark.parametrize(
    "text",
    [
        f"{FIELD}最大值为10.0。",
        f"{FIELD}最小为999.0。",
        f"{FIELD}中位数为50.12。",
        f"{FIELD}均值约37.5。",
        "月份有74个不同取值。",
        "记录ID有6个不同取值。",
        f"{FIELD}的空值率为0%。",
        "月份的空值率为1.33%。",
        "播出时长（小时）最大999.0。",
        "不存在的字段最大999.0。",
        f"{FIELD}最小10.0、另一字段最大999.0。",
        "月份和记录ID有6个不同取值。",
        "月份有6元不同取值。",
        "记录ID不同取值数74%。",
        f"{FIELD}最大999.0小时。",
        "行数75元。",
    ],
)
def test_profile_wrong_role_field_or_unit_is_rejected(text: str) -> None:
    actual = refs(text)
    assert actual and any(not r["supported"] for r in actual), actual


@pytest.mark.parametrize("stat,claim", [("min", "最小10.0"), ("max", "最大10.0")])
def test_sample_value_cannot_prove_an_extremum(stat: str, claim: str) -> None:
    result = copy.deepcopy(CAPTURE["tool_result"])
    result["profile"]["columns"][3][stat] = None
    actual = refs(f"{FIELD}{claim}。", [evidence(result)])
    assert actual and all(not r["supported"] for r in actual), actual


def test_full_row_duplicate_does_not_prove_column_duplicate() -> None:
    from tests.test_manual02_profile_contract import tool_profile

    result = tool_profile(pd.read_csv(Path(__file__).parents[1] / "examples/handoff/broadcast.csv"))
    result["quality"]["issues"] = [
        q for q in result["quality"]["issues"] if q["code"] != "non_unique_identifier"
    ]
    actual = refs("记录ID重复多余记录数为1。", [evidence(result)])
    assert actual and all(not r["supported"] for r in actual), actual


@pytest.mark.parametrize(
    "other_ref,other_version",
    [
        ("e" * 32, "a" * 64),
        ("d" * 32, "b" * 64),
    ],
)
@pytest.mark.parametrize("other_value", [999.0, 888.0])
def test_ambiguous_dataset_or_version_is_rejected_before_value_selection(
    other_ref: str,
    other_version: str,
    other_value: float,
) -> None:
    result = copy.deepcopy(CAPTURE["tool_result"])
    result["profile"]["columns"][3]["max"] = other_value
    actual = refs(
        f"{FIELD}最大999.0。",
        [
            evidence(),
            evidence(result, ref=other_ref, version=other_version, ident="other"),
        ],
    )
    assert actual and not actual[0]["supported"], actual


def test_explicit_dataset_and_version_select_only_matching_evidence() -> None:
    records = [evidence(), evidence(ref="e" * 32, version="b" * 64, ident="other")]
    actual = refs(f"数据集{'d' * 32}版本{'a' * 64}：{FIELD}最大999.0。", records)
    assert actual and all(r["supported"] for r in actual), actual
    assert all(r["evidence_id"] == "profile" for r in actual)
    rejected = refs(f"数据集{'d' * 32}版本{'b' * 64}：{FIELD}最大999.0。", records)
    assert rejected and all(not r["supported"] for r in rejected)


def test_same_display_spelling_never_merges_distinct_source_columns() -> None:
    result = copy.deepcopy(CAPTURE["tool_result"])
    result["profile"]["columns"].append(
        {**result["profile"]["columns"][3], "name": "播出时长(分钟)"}
    )
    actual = refs("播出时长(分钟)最大999.0。", [evidence(result)])
    assert actual and not actual[0]["supported"], actual


def test_exact_column_null_count_is_produced_by_real_tool() -> None:
    df = pd.DataFrame({"度量": [None] + [1.0] * 20000})
    profile = infer_schema({"dataset_ref": save_dataframe(df)}).to_dict()
    column = profile["columns"][0]
    assert column["null_count"] == 1
    assert column["null_ratio"] == 0.0  # Rounded ratio cannot reconstruct this count.
    actual = refs("度量有1个缺失值。", [evidence({"profile": profile})])
    assert actual[0]["supported"] and actual[0]["path"] == "$.profile.columns[0].null_count"


def test_legacy_rounded_ratio_is_not_free_arithmetic_proof() -> None:
    actual = refs(f"{FIELD}有1个缺失值。")
    assert actual and all(not r["supported"] for r in actual), actual


@pytest.mark.parametrize("explicit", [False, True])
def test_captured_and_explicit_answers_use_real_current_profile_tool(explicit: bool) -> None:
    csv = Path(__file__).parent.parent / "examples/handoff/broadcast.csv"
    frame = pd.read_csv(csv)
    data_profile = infer_schema({"dataset_ref": save_dataframe(frame)})
    roles = infer_data_roles(data_profile)
    result = {
        "profile": data_profile.to_dict(),
        "roles": roles,
        "quality": diagnose_data_quality(
            data_profile, roles, duplicate_rows=int(frame.duplicated().sum())
        ),
    }
    record = evidence(result)
    answer = EXPLICIT_ANSWER if explicit else CAPTURE["answer"]
    claims = extract_numeric_claims(
        final_text=answer, goal=CAPTURE["goal"], evidence=[record]
    )
    contract = build_minimal_contract(
        run_id="run",
        user_text=CAPTURE["goal"],
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    verification = verify_completion(
        contract=contract,
        final_text=answer,
        artifacts=[],
        invocations=[],
        evidence=[record],
        claims=claims,
    )
    assert verification.passed is explicit, [(c.statement, c.value_refs) for c in claims]
    assert all(r["supported"] for c in claims for r in c.value_refs) is explicit


def test_captured_frozen_evidence_is_unchanged_and_missing_count_stays_unsupported() -> None:
    record = EvidenceRecord(**CAPTURE["evidence"])
    actual = refs(f"{FIELD}有1个缺失值。", [record])
    assert actual and not actual[0]["supported"]


def test_identical_duplicate_evidence_for_one_version_is_not_ambiguous() -> None:
    record = evidence()
    actual = refs(f"{FIELD}最大999.0。", [record, replace(record, evidence_id="same")])
    assert actual and actual[0]["supported"], actual


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong", [False, True])
async def test_real_upload_host_replays_explicit_basis_control(tmp_path: Path, wrong: bool) -> None:
    from apps.api.deps import session_store_dep
    from apps.api.main import create_app
    from apps.orchestrator.agent_loop import (
        AgentLoopConfig,
        ConversationLockPool,
        stream_agent_chat,
    )
    from apps.orchestrator.agent_tools import AgentContext
    from fastapi.testclient import TestClient
    from packages.models.types import ModelResponse, ToolCall
    from packages.session.store import SessionStore
    from packages.session.task_store import TaskStore

    from tests.test_agent_tools import _registry

    store = SessionStore(str(tmp_path / "host.db"))
    project = store.create_project("画像证据合成回归")
    conversation = store.create_conversation(project.id)
    app = create_app()
    app.dependency_overrides[session_store_dep] = lambda: store
    source = Path(__file__).parent.parent / "examples/handoff/broadcast.csv"
    with TestClient(app) as client:
        upload = client.post(
            "/upload/excel",
            data={"project_id": project.id, "conversation_id": conversation.id},
            files={"file": (source.name, source.read_bytes(), "text/csv")},
        )
        assert upload.status_code == 200, upload.text
    dataset_ref = upload.json()["dataset_ref"]
    answer = EXPLICIT_ANSWER.replace("最大 999.0", "最大 10.0") if wrong else EXPLICIT_ANSWER

    class Gateway:
        turns = 0

        async def stream_turn(self, *args: Any, **kwargs: Any) -> Any:
            self.turns += 1
            if self.turns > 2:
                raise RuntimeError("Offline fixture stops before any repair")
            if self.turns == 1:
                yield ModelResponse(
                    content="",
                    model="offline-captured-profile",
                    tool_calls=[
                        ToolCall(
                            id="profile",
                            name="get_data_profile",
                            arguments=json.dumps({"dataset_ref": dataset_ref}),
                        ),
                    ],
                )
            else:
                yield ModelResponse(content=answer, model="offline-captured-profile")

    gateway = Gateway()
    registry = _registry(AgentContext(store, project.id, conversation.id))
    try:
        events = [
            event
            async for event in stream_agent_chat(
                conversation_id=conversation.id,
                project_id=project.id,
                store=store,
                user_text=CAPTURE["goal"],
                gateway=gateway,
                registry=registry,
                locks=ConversationLockPool(),
                config=AgentLoopConfig(max_tool_calls=12, max_parallel_tools=1),
            )
        ]
    finally:
        await registry.aclose()
    run_id = next(json.loads(e["data"])["run_id"] for e in events if e["event"] == "meta")
    terminal = [json.loads(e["data"]) for e in events if e["event"] in {"done", "error"}][-1]
    invocations = TaskStore(store.db_path).list_invocations(run_id)
    assert len(invocations) == 1 and invocations[0].status == "succeeded"
    assert (terminal.get("run_status") == "completed") is not wrong, terminal
    assert gateway.turns == (3 if wrong else 2)
    profile = next(
        a for a in store.list_artifacts(conversation.id) if a.source_tool == "get_data_profile"
    )
    assert profile.payload["profile"]["columns"][3]["null_count"] == 1
    assert profile.dataset_ref == dataset_ref
    (tmp_path / "witness.json").write_text(
        json.dumps(
            {
                "wrong": wrong,
                "run_id": run_id,
                "terminal": terminal,
                "events": events,
                "tool_result": profile.payload,
                "answer": answer,
                "paid_calls": 0,
                "gateway_fixture_turns": gateway.turns,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def test_profile_report_uses_same_field_and_statistic_binding() -> None:
    from apps.orchestrator.control.report_claims import validate_report_narratives
    from packages.session.models import Artifact

    artifact = Artifact(
        "profile",
        "c",
        "m",
        "profile",
        CAPTURE["tool_result"],
        None,
        "get_data_profile",
        {"dataset_ref": "d" * 32},
        "d" * 32,
        "now",
    )
    text = f"{FIELD}最小10、最大999、均值50.12、中位数37.5。月份有6个不同取值。"
    assert (
        validate_report_narratives([artifact], {"title": "画像", "insights": text})["status"]
        == "passed"
    )
    with pytest.raises(ValueError):
        validate_report_narratives(
            [artifact], {"title": "画像", "insights": text.replace("最大999", "最大10")}
        )


def test_exact_counts_survive_persisted_profile_and_exclude() -> None:
    from mcp_servers.excel_parser.advisor import infer_data_roles_from_mapping
    from packages.governance.data_boundary import ColumnRule, EffectivePolicy
    from packages.governance.redaction import apply_policy

    profile = infer_schema(
        {
            "dataset_ref": save_dataframe(
                pd.DataFrame(
                    {
                        "度量": [None] + [1.0] * 20000,
                    }
                )
            )
        }
    )
    restored = infer_data_roles_from_mapping(profile.to_dict())
    assert restored["columns"][0]["profile_evidence"]["non_null_count"] == 20000
    hidden = apply_policy(profile, EffectivePolicy(columns={"度量": ColumnRule.EXCLUDE}))
    assert hidden.to_dict()["columns"][0]["null_count"] is None
    assert hidden.to_dict()["columns"][0]["null_ratio"] is None
    assert not refs("度量有1个缺失值。", [evidence({"profile": hidden.to_dict()})])[0]["supported"]


@pytest.mark.parametrize("count", [-1, True, 20002, 10000])
def test_invalid_persisted_exact_count_is_rejected(count: Any) -> None:
    from mcp_servers.excel_parser.advisor import infer_data_roles_from_mapping

    profile = {
        "row_count": 20001,
        "column_count": 1,
        "columns": [
            {
                "name": "度量",
                "dtype": "float",
                "null_ratio": 0.0,
                "distinct_count": 1,
                "null_count": count,
            }
        ],
    }
    with pytest.raises(ValueError):
        infer_data_roles_from_mapping(profile)


def test_group_size_never_proves_whole_dataset_shape() -> None:
    group = replace(
        evidence({"rows": [{"group": "A", "row_count": 5}]}),
        evidence_id="group",
        source={"tool": "aggregate_preview"},
    )
    actual = refs("数据有5行×5列。", [evidence(), group])
    assert not actual[0]["supported"]


def test_conflicting_facts_in_one_immutable_profile_are_rejected() -> None:
    result = copy.deepcopy(CAPTURE["tool_result"])
    result["profile"]["columns"][3]["max"] = 888.0
    assert not refs(f"{FIELD}最大999。", [evidence(), evidence(result, ident="conflict")])[0][
        "supported"
    ]


@pytest.mark.parametrize("text", [
    "记录ID存在1个重复值（75行中distinct为74）。",
    "另有1行疑似完全重复的记录。",
])
def test_legacy_ambiguous_duplicate_phrases_are_no_longer_exact_counts(text: str) -> None:
    # Original accepted phrases are retained as explicit contract-change negatives.
    actual = refs(text)
    assert actual and any(not ref["supported"] for ref in actual)
