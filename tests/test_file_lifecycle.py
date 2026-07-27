"""Managed report/chart file cleanup must stay inside configured roots."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from packages.session.file_lifecycle import (
    cleanup_stale_chart_files,
    delete_chart_file,
    delete_report_files,
)


def test_report_cleanup_is_bounded_to_valid_report_id(tmp_path: Path) -> None:
    report_id = "a" * 32
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    markdown = report_dir / f"{report_id}.md"
    pdf = report_dir / f"{report_id}.pdf"
    markdown.write_text("# report", encoding="utf-8")
    pdf.write_bytes(b"%PDF")
    outside = tmp_path / "outside.md"
    outside.write_text("keep", encoding="utf-8")

    removed = delete_report_files(report_id, report_dir)

    assert removed == (f"{report_id}.md", f"{report_id}.pdf")
    assert outside.read_text(encoding="utf-8") == "keep"
    with pytest.raises(ValueError, match="格式非法"):
        delete_report_files("../outside", report_dir)


def test_chart_cleanup_rejects_unmanaged_paths(tmp_path: Path) -> None:
    report_dir = tmp_path / "reports"
    charts = report_dir / "charts"
    charts.mkdir(parents=True)
    managed = charts / f"chart_{'b' * 32}.png"
    unmanaged = tmp_path / "chart.png"
    managed.write_bytes(b"png")
    unmanaged.write_bytes(b"keep")

    assert delete_chart_file(str(managed), report_dir) is True
    assert delete_chart_file(str(unmanaged), report_dir) is False
    assert unmanaged.read_bytes() == b"keep"


def test_startup_cleanup_removes_only_stale_managed_charts(tmp_path: Path) -> None:
    chart_root = tmp_path / "reports" / "charts"
    chart_root.mkdir(parents=True)
    stale = chart_root / f"chart_{'c' * 32}.png"
    fresh = chart_root / f"chart_{'d' * 32}.png"
    unrelated = chart_root / "keep.png"
    for path in (stale, fresh, unrelated):
        path.write_bytes(b"png")
    stale.touch()
    fresh.touch()
    stale_mtime = stale.stat().st_mtime
    os.utime(fresh, (stale_mtime + 30, stale_mtime + 30))

    removed, failures = cleanup_stale_chart_files(
        tmp_path / "reports",
        stale_after_seconds=60,
        now=stale_mtime + 61,
    )

    assert removed == (stale.name,)
    assert failures == ()
    assert fresh.exists()
    assert unrelated.exists()
