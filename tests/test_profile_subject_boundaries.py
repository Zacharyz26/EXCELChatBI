"""MANUAL-01 independent findings plus adjacent subject/unit/format regressions."""

import copy
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

import pandas as pd
import pytest
from apps.api.deps import session_store_dep
from apps.api.main import create_app
from apps.orchestrator.agent_loop import AgentLoopConfig, ConversationLockPool, stream_agent_chat
from apps.orchestrator.agent_tools import AgentContext
from apps.orchestrator.control.claims import build_evidence_summary, extract_numeric_claims
from apps.orchestrator.control.contracts import build_minimal_contract
from apps.orchestrator.control.report_claims import validate_report_narratives
from apps.orchestrator.control.verifier import verify_completion
from fastapi.testclient import TestClient
from mcp_servers.excel_parser.advisor import (
    diagnose_data_quality,
    infer_data_roles,
    infer_data_roles_from_mapping,
)
from mcp_servers.excel_parser.tools import infer_schema
from packages.common.dataset_store import save_dataframe
from packages.models.types import ModelBudgetExceeded, ModelResponse, ToolCall
from packages.session.models import Artifact
from packages.session.store import SessionStore
from packages.session.task_models import EvidenceRecord
from packages.session.task_store import TaskStore

from tests.test_agent_tools import _registry
from tests.test_profile_claim_binding import EXPLICIT_ANSWER

R = Path(__file__).resolve().parents[1]
CAPTURE = json.loads((R / "tests/fixtures/manual01_profile.json").read_text())


@pytest.fixture(scope="module")
def actual_profile():
    ref = save_dataframe(
        pd.DataFrame(
            {
                "收入（元）": [1.0, 2.0, 3.0, None],
                "利润（元）": [10.0, 20.0, 30.0, 40.0],
                "客户ID": ["a", "b", "c", "c"],
            }
        )
    )
    p = infer_schema({"dataset_ref": ref})
    roles = infer_data_roles(p)
    return {
        "profile": p.to_dict(),
        "roles": roles,
        "quality": diagnose_data_quality(p, roles, duplicate_rows=0),
    }


def record(result, ident="one", ref="d" * 32, version="a" * 64):
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
            result=result,
            artifact_id=None,
            arguments={"dataset_ref": ref, "data_version_hash": version},
        ),
        "now",
    )


def check(text, records):
    claims = extract_numeric_claims(final_text=text, goal="复核画像", evidence=records)
    refs = [r for c in claims for r in c.value_refs]
    contract = build_minimal_contract(
        run_id="run",
        user_text="复核画像",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    v = verify_completion(
        contract=contract,
        final_text=text,
        artifacts=[],
        invocations=[],
        evidence=records,
        claims=claims,
    )
    return {
        "text": text,
        "verification": {**asdict(v), "passed": v.passed},
        "refs": refs,
        "claims": [asdict(c) for c in claims],
    }


CASES = [
    ("short", "收入（元）最小1、最大3、均值2、中位数2。", True),
    ("long", "收入（元）最小值1，最大值3，平均值2，中位数2。", True),
    ("percent", "收入（元）空值率25%。", True),
    ("percent_space", "收入（元）空值率25 %。", True),
    ("distinct", "客户ID distinct_count为3。", True),
    ("null_exact", "收入（元）有1个缺失值。", True),
    ("paren", "收入（元）（最小1、最大3）。", True),
    ("colon", "收入（元）最小：1，最大：3。", True),
    ("markdown", "收入（元）的**最小值**为1，**最大值**为3。", True),
    ("shape", "数据共4行、3列。", True),
    ("units", "收入（元）最小1元、最大3元。", True),
    ("bad_max", "收入（元）最大1。", False),
    ("bad_min", "收入（元）最小3。", False),
    ("bad_mean", "收入（元）均值3。", False),
    ("bad_unit", "收入（元）最大3小时。", False),
    ("percent_as_raw", "收入（元）空值率25。", False),
    ("raw_as_percent", "收入（元）空值率0.25%。", False),
    ("percent_money", "收入（元）空值率0.25元。", False),
    ("other_field_same_value", "利润（元）最大3。", False),
    ("unknown_field", "支出最大3。", False),
    ("prefix_field", "预算收入（元）最大3。", False),
    ("change_subject", "收入（元）最小1，以下改看支出，最大3。", False),
    ("change_subject_the_column", "收入（元）最小1，以下改看支出，该列最大3。", False),
    # MANUAL-02: same-item continuous unique subject is now permitted.
    # The adjacent newline, period, competing/unknown-subject negatives remain.
    ("semicolon", "收入（元）最小1；最大3。", True),
    ("newline", "收入（元）最小1\n最大3。", False),
    ("period", "收入（元）最小1。最大3。", False),
    ("competing", "收入（元）最小1，利润（元）最大40，均值2。", False),
    ("row_not_column_duplicate", "收入（元）有0个重复值。", False),
    ("wrong_null_count", "收入（元）有3个缺失值。", False),
    ("null_count_percent", "收入（元）空值数为1%。", False),
    ("fractional_count", "收入（元）有1.1个缺失值。", False),
    ("distinct_wrong", "客户ID有4个不同取值。", False),
    ("distinct_money", "客户ID distinct_count为3元。", False),
    ("row_count_money", "总行数4元。", False),
]


CASES += [
    ("switch_notice", "收入（元）最小1，支出情况如下，最大3。", False),
    ("switch_pronoun", "收入（元）最小1，改看其他列，该列最大3。", False),
    ("switch_parenthetical", "收入（元）（最小1，以下改看支出，最大3）。", False),
    ("switch_no_comma", "收入（元）最小1然后改看支出最大3。", False),
    ("switch_after_statistic", "收入（元）最小1然后改看支出，最大3。", False),
    ("switch_after_statistic_colon", "收入（元）最小：1但现在是支出，该列最大：3。", False),
    ("switch_title", "收入（元）：以下改看支出，最大3。", False),
    ("switch_metadata", "收入的单位：列名注明为元，现在改看支出，该列为整数型，最大3。", False),
    ("prefix_space", "预算 收入（元）最大3。", False),
    ("prefix_bold", "预算**收入（元）**最大3。", False),
    ("prefix_qualified", "税前收入（元）最大3。", False),
    ("prefix_id", "新客户ID有3个不同取值。", False),
    ("suffix_field", "收入（元）预算最大3。", False),
    ("prefix_title", "预算 收入（元）：最大3。", False),
    ("ratio_physical", "收入（元）空值率0.25千克。", False),
    ("ratio_count", "收入（元）空值率0.25个。", False),
    ("ratio_parenthesized_unit", "收入（元）空值率0.25（元）。", False),
    ("ratio_percent_parenthesized_unit", "收入（元）空值率25%（元）。", False),
    ("ratio_bracketed_unit", "收入（元）空值率0.25[元]。", False),
    ("ratio_bold_unit", "收入（元）空值率0.25**元**。", False),
    ("ratio_percent_money", "收入（元）空值率25 %元。", False),
    ("ratio_double_percent", "收入（元）空值率25%%。", False),
    ("ratio_raw", "收入（元）空值率0.25。", True),
    ("colon_ascii", "收入（元）最小:1，最大:3。", True),
    ("colon_title", "收入（元）：最小：1、最大：3。", True),
    ("quoted_column", "「收入（元）」最小1、最大3。", True),
    ("explicit_pronoun", "收入（元）最小1、该列最大3、该列的均值2。", True),
    ("metadata_continuity", "收入的单位：列名注明为元，该列为整数型，最小1、最大3。", True),
    ("bold_values", "**收入（元）**的**最小值**为**1**，**最大值**为**3**。", True),
    ("bold_percent", "收入（元）空值率**25** %。", True),
]


@pytest.mark.parametrize("name,text,expected", CASES, ids=[c[0] for c in CASES])
def test_semantics(actual_profile, name, text, expected):
    p = check(text, [record(actual_profile)])
    assert bool(p["refs"]) and p["verification"]["passed"] is expected, p
    artifact = Artifact(
        "profile",
        "c",
        "m",
        "profile",
        actual_profile,
        None,
        "get_data_profile",
        {"dataset_ref": "d" * 32},
        "d" * 32,
        "now",
    )
    if expected:
        assert (
            validate_report_narratives([artifact], {"title": "画像", "insights": text})["status"]
            == "passed"
        )
    else:
        with pytest.raises(ValueError):
            validate_report_narratives([artifact], {"title": "画像", "insights": text})


@pytest.mark.parametrize(
    "mutation",
    [
        "dataset",
        "version",
        "conflict",
        "duplicate",
        "missing_extrema",
        "legacy_null",
        "quality_moved",
        "quality_wrong_column",
    ],
)
def test_evidence_sources(actual_profile, mutation):
    first = record(actual_profile)
    r = copy.deepcopy(actual_profile)
    text = "收入（元）最大3。"
    expected = False
    if mutation == "dataset":
        records = [first, record(r, "other", ref="e" * 32)]
    elif mutation == "version":
        records = [first, record(r, "other", version="b" * 64)]
    elif mutation == "conflict":
        r["profile"]["columns"][0]["max"] = 4.0
        records = [first, record(r, "other")]
    elif mutation == "duplicate":
        records = [first, record(r, "other")]
        expected = True
    elif mutation == "missing_extrema":
        r["profile"]["columns"][0]["min"] = None
        r["profile"]["columns"][0]["max"] = None
        records = [record(r)]
    elif mutation == "legacy_null":
        r["profile"]["columns"][0].pop("null_count")
        text = "收入（元）有1个缺失值。"
        records = [record(r)]
    else:
        # Quality issue location changes must not create positional column identity.
        r["quality"]["issues"] = [
            {
                "code": "non_unique_identifier",
                "columns": ["客户ID" if mutation == "quality_moved" else "利润（元）"],
                "evidence": {
                    "duplicate_values_at_least": 1,
                    "distinct_count": 3,
                    "non_null_count": 4,
                },
            }
        ]
        text = "客户ID重复多余记录数为1。"
        records = [record(r)]
        expected = mutation == "quality_moved"
    p = check(text, records)
    assert p["verification"]["passed"] is expected, p


@pytest.mark.parametrize("count", [-1, True, 1.5, "1", 5])
def test_invalid_exact_count(actual_profile, count):
    p = copy.deepcopy(actual_profile["profile"])
    p["columns"][0]["null_count"] = count
    with pytest.raises(ValueError):
        infer_data_roles_from_mapping(p)


def test_zero_and_all_null():
    for values, n in [([], 0), ([None, None, None], 3)]:
        p = infer_schema(
            {
                "dataset_ref": save_dataframe(
                    pd.DataFrame({"空列": pd.Series(values, dtype="float64")})
                )
            }
        )
        assert p.to_dict()["columns"][0]["null_count"] == n
        restored = infer_data_roles_from_mapping(p.to_dict())
        assert restored["columns"][0]["profile_evidence"]["non_null_count"] == 0


HOST_CASES = [
    ("control", None, None, True),
    ("switch", "最大 999.0", "以下改看广告收入，最大 999.0", False),
    ("prefix", "播出时长（分钟）有 1 个缺失值", "广告播出时长（分钟）有 1 个缺失值", False),
    ("ratio", "空值率约 1.33%", "空值率约 0.0133元", False),
    ("colon", "最小 10.0、最大 999.0", "最小：10.0、最大：999.0", True),
    ("markdown", "最小 10.0、最大 999.0", "**最小** 10.0、**最大** 999.0", True),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("label,before,after,expected", HOST_CASES, ids=[c[0] for c in HOST_CASES])
async def test_actual_host(tmp_path, label, before, after, expected):
    store = SessionStore(str(tmp_path / "host.db"))
    project = store.create_project("Independent MANUAL-01 review")
    conv = store.create_conversation(project.id)
    app = create_app()
    app.dependency_overrides[session_store_dep] = lambda: store
    with TestClient(app) as client:
        upload = client.post(
            "/upload/excel",
            data={"project_id": project.id, "conversation_id": conv.id},
            files={
                "file": (
                    "broadcast.csv",
                    (R / "examples/handoff/broadcast.csv").read_bytes(),
                    "text/csv",
                )
            },
        )
        assert upload.status_code == 200, upload.text
    ref = upload.json()["dataset_ref"]
    answer = EXPLICIT_ANSWER
    if before:
        assert before in answer
        answer = answer.replace(before, after)

    class Gateway:
        turns = 0

        async def stream_turn(self, *a, **kw):
            self.turns += 1
            if self.turns > 2:
                # The fixture admits only two scripted responses. No provider
                # request is made once that admission budget is exhausted.
                raise ModelBudgetExceeded("Offline scripted response budget exhausted")
            if self.turns == 1:
                yield ModelResponse(
                    content="",
                    model="independent-offline",
                    tool_calls=[
                        ToolCall(
                            id="profile",
                            name="get_data_profile",
                            arguments=json.dumps({"dataset_ref": ref}),
                        )
                    ],
                )
            else:
                yield ModelResponse(content=answer, model="independent-offline")

    gw = Gateway()
    registry = _registry(AgentContext(store, project.id, conv.id))
    try:
        events = [
            e
            async for e in stream_agent_chat(
                conversation_id=conv.id,
                project_id=project.id,
                store=store,
                user_text=CAPTURE["goal"],
                gateway=gw,
                registry=registry,
                locks=ConversationLockPool(),
                config=AgentLoopConfig(max_tool_calls=12, max_parallel_tools=1),
            )
        ]
    finally:
        await registry.aclose()
    runid = next(json.loads(e["data"])["run_id"] for e in events if e["event"] == "meta")
    terminal = [json.loads(e["data"]) for e in events if e["event"] in {"done", "error"}][-1]
    task = TaskStore(store.db_path)
    profile = next(a for a in store.list_artifacts(conv.id) if a.source_tool == "get_data_profile")
    try:
        report_validation = validate_report_narratives(
            [profile], {"title": "画像", "insights": answer}
        )
    except ValueError as error:
        report_validation = {"status": "rejected", "error": str(error)}
    folder = tmp_path / "witnesses" / label
    folder.mkdir(parents=True, exist_ok=True)
    payload = {
        "case": label,
        "answer": answer,
        "expected_completed": expected,
        "terminal": terminal,
        "gateway_fixture_turns": gw.turns,
        "run": asdict(task.get_run(runid)),
        "evidence": [asdict(x) for x in task.list_evidence(runid)],
        "claims": [asdict(x) for x in task.list_claims(runid)],
        "invocations": [asdict(x) for x in task.list_invocations(runid)],
        "profile": asdict(profile),
        "events": events,
        "report_validation": report_validation,
        "paid_calls": 0,
        "live_model": False,
    }
    (folder / "witness.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    with sqlite3.connect(f'file:{tmp_path/"host.db"}?mode=ro', uri=True) as src:
        with sqlite3.connect(folder / "host.db") as dst:
            src.backup(dst)
    assert (terminal.get("run_status") == "completed") is expected, terminal

    assert (report_validation["status"] == "passed") is expected, report_validation
