"""核心启动依赖与 CI 冒烟契约回归。"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _dependency_name(requirement: str) -> str:
    return requirement.split("[", 1)[0].split(">", 1)[0].split("=", 1)[0].strip()


def test_core_runtime_owns_eagerly_imported_stats_dependencies() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    core = {_dependency_name(item) for item in project["dependencies"]}
    optional = project["optional-dependencies"]

    assert {"statsmodels", "scikit-learn"} <= core
    assert "stats" not in optional
    assert {_dependency_name(item) for item in optional["forecast"]} == {"prophet"}


def test_lockfile_matches_core_and_forecast_dependency_tiers() -> None:
    locked = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    project = next(package for package in locked["package"] if package["name"] == "excel-chatbi")
    core = {item["name"] for item in project["dependencies"]}
    optional = project["optional-dependencies"]

    assert {"statsmodels", "scikit-learn"} <= core
    assert "stats" not in optional
    assert {item["name"] for item in optional["forecast"]} == {"prophet"}


def test_ci_smokes_core_only_environment_before_full_backend_install() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    core_install = "uv sync --locked --no-dev"
    smoke = ".venv-core/bin/python scripts/api_startup_smoke.py"
    full_install = (
        "uv sync --frozen --extra forecast --extra report --extra rag-store --extra mcp"
    )
    assert "UV_PROJECT_ENVIRONMENT: .venv-core" in workflow
    assert workflow.index(core_install) < workflow.index(smoke) < workflow.index(full_install)


def test_startup_smoke_forces_an_isolated_baseline_profile() -> None:
    smoke = (ROOT / "scripts/api_startup_smoke.py").read_text(encoding="utf-8")

    assert '"RAG_RUNTIME_PROFILE": "baseline"' in smoke
    assert "app.router.lifespan_context(app)" in smoke
    assert "ASGITransport(app=app)" in smoke


def test_container_dependency_tier_uses_forecast_extra_not_removed_stats_extra() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "--extra stats" not in dockerfile
    assert "--extra forecast" in dockerfile


def test_current_setup_docs_do_not_require_removed_stats_extra() -> None:
    current_docs = (
        "README.md",
        "CLAUDE.md",
        "docs/本地完整BGE与Milvus启动指南.md",
        "docs/知识库升级验收基线.md",
        "docs/知识库部署与运维.md",
    )

    for relative_path in current_docs:
        content = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "--extra stats" not in content, relative_path
