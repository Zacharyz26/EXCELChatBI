"""r9b：真实持久化载荷必须经过完整报告正文校验和渲染。"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from apps.orchestrator.agent_tools import AgentContext, AgentToolError
from apps.orchestrator.control.report_claims import validate_report_narratives
from packages.common.config import get_settings
from packages.session.models import Artifact
from packages.session.store import SessionStore

from tests.test_agent_tools import _registry

FIXTURE = Path(__file__).parent / "fixtures" / "report_r9b_selected.json"


@pytest.fixture
def persisted_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[SessionStore, AgentContext, dict[str, Any], list[Artifact]]]:
    monkeypatch.setenv("REPORT_DIR", str(tmp_path / "reports"))
    get_settings.cache_clear()
    saved = json.loads(FIXTURE.read_text())
    store = SessionStore(str(tmp_path / "report.db"))
    project = store.create_project("合成报告回归")
    conversation = store.create_conversation(project.id, title="r9b")
    context = AgentContext(store=store, project_id=project.id, conversation_id=conversation.id)
    message = store.append_message(
        conversation_id=conversation.id,
        role="assistant",
        content="保存的合成分析",
    )
    ref = saved["artifacts"][0]["dataset_ref"]
    store.register_dataset(ref=ref, project_id=project.id, filename="synthetic.xlsx", profile={})
    for item in saved["artifacts"]:
        store.create_artifact(
            conversation_id=conversation.id,
            message_id=message.id,
            type=item["type"],
            payload=item["payload"],
            source_tool=item["source_tool"],
            params=item["params"],
            dataset_ref=item["dataset_ref"],
        )
    # Reopen SQLite so validation exercises persisted JSON rather than an in-memory stub.
    store = SessionStore(store.db_path)
    context = AgentContext(store=store, project_id=project.id, conversation_id=conversation.id)
    try:
        yield store, context, saved["args"], store.list_artifacts(conversation.id)
    finally:
        get_settings.cache_clear()


def report_args(
    args: dict[str, Any], artifacts: list[Artifact], kind: str, insights: str
) -> dict[str, Any]:
    """只选指定类型的已保存分析，避免其他分析碰巧提供相同数字。"""
    selected = next(item for item in artifacts if item.type == kind)
    return {
        **args,
        "include_pdf": False,
        "analysis_ids": [selected.params["analysis_id"]],
        "insights": insights,
    }


@pytest.mark.parametrize(
    ("kind", "insights", "path"),
    [
        ("table", "销售额总计 1170.0。", "$.total_value"),
        ("table", "销售额总计 1,170.00。", "$.total_value"),
        (
            "table",
            "分组数 6，2026-06销售额 270.0，2026-06有效记录 5。",
            "$.rows[0].value",
        ),
        ("profile", "重复率 0.8。", "duplicate_ratio"),
        ("profile", "重复率 80%。", "duplicate_ratio"),
        ("profile", "重复率 80.0%。", "duplicate_ratio"),
        ("profile", "整行重复多余记录数24。", "duplicate_rows"),
        ("profile", "整行重复多余记录数24（重复率80%）。", "duplicate_rows"),
    ],
)
def test_selected_persisted_typed_values_support_report(
    persisted_report: Any,
    kind: str,
    insights: str,
    path: str,
) -> None:
    _, context, args, artifacts = persisted_report
    result = _registry(context).execute(
        "generate_report",
        json.dumps(report_args(args, artifacts, kind, insights)),
    )
    assert insights in result["markdown"]
    validation = result["validation"]
    assert validation["status"] == "passed"
    selected = next(item for item in artifacts if item.type == kind)
    assert set(validation["source_hashes"]) == {selected.id}
    refs = [ref for claim in validation["claims"] for ref in claim["value_refs"]]
    assert any(path in ref["path"] and ref["artifact_id"] == selected.id for ref in refs)


@pytest.mark.parametrize(
    ("kind", "insights"),
    [
        ("table", "销售额总计 1171.0。"),
        ("table", "销售额总计 1170%。"),
        # MANUAL-02: preserve the formerly accepted ambiguous phrase as a negative.
        ("profile", "重复行 24。"),
        ("profile", "整行重复多余记录数24（重复率81%）。"),
        ("profile", "重复率 0.81。"),
        ("profile", "重复率 81%。"),
        ("profile", "重复率 0.8%。"),
        ("profile", "销售额总计 1170.0。"),
    ],
)
def test_unsupported_unselected_and_wrong_unit_values_fail_before_output(
    persisted_report: Any,
    kind: str,
    insights: str,
) -> None:
    _, context, args, artifacts = persisted_report
    with pytest.raises(AgentToolError, match="数值缺少"):
        _registry(context).execute(
            "generate_report",
            json.dumps(report_args(args, artifacts, kind, insights)),
        )
    assert not list(Path(get_settings().report_dir).glob("*"))


@pytest.mark.parametrize("foreign_project", [False, True])
def test_other_dataset_or_conversation_cannot_supply_unselected_number(
    persisted_report: Any,
    foreign_project: bool,
) -> None:
    store, context, args, artifacts = persisted_report
    project_id = store.create_project("另一项目").id if foreign_project else context.project_id
    conversation_id = (
        store.create_conversation(project_id, title="外部").id
        if foreign_project
        else context.conversation_id
    )
    message = store.append_message(
        conversation_id=conversation_id, role="assistant", content="外部"
    )
    store.register_dataset(ref="f" * 32, project_id=project_id, filename="other.xlsx", profile={})
    store.create_artifact(
        conversation_id=conversation_id,
        message_id=message.id,
        type="table",
        payload={"total_value": 987654.25, "rows": [], "group_total": 0},
        source_tool="aggregate_preview",
        params={"analysis_id": "foreign"},
        dataset_ref="f" * 32,
    )
    with pytest.raises(AgentToolError, match="数值缺少"):
        _registry(context).execute(
            "generate_report",
            json.dumps(
                report_args(args, artifacts, "table", "外部总计 987654.25。"),
            ),
        )
    if foreign_project:
        with pytest.raises(AgentToolError, match="分析不存在"):
            _registry(context).execute(
                "generate_report",
                json.dumps(
                    {**args, "analysis_ids": ["foreign"], "insights": "外部总计 987654.25。"},
                ),
            )


def test_missing_analysis_reference_is_rejected(persisted_report: Any) -> None:
    _, context, args, _ = persisted_report
    with pytest.raises(AgentToolError, match="分析不存在"):
        _registry(context).execute(
            "generate_report",
            json.dumps({**args, "analysis_ids": ["wrong-reference"]}),
        )


@pytest.mark.parametrize("kind", ["profile", "table"])
def test_metadata_recommendations_and_numeric_strings_are_not_evidence(
    persisted_report: Any,
    kind: str,
) -> None:
    _, _, _, artifacts = persisted_report
    artifact = copy.deepcopy(next(item for item in artifacts if item.type == kind))
    payload = artifact.payload
    if kind == "profile":
        payload["quality"]["recommendations"] = [{"message": "987654.25", "value": 987654.25}]
        payload["quality"]["issues"][0]["recommendation"] = 987654.25
        payload["quality"]["issues"][0]["evidence"]["untrusted_text"] = '{"value":987654.25}'
    else:
        payload["analysis_id"] = 987654.25
        payload["rows"][0]["group"] = "987654.25"
        payload["rows"][0]["caption"] = 987654.25
        payload["total_value"] = "987654.25"
    with pytest.raises(ValueError, match="数值缺少"):
        validate_report_narratives([artifact], {"title": "报告", "insights": "数值 987654.25。"})


def test_table_total_survives_bounded_index_and_null_rows(persisted_report: Any) -> None:
    _, _, _, artifacts = persisted_report
    artifact = copy.deepcopy(next(item for item in artifacts if item.type == "table"))
    artifact.payload["rows"] = [
        {"group": None, "value": None, "count": 0, "row_count": 1, "valid_value_count": 0}
    ] + [{"group": str(index), "value": 2.0, "count": 1} for index in range(300)]
    result = validate_report_narratives(
        [artifact],
        {"title": "报告", "insights": "销售额总计 1170.0。"},
    )
    assert result["claims"][0]["value_refs"][0]["path"] == "$.total_value"


@pytest.mark.parametrize("explicit_basis", [False, True])
def test_r9b_original_rejected_or_explicit_basis_renders_prose_table_and_pdf(
    persisted_report: Any,
    monkeypatch: pytest.MonkeyPatch,
    explicit_basis: bool,
) -> None:
    import weasyprint

    _, context, args, artifacts = persisted_report
    # The original r9b payload and response remain unchanged on disk. Its generic
    # duplicate count is now rejected; an explicit-basis control exercises PDF.
    if not explicit_basis:
        with pytest.raises(AgentToolError, match="数值缺少"):
            _registry(context).execute("generate_report", json.dumps(args))
        assert not list(Path(get_settings().report_dir).glob("*"))
        return
    args = copy.deepcopy(args)
    assert "存在 24 行重复记录" in args["insights"]
    args["insights"] = args["insights"].replace("存在 24 行重复记录", "整行重复多余记录数为24")
    original_html = weasyprint.HTML
    rendered: list[str] = []

    class CapturedHTML:
        def __init__(self, **kwargs: Any) -> None:
            self.html = original_html(**kwargs)

        def write_pdf(self) -> bytes:
            document = self.html.render()
            for page in document.pages:
                rendered.extend(
                    box.text for box in page._page_box.descendants() if hasattr(box, "text")
                )
            return document.write_pdf()

    # Instrument the real renderer, never the narrative validator or report assembly.
    monkeypatch.setattr(weasyprint, "HTML", CapturedHTML)
    result = _registry(context).execute("generate_report", json.dumps(args))
    pdf = Path(result["pdf_path"]).read_bytes()
    assert pdf.startswith(b"%PDF-") and len(pdf) > 10000
    text = " ".join(rendered)
    assert "1170.0" in text and "0.8" in text
    for value in ("120.0", "150.0", "180.0", "210.0", "240.0", "270.0"):
        assert value in text
    assert "8074b9f94bb2" in text
    assert result["analysis_ids"] == args["analysis_ids"]
    assert set(result["validation"]["source_hashes"]) == {item.id for item in artifacts}
    assert (
        result["validation"]["content_sha256"]
        == hashlib.sha256(
            Path(result["md_path"]).read_bytes(),
        ).hexdigest()
    )
