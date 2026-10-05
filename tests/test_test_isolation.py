"""Regression coverage for the hermetic pytest configuration and API lifespan."""

from __future__ import annotations

from pathlib import Path

from apps.api.deps import (
    embedder_dep,
    kb_store_dep,
    model_gateway_dep,
    reranker_dep,
    session_store_dep,
    settings_dep,
)
from apps.api.main import create_app
from fastapi.testclient import TestClient
from packages.common.config import get_settings
from packages.rag.store import LocalKnowledgeStore
from packages.session.store import SessionStore


def _is_under(path: str, root: Path) -> bool:
    return Path(path).resolve().is_relative_to(root.resolve())


def test_pytest_settings_ignore_development_dotenv(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A hostile local .env cannot redirect tests to developer data or services."""
    from tests.conftest import TEST_RUNTIME_ROOT

    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    without_dotenv = get_settings()

    (tmp_path / ".env").write_text(
        "\n".join(
            (
                "CHAT_DB_PATH=.data/should-not-be-opened.db",
                "DATASET_DIR=.data/should-not-be-created",
                "REPORT_DIR=.data/should-not-be-created-reports",
                "RAG_EMBEDDER=bge",
                "RAG_RERANKER=bge",
                "RAG_STORE=milvus",
                "MILVUS_URI=http://127.0.0.1:19530",
            )
        ),
        encoding="utf-8",
    )
    get_settings.cache_clear()
    with_dotenv = get_settings()

    assert with_dotenv.rag_store == without_dotenv.rag_store == "local"
    assert with_dotenv.rag_embedder == without_dotenv.rag_embedder == "hashing"
    assert with_dotenv.chat_db_path == without_dotenv.chat_db_path
    assert with_dotenv.dataset_dir == without_dotenv.dataset_dir
    assert with_dotenv.report_dir == without_dotenv.report_dir
    assert with_dotenv.kb_index_dir == without_dotenv.kb_index_dir
    for value in (
        with_dotenv.chat_db_path,
        with_dotenv.dataset_dir,
        with_dotenv.report_dir,
        with_dotenv.kb_index_dir,
    ):
        assert _is_under(value, TEST_RUNTIME_ROOT)
    assert not (tmp_path / ".data").exists()
    get_settings.cache_clear()


def test_lifespan_honors_dependency_overrides_and_never_touches_dotenv_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    forbidden = tmp_path / "real-data"
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "\n".join(
            (
                f"CHAT_DB_PATH={forbidden / 'chatbi.db'}",
                f"DATASET_DIR={forbidden / 'datasets'}",
                f"REPORT_DIR={forbidden / 'reports'}",
                f"KB_INDEX_DIR={forbidden / 'kb'}",
                "RAG_EMBEDDER=bge",
                "RAG_RERANKER=bge",
                "RAG_STORE=milvus",
                "MILVUS_URI=http://127.0.0.1:19530",
            )
        ),
        encoding="utf-8",
    )
    get_settings.cache_clear()
    safe_store = SessionStore(str(tmp_path / "isolated" / "chatbi.db"))
    safe_kb = LocalKnowledgeStore(str(tmp_path / "isolated" / "kb"))
    calls: list[str] = []

    def mark(name: str, value: object):
        def provider() -> object:
            calls.append(name)
            return value

        return provider

    app = create_app()
    app.dependency_overrides[settings_dep] = mark("settings", get_settings())
    app.dependency_overrides[session_store_dep] = mark("session", safe_store)
    app.dependency_overrides[model_gateway_dep] = mark("model", object())
    app.dependency_overrides[embedder_dep] = mark("embedder", object())
    app.dependency_overrides[reranker_dep] = mark("reranker", object())
    app.dependency_overrides[kb_store_dep] = mark("kb", safe_kb)

    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}

    assert {"settings", "session", "model", "embedder", "reranker", "kb"} <= set(calls)
    assert not forbidden.exists()
    get_settings.cache_clear()
