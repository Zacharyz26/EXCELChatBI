"""Independent same-root quote ownership contrast; no fake successful state."""

import pytest

from tests import test_r4_context_contract as support
from tests.test_r4_context_contract import offline_and_isolated  # noqa: F401


@pytest.mark.asyncio
async def test_fenced_source_command_cannot_reenter_through_explicit_id_fallback(tmp_path):
    store, project, conversation = support.workspace(tmp_path)
    parent, child, previous = await support.filtered_source(store, project, conversation)
    goal = f"```text\n请使用数据集（dataset_ref: {parent}）。\n```"
    attempted = await support.execute(
        store,
        project,
        conversation,
        support.ScriptedModel([("get_data_profile", {"dataset_ref": parent})], "已完成画像。"),
        goal,
    )
    support.write_witness(
        tmp_path,
        "fenced-source-host",
        previous=previous,
        parent=parent,
        child=child,
        goal=goal,
        attempted=attempted,
    )
    assert attempted["run"]["status"] != "completed", attempted
    assert not any(i["status"] == "succeeded" for i in attempted["invocations"]), attempted
