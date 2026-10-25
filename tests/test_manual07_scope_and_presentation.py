"""MANUAL-07: independent literal facts, population lineage and presentation boundaries."""

from __future__ import annotations

import copy
from dataclasses import replace

import pandas as pd
import pytest
from apps.orchestrator.control.claims import build_evidence_summary, extract_numeric_claims
from apps.orchestrator.control.numeric_display import NUMBER_PATTERN, numeric_quote_view
from apps.orchestrator.control.profile_population import profile_population_arguments
from apps.orchestrator.control.report_claims import _visible_text, validate_report_narratives
from mcp_servers.dataset_ops.tools import transform_dataset
from mcp_servers.excel_parser.advisor import diagnose_data_quality, infer_data_roles
from mcp_servers.excel_parser.tools import infer_schema
from packages.common.dataset_store import save_dataframe
from packages.session.models import Artifact
from packages.session.store import SessionStore
from packages.session.task_models import EvidenceRecord

from tests.test_profile_subject_boundaries import check

FIELD = "耗时（秒）"


@pytest.fixture()
def populations(tmp_path):
    store = SessionStore(str(tmp_path / "scope.sqlite"))
    project = store.create_project("范围验收")
    frame = pd.DataFrame(
        {
            "设备": ["A"] * 4 + ["B"] * 4,
            "状态": ["运行", "运行", "运行", "停机"] * 2,
            FIELD: [10.0, 20.0, None, 30.0, 100.0, 200.0, None, 300.0],
        }
    )
    refs = {"all": save_dataframe(frame)}
    records, artifacts = {}, {}
    specs = [
        ("all", None, None),
        ("A", "all", {"filters": [{"column": "设备", "op": "==", "value": "A"}]}),
        ("B", "all", {"filters": [{"column": "设备", "op": "==", "value": "B"}]}),
        ("run", "A", {"filters": [{"column": "状态", "op": "==", "value": "运行"}]}),
        ("clean", "A", {"drop_nulls": [FIELD]}),
        ("numeric", "all", {"filters": [{"column": FIELD, "op": ">", "value": 20}]}),
        ("sort", "A", {"sort": [{"column": FIELD, "ascending": True}]}),
    ]
    # These are manually enumerated fixture facts, not computed by the binder.
    oracle = {
        "all": (8, 300, 2),
        "A": (4, 30, 1),
        "B": (4, 300, 1),
        "run": (3, 20, 1),
        "clean": (3, 30, 0),
        "numeric": (4, 300, 0),
        "sort": (4, 30, 1),
    }
    for name, parent, operations in specs:
        if parent:
            result = transform_dataset({"dataset_ref": refs[parent], **operations})
            refs[name] = result["dataset_ref"]
        ref = refs[name]
        p = infer_schema({"dataset_ref": ref})
        roles = infer_data_roles(p)
        payload = {
            "profile": p.to_dict(),
            "roles": roles,
            "quality": diagnose_data_quality(p, roles, duplicate_rows=0),
        }
        c = next(c for c in payload["profile"]["columns"] if c["name"] == FIELD)
        assert (p.row_count, c["max"], c["null_count"]) == oracle[name]
        store.register_dataset(
            ref=ref,
            project_id=project.id,
            filename=name + ".csv",
            profile=p.to_dict(),
            parent_ref=refs[parent] if parent else None,
            transform=operations,
        )
        # Reopening the store proves metadata survives persistence.
        reopened = SessionStore(store.db_path)
        args = profile_population_arguments(reopened, {"dataset_ref": ref}, "version-" + name)
        records[name] = EvidenceRecord(
            name,
            "run",
            "inv-" + name,
            name,
            "tool_result",
            {"tool": "get_data_profile"},
            "hash-" + name,
            build_evidence_summary(
                summary="画像", result=payload, artifact_id=name, arguments=args
            ),
            "now",
        )
        artifacts[name] = Artifact(
            name, "c", "m", "profile", payload, None, "get_data_profile", args, ref, "now"
        )
    return store, records, artifacts


SCOPE_CASES = [
    ("筛选设备=A后，耗时（秒）最大值30。", "A", True),
    ("筛选设备=A后，耗时（秒）最大值300。", "all", False),
    ("筛选设备=A后，耗时（秒）最大值300。", "B", False),
    ("筛选设备=A后，耗时（秒）空值数1。", "B", False),
    ("筛选设备=A，耗时（秒）空值数1。", "A", True),
    ("筛选设备=A，且状态=运行后，耗时（秒）最大值20。", "run", True),
    ("筛选设备=A；状态=运行后，耗时（秒）空值数1。", "A", False),
    ("筛选设备=A；状态=运行后，耗时（秒）最大值20。", "run", True),
    ("筛选设备=A\n状态=运行后，耗时（秒）最大值20。", "run", True),
    ("筛选设备=A，未知字段=运行后，耗时（秒）空值数1。", "A", False),
    ("筛选设备=A或设备=B后，耗时（秒）最大值300。", "all", False),
    ("筛选耗时（秒）>20后，耗时（秒）最大值300。", "numeric", True),
    ("筛选耗时（秒）>20后，耗时（秒）最大值300。", "all", False),
    ("筛选设备=A后，耗时（秒）空值数0。", "clean", True),
    ("筛选设备=A后，耗时（秒）最大值30。", "sort", True),
    ("全表耗时（秒）最大值30。", "A", False),
    ("耗时（秒）最大值300，仅供参考。", "all", True),
    ("仅设备A，耗时（秒）缺失比例0.25（25%）。", "all", False),
]


@pytest.mark.parametrize("text,source,expected", SCOPE_CASES)
def test_scope_chat_report_and_original_tokens(populations, text, source, expected):
    _, records, artifacts = populations
    result = check(text, [records[source]])
    assert result["refs"], result  # A green validator with vanished quantities is a failure.
    assert result["verification"]["passed"] is expected, result
    if expected:
        assert all(r["evidence_id"] == source for r in result["refs"])
        assert validate_report_narratives([artifacts[source]], {"title": "画像", "insights": text})
    else:
        with pytest.raises(ValueError):
            validate_report_narratives([artifacts[source]], {"title": "画像", "insights": text})


@pytest.mark.parametrize(
    "source,text",
    [("all", "全表耗时（秒）最大值300。"), ("B", "筛选设备=B后，耗时（秒）最大值300。")],
)
def test_answer_cannot_replace_requested_population(populations, source, text):
    _, records, artifacts = populations
    claims = extract_numeric_claims(
        final_text=text, goal="筛选设备=A后画像", evidence=[records[source]]
    )
    assert claims and all(not r.get("supported") for c in claims for r in c.value_refs)
    with pytest.raises(ValueError):
        validate_report_narratives(
            [artifacts[source]], {"title": "筛选设备=A后画像", "insights": text}
        )


def test_filter_switch_does_not_reuse_old_source(populations):
    _, records, _ = populations
    for source, value in [("A", "A"), ("B", "B"), ("A", "A")]:
        claims = extract_numeric_claims(
            final_text=f"筛选设备={value}后，耗时（秒）空值数1。",
            goal="画像",
            evidence=[records["A"], records["B"]],
        )
        refs = [r for c in claims for r in c.value_refs]
        assert len(refs) == 1 and refs[0]["supported"] and refs[0]["evidence_id"] == source
    mixed = check(
        "筛选设备=A后，耗时（秒）最大值30。改为筛选设备=B后，耗时（秒）最大值300。", [records["A"]]
    )
    assert [r["supported"] for r in mixed["refs"]] == [True, False]


@pytest.mark.parametrize("mutation", ["version", "dataset", "root", "edge", "filters"])
def test_malformed_population_proof_fails_closed(populations, mutation):
    _, records, _ = populations
    record = copy.deepcopy(records["A"])
    proof = record.summary["numeric_context"]["profile_population"]
    if mutation == "version":
        proof["dataset_version"] = "other"
    if mutation == "dataset":
        proof["dataset_ref"] = "other"
    if mutation == "root":
        proof["root_ref"] = []
    if mutation == "edge":
        proof["steps"][0]["parent_ref"] = "other"
    if mutation == "filters":
        proof["steps"][0]["transform"]["filters"] = None
    result = check("筛选设备=A后，耗时（秒）最大值30。", [record])
    assert result["refs"] and not result["verification"]["passed"]


def test_same_numbers_do_not_resolve_conflicting_versions(populations):
    _, records, _ = populations
    other = copy.deepcopy(records["A"])
    other = replace(other, evidence_id="other")
    context = other.summary["numeric_context"]
    context["data_version_hash"] = "new-version"
    context["profile_population"]["dataset_version"] = "new-version"
    assert not check("筛选设备=A后，耗时（秒）最大值30。", [records["A"], other])["verification"][
        "passed"
    ]


def test_host_ignores_caller_population(populations):
    store, _, artifacts = populations
    args = profile_population_arguments(
        store,
        {"dataset_ref": artifacts["A"].dataset_ref, "profile_population": {"forged": True}},
        "version-A",
    )
    assert args == artifacts["A"].params


@pytest.mark.parametrize(
    "opening,closing", [('"', '"'), ("'", "'"), ("`", "`"), ("``", "``"), ("“", "”"), ("「", "」")]
)
@pytest.mark.parametrize(
    "label,value,expected",
    [
        ("最大值为", "300", True),
        ("缺失比例", "25%", True),
        ("缺失比例", "26%", False),
        ("缺失比例", "0.25", True),
    ],
)
def test_numeric_wrappers_keep_tokens(populations, opening, closing, label, value, expected):
    _, records, artifacts = populations
    text = FIELD + label + opening + value + closing + "。"
    result = check(text, [records["all"]])
    assert [r["token"] for r in result["refs"]] == [value]
    assert result["verification"]["passed"] is expected
    if expected:
        validate_report_narratives([artifacts["all"]], {"title": "画像", "insights": text})
    else:
        with pytest.raises(ValueError):
            validate_report_narratives([artifacts["all"]], {"title": "画像", "insights": text})


@pytest.mark.parametrize(
    "text",
    [
        '备注中的"最大值""300"均为标签。',
        "备注中的`重复组数：``三成`均为标签。",
        "备注中的`重复组数：` `三成`均为标签。",
    ],
)
def test_report_preserves_actual_inline_source(populations, text):
    _, records, artifacts = populations
    assert _visible_text(text).strip() == text
    assert check(text, [records["all"]])["refs"] == []
    validate_report_narratives([artifacts["all"]], {"title": "画像", "insights": text})


@pytest.mark.parametrize(
    "text",
    [
        '耗时（秒）最大值为"301"。',
        "耗时（秒）最大值为\"300'。",
        '耗时（秒）缺失比例"0.25"（即"26%"）。',
        "`重复组数：三成`。",
        '备注中的"最大值""300"均为标签；重复组数为五。',
    ],
)
def test_bad_values_and_true_code_claims_remain(populations, text):
    _, records, artifacts = populations
    result = check(text, [records["all"]])
    assert result["refs"] and not result["verification"]["passed"]
    with pytest.raises(ValueError):
        validate_report_narratives([artifacts["all"]], {"title": "画像", "insights": text})


def test_quote_projection_is_length_and_number_preserving():
    text = '耗时（秒）缺失比例"0.25"（即"25%"）。'
    view, labels = numeric_quote_view(text, {"缺失比例"})
    assert len(view) == len(text) and labels == []
    assert [(m[0], m.span()) for m in NUMBER_PATTERN.finditer(text)] == [
        (m[0], m.span()) for m in NUMBER_PATTERN.finditer(view)
    ]


@pytest.mark.parametrize(
    "text",
    [
        '耗时（秒）最大值为“"300"”。',
        '耗时（秒）缺失比例“"25%"”。',
        "耗时（秒）的`最大值`为`300`。",
        '耗时（秒）缺失比例为"\n25%\n"。',
    ],
)
def test_nested_and_soft_wrappers(populations, text):
    _, records, artifacts = populations
    result = check(text, [records["all"]])
    assert len(result["refs"]) == 1 and result["refs"][0]["supported"], result
    validate_report_narratives([artifacts["all"]], {"title": "画像", "insights": text})


@pytest.mark.parametrize("wrong_root", [False, True])
@pytest.mark.parametrize("surface", ["chat", "report"])
def test_completion_binds_profile_and_report_sources_to_task(populations, wrong_root, surface):
    from types import SimpleNamespace

    from apps.orchestrator.agent_loop import _analysis_scope_error
    from apps.orchestrator.control.contracts import build_minimal_contract
    from apps.orchestrator.control.verifier import verify_completion
    from packages.session.task_models import TaskDatasetBinding, ToolInvocation

    _, records, artifacts = populations
    root = artifacts["all"].dataset_ref
    target = artifacts["A"].dataset_ref
    selected = artifacts["A"]
    evidence = records["A"]
    if wrong_root:
        # Independent Evidence identity has identical literal statistics and
        # identical filter values. Neither can establish the task's root.
        other_ref = "e" * 32
        selected = replace(
            selected,
            id="other",
            dataset_ref=other_ref,
            params={**selected.params, "dataset_ref": other_ref},
        )
        evidence = copy.deepcopy(evidence)
        evidence = replace(evidence, evidence_id="other")
        ctx = evidence.summary["numeric_context"]
        ctx["dataset_ref"] = other_ref
        proof = ctx["profile_population"]
        proof["root_ref"] = "f" * 32
        proof["dataset_ref"] = other_ref
        proof["steps"][0].update(parent_ref="f" * 32, dataset_ref=other_ref)
    tx_args = {"dataset_ref": root, "filters": [{"column": "设备", "op": "==", "value": "A"}]}
    tx = ToolInvocation(
        "tx",
        "run",
        None,
        "txcall",
        "transform_dataset",
        "id",
        "args",
        tx_args,
        "succeeded",
        "hash",
        None,
        None,
        "now",
        "now",
    )
    tx_ev = EvidenceRecord(
        "txe", "run", "tx", None, "tool_result", {"tool": "transform_dataset"}, "hash", {}, "now"
    )
    binding = TaskDatasetBinding("run", target, "derived", root, "tx", "now", "now")
    profile_call = ToolInvocation(
        evidence.invocation_id,
        "run",
        None,
        "profile",
        "get_data_profile",
        "id",
        "args",
        {"dataset_ref": selected.dataset_ref},
        "succeeded",
        evidence.result_hash,
        None,
        None,
        "now",
        "now",
    )
    goal = "筛选：设备=A；输出画像。"
    contract = build_minimal_contract(
        run_id="run",
        user_text=goal,
        chart_required=False,
        report_required=False,
        pdf_required=False,
        columns=("设备", "状态", FIELD),
        dataset_refs=(root,),
    )
    invocations = [tx, profile_call]
    evs = [tx_ev, evidence]
    delivered = []
    text = "筛选设备=A后，耗时（秒）最大值30。"
    if surface == "report":
        args = {"analysis_ids": [selected.id], "title": "画像"}
        task_store = SimpleNamespace(
            list_invocations=lambda run: invocations,
            list_evidence=lambda run: evs,
            list_dataset_bindings=lambda run: [binding],
        )
        error = _analysis_scope_error(
            task_store, "run", contract, "generate_report", args, [selected]
        )
        assert (error is not None) is wrong_root
        report = Artifact(
            "report",
            "c",
            "m",
            "report",
            {},
            "/tmp/unit-report.md",
            "generate_report",
            args,
            None,
            "now",
        )
        invocations.append(
            ToolInvocation(
                "reportcall",
                "run",
                None,
                "rc",
                "generate_report",
                "id",
                "args",
                args,
                "succeeded",
                "rh",
                None,
                report.id,
                "now",
                "now",
            )
        )
        delivered = [report]
        evs.append(
            EvidenceRecord(
                "reporte",
                "run",
                "reportcall",
                report.id,
                "tool_result",
                {"tool": "generate_report"},
                "rh",
                {},
                "now",
            )
        )
        text = "报告已生成。"
    claims = extract_numeric_claims(final_text=text, goal=goal, evidence=evs)
    result = verify_completion(
        contract=contract,
        final_text=text,
        artifacts=delivered,
        invocations=invocations,
        evidence=evs,
        claims=claims,
        dataset_bindings=[binding],
        source_artifacts=[selected],
    )
    assert result.passed is not wrong_root, result
    if wrong_root:
        assert any(issue.code == "required_dataset_not_satisfied" for issue in result.issues)
