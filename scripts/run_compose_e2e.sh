#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

project_name="${CHATBI_COMPOSE_PROJECT_NAME:-chatbi-e2e}"
image_tag="${CHATBI_IMAGE_TAG:-local}"
compose=(docker compose -p "$project_name" -f compose.yaml -f compose.e2e.yaml)

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    "${compose[@]}" logs --no-color || true
  fi
  "${compose[@]}" down --volumes --remove-orphans || true
  exit "$status"
}
trap cleanup EXIT

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
for attempt in $(seq 1 120); do
  if curl --fail --silent http://127.0.0.1:8080/healthz >/dev/null; then
    break
  fi
  if [ "$attempt" = 120 ]; then
    echo "Compose application did not become ready" >&2
    exit 1
  fi
  sleep 1
done
pnpm --dir apps/web test:e2e:compose

run_id="$(node -e "const f=require('./.data/e2e/compose-result.json'); process.stdout.write(f.run_id)")"
pdf_url="$(node -e "const f=require('./.data/e2e/compose-result.json'); process.stdout.write(f.pdf_url)")"
"${compose[@]}" restart api report-tools

for attempt in $(seq 1 60); do
  if curl --fail --silent http://127.0.0.1:8080/api/health/ready >/dev/null; then
    break
  fi
  if [ "$attempt" = 60 ]; then
    echo "API did not recover after restart" >&2
    exit 1
  fi
  sleep 1
done

events="$(
  curl --fail --silent \
    -H "Authorization: Bearer chatbi-local-e2e-token-00000001" \
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
  -H "Authorization: Bearer chatbi-local-e2e-token-00000001" \
  --output .data/e2e/recovered-report.pdf \
  "http://127.0.0.1:8080/api${pdf_url}"
test -s .data/e2e/recovered-report.pdf

echo "Compose E2E and API/report-tools restart recovery passed."
