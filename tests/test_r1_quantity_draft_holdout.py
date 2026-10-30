"""独立 DR09–DR16 草稿反例：期待先冻结，引用仅从真实 Host 目录选择。

Oracle 为已复审数量合同，不使用 prepare/validate 的结果生成预期。
原修前证据仍在独立 dr-baseline，缺接口状态不追溯改写为通过。
"""

from __future__ import annotations

import json
import socket
from dataclasses import replace
from typing import Any

import pytest
from apps.orchestrator.control.claims import build_evidence_summary
from apps.orchestrator.control.quantity_presentation import (
    parse_draft,
    prepare_candidate,
    reference_catalog,
    validate_candidate,
)
from mcp_servers.excel_parser.advisor import diagnose_data_quality
from mcp_servers.excel_parser.profile import ColumnProfile, DataProfile
from packages.orchestration.quality_facts import content_hash
from packages.session.task_models import EvidenceRecord


@pytest.fixture(autouse=True)
def deny_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """此独立用例只计算内存合成数据，不允许网络模型或真实数据库。"""

    def deny(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("DR quantity draft holdout forbids network")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


def evidence(*, dataset: str = "a" * 32, derived: bool = False,
             root_known: bool = True) -> list[EvidenceRecord]:
    """五行手工数据的三项诊断；零模型、零落盘业务数据。

    指标[1,1,2,null,4]；标识[1,1,2,3,4]；一条整行多余记录。
    工具规则为整行重复、指标缺失、标识非唯一；不等同已确认异常实体。
    """
    version = "v2" if derived else "v1"
    root = "c" * 32 if derived else dataset
    columns = [
        ColumnProfile("时长（分钟）", "float", .2, 3, min=1., max=4.,
                      mean=2., median=1.5, null_count=1),
        ColumnProfile("记录ID", "int", 0., 4, min=1., max=4.,
                      mean=2.2, median=2., null_count=0),
    ]
    profile = DataProfile(dataset, 5, 2, columns)
    roles = {"schema": "chatbi-data-roles-v1", "columns": [
        {"column": "时长（分钟）", "primary_role": "metric", "ambiguous": False},
        {"column": "记录ID", "primary_role": "identifier", "ambiguous": False},
    ]}
    quality = diagnose_data_quality(profile, roles, duplicate_rows=1)
    assert quality["summary"]["issue_count"] == 3
    raw = {"profile": profile.to_dict(), "roles": roles, "quality": quality}
    context: dict[str, Any] = {"dataset_ref": dataset, "data_version_hash": version}
    if root_known:
        context["profile_population"] = {
            "schema": "host-profile-population-v1", "dataset_ref": dataset,
            "dataset_version": version, "root_ref": root,
            "steps": [{"parent_ref": root, "dataset_ref": dataset,
                       "transform": {"sort": [{"column": "记录ID", "order": "asc"}]}}]
            if derived else [],
        }
    summary = build_evidence_summary(
        summary="独立合成画像", result=raw, artifact_id=None, arguments=context
    )
    return [EvidenceRecord(
        "dr-evidence-" + dataset, "dr-run", "dr-invocation-" + dataset, None,
        "tool_result", {"tool": "get_data_profile", "dataset_ref": dataset},
        content_hash(raw), summary, "2026-10-30T00:00:00Z",
    )]


def ref_for(records: list[EvidenceRecord], kind: str, **conditions: Any) -> str:
    """按冻结的角色/来源选择对象，不检查被测判定是否为PASS。"""
    selected = [key for key, item in reference_catalog(records).items()
                if item["kind"] == kind
                and all((item.get("binding", {}) if key_name in {"statistic", "metric"}
                         else item).get(key_name) == value
                        for key_name, value in conditions.items())]
    assert len(selected) == 1, (kind, conditions, selected)
    return selected[0]


def wire(*blocks: dict[str, Any]) -> str:
    return json.dumps({"schema": "quantity-presentation-draft-v1", "blocks": blocks},
                      ensure_ascii=False)


def rejected(text: str, records: list[EvidenceRecord]) -> None:
    result = validate_candidate(text, records, goal="复核当前画像")
    assert not result.passed, result
    assert result.prepared.source_text == text
    assert result.issues, "拒绝必须有可审计原因，不能依赖消失的候选"


def test_dr09_duplicate_issue_positions_do_not_prove_distinct_diagnoses() -> None:
    records = evidence()
    first = ref_for(records, "advice", issue_id="quality-001")
    second = ref_for(records, "advice", issue_id="quality-002")
    text = wire(
        {"kind": "prose", "text": "以下三项不同的规则诊断："},
        {"kind": "list", "heading_style": "none", "items": [
            {"kind": "advice", "advice_ref": first},
            {"kind": "advice", "advice_ref": first},
            {"kind": "advice", "advice_ref": second},
        ]},
    )
    # All references resolve; failure must not be manufactured by an absent ref.
    assert parse_draft(text, records)
    rejected(text, records)


@pytest.mark.parametrize("variant", ["maximum", "bare_value", "时长最大值"])
def test_dr10_minimum_cannot_be_rendered_as_maximum_slot(variant: str) -> None:
    records = evidence()
    minimum = ref_for(records, "fact", statistic="min", metric="时长（分钟）")
    text = wire({"kind": "fact", "fact_ref": minimum, "display_variant": variant})
    with pytest.raises(ValueError, match="display"):
        parse_draft(text, records)
    rejected(text, records)


def test_dr10_complete_minimum_fact_is_a_positive_control() -> None:
    records = evidence()
    minimum = ref_for(records, "fact", statistic="min", metric="时长（分钟）")
    text = wire({"kind": "fact", "fact_ref": minimum, "display_variant": "statement"})
    result = validate_candidate(text, records)
    assert result.passed, result
    assert "最小值" in result.text and "时长（分钟）" in result.text
    assert any(r.get("supported") is True and r.get("path", "").endswith(".min")
               for claim in result.claims for r in claim.value_refs)


@pytest.mark.parametrize("heading", ["最大值：", "最大值为"])
def test_dr10_free_prose_cannot_relabel_minimum_fact(heading: str) -> None:
    records = evidence()
    minimum = ref_for(records, "fact", statistic="min", metric="时长（分钟）")
    text = wire({"kind": "prose", "text": heading},
                {"kind": "fact", "fact_ref": minimum, "display_variant": "statement"})
    assert parse_draft(text, records), "所有引用合法，检查外层角色与完整事实的关系"
    rejected(text, records)


@pytest.mark.parametrize("forged", [
    {"free_text": "已核实八个异常"}, {"status": "supported"},
    {"trusted": True}, {"value": 8}, {"role": "verified_anomaly"},
])
def test_dr11_model_authority_fields_are_not_discarded(forged: dict[str, Any]) -> None:
    records = evidence()
    advice = ref_for(records, "advice", issue_id="quality-001")
    text = wire({"kind": "advice", "advice_ref": advice, **forged})
    with pytest.raises(ValueError, match="field"):
        parse_draft(text, records)
    prepared = prepare_candidate(text, records)
    assert not prepared.valid and prepared.text == text
    rejected(text, records)


@pytest.mark.parametrize("text", [
    '{"schema":"quantity-presentation-draft-v1","blocks":[],"blocks":[]}',
    '{"schema":"quantity-presentation-draft-v1","blocks":'
    '[{"kind":"prose","kind":"future","text":"正文"}]}',
    '{"schema":"quantity-presentation-draft-v1","blocks":'
    '[{"kind":"prose","text":"正文","source":"x","source":"y"}]}',
    '{"schema":"quantity-presentation-draft-v1","blocks":[{"kind":"future"}]}',
    '{"schema":"quantity-presentation-draft-v1","blocks":[',
])
def test_dr12_ambiguous_or_unknown_envelope_has_no_legacy_fallback(text: str) -> None:
    records = evidence()
    with pytest.raises(ValueError):
        parse_draft(text, records)
    prepared = prepare_candidate(text, records)
    assert prepared.source_text == prepared.text == text
    assert not prepared.valid and any("draft" in issue for issue in prepared.issues)
    rejected(text, records)


def test_dr13_source_local_issue_id_is_not_a_global_reference() -> None:
    records = evidence() + evidence(dataset="b" * 32)
    catalog = reference_catalog(records)
    valid = [key for key, item in catalog.items()
             if item["kind"] == "advice" and item["issue_id"] == "quality-001"]
    assert len(valid) == 2 and valid[0] != valid[1]
    rejected(wire({"kind": "advice", "advice_ref": "quality-001"}), records)


def test_dr14_unit_inspection_requires_no_invented_typed_unit() -> None:
    records = evidence()
    target = ref_for(records, "target", target_kind="schema")
    text = wire({"kind": "inspection", "action": "review_unit_annotations",
                 "target_ref": target})
    result = validate_candidate(text, records)
    assert result.passed, result
    assert "核对单位" in result.text
    assert not result.claims, "检查建议不能成为单位已经核实的业务Claim"


def test_dr15_current_version_cannot_impersonate_original_root() -> None:
    records = evidence(derived=True)
    current = ref_for(records, "target", target_kind="version")
    root = ref_for(records, "target", target_kind="root")
    assert reference_catalog(records)[root]["root"] != records[0].source["dataset_ref"]
    rejected(wire({"kind": "inspection", "action": "retain_source_reference",
                   "target_ref": current}), records)
    positive = validate_candidate(wire({
        "kind": "inspection", "action": "retain_source_reference", "target_ref": root,
    }), records)
    assert positive.passed, positive


def test_dr16_missing_root_stays_unknown_and_is_not_current_version() -> None:
    records = evidence(root_known=False)
    catalog = reference_catalog(records)
    assert not any(v.get("target_kind") == "root" for v in catalog.values())
    rejected(wire({"kind": "inspection", "action": "retain_source_reference",
                   "target_ref": "target:root:missing"}), records)
    current = ref_for(records, "target", target_kind="version")
    rejected(wire({"kind": "inspection", "action": "retain_source_reference",
                   "target_ref": current}), records)


def test_current_reference_is_invalid_after_source_version_changes() -> None:
    records = evidence()
    minimum = ref_for(records, "fact", statistic="min", metric="时长（分钟）")
    context = {**records[0].summary["numeric_context"], "data_version_hash": "other-version"}
    changed = [replace(records[0], summary={**records[0].summary,
                                           "numeric_context": context})]
    rejected(wire({"kind": "fact", "fact_ref": minimum,
                   "display_variant": "statement"}), changed)
