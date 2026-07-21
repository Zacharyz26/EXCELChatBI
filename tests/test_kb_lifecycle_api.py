"""知识库生命周期 API 集成测试。"""

from __future__ import annotations

from pathlib import Path

from apps.api.deps import embedder_dep, kb_store_dep, settings_dep
from apps.api.main import app
from fastapi.testclient import TestClient
from packages.common.config import Settings
from packages.rag.embedding import HashingEmbedder
from packages.rag.store import LocalKnowledgeStore


def test_ingest_update_rebuild_and_delete(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    source = docs / "metrics.md"
    source.write_text("# 活跃用户\n有效登录用户数", encoding="utf-8")
    store = LocalKnowledgeStore(str(tmp_path / "index"))
    settings = Settings(
        _env_file=None,
        rag_embedder="hashing",
        rag_store="local",
        kb_docs_dir=str(docs),
    )
    app.dependency_overrides[settings_dep] = lambda: settings
    app.dependency_overrides[embedder_dep] = lambda: HashingEmbedder(dim=32)
    app.dependency_overrides[kb_store_dep] = lambda: store
    client = TestClient(app)
    try:
        first = client.post("/kb/ingest", json={"path": str(docs)})
        assert first.status_code == 200, first.text
        assert first.json()["created"] == ["metrics.md"]

        unchanged = client.post("/kb/ingest", json={"path": str(docs)})
        assert unchanged.status_code == 200
        assert unchanged.json()["skipped"] == ["metrics.md"]
        assert unchanged.json()["chunks"] == 0

        source.write_text("# 活跃用户\n最近 30 天有效登录用户数", encoding="utf-8")
        updated = client.post("/kb/ingest", json={"path": str(docs)})
        assert updated.json()["updated"] == ["metrics.md"]

        overview = client.get("/kb/overview")
        assert overview.status_code == 200
        document = overview.json()["documents"][0]
        assert document["source"] == "metrics.md"
        assert document["version"] == 2
        ready = client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json()["knowledge_store"]["backend"] == "local"

        inline = client.post(
            "/kb/ingest", json={"text": "临时规则", "source": "temporary.md"}
        )
        assert inline.status_code == 200
        rebuilt = client.post("/kb/rebuild", json={})
        assert rebuilt.status_code == 200, rebuilt.text
        assert rebuilt.json()["deleted"] == ["temporary.md"]

        document_id = client.get("/kb/overview").json()["documents"][0]["document_id"]
        deleted = client.delete(f"/kb/documents/{document_id}")
        assert deleted.status_code == 200
        assert client.get("/kb/overview").json()["documents"] == []
        assert client.delete(f"/kb/documents/{document_id}").status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_readiness_reports_storage_failure() -> None:
    class BrokenStore:
        def status(self) -> None:
            raise RuntimeError("storage unavailable")

    app.dependency_overrides[kb_store_dep] = BrokenStore
    try:
        response = TestClient(app).get("/health/ready")
        assert response.status_code == 503
        assert response.json()["detail"] == {
            "status": "not_ready",
            "component": "knowledge_store",
            "error": "RuntimeError",
        }
        assert "storage unavailable" not in response.text
    finally:
        app.dependency_overrides.clear()
