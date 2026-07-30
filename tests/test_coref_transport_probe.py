"""v2.5 3C Host 引用在 MCP 双传输中的固定绑定契约。"""

from __future__ import annotations

from pathlib import Path

from scripts.mcp_transport_probe import _resolve_probe_reference

_ROOT = Path(__file__).resolve().parents[1]


def test_transport_probe_resolves_conversation_and_memory_to_same_dataset(
    tmp_path: Path,
) -> None:
    result = _resolve_probe_reference(tmp_path / "reference.db", "2" * 32)

    assert result["dataset_ref"] == "2" * 32
    assert len(result["memory_snapshot_id"]) == 32
    assert len(result["conversation_resolution_hash"]) == 64
    assert len(result["memory_resolution_hash"]) == 64
    assert len(result["target_ref_hash"]) == 64


def test_ci_runs_and_uploads_reference_transport_probe() -> None:
    workflow = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "scripts/mcp_transport_probe.py" in workflow
    assert "--output .data/coref-mcp-transport.json" in workflow
    assert "name: coref-mcp-transport" in workflow
