"""R2 冻结澄清合同：真实上传/SQLite，候选与版本不由客户端自报。"""

import json
import socket
import uuid
from pathlib import Path

import pytest
from apps.api.deps import session_store_dep
from apps.api.main import create_app
from apps.orchestrator.agent_loop import (
    _blocking_clarification,
    _dataset_role_bindings,
    _waiting_user_payload,
)
from fastapi.testclient import TestClient
from packages.orchestration.analysis_requirements import AnalysisRequirements
from packages.orchestration.contracts import build_minimal_contract
from packages.session.store import SessionStore
from packages.session.task_store import ControlConflict, StateVersionConflict, TaskStore

ROOT = Path(__file__).parents[1]
GOAL = "筛选条件：频道=A，状态=已播出。按字段分组汇总播出时长（分钟），并给出总计。"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*_a, **_kw):
        raise AssertionError("R2 clarification tests forbid network/model requests")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)


def waiting_fixture(tmp_path):
    store = SessionStore(str(tmp_path / "clarification.sqlite3"))
    project = store.create_project("R2 synthetic clarification")
    conversation = store.create_conversation(project.id)
    app = create_app()
    app.dependency_overrides[session_store_dep] = lambda: store
    with TestClient(app) as client:
        response = client.post(
            "/upload/excel",
            data={"project_id": project.id, "conversation_id": conversation.id},
            files={
                "file": (
                    "broadcast.csv",
                    (ROOT / "examples/handoff/broadcast.csv").read_bytes(),
                    "text/csv",
                )
            },
        )
    assert response.status_code == 200
    dataset = store.get_dataset(response.json()["dataset_ref"])
    assert dataset is not None
    contract = build_minimal_contract(
        run_id=uuid.uuid4().hex,
        user_text=GOAL,
        chart_required=False,
        report_required=False,
        pdf_required=False,
        dataset_refs=(dataset.ref,),
        columns=tuple(c["name"] for c in dataset.profile["columns"]),
        column_roles=_dataset_role_bindings(dataset),
    )
    requirements = contract.analysis_requirements
    assert requirements is not None and requirements.schema_version == 2
    assert requirements.total and len(requirements.filters) == 2
    question = _blocking_clarification(GOAL, [dataset], [], analysis_requirements=requirements)
    assert question is not None
    assert question["analysis_slot_request"]["slot"] == "group_col"
    tasks = TaskStore(store.db_path)
    message = store.append_message(conversation_id=conversation.id, role="user", content=GOAL)
    run, _ = tasks.create_run(
        project_id=project.id,
        conversation_id=conversation.id,
        user_message_id=message.id,
        contract=contract,
        budget={"max_tool_calls": 8},
    )
    run, plan, _, _ = tasks.save_plan(
        run.run_id,
        expected_version=run.state_version,
        plan={
            "schema_version": 1,
            "summary": "等待分组字段",
            "steps": [],
            "assumptions": [],
            "clarifications": [
                {k: question[k] for k in ("question_id", "about", "question")} | {"blocking": True}
            ],
        },
        reason="initial:fast",
        outline_audit={"route": "fast"},
    )
    payload = _waiting_user_payload(
        {k: question[k] for k in ("question_id", "about", "question")},
        plan_id=plan.plan_id,
        plan_version=plan.version,
        resume_token="r2-clarification-resume-token",
        source_clarification=question,
        data_version_hash=tasks.data_version_hash(run.run_id),
    )
    run, event = tasks.transition(
        run.run_id,
        expected_version=run.state_version,
        status="waiting_user",
        event_type="waiting_user",
        payload=payload,
        checkpoint_reason="waiting_user",
    )
    return store, tasks, run, payload, event, contract


def answer(tasks, run, payload, value="月份", key="independent-r2-answer"):
    return tasks.answer_clarification(
        run.run_id,
        expected_version=run.state_version,
        idempotency_key=key,
        question_id=payload["question_id"],
        resume_token=payload["resume_token"],
        answer=value,
    )


def test_month_option_is_legal_and_answer_changes_only_group_slot(tmp_path):
    store, tasks, run, payload, _, contract = waiting_fixture(tmp_path)
    assert "月份" in payload["answer_schema"]["enum"]
    assert "记录ID" not in payload["answer_schema"]["enum"]
    planning, event, created = answer(tasks, run, payload)
    assert created and planning.status == "planning"
    saved = TaskStore(store.db_path).get_verified_contract(run.run_id)
    req = AnalysisRequirements.from_dict(saved["analysis_requirements"])
    old = contract.analysis_requirements
    assert req.group_col == "月份" and req.revision == old.revision + 1
    assert req.filters == old.filters and req.operations == old.operations
    assert req.total == old.total and req.total_column == old.total_column
    assert req.value_col == old.value_col and req.source_hash == old.source_hash
    assert req.dataset_ref == old.dataset_ref
    assert event.payload["analysis_slot_resolution"]["revision"] == req.revision
    replay, replay_event, repeated = answer(tasks, run, payload)
    assert not repeated and replay == planning and replay_event == event
    assert TaskStore(store.db_path).get_verified_contract(run.run_id) == saved
    (tmp_path / "witness.json").write_text(
        json.dumps({"payload": payload, "contract": saved}, ensure_ascii=False, indent=2)
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "revision",
        "data_version",
        "plan",
        "candidates",
        "dataset",
        "question",
        "token",
        "state",
        "out_of_enum",
    ],
)
def test_invalid_or_stale_answer_has_no_contract_or_message_side_effect(tmp_path, mutation):
    store, tasks, run, payload, event, _ = waiting_fixture(tmp_path)
    before = tasks.get_verified_contract(run.run_id)
    count = len(store.list_messages(run.conversation_id))
    request = json.loads(json.dumps(payload))
    if mutation in {"revision", "data_version", "plan", "candidates", "dataset"}:
        key, value = {
            "revision": ("analysis_revision", 99),
            "data_version": ("data_version_hash", "0" * 64),
            "plan": ("plan_version", 99),
            "candidates": ("candidates", ["记录ID"]),
            "dataset": ("dataset_ref", "f" * 32),
        }[mutation]
        request["analysis_slot_request"][key] = value
        with tasks._connection() as connection, connection:
            connection.execute(
                "UPDATE task_events SET payload_json=? WHERE event_id=?",
                (json.dumps(request), event.event_id),
            )
    with pytest.raises((ControlConflict, StateVersionConflict)):
        tasks.answer_clarification(
            run.run_id,
            expected_version=run.state_version - (mutation == "state"),
            idempotency_key="independent-r2-invalid",
            question_id="analysis:value_col:general"
            if mutation == "question"
            else payload["question_id"],
            resume_token="wrong-token-for-r2" if mutation == "token" else payload["resume_token"],
            answer="记录ID" if mutation == "out_of_enum" else "月份",
        )
    assert tasks.get_verified_contract(run.run_id) == before
    assert tasks.get_run(run.run_id).status == "waiting_user"
    assert len(store.list_messages(run.conversation_id)) == count


def test_saved_slot_survives_reopen_and_planning_crash_stays_fail_closed(tmp_path):
    store, tasks, run, payload, _, _ = waiting_fixture(tmp_path)
    answer(tasks, run, payload)
    frozen = tasks.get_verified_contract(run.run_id)
    reopened = TaskStore(store.db_path)
    reopened.recover_stale_runs(stale_after_seconds=0)
    assert reopened.get_run(run.run_id).status == "failed"
    assert reopened.get_verified_contract(run.run_id) == frozen
    assert frozen["analysis_requirements"]["group_col"] == "月份"
