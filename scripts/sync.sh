#!/usr/bin/env bash
# Trigger a sync now. Pass --full to re-import the whole history (backfill / repair).
set -euo pipefail
source "$(dirname "$0")/config.sh"

full=false
[[ "${1:-}" == "--full" ]] && full=true
admin_curl -X POST -H "Content-Type: application/json" -d "{\"full\": ${full}}" "$(service_url)/admin/sync"
echo
