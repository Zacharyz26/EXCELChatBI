"""Independent delivered artifact cardinality, not a business metric count."""

import copy
import dataclasses
import json
import socket
from pathlib import Path

import pytest
from apps.orchestrator.control.verifier import verify_completion
from packages.orchestration.contracts import build_minimal_contract
from packages.session.models import Artifact
from packages.session.task_models import EvidenceRecord, ToolInvocation


def original_inputs():
    data = json.loads((Path(__file__).parent / "fixtures/r1_chart_delivery.json").read_text())
    evidence = []
    for raw in data["evidence"]:
        item = dict(raw)
        item["source"] = json.loads(item.pop("source_json"))
        item["summary"] = json.loads(item.pop("summary_json"))
        evidence.append(EvidenceRecord(**item))
    invocations = []
    for raw in data["tool_invocations"]:
        item = dict(raw)
        item["args"] = json.loads(item.pop("args_json"))
        invocations.append(ToolInvocation(**item))
    artifacts = []
    for raw in data["artifacts"]:
        item = {k: v for k, v in raw.items() if k in {x.name for x in dataclasses.fields(Artifact)}}
        item["payload"] = json.loads(raw["payload_json"])
        item["params"] = json.loads(raw["params_json"])
        artifacts.append(Artifact(**item))
    return evidence, invocations, artifacts


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a, **kw):
        raise AssertionError("Offline delivered cardinality")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


@pytest.mark.parametrize(
    "name,text,mutation,expected",
    [
        ("original_three_dimensions", "三个维度图表均已生成。", None, True),
        ("three_actual_artifacts", "已生成三张图表。", None, True),
        ("four_dimensions_wrong", "四个维度图表均已生成。", None, False),
        ("two_artifacts_wrong", "已生成两张图表。", None, False),
        ("only_one_does_not_prove_three", "三个维度图表均已生成。", "one", False),
        (
            "duplicate_evidence_does_not_increment",
            "四个维度图表均已生成。",
            "duplicate_evidence",
            False,
        ),
        (
            "duplicate_dimension_is_not_third_dimension",
            "三个维度图表均已生成。",
            "duplicate_dimension",
            False,
        ),
        ("three_artifacts_two_dimensions", "已生成三张图表。", "duplicate_dimension", True),
        ("missing_artifact", "三个维度图表均已生成。", "missing_artifact", False),
        ("invalid_artifact_payload", "三个维度图表均已生成。", "invalid_artifact", False),
        ("different_run", "三个维度图表均已生成。", "wrong_run", False),
        ("failed_invocation", "三个维度图表均已生成。", "failed_invocation", False),
        ("hash_disagreement", "三个维度图表均已生成。", "wrong_hash", False),
        (
            "artifact_encoding_disagreement",
            "三个维度图表均已生成。",
            "wrong_artifact_params",
            False,
        ),
        (
            "dataset_identity_disagreement",
            "三个维度图表均已生成。",
            "wrong_artifact_dataset",
            False,
        ),
        ("chart_count_is_not_data_column_count", "数据集共有三个维度列。", None, False),
    ],
    ids=lambda x: x if isinstance(x, str) and x.isascii() else None,
)
def test_current_run_delivery_counts(name, text, mutation, expected):
    evidence, invocations, artifacts = original_inputs()
    if mutation == "one":
        evidence, invocations, artifacts = evidence[:1], invocations[:1], artifacts[:1]
    elif mutation == "duplicate_evidence":
        evidence.append(dataclasses.replace(evidence[0], evidence_id="duplicate"))
    elif mutation == "duplicate_dimension":
        args = copy.deepcopy(invocations[2].args)
        args["encoding"]["x"] = invocations[0].args["encoding"]["x"]
        invocations[2] = dataclasses.replace(invocations[2], args=args)
        summary = copy.deepcopy(evidence[2].summary)
        summary["numeric_context"]["encoding"] = copy.deepcopy(args["encoding"])
        evidence[2] = dataclasses.replace(evidence[2], summary=summary)
        artifacts[2] = dataclasses.replace(artifacts[2], params=copy.deepcopy(args))
    elif mutation == "missing_artifact":
        artifacts.pop()
    elif mutation == "invalid_artifact":
        artifacts[2] = dataclasses.replace(artifacts[2], payload={"option": {}})
    elif mutation == "wrong_run":
        evidence[2] = dataclasses.replace(evidence[2], run_id="other-run")
    elif mutation == "failed_invocation":
        invocations[2] = dataclasses.replace(invocations[2], status="failed", error_text="failed")
    elif mutation == "wrong_hash":
        evidence[2] = dataclasses.replace(evidence[2], result_hash="wrong")
    elif mutation == "wrong_artifact_params":
        args = copy.deepcopy(artifacts[2].params)
        args["encoding"]["x"] = "产品"
        artifacts[2] = dataclasses.replace(artifacts[2], params=args)
    elif mutation == "wrong_artifact_dataset":
        artifacts[2] = dataclasses.replace(artifacts[2], dataset_ref="other-dataset")
    contract = build_minimal_contract(
        run_id=invocations[0].run_id,
        user_text="核对已生成图表",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    verdict = verify_completion(
        contract=contract,
        final_text=text,
        evidence=evidence,
        invocations=invocations,
        artifacts=artifacts,
        claims=None,
    )
    assert verdict.passed is expected, (name, dataclasses.asdict(verdict))
