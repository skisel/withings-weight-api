#!/usr/bin/env bash
# Rotate the personal API key or admin key without downtime.
#   scripts/rotate-key.sh api            -> new key becomes primary, old key still accepted
#   scripts/rotate-key.sh api --finalize -> drop all keys except the primary
# Same for "admin".
set -euo pipefail
source "$(dirname "$0")/config.sh"

kind="${1:-}"
case "$kind" in
  api) secret=api-keys ;;
  admin) secret=admin-keys ;;
  *) echo "usage: $0 api|admin [--finalize]" >&2; exit 2 ;;
esac

current="$(secret_value "$secret")"
if [[ "${2:-}" == "--finalize" ]]; then
  put_secret "$secret" "$(cut -d, -f1 <<<"$current")"
  log "Only the primary ${kind} key remains valid."
else
  new_key="$(random_key)"
  put_secret "$secret" "${new_key},$(cut -d, -f1 <<<"$current")"
  log "New ${kind} key (old one still valid until --finalize):"
  echo "$new_key"
fi
roll_service
