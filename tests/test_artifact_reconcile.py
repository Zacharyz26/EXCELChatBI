"""Report Artifact/filesystem reconciliation safety tests."""

from __future__ import annotations

import os
from pathlib import Path

from packages.session.artifact_reconcile import reconcile_report_files
from packages.session.store import SessionStore


def test_reconciliation_reports_missing_untracked_and_cleans_only_stale_temp(
    tmp_path: Path,
) -> None:
    store = SessionStore(str(tmp_path / "chatbi.db"))
    project = store.create_project("test")
    conversation = store.create_conversation(project.id, "report")
    message = store.append_message(
        conversation_id=conversation.id,
        role="assistant",
        content="报告",
    )
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    report_id = "a" * 32
    pdf = report_dir / f"{report_id}.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    store.create_artifact(
        conversation_id=conversation.id,
        message_id=message.id,
        type="report",
        payload={
            "report_id": report_id,
            "md_url": f"/analyze/report/{report_id}.md",
            "pdf_url": f"/analyze/report/{report_id}.pdf",
        },
        file_ref=str(pdf),
        source_tool="generate_report",
    )

    legacy_id = "b" * 32
    legacy = report_dir / f"{legacy_id}.md"
    legacy.write_text("legacy", encoding="utf-8")
    stale_temp = report_dir / f".{legacy_id}.pdf.{'c' * 32}.tmp"
    stale_temp.write_bytes(b"partial")
    fresh_temp = report_dir / f".{legacy_id}.md.{'d' * 32}.tmp"
    fresh_temp.write_bytes(b"partial")
    old = stale_temp.stat().st_mtime - 7200
    os.utime(stale_temp, (old, old))

    audit = reconcile_report_files(
        store,
        report_dir,
        stale_after_seconds=3600,
        remove_stale_temporary=True,
        now=fresh_temp.stat().st_mtime,
    )

    assert audit.referenced_files == (f"{report_id}.md", f"{report_id}.pdf")
    assert audit.missing_referenced_files == (f"{report_id}.md",)
    assert audit.untracked_published_files == (f"{legacy_id}.md",)
    assert audit.stale_temporary_files == (stale_temp.name,)
    assert audit.removed_temporary_files == (stale_temp.name,)
    assert not stale_temp.exists()
    assert fresh_temp.exists()
    assert legacy.exists(), "未登记文件可能来自旧端点，不能自动删除"


def test_reconciliation_rejects_artifact_file_ref_outside_report_root(
    tmp_path: Path,
) -> None:
    store = SessionStore(str(tmp_path / "chatbi.db"))
    project = store.create_project("test")
    conversation = store.create_conversation(project.id, "report")
    message = store.append_message(
        conversation_id=conversation.id,
        role="assistant",
        content="报告",
    )
    outside = tmp_path / f"{'e' * 32}.pdf"
    outside.write_bytes(b"%PDF")
    artifact = store.create_artifact(
        conversation_id=conversation.id,
        message_id=message.id,
        type="report",
        payload={"report_id": "e" * 32},
        file_ref=str(outside),
        source_tool="generate_report",
    )

    audit = reconcile_report_files(store, tmp_path / "reports")

    assert audit.unsafe_artifact_refs == (artifact.id,)
    assert audit.healthy is False
