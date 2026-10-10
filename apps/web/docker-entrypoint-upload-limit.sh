#!/bin/sh
set -eu

max_upload_mb="${MAX_UPLOAD_MB:-50}"
overhead_mb="${UPLOAD_MULTIPART_OVERHEAD_MB:-1}"

case "$max_upload_mb" in
  ""|*[!0-9]*) echo "MAX_UPLOAD_MB must be an integer" >&2; exit 2 ;;
esac
case "$overhead_mb" in
  ""|*[!0-9]*) echo "UPLOAD_MULTIPART_OVERHEAD_MB must be an integer" >&2; exit 2 ;;
esac
if [ "$max_upload_mb" -lt 1 ] || [ "$max_upload_mb" -gt 1024 ]; then
  echo "MAX_UPLOAD_MB must be between 1 and 1024" >&2
  exit 2
fi
if [ "$overhead_mb" -lt 1 ] || [ "$overhead_mb" -gt 16 ]; then
  echo "UPLOAD_MULTIPART_OVERHEAD_MB must be between 1 and 16" >&2
  exit 2
fi

MAX_UPLOAD_BODY_MB=$((max_upload_mb + overhead_mb))
export MAX_UPLOAD_BODY_MB

if [ "${1:-}" = "--print-upload-body-limit" ]; then
  printf '%s\n' "$MAX_UPLOAD_BODY_MB"
  exit 0
fi

exec /docker-entrypoint.sh "$@"
