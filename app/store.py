"""Persistence: Firestore in Datastore mode in production, an in-memory twin for tests.

Kinds:
  integrations/withings_<account_id>  tokens (encrypted), expiry, sync checkpoint/status, refresh lock
  measurements/<account>_<grpid>_<type>  one normalized value per Withings measure (idempotent upsert)
  oauth_states/<state>                 short-lived OAuth state values (TTL policy on expires_at)
"""

import threading
from datetime import datetime
from typing import Protocol

from app.withings import Measurement

INTEGRATIONS = "integrations"
MEASUREMENTS = "measurements"
OAUTH_STATES = "oauth_states"


def integration_id(account_id: str) -> str:
    return f"withings_{account_id}"


def _measurement_doc(m: Measurement, imported_at: datetime) -> dict:
    return {
        "account_id": m.account_id,
        "withings_group_id": m.group_id,
        "type": m.type,
        "measured_at": m.measured_at,
        "value": m.value,
        "unit": m.unit,
        "category": m.category,
        "device_id": m.device_id,
        "imported_at": imported_at,
        "source": "withings",
    }


class Store(Protocol):
    def create_oauth_state(self, state: str, expires_at: datetime) -> None: ...
    def consume_oauth_state(self, state: str, now: datetime) -> bool: ...
    def get_integration(self, account_id: str) -> dict | None: ...
    def list_integrations(self) -> list[dict]: ...
    def update_integration(self, account_id: str, fields: dict) -> None: ...
    def try_acquire_refresh_lock(self, account_id: str, now: datetime, until: datetime) -> bool: ...
    def upsert_measurements(self, measurements: list[Measurement], imported_at: datetime) -> int: ...
    def latest_measurement(self, mtype: int) -> dict | None: ...
    def measurements_between(
        self, mtype: int, start: datetime | None, end: datetime | None, limit: int
    ) -> list[dict]: ...


class DatastoreStore:
    """Cloud Firestore in Datastore mode (the project's "(default)" database)."""

    # Long or never-queried properties are kept out of indexes.
    _UNINDEXED = ("encrypted_access_token", "encrypted_refresh_token", "scope")

    def __init__(self, database: str = "", project: str | None = None):
        from google.cloud import datastore

        self._datastore = datastore
        self._client = datastore.Client(project=project, database=database or None)

    def _entity(self, kind: str, name: str, data: dict):
        entity = self._datastore.Entity(
            key=self._client.key(kind, name), exclude_from_indexes=self._UNINDEXED
        )
        entity.update(data)
        return entity

    def _integration_key(self, account_id: str):
        return self._client.key(INTEGRATIONS, integration_id(account_id))

    def create_oauth_state(self, state: str, expires_at: datetime) -> None:
        self._client.put(self._entity(OAUTH_STATES, state, {"expires_at": expires_at}))

    def consume_oauth_state(self, state: str, now: datetime) -> bool:
        key = self._client.key(OAUTH_STATES, state)
        with self._client.transaction():
            entity = self._client.get(key)
            if entity is None:
                return False
            self._client.delete(key)
        return entity["expires_at"] > now

    def get_integration(self, account_id: str) -> dict | None:
        entity = self._client.get(self._integration_key(account_id))
        return dict(entity) if entity is not None else None

    def list_integrations(self) -> list[dict]:
        return [dict(e) for e in self._client.query(kind=INTEGRATIONS).fetch()]

    def update_integration(self, account_id: str, fields: dict) -> None:
        key = self._integration_key(account_id)
        with self._client.transaction():
            entity = self._client.get(key) or self._entity(INTEGRATIONS, key.name, {})
            entity.update({"account_id": account_id, **fields})
            self._client.put(entity)

    def try_acquire_refresh_lock(self, account_id: str, now: datetime, until: datetime) -> bool:
        with self._client.transaction():
            entity = self._client.get(self._integration_key(account_id))
            if entity is None:
                return False
            lock = entity.get("refresh_lock_until")
            if lock is not None and lock > now:
                return False
            entity["refresh_lock_until"] = until
            self._client.put(entity)
            return True

    def upsert_measurements(self, measurements: list[Measurement], imported_at: datetime) -> int:
        entities = [self._entity(MEASUREMENTS, m.key, _measurement_doc(m, imported_at)) for m in measurements]
        for start in range(0, len(entities), 400):
            self._client.put_multi(entities[start : start + 400])
        return len(entities)

    def latest_measurement(self, mtype: int) -> dict | None:
        docs = self.measurements_between(mtype, None, None, 1)
        return docs[0] if docs else None

    def measurements_between(
        self, mtype: int, start: datetime | None, end: datetime | None, limit: int
    ) -> list[dict]:
        from google.cloud.datastore.query import PropertyFilter

        # Served by the composite index in index.yaml (type ASC, measured_at DESC).
        query = self._client.query(kind=MEASUREMENTS)
        query.add_filter(filter=PropertyFilter("type", "=", mtype))
        if start is not None:
            query.add_filter(filter=PropertyFilter("measured_at", ">=", start))
        if end is not None:
            query.add_filter(filter=PropertyFilter("measured_at", "<", end))
        query.order = ["-measured_at"]
        return [dict(e) for e in query.fetch(limit=limit)]


class MemoryStore:
    def __init__(self):
        self._lock = threading.Lock()
        self.states: dict[str, datetime] = {}
        self.integrations: dict[str, dict] = {}
        self.measurements: dict[str, dict] = {}

    def create_oauth_state(self, state: str, expires_at: datetime) -> None:
        with self._lock:
            self.states[state] = expires_at

    def consume_oauth_state(self, state: str, now: datetime) -> bool:
        with self._lock:
            expires_at = self.states.pop(state, None)
            return expires_at is not None and expires_at > now

    def get_integration(self, account_id: str) -> dict | None:
        with self._lock:
            doc = self.integrations.get(integration_id(account_id))
            return dict(doc) if doc else None

    def list_integrations(self) -> list[dict]:
        with self._lock:
            return [dict(d) for d in self.integrations.values()]

    def update_integration(self, account_id: str, fields: dict) -> None:
        with self._lock:
            doc = self.integrations.setdefault(integration_id(account_id), {"account_id": account_id})
            doc.update(fields)

    def try_acquire_refresh_lock(self, account_id: str, now: datetime, until: datetime) -> bool:
        with self._lock:
            doc = self.integrations.get(integration_id(account_id))
            if doc is None:
                return False
            lock = doc.get("refresh_lock_until")
            if lock is not None and lock > now:
                return False
            doc["refresh_lock_until"] = until
            return True

    def upsert_measurements(self, measurements: list[Measurement], imported_at: datetime) -> int:
        with self._lock:
            for m in measurements:
                self.measurements[m.key] = _measurement_doc(m, imported_at)
        return len(measurements)

    def latest_measurement(self, mtype: int) -> dict | None:
        docs = self.measurements_between(mtype, None, None, 1)
        return docs[0] if docs else None

    def measurements_between(
        self, mtype: int, start: datetime | None, end: datetime | None, limit: int
    ) -> list[dict]:
        with self._lock:
            docs = [
                dict(d)
                for d in self.measurements.values()
                if d["type"] == mtype
                and (start is None or d["measured_at"] >= start)
                and (end is None or d["measured_at"] < end)
            ]
        docs.sort(key=lambda d: d["measured_at"], reverse=True)
        return docs[:limit]
