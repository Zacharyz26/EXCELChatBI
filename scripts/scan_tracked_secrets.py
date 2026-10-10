#!/usr/bin/env python3
"""Fail closed when tracked or untracked candidate files contain secret material."""

from __future__ import annotations

import argparse
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

_PATTERNS = {
    "private_key": re.compile(rb"-----BEGIN\s+(?:RSA\s+|EC\s+|OPENSSH\s+)?PRIVATE KEY-----"),
    "aws_access_key": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "github_token": re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"),
    "openai_token": re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b"),
}
_SENSITIVE_SUFFIXES = (".key", ".pem", ".p12", ".pfx", ".jks")
_MAX_SCAN_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class SecretFinding:
    """A location and rule name only; never retain or print matched bytes."""

    path: str
    rule: str
    line: int | None = None

    def render(self) -> str:
        location = f"{self.path}:{self.line}" if self.line is not None else self.path
        return f"{location}: {self.rule}"


def candidate_paths(root: Path) -> tuple[Path, ...]:
    """Return tracked and untracked non-ignored paths for pre-commit scanning."""
    completed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        check=True,
        capture_output=True,
    )
    return tuple(
        root / item.decode("utf-8", errors="surrogateescape")
        for item in completed.stdout.split(b"\0")
        if item
    )


def scan_paths(paths: Iterable[Path], *, root: Path) -> list[SecretFinding]:
    """Scan names and common provider token formats without echoing values."""
    findings: list[SecretFinding] = []
    for path in paths:
        if not path.is_file():
            continue
        relative = path.resolve().relative_to(root.resolve()).as_posix()
        if _sensitive_name(relative):
            findings.append(SecretFinding(relative, "sensitive_filename"))
            continue
        try:
            if path.stat().st_size > _MAX_SCAN_BYTES:
                continue
            payload = path.read_bytes()
        except OSError:
            findings.append(SecretFinding(relative, "unreadable_tracked_file"))
            continue
        if b"\0" in payload:
            continue
        for rule, pattern in _PATTERNS.items():
            match = pattern.search(payload)
            if match is None:
                continue
            line = payload.count(b"\n", 0, match.start()) + 1
            findings.append(SecretFinding(relative, rule, line))
    return sorted(findings, key=lambda item: (item.path, item.line or 0, item.rule))


def _sensitive_name(relative: str) -> bool:
    path = Path(relative)
    name = path.name.lower()
    if name == ".env" or name.startswith(".env.") and not name.endswith(".example"):
        return True
    if relative in {"config/models.yaml", "deploy/milvus/.env"}:
        return True
    if relative.startswith("deploy/secrets/") and name != "readme.md" and not name.endswith(
        ".example"
    ):
        return True
    return name.endswith(_SENSITIVE_SUFFIXES)


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan Git candidate files for secret material")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    root = args.root.resolve()
    findings = scan_paths(candidate_paths(root), root=root)
    for finding in findings:
        print(finding.render())
    if findings:
        print(f"secret scan failed: {len(findings)} finding(s)")
        return 1
    print("secret scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
