"""未知用量在真实评测汇总路径中保持未知，不因部分费用变成完整费用。"""

from pathlib import Path

import pytest
from packages.models.types import ModelResponse
from packages.session.store import SessionStore
from scripts.agent_verifier_eval import _score_rows as score_verifier
from scripts.v23_baseline_eval import DEFAULT_CASES, _observable_row, _score_rows, load_cases


@pytest.mark.parametrize("unknown", [False, True])
def test_eval_round_and_case_rollup_preserves_unknown(tmp_path: Path, unknown: bool) -> None:
    store = SessionStore(str(tmp_path / "isolated.db"))
    project = store.create_project("synthetic usage")
    conversation = store.create_conversation(project.id, "synthetic")
    first = ModelResponse(
        "ok",
        "offline",
        prompt_tokens=200,
        completion_tokens=40,
        usage_available=True,
        cost=0.0004224,
        cost_currency="USD",
    )
    second = ModelResponse(
        "ok",
        "offline",
        prompt_tokens=None if unknown else 0,
        completion_tokens=0,
        usage_available=not unknown,
        cost=None if unknown else 0,
        cost_currency=None if unknown else "USD",
    )
    row = _observable_row(
        load_cases(DEFAULT_CASES)[0],
        model_name="offline",
        repetition=1,
        events=[],
        responses=[first, second],
        store=store,
        conversation=conversation,
        executed=[],
    )
    metrics = _score_rows([row])
    for result in (row, metrics):
        assert result["prompt_tokens"] == (None if unknown else 200)
        assert result["completion_tokens"] == 40
        assert result["cost"] == (None if unknown else 0.0004224)
        assert result["cost_availability"] == ("unavailable" if unknown else "available")
    # A legacy row may carry a partial amount; its explicit unavailable flag
    # must not be promoted by the aggregate merely because cost is non-null.
    row.update(cost=0.0001, cost_availability="unavailable")
    assert _score_rows([row])["cost"] is None


@pytest.mark.parametrize("unknown", [False, True])
def test_semantic_eval_rollup_handles_unknown_and_zero(unknown: bool) -> None:
    row = {
        "matched": True,
        "predicted_verdict": "PASS",
        "expected_verdict": "PASS",
        "error_type": None,
        "prompt_tokens": 200,
        "completion_tokens": 40,
        "cost": 0.0004224,
        "cost_currency": "USD",
    }
    second = {
        **row,
        "prompt_tokens": None if unknown else 0,
        "completion_tokens": 0,
        "cost": None if unknown else 0,
    }
    metrics = score_verifier([row, second])
    assert metrics["prompt_tokens"] == (None if unknown else 200)
    assert metrics["completion_tokens"] == 40
    assert metrics["cost"] == (None if unknown else 0.0004224)
    assert metrics["cost_availability"] == ("unavailable" if unknown else "available")
