"""Constrained semantic Verifier protocol tests."""

from __future__ import annotations

import json
from typing import Any

import pytest
from apps.orchestrator.control.semantic_verifier import (
    SemanticClaim,
    SemanticCriterion,
    SemanticEvidence,
    SemanticVerificationRequest,
    SemanticVerifier,
    SemanticVerifierProtocolError,
)
from apps.orchestrator.control.verifier import (
    VerificationIssue,
    VerificationResult,
)
from packages.models.types import Message, ModelResponse, Scenario


class _Gateway:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[tuple[Scenario, list[Message], dict[str, object] | None]] = []

    async def complete(
        self,
        scenario: Scenario,
        messages: list[Message],
        *,
        params: dict[str, object] | None = None,
    ) -> ModelResponse:
        self.calls.append((scenario, messages, params))
        return ModelResponse(
            content=json.dumps(self.payload, ensure_ascii=False),
            model="semantic-test-model",
            prompt_tokens=100,
            completion_tokens=30,
            latency_ms=12.5,
        )


def _request() -> SemanticVerificationRequest:
    return SemanticVerificationRequest(
        goal="比较华东和华南的销售趋势，并说明局限",
        criteria=(
            SemanticCriterion("regions", "比较华东和华南"),
            SemanticCriterion("limitations", "说明样本局限"),
        ),
        claims=(
            SemanticClaim(
                "claim-1",
                "华东趋势高于华南。",
                ("evidence-trend",),
                ("样本仅覆盖第一季度",),
            ),
        ),
        evidence=(
            SemanticEvidence(
                "evidence-trend",
                {"summary": "华东与华南第一季度趋势比较"},
            ),
        ),
    )


def _pass_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "criteria": [
            {
                "criterion_id": "regions",
                "status": "pass",
                "reason": "Claim 覆盖两个地区的比较。",
                "evidence_ids": ["evidence-trend"],
            },
            {
                "criterion_id": "limitations",
                "status": "pass",
                "reason": "Claim 披露第一季度样本限制。",
                "evidence_ids": ["evidence-trend"],
            },
        ],
        "overclaims": [],
        "limitations_ok": True,
        "next_action": {"kind": "accept", "reason": "成功标准均已覆盖。"},
    }


@pytest.mark.asyncio
async def test_semantic_verifier_derives_pass_from_constrained_decisions() -> None:
    gateway = _Gateway(_pass_payload())

    result = await SemanticVerifier(gateway).evaluate(
        _request(), hard_result=VerificationResult(verdict="PASS")
    )

    assert result.verification.verdict == "PASS"
    assert result.model_called is True
    assert result.model == "semantic-test-model"
    assert result.cost is None
    assert gateway.calls[0][0] == Scenario.COMPLEX_REASONING
    assert gateway.calls[0][2] == {
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
    }
    model_input = json.loads(gateway.calls[0][1][1].content)
    assert model_input["hard_checks"] == "PASS"
    assert model_input["response_rules"] == {
        "required_top_level_keys": [
            "schema_version",
            "criteria",
            "overclaims",
            "limitations_ok",
            "next_action",
        ],
        "criterion_ids": ["regions", "limitations"],
        "known_evidence_ids": ["evidence-trend"],
        "known_claim_ids": ["claim-1"],
    }


@pytest.mark.asyncio
async def test_hard_failure_skips_model_and_cannot_be_overridden() -> None:
    gateway = _Gateway(_pass_payload())
    hard_failure = VerificationResult(
        verdict="NEEDS_ACTION",
        issues=(VerificationIssue("missing_report_artifact", "报告不存在"),),
    )

    result = await SemanticVerifier(gateway).evaluate(
        _request(), hard_result=hard_failure
    )

    assert result.verification is hard_failure
    assert result.next_action == "hard_failure"
    assert result.model_called is False
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_semantic_failure_becomes_needs_action() -> None:
    payload = _pass_payload()
    payload["criteria"][0] = {
        "criterion_id": "regions",
        "status": "fail",
        "reason": "只描述了华东，遗漏华南。",
        "evidence_ids": ["evidence-trend"],
    }
    payload["next_action"] = {"kind": "revise", "reason": "补充华南比较。"}
    gateway = _Gateway(payload)

    result = await SemanticVerifier(gateway).evaluate(
        _request(), hard_result=VerificationResult(verdict="PASS")
    )

    assert result.verification.verdict == "NEEDS_ACTION"
    assert result.verification.issues[0].code == "semantic_criterion_failed"


@pytest.mark.asyncio
async def test_uncertain_criterion_with_clarification_waits_for_user() -> None:
    payload = _pass_payload()
    payload["criteria"][0] = {
        "criterion_id": "regions",
        "status": "uncertain",
        "reason": "地区口径存在两个同样合理的定义。",
        "evidence_ids": ["evidence-trend"],
    }
    payload["next_action"] = {"kind": "clarify", "reason": "请用户确认地区口径。"}

    result = await SemanticVerifier(_Gateway(payload)).evaluate(
        _request(), hard_result=VerificationResult(verdict="PASS")
    )

    assert result.verification.verdict == "WAITING_USER"


@pytest.mark.asyncio
async def test_unknown_evidence_or_accepting_known_issue_is_protocol_error() -> None:
    unknown = _pass_payload()
    unknown["criteria"][0]["evidence_ids"] = ["invented-evidence"]
    with pytest.raises(SemanticVerifierProtocolError, match="不存在的 Evidence"):
        await SemanticVerifier(_Gateway(unknown)).evaluate(
            _request(), hard_result=VerificationResult(verdict="PASS")
        )

    unsafe_accept = _pass_payload()
    unsafe_accept["overclaims"] = [
        {"claim_id": "claim-1", "reason": "把相关性写成因果。"}
    ]
    with pytest.raises(SemanticVerifierProtocolError, match="不能是 accept"):
        await SemanticVerifier(_Gateway(unsafe_accept)).evaluate(
            _request(), hard_result=VerificationResult(verdict="PASS")
        )


@pytest.mark.asyncio
async def test_strict_json_and_exact_criterion_set_are_required() -> None:
    gateway = _Gateway(_pass_payload())

    async def invalid_complete(
        scenario: Scenario,
        messages: list[Message],
        *,
        params: dict[str, object] | None = None,
    ) -> ModelResponse:
        del scenario, messages, params
        return ModelResponse(content="```json\n{}\n```", model="invalid")

    gateway.complete = invalid_complete  # type: ignore[method-assign]
    with pytest.raises(SemanticVerifierProtocolError, match="严格 JSON") as captured:
        await SemanticVerifier(gateway).evaluate(
            _request(), hard_result=VerificationResult(verdict="PASS")
        )
    assert captured.value.model == "invalid"
    assert captured.value.response_hash is not None
    assert captured.value.cost is None

    missing = _pass_payload()
    missing["criteria"] = missing["criteria"][:1]
    with pytest.raises(SemanticVerifierProtocolError, match="全部 criterion_id"):
        await SemanticVerifier(_Gateway(missing)).evaluate(
            _request(), hard_result=VerificationResult(verdict="PASS")
        )
