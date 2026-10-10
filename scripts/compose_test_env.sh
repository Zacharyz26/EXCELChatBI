#!/usr/bin/env bash
# Synthetic, public test-only credentials for Compose validation and E2E.

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  echo "source scripts/compose_test_env.sh instead of executing it" >&2
  exit 2
fi

compose_test_repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -z "${CHATBI_COMPOSE_TEST_SECRET_DIR:-}" ]]; then
  CHATBI_COMPOSE_TEST_SECRET_DIR="$(mktemp -d "${TMPDIR:-/tmp}/chatbi-compose-secrets.XXXXXX")"
  export CHATBI_COMPOSE_TEST_SECRET_DIR
fi
case "$CHATBI_COMPOSE_TEST_SECRET_DIR" in
  "$compose_test_repo_root"/.data/* | /tmp/*) ;;
  *)
    echo "CHATBI_COMPOSE_TEST_SECRET_DIR must stay under /tmp or repository .data" >&2
    return 2
    ;;
esac

umask 077
mkdir -p "$CHATBI_COMPOSE_TEST_SECRET_DIR"
chmod 700 "$CHATBI_COMPOSE_TEST_SECRET_DIR"
export CHATBI_COMPOSE_E2E_TOKEN="chatbi-ci-user-token-20261010-00000001"
compose_test_secret_paths=(
  "$CHATBI_COMPOSE_TEST_SECRET_DIR/api-auth.json"
  "$CHATBI_COMPOSE_TEST_SECRET_DIR/context.key"
  "$CHATBI_COMPOSE_TEST_SECRET_DIR/data.token"
  "$CHATBI_COMPOSE_TEST_SECRET_DIR/stats.token"
  "$CHATBI_COMPOSE_TEST_SECRET_DIR/chart.token"
  "$CHATBI_COMPOSE_TEST_SECRET_DIR/report.token"
  "$CHATBI_COMPOSE_TEST_SECRET_DIR/knowledge.token"
)
rm -f -- "${compose_test_secret_paths[@]}"
printf '{"%s":{"user_id":"local-user","tenant_id":"local","roles":["kb_admin"]}}\n' \
  "$CHATBI_COMPOSE_E2E_TOKEN" > "$CHATBI_COMPOSE_TEST_SECRET_DIR/api-auth.json"
printf '%s\n' 'ci-context-signing-key-20261010-00000001' > "$CHATBI_COMPOSE_TEST_SECRET_DIR/context.key"
printf '%s\n' 'ci-data-service-token-20261010-00000001' > "$CHATBI_COMPOSE_TEST_SECRET_DIR/data.token"
printf '%s\n' 'ci-stats-service-token-20261010-00000001' > "$CHATBI_COMPOSE_TEST_SECRET_DIR/stats.token"
printf '%s\n' 'ci-chart-service-token-20261010-00000001' > "$CHATBI_COMPOSE_TEST_SECRET_DIR/chart.token"
printf '%s\n' 'ci-report-service-token-20261010-00000001' > "$CHATBI_COMPOSE_TEST_SECRET_DIR/report.token"
printf '%s\n' 'ci-knowledge-service-token-20261010-00000001' > "$CHATBI_COMPOSE_TEST_SECRET_DIR/knowledge.token"
# File-backed Compose secrets retain their source modes. Keep the containing
# directory private while making these public synthetic values read-only for
# the non-root container UID.
chmod 0444 "${compose_test_secret_paths[@]}"
for secret_path in "${compose_test_secret_paths[@]}"; do
  if [[ "$(stat -c '%a' "$secret_path")" != "444" ]]; then
    echo "synthetic Compose secret must have mode 0444: $secret_path" >&2
    return 2
  fi
done

export AUTH_TOKENS_FILE_PATH="$CHATBI_COMPOSE_TEST_SECRET_DIR/api-auth.json"
export MCP_CONTEXT_SIGNING_KEY_FILE_PATH="$CHATBI_COMPOSE_TEST_SECRET_DIR/context.key"
export MCP_DATA_TOKEN_FILE_PATH="$CHATBI_COMPOSE_TEST_SECRET_DIR/data.token"
export MCP_STATS_TOKEN_FILE_PATH="$CHATBI_COMPOSE_TEST_SECRET_DIR/stats.token"
export MCP_CHART_TOKEN_FILE_PATH="$CHATBI_COMPOSE_TEST_SECRET_DIR/chart.token"
export MCP_REPORT_TOKEN_FILE_PATH="$CHATBI_COMPOSE_TEST_SECRET_DIR/report.token"
export MCP_KNOWLEDGE_TOKEN_FILE_PATH="$CHATBI_COMPOSE_TEST_SECRET_DIR/knowledge.token"

cleanup_compose_test_secrets() {
  rm -f -- "${compose_test_secret_paths[@]}"
  rmdir "$CHATBI_COMPOSE_TEST_SECRET_DIR" 2>/dev/null || true
}
