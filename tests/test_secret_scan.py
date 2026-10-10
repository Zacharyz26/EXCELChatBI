"""Git candidate secret scanning and sensitive-name regression tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.scan_tracked_secrets import candidate_paths, scan_paths

ROOT = Path(__file__).resolve().parent.parent


def test_scanner_reports_names_and_rules_without_secret_values(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    private_key = tmp_path / "accidental.txt"
    provider_token = tmp_path / "source.py"
    dotenv.write_text("PASSWORD=do-not-print-this-value", encoding="utf-8")
    private_key.write_text(
        "-----BEGIN " + "PRIVATE KEY-----\nsynthetic\n-----END PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    synthetic_provider_token = "gh" + "p_abcdefghijklmnopqrstuvwxyz0123456789"
    provider_token.write_text(
        f"token = '{synthetic_provider_token}'\n",
        encoding="utf-8",
    )

    findings = scan_paths(
        (dotenv, private_key, provider_token),
        root=tmp_path,
    )

    assert {(item.path, item.rule) for item in findings} == {
        (".env", "sensitive_filename"),
        ("accidental.txt", "private_key"),
        ("source.py", "github_token"),
    }
    rendered = "\n".join(item.render() for item in findings)
    assert "do-not-print-this-value" not in rendered
    assert synthetic_provider_token not in rendered


def test_candidate_paths_include_untracked_and_exclude_ignored(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    tracked = tmp_path / "tracked.txt"
    untracked = tmp_path / "untracked.txt"
    ignored = tmp_path / "ignored.txt"
    gitignore = tmp_path / ".gitignore"
    tracked.write_text("tracked", encoding="utf-8")
    untracked.write_text("untracked", encoding="utf-8")
    ignored.write_text("ignored", encoding="utf-8")
    gitignore.write_text("ignored.txt\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "tracked.txt", ".gitignore"],
        cwd=tmp_path,
        check=True,
    )

    candidates = {path.relative_to(tmp_path).as_posix() for path in candidate_paths(tmp_path)}

    assert candidates == {".gitignore", "tracked.txt", "untracked.txt"}


def test_current_candidate_tree_passes_secret_scan() -> None:
    findings = scan_paths(candidate_paths(ROOT), root=ROOT)

    assert findings == []


def test_secret_directory_contains_no_tracked_runtime_values() -> None:
    entries = sorted(path.name for path in (ROOT / "deploy" / "secrets").iterdir())

    assert entries == ["README.md"]
