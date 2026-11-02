"""Independent frozen-proof consumers: real producer, in-memory corruption only.

No TaskContract, Evidence, success history or DB state is manufactured. The
actual Host first produces a complete pure-selection checkpoint. Only a copy
of its immutable contract is changed for gate fault injection. Old/new digest
inputs below follow the documented p2 schema, not the tested helper.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict

import pytest
from apps.orchestrator.agent_loop import _analysis_scope_error, _restore_task_contract
from apps.orchestrator.control.verifier import verify_completion
from packages.session.task_store import TaskStore

from tests import test_r4_context_contract as support
from tests import test_r4_memory_conflict_projection_holdout_v2 as _FIXTURE
from tests.test_r4_context_contract import offline_and_isolated  # noqa: F401

_COREF = "HOST_COREF_V1:"
_MEMORY = "HOST_MEMORY_REF_V1:"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _documented_p2_digest(goal, p2, expected_dataset):
    # Exact documented old p2 projection; optional named_selection is included
    # only when n exists. There is no import of the implementation's helper.
    assert p2["t"] == ["d" + expected_dataset]
    assert p2["q"] == hashlib.sha256(goal.encode()).hexdigest()
    payload = {
        "policy_version": "coref-policy-v2",
        "query_hash": p2["q"],
        "targets": [
            {
                "kind": "dataset",
                "reference_id": expected_dataset,
                "artifact_type": None,
                "analysis_id": None,
                "dataset_ref": expected_dataset,
            }
        ],
        "field_binding": p2["f"],
        "selection_only": p2["s"],
    }
    if "n" in p2:
        payload["named_selection"] = p2["n"]
    return hashlib.sha256(_json(payload).encode()).hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["legacy-without-n", "missing-memory", "mismatched-memory-hash"])
async def test_named_selection_proof_cannot_be_downgraded_or_decoupled(tmp_path, fault):
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
    raw = copy.deepcopy(produced["contract"])
    original = copy.deepcopy(raw)
    prefix_rows = [item for item in raw["assumptions"] if item.startswith(_COREF)]
    assert len(prefix_rows) == 1
    p2 = json.loads(prefix_rows[0][len(_COREF) :])
    assert p2["n"]["memory_id"] == memory_id and p2["n"]["dataset_ref"] == named
    assert p2["h"] == _documented_p2_digest(goal, p2, named)
    if fault == "legacy-without-n":
        p2.pop("n")
        p2["s"] = False
        p2["h"] = _documented_p2_digest(goal, p2, named)
    elif fault == "missing-memory":
        raw["assumptions"] = [item for item in raw["assumptions"] if not item.startswith(_MEMORY)]
    else:
        p2["n"]["memory_binding_hash"] = "0" * 64
        p2["h"] = _documented_p2_digest(goal, p2, named)
    raw["assumptions"] = [
        _COREF + _json(p2) if item.startswith(_COREF) else item for item in raw["assumptions"]
    ]
    assert p2["h"] == _documented_p2_digest(goal, p2, named)
    run_id = produced["run"]["run_id"]
    contract = _restore_task_contract(raw, run_id)
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
        "named-proof-consumer",
        fault=fault,
        named=named,
        current=current,
        memory_id=memory_id,
        original=original,
        modified_in_memory=raw,
        admission=admission,
        verdict=asdict(verdict),
        persisted=tasks.get_verified_contract(run_id),
    )
    assert tasks.get_verified_contract(run_id) == original
    assert admission is not None, (fault, raw, admission)
    assert not verdict.passed, (fault, raw, asdict(verdict))
    assert any(
        issue.code.startswith(("source_selection", "selection_")) for issue in verdict.issues
    ), verdict
