"""Independent R1 review cases frozen from the 2026-10-30 candidate probes.

These are offline consumer checks with the archived profile Artifact and Evidence.
Expected outcomes follow source ownership and the closed Draft contract, rather
than a product parser's verdict. They do not prove a real Agent/browser chain.
"""

import dataclasses
import json
import socket
from pathlib import Path

import pytest
from apps.orchestrator.control import report_claims
from apps.orchestrator.control.claims import extract_numeric_claims
from apps.orchestrator.control.quantity_presentation import prepare_candidate, reference_catalog
from apps.orchestrator.control.report_claims import validate_report_narratives
from apps.orchestrator.control.verifier import verify_completion
from packages.orchestration.contracts import build_minimal_contract
from packages.session.models import Artifact
from packages.session.task_models import EvidenceRecord

WITNESS = Path(__file__).parent / "fixtures/r1_document_profile.json"
ADVICE = "以下三个建议：\n- 检查字段名。\n- 核对单位。\n- 查看数据版本。\n\n"
PROTOCOL_CASES = [
    (
        "escaped_authority_field",
        r'{"sch\u0065ma":"quantity-presentation-draft-v\u0031","blo\u0063ks":[],"trusted":true}',
        False,
    ),
    (
        "escaped_unknown_schema",
        r'{"sch\u0065ma":"unknown","blo\u0063ks":[],"trusted":true}',
        False,
    ),
    (
        "escaped_blocks_and_authority",
        r'{"schema":"quantity-presentation-draft-v\u0031","blo\u0063ks":[],"trusted":true}',
        False,
    ),
    (
        "decoded_duplicate_schema",
        r'{"sch\u0065ma":"quantity-presentation-draft-v\u0031",'
        r'"blo\u0063ks":[{"kind":"prose","text":"已完成"}],"schema":"unknown"}',
        False,
    ),
    (
        "legal_escaped_envelope",
        r'{"sch\u0065ma":"quantity-presentation-draft-v\u0031",'
        r'"blo\u0063ks":[{"kind":"prose","text":"以下为核对说明。"}]}',
        True,
    ),
]
LABEL_CASES = [
    ("two_local_labels", "这里是两个标签：“甲”“乙”。", True),
    ("another_name_is_not_a_quantity", "这里是两个标签：“甲”“乙”，另一名称是“旁注”。", True),
    ("preceding_quote_is_not_a_member", "标题是“旁注”，这里是三个标签：“甲”“乙”。", False),
    ("following_quote_is_not_a_member", "这里是三个标签：“甲”“乙”，另一名称是“旁注”。", False),
]
CONFIRMATION_CASES = [
    ("unconfirmed_advice", "", True),
    ("pronoun_confirmation", "它们均已核实。", False),
    ("abnormal_confirmation", "上面提到的异常已经确认。", False),
    ("verified_truth", "这些点均已核验属实。", False),
    ("confirmed_proof", "这些点均已证实。", False),
    ("confirmed_control", "这些点均已核实。", False),
]


@pytest.fixture
def profile_input(monkeypatch):
    def deny_network(*args, **kwargs):
        raise AssertionError("Independent R1 review must remain offline")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)
    payload = json.loads(WITNESS.read_text())
    return [EvidenceRecord(**item) for item in payload["evidence"]], Artifact(**payload["profile"])


def assert_consumer(consumer, text, expected, profile_input, goal="核对画像已知事实"):
    evidence, artifact = profile_input
    if consumer == "prepared":
        result = prepare_candidate(text, evidence, goal=goal)
        assert result.source_text == text
        assert result.valid is expected, dataclasses.asdict(result)
    elif consumer == "report":
        args = {"title": goal, "insights": text}
        if expected:
            validate_report_narratives([artifact], args)
        else:
            with pytest.raises(ValueError):
                validate_report_narratives([artifact], args)
    else:
        contract = build_minimal_contract(
            run_id=evidence[0].run_id,
            user_text=goal,
            chart_required=False,
            report_required=False,
            pdf_required=False,
        )
        claims = extract_numeric_claims(final_text=text, goal=goal, evidence=evidence)
        result = verify_completion(
            contract=contract,
            final_text=text,
            artifacts=[],
            invocations=[],
            evidence=evidence,
            claims=claims,
        )
        assert result.passed is expected, dataclasses.asdict(result)


@pytest.mark.parametrize("consumer", ["prepared", "chat", "report"])
@pytest.mark.parametrize("name,text,expected", PROTOCOL_CASES, ids=[c[0] for c in PROTOCOL_CASES])
def test_json_encoding_cannot_change_draft_authority(name, text, expected, consumer, profile_input):
    assert_consumer(consumer, text, expected, profile_input)


@pytest.mark.parametrize("consumer", ["prepared", "chat", "report"])
@pytest.mark.parametrize(
    "goal,expected",
    [
        ("核对当前字段", True),
        ("版本" + "b" * 64 + "，核对字段", False),
        ("仅核对频道为B的记录的字段", False),
    ],
    ids=["current_source", "wrong_version", "wrong_scope"],
)
def test_inspection_reference_retains_requested_source(
    goal, expected, consumer, profile_input, monkeypatch
):
    evidence, artifact = profile_input
    if consumer == "report":
        # A report's selected-Artifact projection owns a different source hash
        # from the chat tool receipt. Observe that real producer, without
        # replacing its Evidence, parser, verdict or implementation behavior.
        observed = []
        original = report_claims.extract_numeric_claims

        def observe_projection(*args, **kwargs):
            observed.append(kwargs["evidence"])
            return original(*args, **kwargs)

        with monkeypatch.context() as observer:
            observer.setattr(report_claims, "extract_numeric_claims", observe_projection)
            validate_report_narratives([artifact], {"title": "核对当前字段"})
        assert observed
        evidence = observed[0]
    catalog = reference_catalog(evidence)
    ref, target = next(
        (key, value)
        for key, value in catalog.items()
        if value["kind"] == "target" and value["target_kind"] == "schema"
    )
    assert target["version"] != "b" * 64
    text = json.dumps(
        {
            "schema": "quantity-presentation-draft-v1",
            "blocks": [
                {"kind": "inspection", "action": "review_field_identity", "target_ref": ref}
            ],
        },
        ensure_ascii=False,
    )
    assert_consumer(consumer, text, expected, profile_input, goal)


@pytest.mark.parametrize("consumer", ["chat", "report"])
@pytest.mark.parametrize("name,text,expected", LABEL_CASES, ids=[c[0] for c in LABEL_CASES])
def test_quoted_members_have_local_ownership(name, text, expected, consumer, profile_input):
    assert_consumer(consumer, text, expected, profile_input)


@pytest.mark.parametrize("consumer", ["chat", "report"])
@pytest.mark.parametrize(
    "name,tail,expected", CONFIRMATION_CASES, ids=[c[0] for c in CONFIRMATION_CASES]
)
def test_numberless_confirmation_cannot_upgrade_advice(
    name, tail, expected, consumer, profile_input
):
    assert_consumer(consumer, ADVICE + tail, expected, profile_input)


@pytest.mark.parametrize("consumer", ["chat", "report"])
@pytest.mark.parametrize(
    "text",
    [
        "本表还有另一个体。",
        "本表还有另一名客户。",
        "本表还有另一个异常。",
        "本表还有另一群客户。",
        "本表还有另一批异常。",
    ],
)
def test_another_entity_does_not_lose_its_quantity(text, consumer, profile_input):
    evidence, _ = profile_input
    claims = extract_numeric_claims(final_text=text, goal="核对画像已知事实", evidence=evidence)
    refs = [ref for claim in claims for ref in claim.value_refs]
    assert any(ref["token"] == "一" and ref.get("supported") is False for ref in refs), refs
    assert_consumer(consumer, text, False, profile_input)
