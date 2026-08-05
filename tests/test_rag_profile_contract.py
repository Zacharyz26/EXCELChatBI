"""5B-5 source/index/cache separation and CPU/GPU profile gates."""

from __future__ import annotations

from pathlib import Path

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[1]


def _yaml(name: str) -> dict[str, object]:
    payload = yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_compose_separates_source_index_backup_and_model_cache_volumes() -> None:
    compose = _yaml("compose.yaml")
    volumes = compose["volumes"]
    assert isinstance(volumes, dict)
    assert {
        "chatbi-kb-index",
        "chatbi-kb-sources",
        "chatbi-kb-backups",
        "chatbi-model-cache",
    } <= volumes.keys()
    assert "chatbi-kb" not in volumes

    services = compose["services"]
    assert isinstance(services, dict)
    api = services["api"]
    knowledge = services["knowledge-tools"]
    assert "chatbi-kb-sources:/var/lib/chatbi/kb/sources" in api["volumes"]
    assert "chatbi-kb-index:/var/lib/chatbi/kb/index" in api["volumes"]
    assert "chatbi-model-cache:/var/cache/chatbi/models" in api["volumes"]
    assert all("kb/sources" not in mount for mount in knowledge["volumes"])
    assert "chatbi-kb-index:/var/lib/chatbi/kb/index:ro" in knowledge["volumes"]


def test_cpu_gpu_profiles_share_semantic_contract_and_fail_closed_devices() -> None:
    common = _yaml("compose.rag.yaml")
    cpu = _yaml("compose.rag.cpu.yaml")
    gpu = _yaml("compose.rag.gpu.yaml")
    common_services = common["services"]
    cpu_services = cpu["services"]
    gpu_services = gpu["services"]
    assert isinstance(common_services, dict)
    assert isinstance(cpu_services, dict)
    assert isinstance(gpu_services, dict)

    for name in ("api", "knowledge-tools"):
        common_service = common_services[name]
        cpu_service = cpu_services[name]
        gpu_service = gpu_services[name]
        environment = common_service["environment"]
        assert environment["RAG_EMBEDDER"] == "bge"
        assert environment["RAG_RERANKER"] == "bge"
        assert environment["RAG_STORE"] == "milvus"
        assert environment["MILVUS_URI"] == "http://milvus:19530"
        assert common_service["build"]["target"] == "api-rag"
        assert cpu_service["environment"] == {
            "RAG_RUNTIME_PROFILE": "cpu",
            "EMBEDDING_DEVICE": "cpu",
        }
        assert gpu_service["environment"] == {
            "RAG_RUNTIME_PROFILE": "gpu",
            "EMBEDDING_DEVICE": "cuda",
        }
        assert gpu_service["gpus"] == "all"

    for dependency in ("etcd", "minio", "milvus"):
        assert common_services[dependency]["profiles"] == ["rag"]


def test_rag_image_is_an_explicit_heavy_target() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM python-builder AS python-rag-builder" in dockerfile
    assert "--extra rag " in dockerfile
    assert "FROM api AS api-rag" in dockerfile
