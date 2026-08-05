"""One-shot Compose storage initializer and SQLite migration gate."""

from __future__ import annotations

from pathlib import Path

from packages.common.config import get_settings
from packages.session.store import SessionStore


def main() -> None:
    settings = get_settings()
    for raw_path in (
        settings.upload_dir,
        settings.dataset_dir,
        settings.report_dir,
        settings.workspace_backup_dir,
        settings.kb_index_dir,
        settings.kb_source_dir,
        settings.kb_backup_dir,
        settings.model_cache_dir,
    ):
        Path(raw_path).mkdir(parents=True, exist_ok=True)
    (Path(settings.kb_index_dir) / "generations").mkdir(parents=True, exist_ok=True)
    (Path(settings.kb_source_dir) / "documents").mkdir(parents=True, exist_ok=True)
    store = SessionStore(
        settings.chat_db_path,
        cache_size=settings.conversation_cache_size,
    )
    status = store.readiness_status()
    schema_version = status["schema_version"]
    if not isinstance(schema_version, int) or schema_version <= 0:
        raise RuntimeError("SQLite schema 初始化失败")


if __name__ == "__main__":
    main()
