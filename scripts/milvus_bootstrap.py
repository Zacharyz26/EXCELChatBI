#!/usr/bin/env python3
"""为 Milvus Standalone 创建 ChatBI 最小日常账号，并可轮换默认 root 密码。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _contains_privilege(
    role_info: dict[str, Any], privilege: str, collection: str
) -> bool:
    encoded = json.dumps(role_info.get("privileges", []), ensure_ascii=False)
    return privilege in encoded and collection in encoded


def _bootstrap(
    client: Any,
    *,
    username: str,
    password: str,
    role: str,
    collection: str,
) -> dict[str, object]:
    users = set(client.list_users())
    roles = set(client.list_roles())
    user_created = username not in users
    role_created = role not in roles
    if user_created:
        client.create_user(user_name=username, password=password)
    if role_created:
        client.create_role(role_name=role, description="ChatBI collection lifecycle")

    role_info = client.describe_role(role_name=role)
    privilege_granted = not _contains_privilege(
        role_info, "CollectionAdmin", collection
    )
    if privilege_granted:
        client.grant_privilege_v2(
            role_name=role,
            privilege="CollectionAdmin",
            collection_name=collection,
        )

    user_info = client.describe_user(user_name=username)
    assigned_roles = user_info.get("roles", [])
    role_granted = role not in assigned_roles
    if role_granted:
        client.grant_role(user_name=username, role_name=role)
    return {
        "status": "ready",
        "username": username,
        "role": role,
        "collection_scope": collection,
        "user_created": user_created,
        "role_created": role_created,
        "privilege_granted": privilege_granted,
        "role_granted": role_granted,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="初始化 ChatBI Milvus RBAC")
    parser.add_argument("--uri", default="http://127.0.0.1:19530")
    parser.add_argument("--username", default="chatbi")
    parser.add_argument("--role", default="chatbi_collection_admin")
    parser.add_argument("--collection", default="*", help="物理集合动态换代，默认授权 *")
    args = parser.parse_args()
    root_token = os.getenv("MILVUS_BOOTSTRAP_TOKEN", "")
    app_password = os.getenv("MILVUS_APP_PASSWORD", "")
    new_root_password = os.getenv("MILVUS_NEW_ROOT_PASSWORD", "")
    if not root_token or not app_password:
        parser.error("必须通过环境变量设置 MILVUS_BOOTSTRAP_TOKEN 和 MILVUS_APP_PASSWORD")

    try:
        from pymilvus import MilvusClient

        client = MilvusClient(uri=args.uri, token=root_token)
        try:
            result = _bootstrap(
                client,
                username=args.username,
                password=app_password,
                role=args.role,
                collection=args.collection,
            )
            root_rotated = False
            if new_root_password:
                root_name, separator, old_root_password = root_token.partition(":")
                if root_name != "root" or not separator or not old_root_password:
                    raise RuntimeError(
                        "轮换 root 密码要求 MILVUS_BOOTSTRAP_TOKEN 使用 root:旧密码 格式"
                    )
                client.update_password(
                    user_name="root",
                    old_password=old_root_password,
                    new_password=new_root_password,
                )
                root_rotated = True
            result["root_rotated"] = root_rotated
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            client.close()
    except Exception as exc:
        print(f"错误：Milvus 初始化失败（{type(exc).__name__}）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
