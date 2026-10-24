#!/usr/bin/env bash
# Print the personal API bearer token(s) and the service URL.
set -euo pipefail
source "$(dirname "$0")/config.sh"

echo "SERVICE_URL=$(service_url)"
echo "API_KEY=$(secret_value api-keys | cut -d, -f1)"
if [[ "${1:-}" == "--admin" ]]; then
  echo "ADMIN_KEY=$(secret_value admin-keys | cut -d, -f1)"
fi
