#!/usr/bin/env bash
# Build the image with Cloud Build and deploy/update the Cloud Run service and the daily
# reconciliation job. Run after scripts/setup.sh; re-run for every update.
set -euo pipefail
source "$(dirname "$0")/config.sh"
cd "$REPO_ROOT"

for name in withings-client-id withings-client-secret; do
  secret_has_version "$name" || { warn "Secret $name has no value. Run scripts/set-withings-credentials.sh first."; exit 1; }
done

URL="$(service_url)"
TAG="$(git rev-parse --short HEAD 2>/dev/null || echo manual)"
if [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
  TAG="${TAG}-dirty-$(date -u +%Y%m%d%H%M%S)"
fi
IMAGE="${IMAGE_BASE}:${TAG}"

log "Building ${IMAGE}"
gcloud builds submit --region="$REGION" --tag="$IMAGE" --quiet .

log "Deploying Cloud Run service ${SERVICE}"
gcloud run deploy "$SERVICE" \
  --image="$IMAGE" \
  --region="$REGION" \
  --service-account="$RUN_SA" \
  --allow-unauthenticated \
  --ingress=all \
  --min-instances=0 \
  --max-instances=2 \
  --concurrency=20 \
  --cpu=1 \
  --memory=512Mi \
  --cpu-throttling \
  --timeout=300 \
  --set-env-vars="PUBLIC_BASE_URL=${URL},TASKS_QUEUE=${TASKS_QUEUE_PATH},INVOKER_SERVICE_ACCOUNT=${INVOKER_SA}" \
  --set-secrets="$SECRET_ENV_MAPPING" \
  --quiet

log "Allowing ${INVOKER_SA} to invoke the service"
gcloud run services add-iam-policy-binding "$SERVICE" --region="$REGION" \
  --member="serviceAccount:${INVOKER_SA}" --role=roles/run.invoker --quiet >/dev/null

log "Daily reconciliation job (${SCHEDULER_CRON} ${SCHEDULER_TZ})"
scheduler_args=(
  --location="$REGION"
  --schedule="$SCHEDULER_CRON"
  --time-zone="$SCHEDULER_TZ"
  --uri="${URL}/internal/sync"
  --http-method=POST
  --headers="Content-Type=application/json"
  --message-body='{}'
  --oidc-service-account-email="$INVOKER_SA"
  --oidc-token-audience="$URL"
  --attempt-deadline=300s
  --quiet
)
if gcloud scheduler jobs describe "$SCHEDULER_JOB" --location="$REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "$SCHEDULER_JOB" "${scheduler_args[@]}" >/dev/null
else
  gcloud scheduler jobs create http "$SCHEDULER_JOB" "${scheduler_args[@]}" >/dev/null
fi

log "Smoke test"
curl -fsS "${URL}/health" && echo
log "Deployed ${IMAGE} -> ${URL}"
