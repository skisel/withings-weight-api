"""Runtime configuration, read from environment variables.

On Cloud Run the sensitive values are injected from Secret Manager
(see scripts/deploy.sh); locally they can come from a .env-style export.
"""

import os
from dataclasses import dataclass
from functools import lru_cache


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable {name}")
    return value


def _split(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


@dataclass(frozen=True)
class Settings:
    withings_client_id: str
    withings_client_secret: str
    token_encryption_key: str
    # Several comma-separated keys are accepted to allow rotation without downtime.
    api_keys: tuple[str, ...]
    admin_keys: tuple[str, ...]
    webhook_secret: str
    # Public https base URL of this service, e.g. https://withings-123.europe-west6.run.app
    public_base_url: str
    # Full Cloud Tasks queue path; when empty, sync runs inline in the request.
    tasks_queue: str
    # Service account whose OIDC tokens are accepted on /internal/* (Cloud Tasks, Scheduler).
    invoker_service_account: str
    firestore_database: str
    withings_scope: str = "user.metrics"

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_base_url}/auth/callback"

    @property
    def webhook_url(self) -> str:
        return f"{self.public_base_url}/webhooks/withings/{self.webhook_secret}"


@lru_cache
def get_settings() -> Settings:
    return Settings(
        withings_client_id=_required("WITHINGS_CLIENT_ID"),
        withings_client_secret=_required("WITHINGS_CLIENT_SECRET"),
        token_encryption_key=_required("TOKEN_ENCRYPTION_KEY"),
        api_keys=_split(_required("API_KEYS")),
        admin_keys=_split(_required("ADMIN_KEYS")),
        webhook_secret=_required("WEBHOOK_SECRET"),
        public_base_url=_required("PUBLIC_BASE_URL").rstrip("/"),
        tasks_queue=os.environ.get("TASKS_QUEUE", "").strip(),
        invoker_service_account=os.environ.get("INVOKER_SERVICE_ACCOUNT", "").strip(),
        firestore_database=os.environ.get("FIRESTORE_DATABASE", "(default)").strip(),
    )
