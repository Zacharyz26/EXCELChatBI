"""Read-only reconciliation between persisted report Artifacts and report files.

The legacy ``/analyze/report`` endpoint intentionally creates downloadable files
without a conversation Artifact.  Therefore an untracked published report is a
cleanup *candidate*, not proof of an orphan, and this module never deletes one.
Only exact atomic-write temporary names can be removed when an operator opts in.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from packages.session.models import Artifact
from packages.session.store import SessionStore

_REPORT_NAME = re.compile(r"^(?P<report_id>[0-9a-f]{32})\.(?P<ext>md|pdf)$")
_TEMP_NAME = re.compile(
    r"^\.(?P<report_id>[0-9a-f]{32})\.(?P<ext>md|pdf)\.[0-9a-f]{32}\.tmp$"
)


@dataclass(frozen=True, slots=True)
class ReportFileReconciliation:
    """Bounded, path-relative reconciliation result suitable for structured logs."""

    referenced_files: tuple[str, ...]
    missing_referenced_files: tuple[str, ...]
    untracked_published_files: tuple[str, ...]
    stale_temporary_files: tuple[str, ...]
    removed_temporary_files: tuple[str, ...]
    cleanup_failures: tuple[str, ...]
    unsafe_artifact_refs: tuple[str, ...]

    @property
    def healthy(self) -> bool:
        return not self.missing_referenced_files and not self.unsafe_artifact_refs

    def log_fields(self) -> dict[str, object]:
        """Return counts plus bounded identifiers; never include report contents."""
        return {
            "healthy": self.healthy,
            "referenced_count": len(self.referenced_files),
            "missing_referenced": list(self.missing_referenced_files[:20]),
            "untracked_published": list(self.untracked_published_files[:20]),
            "stale_temporary": list(self.stale_temporary_files[:20]),
            "removed_temporary": list(self.removed_temporary_files[:20]),
            "cleanup_failures": list(self.cleanup_failures[:20]),
            "unsafe_artifact_refs": list(self.unsafe_artifact_refs[:20]),
        }


def reconcile_report_files(
    store: SessionStore,
    report_dir: str | Path,
    *,
    stale_after_seconds: int = 3600,
    remove_stale_temporary: bool = False,
    now: float | None = None,
) -> ReportFileReconciliation:
    """Compare report Artifacts with disk and optionally remove stale temp files.

    Published files are always read-only here because a legacy endpoint may own
    them.  Temporary cleanup is constrained to the exact filename emitted by the
    atomic report writer, regular non-symlink files, and an age threshold.
    """
    if stale_after_seconds < 0:
        raise ValueError("stale_after_seconds 不能为负数")
    root = Path(report_dir).resolve()
    referenced: set[str] = set()
    unsafe_refs: set[str] = set()
    for artifact in store.list_report_artifacts():
        _collect_artifact_refs(artifact, root, referenced, unsafe_refs)

    published: set[str] = set()
    stale: set[str] = set()
    removed: set[str] = set()
    cleanup_failures: set[str] = set()
    current = time.time() if now is None else now
    if root.is_dir():
        for path in root.iterdir():
            if path.is_symlink() or not path.is_file():
                continue
            if _REPORT_NAME.fullmatch(path.name):
                published.add(path.name)
                continue
            if _TEMP_NAME.fullmatch(path.name):
                try:
                    old_enough = current - path.stat().st_mtime >= stale_after_seconds
                except OSError:
                    cleanup_failures.add(path.name)
                    continue
                if not old_enough:
                    continue
                stale.add(path.name)
                if remove_stale_temporary:
                    try:
                        path.unlink()
                    except OSError:
                        cleanup_failures.add(path.name)
                    else:
                        removed.add(path.name)

    return ReportFileReconciliation(
        referenced_files=tuple(sorted(referenced)),
        missing_referenced_files=tuple(sorted(referenced - published)),
        untracked_published_files=tuple(sorted(published - referenced)),
        stale_temporary_files=tuple(sorted(stale)),
        removed_temporary_files=tuple(sorted(removed)),
        cleanup_failures=tuple(sorted(cleanup_failures)),
        unsafe_artifact_refs=tuple(sorted(unsafe_refs)),
    )


def _collect_artifact_refs(
    artifact: Artifact,
    root: Path,
    referenced: set[str],
    unsafe_refs: set[str],
) -> None:
    payload = artifact.payload or {}
    report_id = payload.get("report_id")
    if isinstance(report_id, str) and re.fullmatch(r"[0-9a-f]{32}", report_id):
        if isinstance(payload.get("md_url"), str):
            referenced.add(f"{report_id}.md")
        if isinstance(payload.get("pdf_url"), str):
            referenced.add(f"{report_id}.pdf")

    if not artifact.file_ref:
        return
    candidate = Path(artifact.file_ref)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    resolved = candidate.resolve()
    if resolved.parent != root or _REPORT_NAME.fullmatch(resolved.name) is None:
        unsafe_refs.add(artifact.id)
        return
    referenced.add(resolved.name)
