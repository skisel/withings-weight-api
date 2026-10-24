#!/usr/bin/env bash
# Call the personal API with the stored bearer token, e.g.:
#   scripts/api.sh /api/weight/latest
#   scripts/api.sh '/api/weight/history?from=2026-01-01'
set -euo pipefail
source "$(dirname "$0")/config.sh"

path="${1:-/api/weight/latest}"
api_key="$(secret_value api-keys | cut -d, -f1)"
curl -fsS -H "Authorization: Bearer ${api_key}" "$(service_url)${path}"
echo
