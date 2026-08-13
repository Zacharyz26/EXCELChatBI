"""GitHub Actions 质量门禁编排回归。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


@pytest.mark.parametrize(
    ("gate_id", "artifact_name"),
    [
        ("compaction_quality", "compaction-quality"),
        ("coref_quality", "coref-quality"),
        ("lineage_quality", "lineage-quality"),
        ("coref_mcp_transport", "coref-mcp-transport"),
        ("kb_quality", "kb-evaluation"),
        ("forecast_quality", "forecast-quality"),
    ],
)
def test_quality_report_upload_only_runs_after_gate_execution(
    gate_id: str,
    artifact_name: str,
) -> None:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["backend"]["steps"]
    gate_index = next(index for index, step in enumerate(steps) if step.get("id") == gate_id)
    upload_index, upload = next(
        (index, step)
        for index, step in enumerate(steps)
        if step.get("uses") == "actions/upload-artifact@v5"
        and step.get("with", {}).get("name") == artifact_name
    )

    assert gate_index < upload_index
    assert upload["if"] == (
        f"${{{{ !cancelled() && steps.{gate_id}.outcome != 'skipped' }}}}"
    )
    assert upload["with"]["if-no-files-found"] == "error"
