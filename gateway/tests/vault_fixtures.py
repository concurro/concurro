"""An isolated vault with fake GitHub identity and no provider transport."""

import json
import os
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vault"))
import vault
from vault import GITHUB_USER, Envelope, Identity, Sealed, Store, create_app

ALICE, BOB = "ghu_alice-token", "ghu_bob-token"
USERS = {ALICE: {"id": 101, "login": "alice"}, BOB: {"id": 202, "login": "bob"}}
ANTHROPIC = "sk-ant-api03-validsecretAAAA"
OPENAI = "sk-proj-validsecretBBBB"


class FakeTransport:
    """Serve synthetic GitHub identities and reject unexpected network calls."""

    def __init__(self):
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        if url == GITHUB_USER:
            user = USERS.get(headers["Authorization"].removeprefix("Bearer "))
            return (200, json.dumps(user).encode()) if user else (401, b'{"message": "Bad credentials"}')
        raise AssertionError("unexpected provider request")

    def github_calls(self):
        return [call for call in self.calls if call[0] == GITHUB_USER]


# The schema 3 layout as deployed before list entries, frozen for forward-migration fixtures.
SCHEMA_3 = """
CREATE TABLE credentials (user_id INTEGER NOT NULL,route TEXT NOT NULL,version INTEGER NOT NULL,login TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('valid', 'invalid', 'revoked')),fingerprint TEXT NOT NULL,wrapped_key BLOB,
    nonce BLOB,ciphertext BLOB,created TEXT NOT NULL,updated TEXT NOT NULL,PRIMARY KEY (user_id, route, version));
CREATE TABLE credential_metadata (user_id INTEGER,route TEXT,version INTEGER,models TEXT,retired TEXT,kind TEXT,
    account_label TEXT,verified_account TEXT,status_reason TEXT,validated TEXT,expires_at TEXT,
    expiry_known INTEGER NOT NULL DEFAULT 0,token_revision INTEGER NOT NULL DEFAULT 1,last_used TEXT,
    key_id TEXT NOT NULL DEFAULT 'legacy',envelope_format INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(user_id,route,version));
CREATE TABLE credential_events (sequence INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,
    record_id TEXT NOT NULL,user_id INTEGER NOT NULL,route TEXT NOT NULL,version INTEGER NOT NULL,type TEXT NOT NULL,
    status TEXT NOT NULL,reason TEXT,at TEXT NOT NULL);
CREATE TABLE vault_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE credential_fences(user_id INTEGER NOT NULL,route TEXT NOT NULL,revision INTEGER NOT NULL,
    PRIMARY KEY(user_id,route));
INSERT INTO vault_meta VALUES('schema','3');
"""


def legacy_vault(path, envelope, records):
    """Write a schema 3 database holding `(user_id, route, version, secret, status, at)` records.

    A `None` secret is destroyed material, retired when its successor was saved. Events and fences match what schema 3 saves and revokes left.
    """
    db = sqlite3.connect(path)
    db.executescript(SCHEMA_3)
    replaced = {(user_id, route, version): at for user_id, route, version, _, _, at in records}
    for user_id, route, version, secret, status, at in records:
        sealed = envelope.seal(secret, vault.aad(user_id, route, version)) if secret else None
        db.execute(
            "INSERT INTO credentials VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                user_id,
                route,
                version,
                "owner",
                status,
                vault.fingerprint(secret or "destroyed"),
                *((sealed.wrapped_key, sealed.nonce, sealed.ciphertext) if sealed else (None, None, None)),
                at,
                at,
            ),
        )
        db.execute(
            "INSERT INTO credential_metadata(user_id,route,version,kind,status_reason,retired,key_id) VALUES(?,?,?,?,?,?,?)",
            (
                user_id,
                route,
                version,
                vault.ROUTES[route][1],
                "stored" if secret else "revoked" if status == "revoked" else "replaced",
                None if secret else replaced.get((user_id, route, version + 1), at),
                envelope.current,
            ),
        )
        db.execute(
            "INSERT INTO credential_events(id,record_id,user_id,route,version,type,status,reason,at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                f"event-{user_id}-{route}-{version}",
                vault.record_id(user_id, route, version),
                user_id,
                route,
                version,
                "stored",
                "valid",
                "stored",
                at,
            ),
        )
        db.execute(
            "INSERT INTO credential_fences VALUES(?,?,1) ON CONFLICT(user_id,route) DO UPDATE SET revision=revision+1",
            (user_id, route),
        )
    db.commit()
    db.close()


class VaultFixture:
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.now = 1000.0
        self.transport = FakeTransport()
        self.envelope = Envelope(os.urandom(32))
        self.store = Store(self.directory / "vault.db", self.envelope)
        self.addCleanup(self.store.close)
        self.identity = Identity(self.transport, clock=lambda: self.now)
        self.client = TestClient(create_app(self.store, self.identity))

    def request(self, method, path, token=ALICE, **kwargs):
        return self.client.request(method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs)

    def put(self, route, secret, token=ALICE, **fields):
        """Add a new entry through the owner API."""
        body = {"route": route, "name": "Test credential", "secret": secret, "mutation_id": uuid.uuid4().hex, **fields}
        return self.request("POST", "/credentials", token, json=body)

    def replace(self, entry, secret, token=ALICE, **fields):
        """Replace one entry's token at the revision last returned for it."""
        body = {"secret": secret, "revision": entry["revision"], "mutation_id": uuid.uuid4().hex, **fields}
        return self.request("PUT", f"/credentials/{entry['entry_id']}/secret", token, json=body)

    def remove(self, entry, token=ALICE):
        """Remove one entry at the revision last returned for it."""
        path = "/credentials/" + entry["entry_id"]
        return self.request("DELETE", path, token, params={"revision": entry["revision"]})

    def entries(self, token=ALICE):
        """The owner's entries across providers, in list order."""
        return [e for group in self.request("GET", "/credentials", token).json()["providers"] for e in group["entries"]]

    def rows(self):
        return [dict(row) for row in self.store.db.execute("SELECT * FROM credentials ORDER BY route, version")]

    @staticmethod
    def sealed(row):
        """One stored row's ciphertext, as the envelope opens it."""
        return Sealed(row["wrapped_key"], row["nonce"], row["ciphertext"])
