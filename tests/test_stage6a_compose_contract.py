"""v2.5 6A 受控并行 Compose 发布门禁的静态契约。"""

from __future__ import annotations

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent


def test_compose_exposes_the_bounded_parallelism_setting() -> None:
    compose = yaml.safe_load((_ROOT / "compose.yaml").read_text(encoding="utf-8"))
    environment = compose["services"]["api"]["environment"]

    assert environment["AGENT_MAX_PARALLEL_TOOLS"] == "${AGENT_MAX_PARALLEL_TOOLS:-4}"


def test_compose_gate_persists_and_rechecks_parallel_control_state() -> None:
    gate = (_ROOT / "scripts" / "run_compose_e2e.sh").read_text(encoding="utf-8")
    browser = (_ROOT / "apps" / "web" / "e2e" / "compose-full-stack.spec.ts").read_text(
        encoding="utf-8"
    )

    assert "verify_parallel_run" in gate
    assert "parallel_tool_batches !== 1" in gate
    assert gate.count("verify_parallel_run") >= 3
    assert "COMPOSE_6A_PARALLEL" in browser
    assert "evidence_ledger_sequence" in browser
    assert "data_version_hash" in browser
    assert "cancellation_status" in browser
