"""dataset_ref 是不透明标识符，不能退化为路径或跨项目句柄。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from packages.common import dataset_store
from packages.common.identifiers import InvalidDatasetRefError
from packages.governance.permissions import Principal
from packages.governance.policy import ToolPolicyGateway, ToolPolicyRequest

_VALID_REF = "a" * 32


@pytest.mark.parametrize(
    "malicious_ref",
    [
        "../../outside",
        "/tmp/outside",
        "a" * 31,
        "A" * 32,
        " a" * 16,
        "",
    ],
)
def test_dataset_path_rejects_non_opaque_refs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    malicious_ref: str,
) -> None:
    monkeypatch.setattr(dataset_store, "_base_dir", lambda: tmp_path / "datasets")

    with pytest.raises(InvalidDatasetRefError):
        dataset_store._path_of(malicious_ref)


def test_valid_dataset_ref_stays_inside_dataset_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = tmp_path / "datasets"
    monkeypatch.setattr(dataset_store, "_base_dir", lambda: base)

    path = dataset_store._path_of(_VALID_REF)

    assert path == base.resolve() / f"{_VALID_REF}.parquet"
    assert path.parent == base.resolve()


def _policy_request(
    *,
    dataset_ref: str,
    project_id: str = "project-a",
    resource_project_id: str | None,
) -> ToolPolicyRequest:
    return ToolPolicyRequest(
        principal=Principal("user-a", "tenant-a"),
        project_id=project_id,
        conversation_id="conversation-a",
        run_id="run-a",
        tool_name="get_data_profile",
        arguments={"dataset_ref": dataset_ref},
        calls_used=0,
        max_tool_calls=3,
        resource_project_id=resource_project_id,
    )


def test_policy_denies_unknown_dataset_by_default() -> None:
    decision = ToolPolicyGateway(audit_recorder=lambda _event: None).authorize(
        _policy_request(dataset_ref=_VALID_REF, resource_project_id=None)
    )

    assert decision.allowed is False
    assert decision.code == "unregistered_resource_denied"


def test_policy_denies_invalid_and_cross_project_dataset_refs() -> None:
    gateway = ToolPolicyGateway(audit_recorder=lambda _event: None)

    invalid = gateway.authorize(
        _policy_request(dataset_ref="../../outside", resource_project_id="project-a")
    )
    foreign = gateway.authorize(
        _policy_request(dataset_ref=_VALID_REF, resource_project_id="project-b")
    )

    assert invalid.code == "invalid_dataset_ref"
    assert foreign.code == "cross_project_resource_denied"


def test_saved_dataframes_use_valid_opaque_refs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = tmp_path / "datasets"
    base.mkdir()
    monkeypatch.setattr(dataset_store, "_base_dir", lambda: base)

    ref = dataset_store.save_dataframe(pd.DataFrame({"value": [1, 2]}))

    assert len(ref) == 32
    assert ref.isascii() and ref.isalnum() and ref == ref.lower()
    assert dataset_store._path_of(ref).exists()
