"""Author boundary obligations, frozen before the second candidate checks."""

import pytest

from tests.test_segmented_quantity_fix import captured, check, claims


@pytest.mark.parametrize(
    "text",
    [
        "播出时长（分钟）统计口径：仅保留奇数行。最大值为999。",
        "播出时长（分钟）统计口径：基于当前上传数据、未做任何筛选或清洗，统计对象为该列全部非空记录（非空数74）。空值数为1，缺失比例为0.0133。",
        "播出时长（分钟）统计口径：基于当前上传数据。记录ID最大值为999。",
        "播出时长（分钟）统计口径：基于当前上传数据。\n\n最大值为999。",
    ],
)
def test_profile_parent_never_grants_unknown_scope_or_other_subject(text):
    assert not check("s0-chain-profile", text).valid


@pytest.mark.parametrize(
    "text",
    [
        "其他播出时长总计为1350分钟。",
        "播出时长（分钟）总计为1350秒。",
        "整个期间（2026-02至2026-06）播出时长总计为1350分钟。",
    ],
)
def test_total_unit_identity_and_period_are_not_same_value_aliases(text):
    assert not check("s4-monthly", text).valid


@pytest.mark.parametrize("kind", ["hard-paragraph", "quoted", "new-heading", "unknown-value"])
def test_list_structure_cannot_lend_fact_to_other_container(kind):
    _, _, text = captured("s4-monthly")
    if kind == "hard-paragraph":
        text = text.replace("- ", "")
    if kind == "quoted":
        text = text.replace("- 2026-01：75.0", "> - 2026-01：75.0")
    if kind == "new-heading":
        text = text.replace("- 2026-03：195.0", "另一个统计：\n- 2026-03：195.0")
    if kind == "unknown-value":
        text = text.replace("- 2026-03：195.0", "- 2026-03：195.0，异常组数为1")
    assert not check("s4-monthly", text).valid


def test_s5_real_group_values_supported_but_delta_not():
    _, _, text = captured("s5-chart")
    refs = [r for c in claims("s5-chart", text) for r in c.value_refs]
    assert (
        sum(
            r.get("supported")
            and r.get("token") in {"75.0", "135.0", "195.0", "255.0", "315.0", "375.0"}
            for r in refs
        )
        == 6
    )
    delta = [r for r in refs if r.get("token") == "60"]
    assert delta and all(not r.get("supported") for r in delta)
