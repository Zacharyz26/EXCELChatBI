"""A whole missing p2 cannot erase a governed source-only obligation.

Actual Host producer; only a contract copy is mutated in memory, no database or
success state is changed. This is not a live Host resume claim.
"""

from __future__ import annotations

import copy
from dataclasses import asdict

import pytest
from apps.orchestrator.agent_loop import _analysis_scope_error, _restore_task_contract
from apps.orchestrator.control.verifier import verify_completion
from packages.session.task_store import TaskStore

from tests import test_r4_context_contract as support
from tests import test_r4_memory_conflict_projection_holdout_v2 as _FIXTURE
from tests.test_r4_context_contract import offline_and_isolated  # noqa: F401


@pytest.mark.asyncio
async def test_missing_entire_coref_proof_does_not_erase_selection_obligation(tmp_path):
    store, project, conversation, named, current, memory_id = _FIXTURE._sources(tmp_path)
    goal = "使用主数据"
    produced = await support.execute(
        store,
        project,
        conversation,
        support.ScriptedModel([], "已选择主数据。"),
        goal,
    )
    assert produced["run"]["status"] == "completed", produced
    assert produced["invocations"] == [], produced
    original = copy.deepcopy(produced["contract"])
    modified = copy.deepcopy(original)
    assert any(a.startswith("HOST_COREF_V1:") for a in modified["assumptions"])
    assert any(a.startswith("HOST_MEMORY_REF_V1:") for a in modified["assumptions"])
    modified["assumptions"] = [
        a for a in modified["assumptions"] if not a.startswith("HOST_COREF_V1:")
    ]
    run_id = produced["run"]["run_id"]
    contract = _restore_task_contract(modified, run_id)
    tasks = TaskStore(store.db_path)
    admission = _analysis_scope_error(
        tasks,
        run_id,
        contract,
        "transform_dataset",
        {"dataset_ref": named, "filters": [{"column": "月份", "op": "==", "value": "2026-03"}]},
    )
    verdict = verify_completion(
        contract=contract,
        final_text="已选择主数据。",
        artifacts=[],
        invocations=[],
        evidence=[],
        claims=[],
        plan_steps=[],
        dataset_bindings=tasks.list_dataset_bindings(run_id),
    )
    support.write_witness(
        tmp_path,
        "named-missing-coref",
        goal=goal,
        named=named,
        current=current,
        memory_id=memory_id,
        original=original,
        modified_in_memory=modified,
        admission=admission,
        verdict=asdict(verdict),
        persisted=tasks.get_verified_contract(run_id),
    )
    assert tasks.get_verified_contract(run_id) == original
    assert admission is not None, (admission, asdict(verdict))
    assert not verdict.passed, asdict(verdict)
