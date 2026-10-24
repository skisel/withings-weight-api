"""Persistence: Firestore in production, an in-memory twin for tests and local runs.

Collections:
  integrations/withings_<account_id>  tokens (encrypted), expiry, sync checkpoint/status, refresh lock
  measurements/<account>_<grpid>_<type>  one normalized value per Withings measure (idempotent upsert)
  oauth_states/<state>                 short-lived OAuth state values (Firestore TTL on expires_at)
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


class FirestoreStore:
    def __init__(self, database: str = "(default)", project: str | None = None):
        from google.cloud import firestore

        self._firestore = firestore
        self._db = firestore.Client(project=project, database=database)

    def create_oauth_state(self, state: str, expires_at: datetime) -> None:
        self._db.collection(OAUTH_STATES).document(state).set({"expires_at": expires_at})

    def consume_oauth_state(self, state: str, now: datetime) -> bool:
        ref = self._db.collection(OAUTH_STATES).document(state)

        @self._firestore.transactional
        def txn(transaction) -> bool:
            snap = ref.get(transaction=transaction)
            if not snap.exists:
                return False
            transaction.delete(ref)
            return snap.get("expires_at") > now

        return txn(self._db.transaction())

    def get_integration(self, account_id: str) -> dict | None:
        snap = self._db.collection(INTEGRATIONS).document(integration_id(account_id)).get()
        return snap.to_dict() if snap.exists else None

    def list_integrations(self) -> list[dict]:
        return [s.to_dict() for s in self._db.collection(INTEGRATIONS).stream()]

    def update_integration(self, account_id: str, fields: dict) -> None:
        ref = self._db.collection(INTEGRATIONS).document(integration_id(account_id))
        ref.set({"account_id": account_id, **fields}, merge=True)

    def try_acquire_refresh_lock(self, account_id: str, now: datetime, until: datetime) -> bool:
        ref = self._db.collection(INTEGRATIONS).document(integration_id(account_id))

        @self._firestore.transactional
        def txn(transaction) -> bool:
            snap = ref.get(transaction=transaction)
            if not snap.exists:
                return False
            lock = (snap.to_dict() or {}).get("refresh_lock_until")
            if lock is not None and lock > now:
                return False
            transaction.update(ref, {"refresh_lock_until": until})
            return True

        return txn(self._db.transaction())

    def upsert_measurements(self, measurements: list[Measurement], imported_at: datetime) -> int:
        collection = self._db.collection(MEASUREMENTS)
        for start in range(0, len(measurements), 400):
            batch = self._db.batch()
            for m in measurements[start : start + 400]:
                batch.set(collection.document(m.key), _measurement_doc(m, imported_at))
            batch.commit()
        return len(measurements)

    def _type_query(self, mtype: int):
        from google.cloud.firestore_v1.base_query import FieldFilter

        return self._db.collection(MEASUREMENTS).where(filter=FieldFilter("type", "==", mtype))

    def latest_measurement(self, mtype: int) -> dict | None:
        query = self._type_query(mtype).order_by(
            "measured_at", direction=self._firestore.Query.DESCENDING
        ).limit(1)
        docs = [d.to_dict() for d in query.stream()]
        return docs[0] if docs else None

    def measurements_between(
        self, mtype: int, start: datetime | None, end: datetime | None, limit: int
    ) -> list[dict]:
        from google.cloud.firestore_v1.base_query import FieldFilter

        query = self._type_query(mtype)
        if start is not None:
            query = query.where(filter=FieldFilter("measured_at", ">=", start))
        if end is not None:
            query = query.where(filter=FieldFilter("measured_at", "<", end))
        query = query.order_by("measured_at", direction=self._firestore.Query.DESCENDING).limit(limit)
        return [d.to_dict() for d in query.stream()]


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
