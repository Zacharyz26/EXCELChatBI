"""Independent R3 parser migration gate; synthetic historical DB, no paid calls."""

import json
import socket
from dataclasses import asdict

import pytest
from apps.orchestrator.control.verifier import verify_completion
from packages.orchestration.contracts import QUANTITY_CONTRACT_VERSION, build_minimal_contract
from packages.session.store import SessionStore
from packages.session.task_store import TaskStore

from tests import test_r1_quantity_plumbing as plumbing


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a, **kw):
        raise AssertionError("version holdouts forbid external model/network")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


@pytest.fixture
def workspace(tmp_path):
    store = SessionStore(str(tmp_path / "migration.sqlite3"))
    project = store.create_project("R3 migration fixture")
    conversation = store.create_conversation(project.id)
    return store, conversation, TaskStore(store.db_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["planning", "running", "waiting_user", "paused", "verifying"])
async def test_real_v1_active_run_is_blocked_before_model_and_preserved(workspace, state, tmp_path):
    await plumbing.test_old_active_run_blocks_before_model_and_preserves_contract(
        workspace, remove=False, version="quantity-contract-v1", state=state
    )
    store, _, tasks = workspace
    (tmp_path / "witness.json").write_text(
        json.dumps(
            {
                "run": asdict(tasks.get_run("quantity-run")),
                "contract": tasks.get_verified_contract("quantity-run"),
                "prior_state": state,
                "model_calls": 0,
                "tool_catalog_calls": 0,
                "layer": "synthetic legacy contract SQLite/Host initialization",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@pytest.mark.asyncio
async def test_parser_mismatch_uses_same_host_and_termination_gate(workspace, monkeypatch):
    from packages.common import text_surface

    monkeypatch.setattr(text_surface, "SURFACE_PARSER_VERSION", "incompatible-parser")
    await plumbing.test_old_active_run_blocks_before_model_and_preserves_contract(
        workspace, remove=False, version=QUANTITY_CONTRACT_VERSION, state="planning"
    )


def test_incompatible_parser_cannot_complete_current_quantity_contract(monkeypatch, tmp_path):
    from packages.common import text_surface

    monkeypatch.setattr(text_surface, "SURFACE_PARSER_VERSION", "incompatible-parser")
    contract = build_minimal_contract(
        run_id="bad-parser",
        user_text="确认文本",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    verdict = verify_completion(
        contract=contract, final_text="完成。", artifacts=[], invocations=[], evidence=[], claims=[]
    )
    (tmp_path / "witness.json").write_text(
        json.dumps(asdict(verdict), ensure_ascii=False, indent=2)
    )
    assert not verdict.passed


@pytest.mark.asyncio
async def test_legacy_completed_run_is_readable_without_rewriting_or_reexecution(tmp_path):
    # Explicit historical fixture, not a fabricated proof of a real Agent run.
    import sqlite3
    from dataclasses import replace

    from apps.api.routers.agent_runs import get_agent_run
    from packages.governance.permissions import Principal
    from packages.session.store import SessionStore
    from packages.session.task_store import TaskStore

    store = SessionStore(str(tmp_path / "historical.sqlite3"))
    project = store.create_project("synthetic historical version")
    conversation = store.create_conversation(project.id)
    message = store.append_message(conversation_id=conversation.id, role="user", content="历史回答")
    old = replace(
        build_minimal_contract(
            run_id="old-terminal",
            user_text="历史回答",
            chart_required=False,
            report_required=False,
            pdf_required=False,
        ),
        quantity_contract_version="quantity-contract-v1",
    )
    tasks = TaskStore(store.db_path)
    run, _ = tasks.create_run(
        project_id=project.id,
        conversation_id=conversation.id,
        user_message_id=message.id,
        contract=old,
        budget={"max_tool_calls": 3},
    )
    for status in ("running", "verifying", "completed"):
        run, _ = tasks.transition(
            run.run_id,
            expected_version=run.state_version,
            status=status,
            event_type="historical_fixture." + status,
            payload={},
        )

    def dump():
        with sqlite3.connect(store.db_path) as connection:
            return list(connection.iterdump())

    before = dump()
    readonly = SessionStore(str(store.db_path), read_only=True)
    visible = await get_agent_run(run.run_id, readonly, Principal(user_id="local-user"))
    assert visible["run"]["status"] == "completed"
    assert visible["contract"]["quantity_contract_version"] == "quantity-contract-v1"
    assert (
        tasks.get_verified_contract(run.run_id)["quantity_contract_version"]
        == "quantity-contract-v1"
    )
    assert (
        tasks.terminate_active_run(
            run.run_id,
            status="blocked",
            reason="quantity_contract_unavailable",
            event_type="run.blocked",
        )
        is None
    )
    assert dump() == before
    (tmp_path / "witness.json").write_text(
        json.dumps(
            {
                "api_projection": visible,
                "state_unchanged": True,
                "model_calls": 0,
                "layer": "historical synthetic SQLite/API readonly projection",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
