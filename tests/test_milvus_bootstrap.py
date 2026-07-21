"""Milvus 生产账号初始化的幂等逻辑测试。"""

from __future__ import annotations

from typing import Any

from scripts.milvus_bootstrap import _bootstrap


class FakeClient:
    def __init__(self) -> None:
        self.users: dict[str, list[str]] = {}
        self.roles: dict[str, list[dict[str, str]]] = {}
        self.calls: list[str] = []

    def list_users(self) -> list[str]:
        return list(self.users)

    def list_roles(self) -> list[str]:
        return list(self.roles)

    def create_user(self, *, user_name: str, password: str) -> None:
        assert password
        self.users[user_name] = []
        self.calls.append("create_user")

    def create_role(self, *, role_name: str, description: str) -> None:
        assert description
        self.roles[role_name] = []
        self.calls.append("create_role")

    def describe_role(self, *, role_name: str) -> dict[str, Any]:
        return {"role": role_name, "privileges": self.roles[role_name]}

    def grant_privilege_v2(
        self, *, role_name: str, privilege: str, collection_name: str
    ) -> None:
        self.roles[role_name].append(
            {"privilege": privilege, "collection_name": collection_name}
        )
        self.calls.append("grant_privilege")

    def describe_user(self, *, user_name: str) -> dict[str, Any]:
        return {"user_name": user_name, "roles": self.users[user_name]}

    def grant_role(self, *, user_name: str, role_name: str) -> None:
        self.users[user_name].append(role_name)
        self.calls.append("grant_role")


def test_bootstrap_is_idempotent() -> None:
    client = FakeClient()
    first = _bootstrap(
        client,
        username="chatbi",
        password="Strong-pass-123",
        role="chatbi_admin",
        collection="*",
    )
    assert first["user_created"] is True
    assert first["privilege_granted"] is True

    client.calls.clear()
    second = _bootstrap(
        client,
        username="chatbi",
        password="unused-existing-password",
        role="chatbi_admin",
        collection="*",
    )
    assert second["user_created"] is False
    assert second["privilege_granted"] is False
    assert second["role_granted"] is False
    assert client.calls == []
