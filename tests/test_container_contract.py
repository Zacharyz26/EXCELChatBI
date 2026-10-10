"""Static container contract checks for environments without a Docker daemon."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
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
    assert "proxy_set_header Last-Event-ID $http_last_event_id;" in nginx
    assert "proxy_buffering off;" in nginx
    assert "location = /mcp" in nginx
    assert "location ^~ /mcp/" in nginx
    assert "try_files /index.html =503;" in nginx
    assert "client_max_body_size ${MAX_UPLOAD_BODY_MB}m;" in nginx
    assert 'return 413 \'{"detail":"文件过大（上限 ${MAX_UPLOAD_MB} MB）"}\';' in nginx
    assert (
        "COPY apps/web/nginx.conf /etc/nginx/templates/default.conf.template"
        in dockerfile
    )
    assert "docker-entrypoint-upload-limit.sh" in dockerfile
    assert (
        'ENTRYPOINT ["/usr/local/bin/chatbi-web-entrypoint"]'
        in dockerfile
    )
    entrypoint_position = dockerfile.index("ENTRYPOINT")
    default_command = 'CMD ["nginx", "-g", "daemon off;"]'
    assert default_command in dockerfile
    assert entrypoint_position < dockerfile.index(default_command)


def test_build_context_excludes_local_state_and_secrets() -> None:
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert ".data" in ignored
    assert ".venv" in ignored
    assert ".env" in ignored
    assert "config/models.yaml" in ignored
    assert "**/node_modules" in ignored
    assert "deploy/secrets/*" in ignored


def test_compose_upload_limit_and_secret_inputs_fail_closed() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert compose.count("MAX_UPLOAD_MB: ${MAX_UPLOAD_MB:-50}") == 2
    assert compose.count("UPLOAD_MULTIPART_OVERHEAD_MB: ${UPLOAD_MULTIPART_OVERHEAD_MB:-1}") == 2
    assert "AUTH_TOKENS_JSON:" not in compose
    assert "AUTH_TOKENS_FILE: /run/secrets/api_auth_tokens" in compose
    assert "chatbi-local-e2e-token" not in compose
    assert ".dev}" not in compose
    for variable in (
        "AUTH_TOKENS_FILE_PATH",
        "MCP_CONTEXT_SIGNING_KEY_FILE_PATH",
        "MCP_DATA_TOKEN_FILE_PATH",
        "MCP_STATS_TOKEN_FILE_PATH",
        "MCP_CHART_TOKEN_FILE_PATH",
        "MCP_REPORT_TOKEN_FILE_PATH",
        "MCP_KNOWLEDGE_TOKEN_FILE_PATH",
    ):
        assert f"${{{variable}:?" in compose


def test_web_upload_envelope_adds_bounded_multipart_headroom() -> None:
    script = ROOT / "apps/web/docker-entrypoint-upload-limit.sh"
    environment = {
        **os.environ,
        "MAX_UPLOAD_MB": "50",
        "UPLOAD_MULTIPART_OVERHEAD_MB": "1",
    }

    completed = subprocess.run(
        ["sh", str(script), "--print-upload-body-limit"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.stdout.strip() == "51"

    environment["UPLOAD_MULTIPART_OVERHEAD_MB"] = "0"
    rejected = subprocess.run(
        ["sh", str(script), "--print-upload-body-limit"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert rejected.returncode == 2


def test_compose_test_secrets_support_non_root_bind_mounts(tmp_path: Path) -> None:
    secret_dir = tmp_path / "compose-secrets"
    completed = subprocess.run(
        [
            "/bin/bash",
            "-c",
            (
                "source scripts/compose_test_env.sh; "
                "source scripts/compose_test_env.sh; printf '%s\\n' \"$CHATBI_COMPOSE_E2E_TOKEN\""
            ),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "CHATBI_COMPOSE_TEST_SECRET_DIR": str(secret_dir),
        },
    )

    assert completed.returncode == 0, completed.stderr
    exported_token = completed.stdout.strip()
    registry = json.loads(
        (secret_dir / "api-auth.json").read_text(encoding="utf-8")
    )
    assert list(registry) == [exported_token]
    assert stat.S_IMODE(secret_dir.stat().st_mode) == 0o700
    secret_files = sorted(secret_dir.iterdir())
    assert [path.name for path in secret_files] == [
        "api-auth.json",
        "chart.token",
        "context.key",
        "data.token",
        "knowledge.token",
        "report.token",
        "stats.token",
    ]
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o444 for path in secret_files
    )


def test_compose_e2e_authentication_uses_one_synthetic_token_source() -> None:
    helper = (ROOT / "scripts/compose_test_env.sh").read_text(encoding="utf-8")
    runner = (ROOT / "scripts/run_compose_e2e.sh").read_text(encoding="utf-8")
    shared_auth = (ROOT / "apps/web/e2e/compose-auth.ts").read_text(
        encoding="utf-8"
    )
    specs = [
        ROOT / "apps/web/e2e/compose-full-stack.spec.ts",
        ROOT / "apps/web/e2e/report-parallel-fix.spec.ts",
        ROOT / "apps/web/e2e/compose-recovery.spec.ts",
    ]
    spec_sources = [path.read_text(encoding="utf-8") for path in specs]

    all_sources = "\n".join([helper, runner, shared_auth, *spec_sources])
    assert all_sources.count("chatbi-ci-user-token-20261010-00000001") == 1
    assert "chatbi-local-e2e-token-00000001" not in all_sources
    assert "CHATBI_COMPOSE_E2E_TOKEN" in shared_auth
    assert "${CHATBI_COMPOSE_E2E_TOKEN}" in runner
    assert all(
        'from "./compose-auth"' in source for source in spec_sources
    )


def test_compose_e2e_cleans_synthetic_secrets_when_docker_is_missing(
    tmp_path: Path,
) -> None:
    secret_dir = tmp_path / "compose-secrets"
    isolated_bin = tmp_path / "bin"
    isolated_bin.mkdir()
    for command in ("chmod", "dirname", "mkdir", "rm", "rmdir", "stat"):
        executable = shutil.which(command)
        assert executable is not None
        (isolated_bin / command).symlink_to(executable)

    # A DEBUG trap records any attempted Docker execution before command lookup.
    # This proves the early-exit path never reaches real Docker even on CI runners
    # where /usr/bin/docker exists.
    docker_called = tmp_path / "docker-called"
    bash_env = tmp_path / "bash-env"
    bash_env.write_text(
        "trap 'case \"$BASH_COMMAND\" in docker\\ *) "
        ": > \"$DOCKER_CALLED_SENTINEL\" ;; esac' DEBUG\n",
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "PATH": str(isolated_bin),
        "BASH_ENV": str(bash_env),
        "DOCKER_CALLED_SENTINEL": str(docker_called),
        "CHATBI_COMPOSE_TEST_SECRET_DIR": str(secret_dir),
    }
    assert shutil.which("docker", path=environment["PATH"]) is None

    completed = subprocess.run(
        ["/bin/bash", str(ROOT / "scripts/run_compose_e2e.sh")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 127
    assert "Docker CLI is required" in completed.stderr
    assert not secret_dir.exists()
    assert not docker_called.exists()
