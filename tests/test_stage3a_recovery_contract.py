"""v2.5 3A-3E Compose 离线恢复发布门禁的静态契约测试。"""

from __future__ import annotations

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]


def test_compose_persists_workspace_backups_for_api_and_storage_admin() -> None:
    compose = yaml.safe_load((_ROOT / "compose.yaml").read_text(encoding="utf-8"))

    assert "chatbi-backups" in compose["volumes"]
    for service_name in ("storage-init", "api"):
        service = compose["services"][service_name]
        assert (
            service["environment"]["WORKSPACE_BACKUP_DIR"]
            == "/var/lib/chatbi/backups"
        )
        assert "chatbi-backups:/var/lib/chatbi/backups" in service["volumes"]


def test_compose_e2e_runs_destructive_restore_and_joint_reference_gate() -> None:
    script = (_ROOT / "scripts" / "run_compose_e2e.sh").read_text(
        encoding="utf-8",
    )

    required_fragments = (
        "memory_recovery_probe seed",
        "workspace_admin backup --service-stopped",
        "workspace_admin verify",
        "CHATBI_RECOVERY_PROBE_ALLOW_DESTRUCTIVE=1",
        "memory_recovery_probe disturb",
        "workspace_admin restore",
        "--replace-files",
        "memory_recovery_probe verify",
        "--compaction-id",
        "--compaction-summary-hash",
        "--latest-compaction-id",
        "--plan-id",
        "--plan-version",
        "--plan-hash",
        "--reference-resolution-hash",
        "--memory-reference-resolution-hash",
        "--lineage-graph-hash",
        "--lineage-node-count",
        "--lineage-edge-count",
        "reference-restart-verified.json",
        "verify_original_run",
    )
    for fragment in required_fragments:
        assert fragment in script

    workflow = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8",
    )
    assert "./scripts/run_compose_e2e.sh" in workflow
