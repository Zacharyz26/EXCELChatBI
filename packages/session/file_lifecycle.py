"""Constrained deletion helpers for files owned by persisted resources."""

from __future__ import annotations

import re
import time
from pathlib import Path

from packages.common.identifiers import validate_report_id

_CHART_NAME = re.compile(r"^chart_[0-9a-f]{32}\.png$")


def delete_report_files(report_id: str, report_dir: str | Path) -> tuple[str, ...]:
    """Delete one report's Markdown/PDF files without accepting arbitrary paths."""
    clean_id = validate_report_id(report_id)
    root = Path(report_dir).resolve()
    removed: list[str] = []
    for extension in ("md", "pdf"):
        path = (root / f"{clean_id}.{extension}").resolve()
        if path.parent != root:
            raise ValueError("报告文件路径超出存储目录")
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        removed.append(path.name)
    return tuple(removed)


def delete_chart_file(file_ref: object, report_dir: str | Path) -> bool:
    """Delete a generated chart screenshot only when it is inside ``charts``."""
    if not isinstance(file_ref, str) or not file_ref.strip():
        return False
    chart_root = (Path(report_dir).resolve() / "charts").resolve()
    candidate = Path(file_ref)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    path = candidate.resolve()
    if path.parent != chart_root or _CHART_NAME.fullmatch(path.name) is None:
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def cleanup_stale_chart_files(
    report_dir: str | Path,
    *,
    stale_after_seconds: int,
    now: float | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Remove abandoned transient screenshots after a conservative age window."""
    if stale_after_seconds < 0:
        raise ValueError("stale_after_seconds 不能为负数")
    chart_root = (Path(report_dir).resolve() / "charts").resolve()
    if not chart_root.is_dir():
        return (), ()
    current = time.time() if now is None else now
    removed: list[str] = []
    failures: list[str] = []
    for path in chart_root.iterdir():
        if (
            path.is_symlink()
            or not path.is_file()
            or _CHART_NAME.fullmatch(path.name) is None
        ):
            continue
        try:
            old_enough = current - path.stat().st_mtime >= stale_after_seconds
            if not old_enough:
                continue
            path.unlink()
        except OSError:
            failures.append(path.name)
        else:
            removed.append(path.name)
    return tuple(sorted(removed)), tuple(sorted(failures))
