"""R3 原文位置与格式语义；数值预期来自固定合成数据，不取被测解析结果。"""

import hashlib

import pytest
from apps.orchestrator.control.claims import extract_numeric_claims
from apps.orchestrator.control.quantity_presentation import validate_candidate

from tests.test_r1_quantity_source_map import DATASET, FIELD, OTHER, VERSION, _profile


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_source_surface_retains_same_value_distinct_cell_positions(newline):
    from packages.common.text_surface import parse_text_surface

    text = f"|字段|最小值|最大值|{newline}|---|---|---|{newline}|{OTHER}|40|40|{newline}"
    surface = parse_text_surface(text)
    assert surface.source_text == text
    assert surface.source_hash == hashlib.sha256(text.encode()).hexdigest()
    values = [
        node
        for node in surface.nodes
        if node.kind == "table_cell" and text[slice(*node.content_span)] == "40"
    ]
    assert [node.content_span for node in values] == [
        (text.index("|40|") + 1, text.index("|40|") + 3),
        (text.rindex("40|"), text.rindex("40|") + 2),
    ]
    assert values[0].span != values[1].span


@pytest.mark.parametrize("text,closed", [("```\n三项提示\n```", True), ("```\n三项提示", False)])
def test_source_fence_closure_cannot_be_repaired_by_projection(text, closed):
    from packages.common.text_surface import parse_text_surface

    surface = parse_text_surface(text)
    assert ("structure_incomplete" not in surface.issues) is closed
    assert surface.source_text == text


@pytest.mark.parametrize(
    "text,expected,role",
    [
        (f'"（{FIELD}最大40。{FIELD}最小10）"。', True, "max"),
        (f'"（{FIELD}最大40。{FIELD}最小40）"。', False, "max"),
        (f"画像印证了这一点——{FIELD}最大40。", True, "max"),
        (f"画像印证了这一点——{FIELD}最大41。", False, None),
        (f"|字段|最小值|最大值|\n|---|---|---|\n|{OTHER}|40|40|", True, "min"),
        (f"|字段|最小值|最大值|\n|---|---|---|\n|{FIELD}|40|40|", False, None),
        (f"数据集{DATASET}版本{'c' * 64}：**{FIELD}最大40**。", False, None),
        (f"数据集{DATASET}版本{VERSION}：**{FIELD}最大40**。", True, "max"),
    ],
)
def test_binding_preserves_role_field_version_through_surface(text, expected, role):
    evidence = [_profile()]
    result = validate_candidate(text, evidence, goal="复核画像")
    assert result.passed is expected
    refs = [ref for claim in result.claims for ref in claim.value_refs]
    if role is not None:
        supported = [
            ref
            for ref in refs
            if ref.get("supported") is True
            and ref.get("semantic_binding", {}).get("statistic") == role
        ]
        assert supported
        for ref in supported:
            assert text[slice(*ref["source_span"])] == ref["source_token"]


@pytest.mark.parametrize("quote", [('"', '"'), ("「", "」"), ("`", "`")])
def test_label_structure_never_erases_adjacent_real_quantity(quote):
    opening, closing = quote
    text = (
        f"备注中的{opening}重复组数：{closing}{opening}三成{closing}均为标签；另有五\n笔重复记录。"
    )
    claims = extract_numeric_claims(final_text=text, goal="复核画像", evidence=[_profile()])
    refs = [ref for claim in claims for ref in claim.value_refs]
    real = [ref for ref in refs if not str(ref.get("token", "")).startswith("[")]
    assert [ref["token"] for ref in real] == ["五"]
    assert real[0]["supported"] is False
    assert real[0]["source_span"] == [text.index("五"), text.index("五") + 1]
    assert not validate_candidate(text, [_profile()], goal="复核画像").passed


@pytest.mark.parametrize(
    "text",
    ['"重复组数"为"三"。', f'{FIELD}缺失比例"三成"。', f"{FIELD}有（\nb17\n）个\n「缺失值」。"],
)
def test_real_unknown_assignment_is_never_a_label(text):
    result = validate_candidate(text, [_profile()], goal="复核画像")
    assert not result.passed
    assert any(quantity["obligation"] == "blocked" for quantity in result.quantities)
