#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

project_name="${CHATBI_COMPOSE_PROJECT_NAME:-chatbi-e2e}"
image_tag="${CHATBI_IMAGE_TAG:-local}"
compose=(docker compose -p "$project_name" -f compose.yaml -f compose.e2e.yaml)
auth_header="Authorization: Bearer chatbi-local-e2e-token-00000001"

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    "${compose[@]}" logs --no-color || true
  fi
  "${compose[@]}" down --volumes --remove-orphans || true
  exit "$status"
}
trap cleanup EXIT

wait_for_application() {
  phase="$1"
  for attempt in $(seq 1 120); do
    if curl --fail --silent http://127.0.0.1:8080/api/health/ready >/dev/null; then
      return
    fi
    if [ "$attempt" = 120 ]; then
      echo "Compose application did not become ready: $phase" >&2
      exit 1
    fi
    sleep 1
  done
}

verify_original_run() {
  events="$(
    curl --fail --silent \
      -H "$auth_header" \
      "http://127.0.0.1:8080/api/agent/runs/${run_id}/events"
  )"
  EVENTS_JSON="$events" node -e '
  const payload = JSON.parse(process.env.EVENTS_JSON);
  const count = payload.events.filter(
    (event) => event.event_type === "step.completed"
      && event.payload.tool === "generate_report",
  ).length;
  if (count !== 1) {
    throw new Error(`report side effect was committed ${count} times`);
  }
  '
  curl --fail --silent \
    -H "$auth_header" \
    --output .data/e2e/recovered-report.pdf \
    "http://127.0.0.1:8080/api${pdf_url}"
  test -s .data/e2e/recovered-report.pdf
}

verify_reference_recovery() {
  "${compose[@]}" exec -T api \
    python -m apps.api.memory_recovery_probe verify \
    --original-run-id "$run_id" \
    --probe-run-id "$probe_run_id" \
    --memory-snapshot-id "$memory_snapshot_id" \
    --memory-content-hash "$memory_content_hash" \
    --compaction-id "$compaction_id" \
    --compaction-summary-hash "$compaction_summary_hash" \
    --latest-compaction-id "$latest_compaction_id" \
    --memory-id "$memory_id" \
    --artifact-id "$artifact_id" \
    --plan-id "$plan_id" \
    --plan-version "$plan_version" \
    --plan-hash "$plan_hash" \
    --reference-resolution-hash "$reference_resolution_hash" \
    --memory-reference-resolution-hash "$memory_reference_resolution_hash"
}

"${compose[@]}" down --volumes --remove-orphans
if [ "${CHATBI_COMPOSE_NO_BUILD:-0}" != "1" ]; then
  CHATBI_IMAGE_TAG="$image_tag" "${compose[@]}" build
fi

mkdir -p .data/e2e
docker run --rm \
  --user "$(id -u):$(id -g)" \
  -e E2E_FIXTURE_DIR=/fixtures \
  -v "$repo_root/.data/e2e:/fixtures" \
  --entrypoint python \
  "chatbi-python:$image_tag" \
  -m apps.e2e_model.prepare_fixture

CHATBI_IMAGE_TAG="$image_tag" "${compose[@]}" up -d --no-build
wait_for_application "initial startup"
pnpm --dir apps/web test:e2e:compose

run_id="$(node -e "const f=require('./.data/e2e/compose-result.json'); process.stdout.write(f.run_id)")"
pdf_url="$(node -e "const f=require('./.data/e2e/compose-result.json'); process.stdout.write(f.pdf_url)")"
"${compose[@]}" restart api report-tools
wait_for_application "API/report-tools restart"
verify_original_run

probe_output="$(
  "${compose[@]}" exec -T api \
    python -m apps.api.memory_recovery_probe seed \
    --original-run-id "$run_id"
)"
printf '%s\n' "$probe_output" > .data/e2e/memory-recovery-seed.log
probe_json="$(
  PROBE_OUTPUT="$probe_output" node -e '
  const lines = process.env.PROBE_OUTPUT.trim().split(/\r?\n/).reverse();
  for (const line of lines) {
    try {
      const value = JSON.parse(line);
      if (value.status === "seeded") {
        process.stdout.write(JSON.stringify(value));
        process.exit(0);
      }
    } catch {}
  }
  throw new Error("memory recovery seed JSON was not found");
  '
)"
printf '%s\n' "$probe_json" > .data/e2e/memory-recovery-seed.json
probe_run_id="$(
  PROBE_JSON="$probe_json" node -e \
    'process.stdout.write(JSON.parse(process.env.PROBE_JSON).probe_run_id)'
)"
memory_snapshot_id="$(
  PROBE_JSON="$probe_json" node -e \
    'process.stdout.write(JSON.parse(process.env.PROBE_JSON).memory_snapshot_id)'
)"
memory_content_hash="$(
  PROBE_JSON="$probe_json" node -e \
    'process.stdout.write(JSON.parse(process.env.PROBE_JSON).memory_content_hash)'
)"
compaction_id="$(
  PROBE_JSON="$probe_json" node -e \
    'process.stdout.write(JSON.parse(process.env.PROBE_JSON).compaction_id)'
)"
compaction_summary_hash="$(
  PROBE_JSON="$probe_json" node -e \
    'process.stdout.write(JSON.parse(process.env.PROBE_JSON).compaction_summary_hash)'
)"
latest_compaction_id="$(
  PROBE_JSON="$probe_json" node -e \
    'process.stdout.write(JSON.parse(process.env.PROBE_JSON).latest_compaction_id)'
)"
memory_id="$(
  PROBE_JSON="$probe_json" node -e \
    'process.stdout.write(JSON.parse(process.env.PROBE_JSON).memory_id)'
)"
artifact_id="$(
  PROBE_JSON="$probe_json" node -e \
    'process.stdout.write(JSON.parse(process.env.PROBE_JSON).artifact_id)'
)"
plan_id="$(
  PROBE_JSON="$probe_json" node -e \
    'process.stdout.write(JSON.parse(process.env.PROBE_JSON).plan_id)'
)"
plan_version="$(
  PROBE_JSON="$probe_json" node -e \
    'process.stdout.write(String(JSON.parse(process.env.PROBE_JSON).plan_version))'
)"
plan_hash="$(
  PROBE_JSON="$probe_json" node -e \
    'process.stdout.write(JSON.parse(process.env.PROBE_JSON).plan_hash)'
)"
reference_resolution_hash="$(
  PROBE_JSON="$probe_json" node -e \
    'process.stdout.write(JSON.parse(process.env.PROBE_JSON).reference_resolution_hash)'
)"
memory_reference_resolution_hash="$(
  PROBE_JSON="$probe_json" node -e \
    'process.stdout.write(JSON.parse(process.env.PROBE_JSON).memory_reference_resolution_hash)'
)"

"${compose[@]}" restart api
wait_for_application "fixed reference API restart"
restart_reference_json="$(verify_reference_recovery)"
printf '%s\n' "$restart_reference_json" > .data/e2e/reference-restart-verified.json

CHATBI_IMAGE_TAG="$image_tag" "${compose[@]}" stop
backup_json="$(
  CHATBI_IMAGE_TAG="$image_tag" "${compose[@]}" run --rm --no-deps storage-init \
    python -m apps.api.workspace_admin backup --service-stopped
)"
printf '%s\n' "$backup_json" > .data/e2e/workspace-backup.json
backup_path="$(
  BACKUP_JSON="$backup_json" node -e \
    'process.stdout.write(JSON.parse(process.env.BACKUP_JSON).path)'
)"
CHATBI_IMAGE_TAG="$image_tag" "${compose[@]}" run --rm --no-deps storage-init \
  python -m apps.api.workspace_admin verify --input "$backup_path"
CHATBI_IMAGE_TAG="$image_tag" "${compose[@]}" run --rm --no-deps \
  -e CHATBI_RECOVERY_PROBE_ALLOW_DESTRUCTIVE=1 \
  storage-init python -m apps.api.memory_recovery_probe disturb \
  --artifact-id "$artifact_id"
restore_json="$(
  CHATBI_IMAGE_TAG="$image_tag" "${compose[@]}" run --rm --no-deps storage-init \
    python -m apps.api.workspace_admin restore \
    --input "$backup_path" \
    --service-stopped \
    --yes \
    --replace-files
)"
printf '%s\n' "$restore_json" > .data/e2e/workspace-restore.json

CHATBI_IMAGE_TAG="$image_tag" "${compose[@]}" up -d --no-build
wait_for_application "offline workspace restore"
recovery_json="$(verify_reference_recovery)"
printf '%s\n' "$recovery_json" > .data/e2e/memory-recovery-verified.json
verify_original_run

echo "Compose E2E, restart, offline backup/restore and fixed reference recovery passed."
