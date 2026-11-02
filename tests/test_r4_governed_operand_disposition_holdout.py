"""Independent named operand ownership: mention resolution is not authorization."""

from __future__ import annotations

import pytest
from packages.session.memory_refs import MemoryReferenceResolver, governed_execution_resolution
from packages.session.memory_store import MemoryStore

from tests import test_r4_context_contract as support
from tests.test_agent_loop import _remember_agent_reference
from tests.test_r4_context_contract import offline_and_isolated  # noqa: F401


@pytest.mark.parametrize(
    ("goal", "allowed"),
    [("分析主数据", True), ("不要分析主数据", False), ("主数据", False)],
    ids=["affirmative-predicate", "prohibited-same-predicate", "bare-name-unknown"],
)
def test_governed_name_needs_owned_current_purpose(tmp_path, goal, allowed):
    store, project, conversation = support.workspace(tmp_path)
    dataset = support.upload(
        store,
        project,
        conversation,
        body=(
            "月份,频道,状态,时长（分钟）\n"
            + "2026-01,A,已播出,10\n" * 6
            + "2026-02,A,已播出,20\n" * 6
        ).encode(),
        name="governed-source.csv",
    )
    memory_id = _remember_agent_reference(
        store,
        conversation,
        alias="主数据",
        target_ref=dataset,
        key="independent-r4-governed-operand-purpose",
    )
    memories = MemoryStore(store)
    snapshot, _ = memories.create_snapshot(
        project_id=project.id,
        conversation_id=conversation.id,
        principal=support.PRINCIPAL,
    )
    raw = MemoryReferenceResolver(store, memories).resolve(
        goal,
        project_id=project.id,
        conversation_id=conversation.id,
        principal=support.PRINCIPAL,
        memory_snapshot_id=snapshot.memory_snapshot_id,
    )
    assert raw.status == "resolved" and len(raw.bindings) == 1
    assert raw.bindings[0].target_ref == dataset
    result = governed_execution_resolution(raw)
    support.write_witness(
        tmp_path,
        "governed-operand-purpose",
        goal=goal,
        allowed=allowed,
        dataset=dataset,
        memory_id=memory_id,
        raw_status=raw.status,
        final_status=result.status,
        final_reason=result.reason_code,
        final_bindings=result.bindings,
    )
    if allowed:
        assert result.status == "resolved", result
        assert len(result.bindings) == 1 and result.bindings[0].target_ref == dataset, result
    else:
        assert result.status != "resolved", result
        assert not result.bindings, result
