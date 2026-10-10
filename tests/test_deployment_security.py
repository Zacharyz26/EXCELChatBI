"""Production deployment security configuration regressions."""

from __future__ import annotations

from pathlib import Path

import pytest
from packages.common.config import Settings


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ('{"production-auth-token-000000000001":[]}', "认证主体记录"),
        (
            '{"production-auth-token-000000000001":{"user_id":"","tenant_id":"t"}}',
            "user_id",
        ),
        (
            '{"production-auth-token-000000000001":{"user_id":"u","tenant_id":""}}',
            "tenant_id",
        ),
        (
            '{"production-auth-token-000000000001":'
            '{"user_id":"u","tenant_id":"t","roles":[""]}}',
            "roles",
        ),
    ],
)
def test_deployed_settings_reject_malformed_auth_records(
    tmp_path: Path,
    payload: str,
    message: str,
) -> None:
    auth_registry = tmp_path / "api-auth.json"
    auth_registry.write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        Settings(
            _env_file=None,
            app_env="production",
            auth_mode="bearer",
            auth_tokens_file=str(auth_registry),
        )
