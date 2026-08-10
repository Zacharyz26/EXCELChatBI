"""Prepare an isolated, deterministic workspace for the real full-stack E2E."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from apps.e2e_model.prepare_fixture import build_sales_fixture
from packages.governance.permissions import Principal
from packages.session.memory_models import MemoryDraft
from packages.session.memory_store import MemoryStore
from packages.session.store import SessionStore

ROOT = Path(__file__).resolve().parent.parent
E2E_ROOT = (ROOT / ".data" / "e2e").resolve()


def main() -> None:
    expected_parent = (ROOT / ".data").resolve()
    if E2E_ROOT.parent != expected_parent or E2E_ROOT.name != "e2e":
        raise RuntimeError("拒绝清理非预期的 E2E 数据目录")
    shutil.rmtree(E2E_ROOT, ignore_errors=True)
    E2E_ROOT.mkdir(parents=True)
    build_sales_fixture().to_excel(E2E_ROOT / "sales.xlsx", index=False)
    _seed_workspace_memory()


def _seed_workspace_memory() -> None:
    """预置一条带真实来源的记忆，供浏览器验证纠正与软删除。"""
    principal = Principal(user_id="e2e-user", tenant_id="e2e-tenant")
    store = SessionStore(str(E2E_ROOT / "chatbi.db"))
    project = store.create_project(
        "我的分析项目",
        owner_user_id=principal.user_id,
        tenant_id=principal.tenant_scope,
    )
    conversation = store.create_conversation(project.id)
    message = store.append_message(
        conversation_id=conversation.id,
        role="user",
        content="将外部编号统一称为对象 ID",
    )
    MemoryStore(store, audit_recorder=lambda _event: None).remember(
        project_id=project.id,
        principal=principal,
        draft=MemoryDraft(
            scope="project",
            kind="field_alias",
            semantic_key="field-alias.object-id",
            content_summary="外部编号的展示名称是对象 ID",
            source_type="user_confirmation",
            source_ref=message.id,
            source_hash=hashlib.sha256(message.content.encode("utf-8")).hexdigest(),
            confidence=0.95,
        ),
        idempotency_key="fullstack-memory-seed",
    )


if __name__ == "__main__":
    main()
