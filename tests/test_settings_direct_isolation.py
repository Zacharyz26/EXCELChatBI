"""Regression tests for direct Settings isolation and production dotenv behavior."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from packages.common.config import Settings


def _is_under(path: str, root: Path) -> bool:
    return Path(path).resolve().is_relative_to(root.resolve())


def test_direct_settings_instances_ignore_development_dotenv(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from tests.conftest import TEST_RUNTIME_ROOT

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "\n".join(
            (
                'AUTH_TOKENS_JSON={"dotenv-user":"dotenv-only-secret"}',
                "AGENT_MCP_SERVICE_TOKEN=dotenv-only-service-secret",
                "CHAT_DB_PATH=.data/direct-settings-escape.db",
            )
        ),
        encoding="utf-8",
    )

    settings = Settings()

    assert settings.auth_tokens_json == ""
    assert settings.agent_mcp_service_token == ""
    assert "direct-settings-escape" not in settings.chat_db_path
    assert _is_under(settings.chat_db_path, TEST_RUNTIME_ROOT)
    assert not (tmp_path / ".data").exists()


def test_production_settings_default_still_reads_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        'AUTH_TOKENS_JSON={"prod-user":"production-dotenv-sentinel"}\n',
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment.pop("CHATBI_TEST_ISOLATION", None)
    environment.pop("AUTH_TOKENS_JSON", None)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from packages.common.config import Settings; print(Settings().auth_tokens_json)",
        ],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "production-dotenv-sentinel" in completed.stdout
