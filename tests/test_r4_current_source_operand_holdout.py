"""Independent current-source operand uses actual conversation upload proof.

This is actual upload/API and SQLite resolver coverage, not real Agent/model.
A real project dataset uploaded elsewhere must not supply a missing source.
"""

from __future__ import annotations

import pytest

from tests import test_r4_context_contract as support
from tests.test_r4_context_contract import offline_and_isolated  # noqa: F401


@pytest.mark.parametrize("same_conversation", [True, False], ids=["actual-current", "no-current"])
def test_continue_current_source_requires_actual_conversation_source(tmp_path, same_conversation):
    store, project, conversation = support.workspace(tmp_path)
    destination = conversation if same_conversation else store.create_conversation(project.id)
    dataset = support.upload(
        store,
        project,
        destination,
        body=(
            "月份,频道,状态,时长（分钟）\n"
            + "2026-01,A,已播出,10\n" * 6
            + "2026-02,A,已播出,20\n" * 6
        ).encode(),
        name="valid-current-source.csv",
    )
    query = "继续使用当前数据集"
    resolution = support.resolve(store, project, conversation, query)
    detail = support.conversation_detail(store, conversation)
    support.write_witness(
        tmp_path,
        "current-operand",
        query=query,
        expected_current=dataset if same_conversation else None,
        project_dataset=dataset,
        actual_targets=resolution.targets,
        resolution_status=resolution.status,
        reason=resolution.reason_code,
        detail=detail,
    )
    if same_conversation:
        assert resolution.status == "resolved", resolution
        assert support.ref_set(resolution) == {dataset}, resolution
        assert support.execution_projection(detail)["dataset_ref"] == dataset, detail
    else:
        assert resolution.status != "resolved", resolution
        assert not resolution.targets, resolution
        assert support.execution_projection(detail)["availability"] != "available", detail
