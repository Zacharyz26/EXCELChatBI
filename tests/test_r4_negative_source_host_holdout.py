"""Finite negative-source Host holdout; component None is not authorization.

Real prior upload/transform and current Host/SQLite. The model attempts the
explicitly forbidden source. No successful state/Evidence is manufactured.
"""

import pytest

from tests import test_r4_context_contract as support
from tests.test_r4_context_contract import offline_and_isolated  # noqa: F401


@pytest.mark.asyncio
async def test_negative_first_source_cannot_reenter_through_explicit_id_fallback(tmp_path):
    store, project, conversation = support.workspace(tmp_path)
    parent, child, previous = await support.filtered_source(store, project, conversation)
    goal = f"不要使用数据集（dataset_ref: {parent}）。"
    attempted = await support.execute(
        store,
        project,
        conversation,
        support.ScriptedModel([("get_data_profile", {"dataset_ref": parent})], "已完成画像。"),
        goal,
    )
    support.write_witness(
        tmp_path,
        "negative-source-host",
        previous=previous,
        parent=parent,
        child=child,
        goal=goal,
        attempted=attempted,
    )
    assert attempted["run"]["status"] != "completed", attempted
    assert not any(i["status"] == "succeeded" for i in attempted["invocations"]), attempted
