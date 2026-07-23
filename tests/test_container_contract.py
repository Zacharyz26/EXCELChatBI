"""Static container contract checks for environments without a Docker daemon."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_api_image_is_non_root_and_uses_persistent_state_paths() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "USER ${APP_UID}:${APP_GID}" in dockerfile
    assert "CHAT_DB_PATH=/var/lib/chatbi/db/chatbi.db" in dockerfile
    assert "DATASET_DIR=/var/lib/chatbi/datasets" in dockerfile
    assert "REPORT_DIR=/var/lib/chatbi/artifacts" in dockerfile
    assert "HEALTHCHECK" in dockerfile and "/health" in dockerfile
    assert "Docker Socket" not in dockerfile


def test_web_image_is_non_root_and_preserves_sse_and_download_proxy() -> None:
    dockerfile = (ROOT / "apps/web/Dockerfile").read_text(encoding="utf-8")
    nginx = (ROOT / "apps/web/nginx.conf").read_text(encoding="utf-8")
    assert "nginx-unprivileged" in dockerfile and "USER 101:101" in dockerfile
    assert "location /api/" in nginx
    assert "proxy_pass http://api:8000/;" in nginx
    assert "proxy_buffering off;" in nginx
    assert "try_files /index.html =503;" in nginx


def test_build_context_excludes_local_state_and_secrets() -> None:
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert ".data" in ignored
    assert ".venv" in ignored
    assert ".env" in ignored
    assert "config/models.yaml" in ignored
    assert "**/node_modules" in ignored
