"""Freeze the exact UUID from full2; typed identity cells are not quantities.

Real local dataframe persistence and infer_schema; component Verifier/report
binding, not Host or SQLite. UUID allocation alone is deterministic in fixture.
The original random profile identity test remains unchanged.
"""

import hashlib
import json
import socket
from dataclasses import asdict
from types import SimpleNamespace

import pandas as pd
import pytest
from apps.orchestrator.control.claims import build_evidence_summary
from apps.orchestrator.control.quantity_presentation import validate_candidate
from apps.orchestrator.control.report_claims import validate_report_narratives
from apps.orchestrator.control.verifier import verify_completion
from mcp_servers.excel_parser.tools import infer_schema
from packages.common import dataset_store
from packages.common.config import get_settings
from packages.orchestration.contracts import build_minimal_contract
from packages.session.models import Artifact
from packages.session.task_models import EvidenceRecord

DATASET = "390732ba8c1f4e94a78be49188b6f484"
VERSION = "a" * 64
FIELD = "耗时（秒）"


@pytest.fixture
def fixed_source(tmp_path, monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("Identity holdout forbids network")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setenv("DATASET_DIR", str(tmp_path / "datasets"))
    get_settings.cache_clear()
    frame = pd.DataFrame({FIELD: [2.0, 4.0, 8.0, None], "等待（秒）": [8.0, 8.0, None, None]})
    # Fix allocation, not the profile output, evidence identity or validator.
    with monkeypatch.context() as allocation:
        allocation.setattr(
            dataset_store, "uuid", SimpleNamespace(uuid4=lambda: SimpleNamespace(hex=DATASET))
        )
        dataset = dataset_store.save_dataframe(frame)
    assert dataset == DATASET
    payload = {"profile": infer_schema({"dataset_ref": dataset}).to_dict()}
    args = {"dataset_ref": dataset, "data_version_hash": VERSION}
    summary = build_evidence_summary(
        summary="independent fixed identity profile",
        result=payload,
        artifact_id=None,
        arguments=args,
    )
    record = EvidenceRecord(
        "fixed-profile",
        "fixed-run",
        "fixed-call",
        None,
        "tool_result",
        {"tool": "get_data_profile"},
        hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest(),
        summary,
        "2026-11-01T00:00:00Z",
    )
    artifact = Artifact(
        "fixed-profile-artifact",
        "fixed-conversation",
        "fixed-message",
        "profile",
        payload,
        None,
        "get_data_profile",
        args,
        dataset,
        "2026-11-01T00:00:00Z",
    )
    try:
        yield record, artifact
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(
    "case,expected",
    [
        ("exact-original-uuid", True),
        ("wrong-dataset", False),
        ("wrong-version", False),
        ("uuid-in-value-slot", False),
    ],
)
def test_identity_cell_must_bind_its_header_before_numeric_audit(
    fixed_source, tmp_path, case, expected
):
    record, artifact = fixed_source
    dataset = DATASET[:-1] + "5" if case == "wrong-dataset" else DATASET
    version = DATASET * 2 if case == "wrong-version" else VERSION
    value = DATASET if case == "uuid-in-value-slot" else "8"
    text = f"|字段|数据集|版本|最大值|\n|---|---|---|---|\n|{FIELD}|{dataset}|{version}|{value}|"
    candidate = validate_candidate(text, [record], goal="复核表格事实")
    contract = build_minimal_contract(
        run_id="fixed-run",
        user_text="复核表格事实",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    verdict = verify_completion(
        contract=contract,
        final_text=text,
        artifacts=[],
        invocations=[],
        evidence=[record],
        claims=None,
    )
    try:
        report = validate_report_narratives([artifact], {"title": "身份表格复核", "insights": text})
    except ValueError as error:
        report = {"status": "rejected", "error": str(error)}
    witness = {
        "input": text,
        "expected": expected,
        "source": asdict(record),
        "artifact": asdict(artifact),
        "candidate": asdict(candidate),
        "verdict": asdict(verdict),
        "report": report,
        "layer": (
            "real infer_schema and parquet, typed component Verifier/report; no Host/SQLite/model"
        ),
    }
    (tmp_path / "witness.json").write_text(json.dumps(witness, ensure_ascii=False, indent=2))
    assert candidate.passed is expected, witness
    assert verdict.passed is expected, witness
    assert (report.get("status") == "passed") is expected, witness
    if expected:
        refs = [ref for claim in candidate.claims for ref in claim.value_refs]
        assert len(refs) == 1 and refs[0]["token"] == "8" and refs[0]["supported"] is True
