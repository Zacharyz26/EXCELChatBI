"""Independent R3 report source mapper; renderer mutation is explicit fault injection."""

import json

import pytest
from apps.orchestrator.control.report_claims import validate_report_narratives

from tests.test_r3_independent_surface import FIELD, SECOND, source  # noqa: F401


@pytest.mark.parametrize("mutation", ["wrong-field-same-value", "drop-fact", "duplicate-fact"])
def test_render_source_links_require_same_owner_and_occurrence(
    source,  # noqa: F811
    monkeypatch,
    tmp_path,
    mutation,  # noqa: F811
):
    from apps.orchestrator.control import report_claims

    _, artifact = source
    text = f"{FIELD}最大值8。"
    rendered = {
        "wrong-field-same-value": f"{SECOND}最大值8。",
        "drop-fact": "已生成。",
        "duplicate-fact": text + text,
    }[mutation]
    old = report_claims._visible_text
    monkeypatch.setattr(
        report_claims,
        "_visible_text",
        lambda value, **kw: rendered if value == text else old(value, **kw),
    )
    try:
        result = validate_report_narratives([artifact], {"title": "核验", "insights": text})
    except ValueError as error:
        result = {"status": "rejected", "error": str(error)}
    (tmp_path / "witness.json").write_text(
        json.dumps(
            {"source": text, "rendered": rendered, "result": result}, ensure_ascii=False, indent=2
        )
    )
    assert result["status"] == "rejected"


def test_source_and_rendered_links_preserve_duplicate_occurrences(source):  # noqa: F811
    _, artifact = source
    text = f"{FIELD}最大值8。{FIELD}最大值8。"
    result = validate_report_narratives([artifact], {"title": "核验", "insights": text})
    links = result["text_sources"]["insights"]["fact_links"]
    assert len(links) == 2
    expected = [i for i, c in enumerate(text) if c == "8"]
    assert [link["source_span"] for link in links] == [[i, i + 1] for i in expected]
    assert len({tuple(link["rendered_span"]) for link in links}) == 2
