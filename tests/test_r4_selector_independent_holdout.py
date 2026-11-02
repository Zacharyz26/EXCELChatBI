"""Independent frozen finite selector contrasts, NOT_RUN.

The pure cases may be run against the explicit B draft using the optional
R4_SELECTOR_REVIEW_PATH. Otherwise they use the actual product module. Host test
always uses the actual repository. No Evidence, run status or source is forged.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from dataclasses import asdict

import pytest

from tests import test_r4_context_contract as support
from tests.test_r4_context_contract import offline_and_isolated  # noqa: F401
from tests.test_r4_local_chain_draft import conversation_detail

A = "a" * 31 + "1"
B = "a" * 31 + "2"  # same prefix is deliberately not the same resource
PREFIX = f"请使用数据集（dataset_ref: {A}）"


def selector():
    path = os.environ.get("R4_SELECTOR_REVIEW_PATH")
    if path:
        name = "independent_r4_selector_under_review"
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    else:
        module = importlib.import_module("packages.orchestration.context_selection")
    return module.explicit_dataset_selection


@pytest.mark.parametrize(
    "text,expected",
    [
        (PREFIX + "；先别用它。", "conflict"),
        (PREFIX + "；请说明字段“切换次数”的含义。", "selected"),
        (PREFIX + f"；撤回对数据集（dataset_ref: {B}）的选择。", "selected"),
        (PREFIX + f"；撤回对数据集（dataset_ref: {A}）的选择。", "conflict"),
        (PREFIX + "；也许改用之前那个数据集。", "unknown"),
        (PREFIX + f"；说明文字“不要使用 dataset_ref={B}”的含义。", "selected"),
        (f"不要使用数据集（dataset_ref: {A}）。", "not_selected"),
        (f"```text\n请使用数据集（dataset_ref: {A}）。\n```", "not_selected"),
    ],
    ids=[
        "late-pronoun-withdrawal",
        "quoted-field",
        "different-similar-ref-withdrawal",
        "same-ref-withdrawal",
        "unknown-replacement",
        "quoted-other-ref",
        "negative-first",
        "fenced-command",
    ],
)
def test_finite_source_ownership_and_unknown_do_not_guess(text, expected, tmp_path):
    result = selector()(text)
    support.write_witness(
        tmp_path,
        "selector-witness",
        text=text,
        expected=expected,
        actual=asdict(result) if result is not None else None,
        layer="independent pure finite source-intent grammar; not Host/runtime proof",
        source_override=os.environ.get("R4_SELECTOR_REVIEW_PATH"),
    )
    if expected == "not_selected":
        assert result is None or result.reason_code is not None
        return
    assert result is not None and result.dataset_ref == A
    assert text[result.source_span[0] : result.source_span[1]] == PREFIX
    if expected == "selected":
        assert result.reason_code is None, result
    else:
        assert result.reason_code is not None and not result.selection_only, result
    if "请说明字段" in text or "说明文字" in text:
        assert not result.selection_only  # a second explanation obligation is not selection-only


@pytest.mark.asyncio
async def test_withdrawn_new_source_cannot_execute_or_become_available_context(tmp_path):
    store, project, conversation = support.workspace(tmp_path)
    original, child, first = await support.filtered_source(store, project, conversation)
    goal = f"请使用数据集（dataset_ref: {original}）；先别用它。"
    second = await support.execute(
        store,
        project,
        conversation,
        support.ScriptedModel([("get_data_profile", {"dataset_ref": original})], "已完成画像。"),
        goal,
    )
    detail = await conversation_detail(store, conversation)
    support.write_witness(
        tmp_path,
        "withdrawal-host",
        first=first,
        second=second,
        original=original,
        child=child,
        goal=goal,
        detail=detail,
    )
    assert second["run"]["status"] != "completed", second
    assert not any(i["status"] == "succeeded" for i in second["invocations"]), second
    projection = support.execution_projection(detail)
    # Preserve the unresolved latest intent: neither a withdrawn new source
    # nor a silent fallback to the older child is a current authorized source.
    assert projection["availability"] == "unavailable" and projection["dataset_ref"] is None
