"""Independent group-role versus metric-role contract, no Host or model."""

import json

import pytest
from packages.orchestration.analysis_requirements import (
    ColumnRoleBinding,
    explicit_analysis_requirements,
)

COLUMNS = ("月份", "频道", "状态", "时长（分钟）")
ROLES = tuple(
    ColumnRoleBinding(name, role)
    for name, role in zip(COLUMNS, ("time", "dimension", "dimension", "metric"), strict=True)
)


@pytest.mark.parametrize(
    "goal,allowed",
    [
        ("请按频道汇总时长（分钟），说明各频道合计。", True),
        ("请按频道汇总频道的合计。", False),
    ],
)
def test_group_wording_cannot_override_or_invent_an_explicit_metric(tmp_path, goal, allowed):
    req = explicit_analysis_requirements(
        goal, columns=COLUMNS, dataset_refs=("f" * 32,), column_roles=ROLES
    )
    payload = {
        "input": goal,
        "contract": req.to_dict(),
        "expected_allowed": allowed,
        "basis": (
            "known dimension 频道 is not a numerical metric; "
            "known measure 时长 is explicit only in positive"
        ),
        "layer": "pure task compiler with independent typed schema",
    }
    (tmp_path / "witness.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    assert req.group_col == "频道", payload
    if allowed:
        assert not req.needs_clarification(), payload
        assert req.value_col == "时长（分钟）" and req.aggregation == "sum", payload
        assert len(req.active_operations()) == 1
        assert req.operation_arguments(req.active_operations()[0].operation_id) == {
            "group_col": "频道",
            "value_col": "时长（分钟）",
            "agg": "sum",
        }, payload
    else:
        assert req.needs_clarification(), payload
        assert req.value_col is None, payload
        assert any(c.slot == "value_col" for c in req.clarifications), payload
