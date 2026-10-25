"""MANUAL-02: actual upload, Host persistence and report tool; offline model fixture only."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from apps.orchestrator.agent_tools import AgentContext, AgentToolError
from packages.session.store import SessionStore
from packages.session.task_store import TaskStore

from tests import test_profile_subject_boundaries as host
from tests.test_agent_tools import _registry
from tests.test_manual02_profile_contract import CAPTURE, EXPLICIT_ANSWER


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "label,text,expected",
    [
        ("original-unmodified", CAPTURE["answer"], False),
        ("explicit-basis", EXPLICIT_ANSWER, True),
        ("wrong-non-null", EXPLICIT_ANSWER.replace("非空 74 条", "非空 75 条"), False),
        (
            "wrong-column-basis",
            EXPLICIT_ANSWER.replace("记录ID重复多余记录数为1", "记录ID参与重复记录数为1"),
            False,
        ),
        (
            "wrong-row-basis",
            EXPLICIT_ANSWER.replace("整行重复多余记录数为1", "整行重复组数为1"),
            False,
        ),
        ("cross-paragraph", EXPLICIT_ANSWER.replace("条；最小值", "条；\n\n最小值"), False),
        ("wrong-unit", EXPLICIT_ANSWER.replace("最大值 999", "最大值 999小时"), False),
    ],
)
async def test_actual_host_and_report_tool(tmp_path, monkeypatch, label, text, expected):
    monkeypatch.setattr(host, "EXPLICIT_ANSWER", text)
    monkeypatch.setattr(host, "CAPTURE", CAPTURE)
    await host.test_actual_host(tmp_path, label, None, None, expected)
    folder = tmp_path / "witnesses" / label
    witness = json.loads((folder / "witness.json").read_text())
    # Reopen from the physical database, not the live Host's object cache.
    store = SessionStore(str(tmp_path / "host.db"))
    task = TaskStore(store.db_path)
    run = task.get_run(witness["run"]["run_id"])
    assert run is not None and (run.status == "completed") is expected
    claims = task.list_claims(run.run_id)
    evidence = task.list_evidence(run.run_id)
    assert claims and evidence and all(e.source["tool"] == "get_data_profile" for e in evidence)
    refs = [ref for c in claims for ref in c.value_refs]
    if expected:
        assert all(ref.get("supported") for ref in refs)
        assert any(
            ref.get("semantic_binding", {}).get("statistic") == "non_null_count" for ref in refs
        )
    profile = next(
        a for a in store.list_artifacts(run.conversation_id) if a.source_tool == "get_data_profile"
    )
    context = AgentContext(store, run.project_id, run.conversation_id)
    registry = _registry(context)
    args = {
        "title": "节目画像报告",
        "analysis_ids": [profile.params["analysis_id"]],
        "insights": text,
        "include_pdf": False,
    }
    try:
        if not expected:
            with pytest.raises(AgentToolError, match="数值缺少所选分析的证据"):
                registry.execute("generate_report", json.dumps(args, ensure_ascii=False))
        else:
            result = registry.execute("generate_report", json.dumps(args, ensure_ascii=False))
            md = Path(result["md_path"]).read_bytes()
            assert text in md.decode()
            assert result["validation"]["status"] == "passed"
            assert result["validation"]["content_sha256"] == hashlib.sha256(md).hexdigest()
            (folder / "actual-report.md").write_bytes(md)
            (folder / "actual-report-result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2)
            )
    finally:
        await registry.aclose()
