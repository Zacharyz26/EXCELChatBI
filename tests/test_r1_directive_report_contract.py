"""Frozen independent speech-act contrasts; no implementation oracle."""

import dataclasses
import json
import socket
from pathlib import Path

import pytest
from apps.orchestrator.control import report_claims
from apps.orchestrator.control.statistic_statements import count_expressions
from packages.session.models import Artifact

HERE = Path(__file__).parent
CASES = json.loads((HERE / "fixtures/r1_directive_cases.json").read_text())


@pytest.fixture
def selected_artifacts(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("No network in independent report contract review")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    fixture = json.loads((HERE / "fixtures/r1_directive_report.json").read_text())["s5-chart"]
    ids = {x["artifact_id"] for x in fixture["evidence"] if x.get("artifact_id")}
    output = []
    for raw in fixture["artifacts"]:
        if raw["id"] not in ids:
            continue
        item = {k: v for k, v in raw.items() if k in {f.name for f in dataclasses.fields(Artifact)}}
        item["payload"] = json.loads(raw["payload_json"])
        item["params"] = json.loads(raw["params_json"])
        output.append(Artifact(**item))
    return output


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_report_directive_is_not_a_measured_fact_but_assertions_remain(
    case, selected_artifacts, monkeypatch
):
    observed = []
    original = report_claims.extract_numeric_claims

    def capture(**kwargs):
        claims = original(**kwargs)
        observed.extend(r for claim in claims for r in claim.value_refs)
        return claims

    monkeypatch.setattr(report_claims, "extract_numeric_claims", capture)
    try:
        report_claims.validate_report_narratives(
            selected_artifacts, {"title": case["title"], "insights": case["insights"]}
        )
        actual, error = True, None
    except ValueError as exc:
        actual, error = False, str(exc)
    assert actual is case["expected"], (case, error, observed)
    if case["rejected_token"]:
        assert any(
            r["token"] == case["rejected_token"] and r.get("supported") is False for r in observed
        ), ("Unrelated rejection masks missing real claim", case, observed)
    if "画一张" in case["title"] or "画一张" in case["insights"]:
        # Discovery must preserve the original quantity; its speech-act role
        # decides whether it belongs to assertion binding, never silent erasure.
        text = case["title"] + "。" + case["insights"]
        assert any(text[slice(*item.value_span)] == "一" for item in count_expressions(text)), case
