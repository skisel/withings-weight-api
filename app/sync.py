"""Token lifecycle and measurement synchronization."""

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from app.crypto import TokenCipher
from app.store import Store
from app.withings import (
    TokenSet,
    WithingsAuthError,
    WithingsClient,
    WithingsError,
    WithingsTransientError,
)

log = logging.getLogger(__name__)

REFRESH_MARGIN = timedelta(minutes=10)
REFRESH_LOCK_TTL = timedelta(seconds=30)
REFRESH_LOCK_WAIT = timedelta(seconds=15)
# Re-read this much before the last checkpoint so delayed uploads are not missed.
SYNC_OVERLAP = timedelta(hours=6)

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_AUTHORIZED = "authorized"
STATUS_REAUTH = "reauthorization_required"


class ReauthorizationRequired(Exception):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SyncService:
    def __init__(
        self,
        store: Store,
        client: WithingsClient,
        cipher: TokenCipher,
        now: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._store = store
        self._client = client
        self._cipher = cipher
        self._now = now
        self._sleep = sleep

    # ---- tokens ----

    def save_authorization(self, tokens: TokenSet) -> None:
        self._save_tokens(tokens, extra={"sync_status": STATUS_AUTHORIZED, "authorized_at": self._now()})

    def _save_tokens(self, tokens: TokenSet, extra: dict | None = None) -> None:
        self._store.update_integration(
            tokens.user_id,
            {
                "encrypted_access_token": self._cipher.encrypt(tokens.access_token),
                "encrypted_refresh_token": self._cipher.encrypt(tokens.refresh_token),
                "access_token_expires_at": tokens.expires_at,
                "scope": tokens.scope,
                "refresh_lock_until": None,
                "updated_at": self._now(),
                **(extra or {}),
            },
        )

    def access_token(self, account_id: str, force_refresh: bool = False) -> str:
        integration = self._store.get_integration(account_id)
        if not integration or not integration.get("encrypted_refresh_token"):
            raise ReauthorizationRequired(f"No authorization stored for account {account_id}")
        if integration.get("sync_status") == STATUS_REAUTH:
            raise ReauthorizationRequired(f"Account {account_id} requires reauthorization")
        if not force_refresh and self._is_fresh(integration):
            return self._cipher.decrypt(integration["encrypted_access_token"])
        return self._refresh(account_id, seen_access=integration.get("encrypted_access_token"))

    def _is_fresh(self, integration: dict) -> bool:
        expires_at = integration.get("access_token_expires_at")
        return expires_at is not None and expires_at - self._now() > REFRESH_MARGIN

    def _refresh(self, account_id: str, seen_access: str | None) -> str:
        deadline = self._now() + REFRESH_LOCK_WAIT
        while not self._store.try_acquire_refresh_lock(
            account_id, self._now(), self._now() + REFRESH_LOCK_TTL
        ):
            # Someone else is refreshing; use their result once it lands.
            self._sleep(1.0)
            current = self._store.get_integration(account_id) or {}
            if current.get("encrypted_access_token") != seen_access and self._is_fresh(current):
                return self._cipher.decrypt(current["encrypted_access_token"])
            if self._now() > deadline:
                raise WithingsTransientError("Timed out waiting for token refresh lock")

        current = self._store.get_integration(account_id) or {}
        if current.get("encrypted_access_token") != seen_access and self._is_fresh(current):
            self._store.update_integration(account_id, {"refresh_lock_until": None})
            return self._cipher.decrypt(current["encrypted_access_token"])

        try:
            tokens = self._client.refresh(self._cipher.decrypt(current["encrypted_refresh_token"]))
        except WithingsTransientError:
            self._store.update_integration(account_id, {"refresh_lock_until": None})
            raise
        except WithingsError as exc:
            log.error(
                "Withings token refresh rejected; reauthorization required",
                extra={"fields": {"account_id": account_id, "status": exc.status}},
            )
            self._store.update_integration(
                account_id,
                {"sync_status": STATUS_REAUTH, "refresh_lock_until": None, "updated_at": self._now()},
            )
            raise ReauthorizationRequired(f"Refresh rejected for account {account_id}") from None

        # Withings rotates the refresh token: persist the new one immediately.
        self._save_tokens(tokens)
        log.info("Refreshed Withings access token", extra={"fields": {"account_id": account_id}})
        return tokens.access_token

    # ---- sync ----

    def sync_account(self, account_id: str, full: bool = False) -> int:
        started_at = self._now()
        integration = self._store.get_integration(account_id) or {}
        checkpoint = None if full else integration.get("last_successful_sync_at")
        last_update = checkpoint - SYNC_OVERLAP if checkpoint else None
        try:
            token = self.access_token(account_id)
            try:
                measurements = self._client.get_measures(token, account_id, last_update=last_update)
            except WithingsAuthError:
                token = self.access_token(account_id, force_refresh=True)
                measurements = self._client.get_measures(token, account_id, last_update=last_update)
            count = self._store.upsert_measurements(measurements, imported_at=self._now())
        except ReauthorizationRequired:
            raise
        except Exception as exc:
            self._store.update_integration(
                account_id,
                {
                    "sync_status": STATUS_ERROR,
                    "last_sync_error": type(exc).__name__,
                    "last_sync_attempt_at": started_at,
                },
            )
            raise

        self._store.update_integration(
            account_id,
            {
                "sync_status": STATUS_OK,
                "last_successful_sync_at": started_at,
                "last_sync_attempt_at": started_at,
                "last_sync_count": count,
                "last_sync_error": None,
            },
        )
        log.info(
            "Withings sync completed",
            extra={"fields": {"account_id": account_id, "full": full, "upserted": count}},
        )
        return count

    def sync_all(self, full: bool = False) -> dict[str, str]:
        results: dict[str, str] = {}
        for integration in self._store.list_integrations():
            account_id = integration["account_id"]
            try:
                results[account_id] = f"ok:{self.sync_account(account_id, full=full)}"
            except ReauthorizationRequired:
                results[account_id] = STATUS_REAUTH
            except Exception as exc:  # keep going for other accounts
                log.exception("Withings sync failed", extra={"fields": {"account_id": account_id}})
                results[account_id] = f"error:{type(exc).__name__}"
        return results
