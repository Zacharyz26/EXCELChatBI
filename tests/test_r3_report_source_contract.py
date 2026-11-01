"""报告源位置及冻结版本契约：独立输入的原字符区间是 oracle。"""

import hashlib
from dataclasses import replace

import pytest
from apps.orchestrator.control.report_claims import validate_report_narratives
from apps.orchestrator.control.verifier import verify_completion
from packages.orchestration.contracts import build_minimal_contract

from tests.test_r3_independent_surface import FIELD, source  # noqa: F401, F811


def test_report_preserves_source_and_rendered_fact_identity(source):  # noqa: F811
    _, artifact = source
    text = f"**{FIELD}**最小值为**2**，最大值为**8**。"
    result = validate_report_narratives([artifact], {"title": "核验", "insights": text})
    assert result["text_parser_version"] == "text-surface-v2"
    projected = result["text_sources"]["insights"]
    assert projected["source_hash"] == hashlib.sha256(text.encode()).hexdigest()
    links = projected["fact_links"]
    assert len(links) == 2
    assert [text[a:b] for a, b in (link["source_span"] for link in links)] == ["2", "8"]
    assert {link["semantic_binding"]["statistic"] for link in links} == {"min", "max"}
    assert all(link["artifact_id"] == artifact.id for link in links)
    assert all(link["rendered_span"] for link in links)


@pytest.mark.parametrize("version", [None, "quantity-contract-v1", "unknown"])
def test_legacy_or_unknown_version_cannot_complete_even_empty_claims(version):
    contract = build_minimal_contract(
        run_id="version-run",
        user_text="确认文本",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    old = replace(contract, quantity_contract_version=version)
    verdict = verify_completion(
        contract=old, final_text="完成。", artifacts=[], invocations=[], evidence=[], claims=[]
    )
    assert not verdict.passed
    with pytest.raises(ValueError):
        validate_report_narratives([], {"title": "核验"}, quantity_contract_version=version)


def test_render_cannot_replace_one_supported_fact_with_another(source, monkeypatch):  # noqa: F811
    from apps.orchestrator.control import report_claims

    _, artifact = source
    original = report_claims._visible_text
    text = f"{FIELD}最小值2。"
    monkeypatch.setattr(
        report_claims,
        "_visible_text",
        lambda value, **kw: f"{FIELD}最大值8。" if value == text else original(value, **kw),
    )
    with pytest.raises(ValueError):
        validate_report_narratives([artifact], {"title": "核验", "insights": text})
