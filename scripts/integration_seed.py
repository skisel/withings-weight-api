"""Seed/remove a clearly-fake weight entity (dated 2000-01-01) for scripts/integration-test.sh.

Usage: python scripts/integration_seed.py seed|clean <project>   (access token in GCLOUD_TOKEN)
"""

import os
import sys
from datetime import datetime, timezone

from google.cloud import datastore
from google.oauth2.credentials import Credentials

KEY = "integration-test_0_1"

action, project = sys.argv[1], sys.argv[2]
client = datastore.Client(project=project, credentials=Credentials(token=os.environ["GCLOUD_TOKEN"]))
key = client.key("measurements", KEY)
if action == "seed":
    entity = datastore.Entity(key=key)
    entity.update(
        {
            "account_id": "integration-test",
            "withings_group_id": 0,
            "type": 1,
            "measured_at": datetime(2000, 1, 1, 6, 0, tzinfo=timezone.utc),
            "value": 12.345,
            "unit": "kg",
            "category": 1,
            "device_id": None,
            "imported_at": datetime.now(timezone.utc),
            "source": "integration-test",
        }
    )
    client.put(entity)
else:
    client.delete(key)
