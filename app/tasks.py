"""Hand sync work off to Cloud Tasks so the webhook can acknowledge quickly.

Cloud Run (request-based billing) throttles CPU once a response is sent, so
work is not done in-process after replying. Cloud Tasks calls back
/internal/sync with an OIDC token and retries on failure.
"""

import json
import logging
from collections.abc import Callable

from app.config import Settings

log = logging.getLogger(__name__)


class SyncDispatcher:
    def __init__(self, settings: Settings, run_inline: Callable[[str | None, bool], object]):
        self._settings = settings
        self._run_inline = run_inline
        self._client = None

    def dispatch(self, account_id: str | None, full: bool = False) -> str:
        if not self._settings.tasks_queue:
            self._run_inline(account_id, full)
            return "inline"

        from google.cloud import tasks_v2

        if self._client is None:
            self._client = tasks_v2.CloudTasksClient()
        task = tasks_v2.Task(
            http_request=tasks_v2.HttpRequest(
                http_method=tasks_v2.HttpMethod.POST,
                url=f"{self._settings.public_base_url}/internal/sync",
                headers={"Content-Type": "application/json"},
                body=json.dumps({"account_id": account_id, "full": full}).encode(),
                oidc_token=tasks_v2.OidcToken(
                    service_account_email=self._settings.invoker_service_account,
                    audience=self._settings.public_base_url,
                ),
            ),
        )
        created = self._client.create_task(parent=self._settings.tasks_queue, task=task)
        log.info("Enqueued sync task", extra={"fields": {"account_id": account_id, "full": full, "task": created.name}})
        return "queued"
