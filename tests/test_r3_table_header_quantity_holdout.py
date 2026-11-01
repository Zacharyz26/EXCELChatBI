"""Whole table source remains audited; header prose does not hide business facts.

Frozen independently after source review. Real local profile oracle is max8;
no product parser/verdict determines expectations. Root executes this file.
"""

from dataclasses import replace

import pytest
from apps.orchestrator.control.claims import build_evidence_summary

from tests.test_r3_independent_surface import FIELD, check_all, source  # noqa: F401


@pytest.mark.parametrize(
    "header,expected",
    [
        ("说明", True),
        (f"说明（{FIELD}最大值为888）", False),
        (f"说明（{FIELD}最大值为8）", True),
    ],
    ids=["neutral-header", "unsupported-fact-in-header", "supported-fact-in-header"],
)
def test_table_header_prose_retains_fact_claim(source, tmp_path, header, expected):  # noqa: F811
    text = f"|字段|{header}|最大值|\n|---|---|---|\n|{FIELD}|已核对|8|"
    witness = check_all(source, text, expected, tmp_path)
    if header != "说明":
        token = "888" if not expected else "8"
        source_start = text.index(token)
        refs = [ref for claim in witness["claims"] for ref in claim["value_refs"]]
        assert any(
            ref.get("source_span") == [source_start, source_start + len(token)]
            and ref.get("source_token") == token
            for ref in refs
        ), witness


@pytest.mark.parametrize("correct", [True, False])
def test_table_header_explicit_dataset_identity_is_not_a_measurement(source, tmp_path, correct):  # noqa: F811
    evidence, artifact = source
    args = {"dataset_ref": artifact.dataset_ref}
    evidence = replace(
        evidence,
        summary=build_evidence_summary(
            summary="real local profile identity",
            result=artifact.payload,
            artifact_id=None,
            arguments=args,
        ),
    )
    dataset = artifact.dataset_ref if correct else "1" * 32
    text = f"|字段|说明（数据集{dataset}）|最大值|\n" f"|---|---|---|\n|{FIELD}|已核对|8|"
    check_all((evidence, artifact), text, correct, tmp_path)
