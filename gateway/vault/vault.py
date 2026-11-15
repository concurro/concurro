"""Concurro's credentials vault: per-user provider secrets, stored write-only.

Callers present a GitHub user token; the vault resolves the user itself through
`GET https://api.github.com/user`. Each secret is stored without a provider call and
sealed with a fresh data key that the master key wraps (envelope encryption, AES-256-GCM).
Public endpoints never return secrets. Only the separately authenticated gateway listener resolves exact versions.
"""

import argparse
import hashlib
import hmac
import json
import os
import socket
import sqlite3
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

GITHUB_USER = "https://api.github.com/user"
IDENTITY_TTL = 300
HTTP_TIMEOUT = 10
NONCE = 12

# GET `url` with `headers`, returning (status, body). Raises Unavailable when no response arrives.
Transport = Callable[[str, Mapping[str, str]], tuple[int, bytes]]


class VaultError(Exception):
    """A request the vault will not serve; `status` is its HTTP status."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


class Unavailable(VaultError):
    """GitHub or a provider gave no usable answer."""

    def __init__(self, reason: str):
        super().__init__(502, reason)


class ConfigError(Exception):
    """The vault cannot start with the configured files."""


def http_get(url: str, headers: Mapping[str, str]) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": "concurro-vault", **headers})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        raise Unavailable("upstream unreachable") from None


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def fingerprint(secret: str) -> str:
    return f"{secret[-4:]}:{digest(secret)[:8]}"


def private_file(path: str | Path) -> bytes:
    """Read an owner-only regular file without following a final symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise ConfigError("private key file is missing") from None
    except OSError:
        raise ConfigError("private key file cannot be opened") from None
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ConfigError("private key file must be owned by this process with mode 0400 or 0600 and be regular")
        return file.read()


def load_master_key(path: str | Path) -> bytes:
    """Read a separately provisioned 32-byte key, encoded as 64 hex characters."""
    try:
        key = bytes.fromhex(private_file(path).decode("ascii").strip())
    except (ValueError, UnicodeError):
        key = b""
    if len(key) != 32:
        raise ConfigError("master key file must hold 32 bytes as 64 hex characters")
    return key


def load_envelope(master_path: str | Path, keyring_path: str | Path | None = None):
    """Load a current wrapping key and retained decrypt keys; legacy installations need no migration file."""
    if not keyring_path:
        return Envelope(load_master_key(master_path))
    try:
        data = json.loads(private_file(keyring_path))
        if set(data) != {"active", "keys"} or not isinstance(data["keys"], dict):
            raise ValueError()
        if any(not isinstance(k, str) or not k or not isinstance(v, str) for k, v in data["keys"].items()):
            raise ValueError()
        keys = {name: load_master_key(path) for name, path in data["keys"].items()}
        active = data["active"]
        return Envelope(keys[active], active, keys)
    except (ValueError, KeyError, TypeError):
        raise ConfigError("invalid wrapping keyring") from None


def load_manifest(path: str | Path | None) -> dict:
    """Read the service's nonsecret owner/provider ordering manifest for the schema 4 migration."""
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text())
        if not isinstance(data, dict) or data.get("format") != 1 or not isinstance(data.get("owners"), dict):
            raise ValueError()
        if any(
            not isinstance(lists, dict) or any(not isinstance(preference, dict) for preference in lists.values())
            for lists in data["owners"].values()
        ):
            raise ValueError()
        return data
    except (OSError, ValueError):
        raise ConfigError("invalid credential order manifest") from None


def record_id(user_id: int, route: str, version: int) -> str:
    """The immutable opaque identity shared by metadata, events and exact-version resolution."""
    return digest(f"{user_id}/{route}/{version}")


def instant(value: str) -> str:
    """Normalize an aware timestamp without inventing expiry for opaque credentials."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(UTC).isoformat(timespec="seconds")
    except (ValueError, TypeError, AttributeError):
        raise VaultError(422, "invalid metadata timestamp") from None


@dataclass(frozen=True)
class User:
    id: int
    login: str


class Identity:
    """Resolves GitHub user tokens, caching each user for IDENTITY_TTL seconds by token digest."""

    def __init__(self, transport: Transport, clock: Callable[[], float] = time.monotonic):
        self.transport, self.clock = transport, clock
        self.cache: dict[str, tuple[float, User]] = {}
        self.lock = threading.Lock()

    def resolve(self, token: str) -> User:
        key, now = digest(token), self.clock()
        with self.lock:
            cached = self.cache.get(key)
            if cached and cached[0] > now:
                return cached[1]
        status, body = self.transport(
            GITHUB_USER,
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        if status == 401:
            raise VaultError(401, "invalid GitHub token")
        if status != 200:
            raise Unavailable(f"GitHub user lookup returned {status}")
        try:
            data = json.loads(body)
            user = User(int(data["id"]), str(data["login"]))
        except (ValueError, KeyError, TypeError):
            raise Unavailable("GitHub user lookup returned an unexpected body") from None
        with self.lock:
            self.cache = {k: v for k, v in self.cache.items() if v[0] > now}
            self.cache[key] = (now + IDENTITY_TTL, user)
        return user


@dataclass(frozen=True)
class Sealed:
    wrapped_key: bytes
    nonce: bytes
    ciphertext: bytes


class Envelope:
    """AES-256-GCM envelope encryption with named wrapping keys and unchanged record AAD."""

    def __init__(self, master: bytes, key_id: str = "legacy", keys: Mapping[str, bytes] | None = None):
        """Select a current key while retaining old keys only for opening/rewrapping existing rows."""
        self.current = key_id
        self.keys = {name: AESGCM(value) for name, value in (keys or {}).items()}
        self.keys[key_id] = AESGCM(master)
        self.master = self.keys[key_id]

    def seal(self, secret: str, aad: bytes) -> Sealed:
        """Encrypt fresh material with a random data key and wrap it under the current master."""
        data_key = AESGCM.generate_key(bit_length=256)
        nonce, wrap_nonce = os.urandom(NONCE), os.urandom(NONCE)
        ciphertext = AESGCM(data_key).encrypt(nonce, secret.encode(), aad)
        return Sealed(wrap_nonce + self.master.encrypt(wrap_nonce, data_key, aad), nonce, ciphertext)

    def open(self, sealed: Sealed, aad: bytes, key_id: str | None = None) -> str:
        """Open only under the row's recorded wrapping key and owner/route/version AAD."""
        wrap_nonce, wrapped = sealed.wrapped_key[:NONCE], sealed.wrapped_key[NONCE:]
        data_key = self.keys[key_id or self.current].decrypt(wrap_nonce, wrapped, aad)
        return AESGCM(data_key).decrypt(sealed.nonce, sealed.ciphertext, aad).decode()

    def rewrap(self, sealed: Sealed, aad: bytes, key_id: str) -> Sealed:
        """Rotate only the encrypted data key; token ciphertext and version do not change."""
        nonce = sealed.wrapped_key[:NONCE]
        data_key = self.keys[key_id].decrypt(nonce, sealed.wrapped_key[NONCE:], aad)
        new_nonce = os.urandom(NONCE)
        return Sealed(new_nonce + self.master.encrypt(new_nonce, data_key, aad), sealed.nonce, sealed.ciphertext)


def aad(user_id: int, route: str, version: int) -> bytes:
    return f"concurro-vault/{user_id}/{route}/{version}".encode()


SCHEMA = """
CREATE TABLE IF NOT EXISTS credentials (
    user_id INTEGER NOT NULL,
    route TEXT NOT NULL,
    version INTEGER NOT NULL,
    login TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('valid', 'invalid', 'revoked')),
    fingerprint TEXT NOT NULL,
    wrapped_key BLOB,
    nonce BLOB,
    ciphertext BLOB,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    PRIMARY KEY (user_id, route, version)
);
CREATE TABLE IF NOT EXISTS credential_metadata (user_id INTEGER,route TEXT,version INTEGER,models TEXT,retired TEXT,
    PRIMARY KEY(user_id,route,version));
CREATE TABLE IF NOT EXISTS credential_events (sequence INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,
    record_id TEXT NOT NULL,user_id INTEGER NOT NULL,route TEXT NOT NULL,version INTEGER NOT NULL,type TEXT NOT NULL,
    status TEXT NOT NULL,reason TEXT,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vault_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS credential_entries (entry_id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,provider TEXT NOT NULL,
    route TEXT NOT NULL,name TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,roles TEXT NOT NULL DEFAULT '[]',
    profiles TEXT NOT NULL DEFAULT '[]',revision INTEGER NOT NULL,created TEXT NOT NULL,updated TEXT NOT NULL,removed TEXT);
CREATE TABLE IF NOT EXISTS credential_lists (user_id INTEGER NOT NULL,provider TEXT NOT NULL,revision INTEGER NOT NULL,
    entries TEXT NOT NULL,PRIMARY KEY(user_id,provider));
CREATE TABLE IF NOT EXISTS credential_policy (user_id INTEGER NOT NULL,provider TEXT NOT NULL,revision INTEGER NOT NULL,
    at TEXT NOT NULL,document TEXT NOT NULL,PRIMARY KEY(user_id,provider,revision));
CREATE TABLE IF NOT EXISTS credential_mutations (user_id INTEGER NOT NULL,id TEXT NOT NULL,digest TEXT NOT NULL,
    response TEXT NOT NULL,PRIMARY KEY(user_id,id));
"""

# Columns added since a table was first created.
COLUMNS = {
    "credential_metadata": (
        ("kind", "TEXT"),
        ("account_label", "TEXT"),
        ("verified_account", "TEXT"),
        ("status_reason", "TEXT"),
        ("validated", "TEXT"),
        ("expires_at", "TEXT"),
        ("expiry_known", "INTEGER NOT NULL DEFAULT 0"),
        ("token_revision", "INTEGER NOT NULL DEFAULT 1"),
        ("last_used", "TEXT"),
        ("key_id", "TEXT NOT NULL DEFAULT 'legacy'"),
        ("envelope_format", "INTEGER NOT NULL DEFAULT 1"),
        ("entry_id", "TEXT"),
    ),
    "credential_events": (("entry_id", "TEXT"), ("provider", "TEXT"), ("revision", "INTEGER")),
}

# Route -> (provider, kind, default entry name).
ROUTES = {
    "openai-api-key": ("openai", "api_key", "OpenAI API key"),
    "anthropic-api-key": ("anthropic", "api_key", "Anthropic API key"),
    "chatgpt-subscription": ("openai", "subscription", "ChatGPT subscription"),
    "claude-subscription": ("anthropic", "subscription", "Claude subscription"),
}
PROVIDERS = tuple(sorted({provider for provider, *_ in ROUTES.values()}))
KINDS = ("subscription", "api_key")
CONFLICT = "credential changed; refresh before retrying"


class Store:
    """Ordered per-provider entries, their versioned encrypted records and atomic nonsecret lifecycle events."""

    def __init__(
        self,
        path: str | Path,
        envelope: Envelope,
        clock: Callable[[], float] = time.time,
        manifest: Callable[[], Mapping] = dict,
    ):
        """Migrate forward without guessing legacy validation or retirement evidence.

        `manifest` is read only to seed each owner's list order while schema 3 records are adopted.
        """
        self.envelope, self.clock = envelope, clock
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA secure_delete=ON")
        self.lock = threading.RLock()
        self.migration: list[dict] | None = None
        with self.transaction():
            for statement in SCHEMA.split(";"):
                self.db.execute(statement)
            for table, additions in COLUMNS.items():
                columns = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
                for name, definition in additions:
                    if name not in columns:
                        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            schema = self.db.execute("SELECT value FROM vault_meta WHERE key='schema'").fetchone()
            if schema and schema[0] not in ("2", "3", "4"):
                raise ConfigError("unsupported vault schema")
            if schema is None:
                for row in self.db.execute("SELECT * FROM credentials ORDER BY user_id,route,version").fetchall():
                    self.event(
                        row["user_id"],
                        row["route"],
                        row["version"],
                        "snapshot",
                        row["status"] if row["ciphertext"] is not None else "retired",
                        "legacy_snapshot",
                        "migration:" + record_id(row["user_id"], row["route"], row["version"]),
                    )
            if schema is None or schema[0] != "4":
                self.migration = self.adopt(manifest())
            self.db.execute(
                "INSERT INTO vault_meta VALUES('schema','4') ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )

    def adopt(self, manifest: Mapping) -> list[dict]:
        """Give each legacy owner/route one entry holding all its versions; emits no events and saves nothing."""
        self.db.execute(
            "INSERT OR IGNORE INTO credential_metadata(user_id,route,version) SELECT user_id,route,version FROM credentials"
        )
        fences = {}
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='credential_fences'").fetchone():
            fences = {
                (r[0], r[1]): r[2] for r in self.db.execute("SELECT user_id,route,revision FROM credential_fences")
            }
        members: dict[tuple[int, str], list[dict]] = {}
        for row in self.db.execute(
            "SELECT c.user_id,c.route,c.status,c.updated,(SELECT MIN(created) FROM credentials f WHERE f.user_id=c.user_id"
            " AND f.route=c.route) AS created FROM credentials c WHERE c.version=(SELECT MAX(version) FROM credentials l"
            " WHERE l.user_id=c.user_id AND l.route=c.route) ORDER BY c.user_id,c.route"
        ).fetchall():
            user_id, route = row["user_id"], row["route"]
            provider, kind, name = ROUTES[route]
            entry_id = digest(f"entry/{user_id}/{route}")[:32]
            removed = row["updated"] if row["status"] == "revoked" else None
            self.db.execute(
                "INSERT INTO credential_entries VALUES(?,?,?,?,?,1,'[]','[]',?,?,?,?)",
                (
                    entry_id,
                    user_id,
                    provider,
                    route,
                    name,
                    max(1, fences.get((user_id, route), 0)),
                    row["created"],
                    row["updated"],
                    removed,
                ),
            )
            for table in ("credential_metadata", "credential_events"):
                self.db.execute(
                    f"UPDATE {table} SET entry_id=? WHERE user_id=? AND route=?", (entry_id, user_id, route)
                )
            self.db.execute(
                "UPDATE credential_metadata SET kind=COALESCE(kind,?) WHERE user_id=? AND route=?",
                (kind, user_id, route),
            )
            self.db.execute(
                "UPDATE credential_events SET provider=? WHERE user_id=? AND route=?", (provider, user_id, route)
            )
            members.setdefault((user_id, provider), []).append(
                {"entry_id": entry_id, "route": route, "kind": kind, "removed": removed}
            )
        summary = []
        for (user_id, provider), entries in members.items():
            preference = manifest.get("owners", {}).get(str(user_id), {}).get(provider, {})
            first = preference.get("first") if preference.get("first") in KINDS else None
            entries.sort(key=lambda e: (e["kind"] != (first or KINDS[0]), e["route"]))
            # Revision 0 is the legacy boundary: every adopted entry, for as-of reads before this migration.
            self.snapshot(user_id, provider, 0, "", [e["entry_id"] for e in entries])
            live = [e["entry_id"] for e in entries if not e["removed"]]
            self.db.execute("INSERT INTO credential_lists VALUES(?,?,1,?)", (user_id, provider, json.dumps(live)))
            self.snapshot(user_id, provider, 1, self.now(), live)
            summary.append(
                {
                    "user_id": str(user_id),
                    "provider": provider,
                    "order": [e["route"] for e in entries if not e["removed"]],
                    "source": "manifest" if first else "default",
                    "evidence": preference.get("evidence", "unavailable"),
                }
            )
        return summary

    def now(self) -> str:
        """A UTC instant from the injected clock."""
        return datetime.fromtimestamp(self.clock(), UTC).isoformat(timespec="seconds")

    def close(self) -> None:
        """Close this store's connection."""
        self.db.close()

    def event(self, user_id, route, version, kind, status, reason=None, identifier=None, **policy):
        """Append an immutable secret-free event in the caller's write transaction.

        Record events name their entry; `policy` supplies entry, provider and list revision for selection events.
        """
        if version:
            row = self.db.execute(
                "SELECT entry_id FROM credential_metadata WHERE user_id=? AND route=? AND version=?",
                (user_id, route, version),
            ).fetchone()
            policy = {"entry_id": row["entry_id"] if row else None, "provider": ROUTES[route][0]}
        self.db.execute(
            "INSERT OR IGNORE INTO credential_events(id,record_id,user_id,route,version,type,status,reason,at,"
            "entry_id,provider,revision) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                identifier or uuid.uuid4().hex,
                record_id(user_id, route, version) if version else "",
                user_id,
                route,
                version,
                kind,
                status,
                reason,
                self.now(),
                policy.get("entry_id"),
                policy.get("provider"),
                policy.get("revision"),
            ),
        )

    def events(self, after=0):
        """Read a replayable, paginated event cursor; consumers own their acknowledgment."""
        if type(after) is not int or not 0 <= after <= 2**63 - 1:
            raise VaultError(422, "invalid event cursor")
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM credential_events WHERE sequence>? ORDER BY sequence LIMIT 101", (after,)
            ).fetchall()
        values = [dict(row) for row in rows[:100]]
        return {"events": values, "next": values[-1]["sequence"] if values else after, "more": len(rows) > 100}

    def order(self, user_id: int, provider: str) -> tuple[int, list[str]]:
        """One provider list's revision and live entry IDs in priority order."""
        row = self.db.execute(
            "SELECT revision,entries FROM credential_lists WHERE user_id=? AND provider=?", (user_id, provider)
        ).fetchone()
        return (row["revision"], json.loads(row["entries"])) if row else (0, [])

    def snapshot(self, user_id: int, provider: str, revision: int, at: str, entries: list[str]) -> None:
        """Record the selection policy a list revision carries, for as-of reads."""
        rows = {
            row["entry_id"]: row
            for row in self.db.execute(
                "SELECT * FROM credential_entries WHERE user_id=? AND provider=?", (user_id, provider)
            )
        }
        document = [
            {
                "entry_id": entry_id,
                "name": rows[entry_id]["name"],
                "enabled": bool(rows[entry_id]["enabled"]),
                "roles": json.loads(rows[entry_id]["roles"]),
                "profiles": json.loads(rows[entry_id]["profiles"]),
            }
            for entry_id in entries
        ]
        self.db.execute(
            "INSERT INTO credential_policy VALUES(?,?,?,?,?)",
            (user_id, provider, revision, at, json.dumps(document, separators=(",", ":"))),
        )

    def arrange(self, user_id: int, provider: str, entries: list[str], reason: str, entry_id: str | None = None):
        """Commit a list revision with its policy snapshot and selection event in the caller's transaction."""
        revision = self.order(user_id, provider)[0] + 1
        self.db.execute(
            "INSERT INTO credential_lists VALUES(?,?,?,?) ON CONFLICT(user_id,provider) DO UPDATE SET"
            " revision=excluded.revision,entries=excluded.entries",
            (user_id, provider, revision, json.dumps(entries)),
        )
        self.snapshot(user_id, provider, revision, self.now(), entries)
        self.event(user_id, "", 0, "policy", "policy", reason, entry_id=entry_id, provider=provider, revision=revision)

    def owned(self, user_id: int, entry_id: str, revision: int | None = None) -> sqlite3.Row:
        """This owner's live entry; a tombstone or a stale revision refuses the write."""
        row = self.db.execute(
            "SELECT * FROM credential_entries WHERE entry_id=? AND user_id=?", (entry_id, user_id)
        ).fetchone()
        if row is None:
            raise VaultError(404, "no such credential")
        if row["removed"]:
            raise VaultError(410, "credential was removed")
        if revision is not None and row["revision"] != revision:
            raise VaultError(409, CONFLICT)
        return row

    def route(self, user_id: int, entry_id: str) -> str:
        """The fixed route of one of this owner's entries, removed or not."""
        with self.lock:
            row = self.db.execute(
                "SELECT route FROM credential_entries WHERE entry_id=? AND user_id=?", (entry_id, user_id)
            ).fetchone()
        if row is None:
            raise VaultError(404, "no such credential")
        return row["route"]

    def once(self, user_id: int, mutation: str | None, request: dict, action: Callable[[], dict]) -> dict:
        """Apply an owner mutation once; a replay of the same request returns the first result."""
        if mutation is None:
            return action()
        requested = digest(json.dumps(request, sort_keys=True))
        row = self.db.execute(
            "SELECT digest,response FROM credential_mutations WHERE user_id=? AND id=?", (user_id, mutation)
        ).fetchone()
        if row:
            if row["digest"] != requested:
                raise VaultError(409, "mutation ID was already used for a different request")
            return json.loads(row["response"])
        result = action()
        self.db.execute(
            "INSERT INTO credential_mutations VALUES(?,?,?,?)", (user_id, mutation, requested, json.dumps(result))
        )
        return result

    def seal(self, user: User, entry: sqlite3.Row, material: dict) -> None:
        """Store this entry's next version without provider validation, retiring only its own prior material."""
        secret, account = material["secret"], material.get("account")
        expires_at, expiry_known = material.get("expires_at"), material.get("expiry_known", False)
        if expiry_known and expires_at is None or expires_at is not None and not expiry_known:
            raise VaultError(422, "expiry requires trusted evidence")
        expiry = instant(expires_at) if expires_at else None
        if expiry and datetime.fromisoformat(expiry).timestamp() <= self.clock():
            raise VaultError(422, "credential is already expired")
        now, route = self.now(), entry["route"]
        # Versions stay monotonic per owner/route across entries: record identities and AAD depend on it.
        version = self.db.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM credentials WHERE user_id=? AND route=?", (user.id, route)
        ).fetchone()[0]
        payload = json.dumps({"secret": secret, "account": account}, separators=(",", ":")) if account else secret
        sealed = self.envelope.seal(payload, aad(user.id, route, version))
        self.destroy(entry["entry_id"], "replaced")
        self.db.execute(
            "INSERT INTO credentials VALUES(?,?,?,?,'valid',?,?,?,?,?,?)",
            (
                user.id,
                route,
                version,
                user.login,
                fingerprint(secret),
                sealed.wrapped_key,
                sealed.nonce,
                sealed.ciphertext,
                now,
                now,
            ),
        )
        self.db.execute(
            "INSERT INTO credential_metadata(user_id,route,version,kind,account_label,verified_account,"
            "status_reason,expires_at,expiry_known,key_id,envelope_format,entry_id)"
            " VALUES(?,?,?,?,?,?,'stored',?,?,?,?,?)",
            (
                user.id,
                route,
                version,
                ROUTES[route][1],
                material.get("account_label"),
                material.get("verified_account"),
                expiry,
                int(expiry_known),
                self.envelope.current,
                2 if account else 1,
                entry["entry_id"],
            ),
        )
        self.event(user.id, route, version, "stored", "valid", "stored")

    def touch(self, entry: sqlite3.Row, reason: str, entries: list[str] | None = None, **changes) -> dict:
        """Advance an entry's revision with its list snapshot, applying `changes` to its policy columns."""
        user_id, provider, entry_id = entry["user_id"], entry["provider"], entry["entry_id"]
        assignments = "".join(f",{column}=?" for column in changes)
        self.db.execute(
            f"UPDATE credential_entries SET revision=revision+1,updated=?{assignments} WHERE entry_id=?",
            (self.now(), *changes.values(), entry_id),
        )
        self.arrange(
            user_id, provider, self.order(user_id, provider)[1] if entries is None else entries, reason, entry_id
        )
        return self.view(self.db.execute("SELECT * FROM credential_entries WHERE entry_id=?", (entry_id,)).fetchone())

    def add(
        self,
        user: User,
        route: str,
        secret: str,
        *,
        name: str | None = None,
        roles: list[str] | None = None,
        profiles: list[dict] | None = None,
        mutation: str | None = None,
        **material,
    ) -> dict:
        """Append a new enabled entry holding `secret` to its provider's list."""
        if route not in ROUTES:
            raise VaultError(422, "unsupported credential kind")
        provider, _, default = ROUTES[route]
        policy = {"name": name or default, "roles": roles or [], "profiles": profiles or []}

        def action():
            entry_id, now = uuid.uuid4().hex, self.now()
            self.db.execute(
                "INSERT INTO credential_entries VALUES(?,?,?,?,?,1,?,?,0,?,?,NULL)",
                (
                    entry_id,
                    user.id,
                    provider,
                    route,
                    policy["name"],
                    json.dumps(policy["roles"]),
                    json.dumps(policy["profiles"]),
                    now,
                    now,
                ),
            )
            entry = self.owned(user.id, entry_id)
            self.seal(user, entry, {"secret": secret, **material})
            return self.touch(entry, "added", [*self.order(user.id, provider)[1], entry_id])

        request = {"add": route, "secret": secret, **policy, **material}
        with self.lock, self.transaction():
            return self.once(user.id, mutation, request, action)

    def replace(
        self,
        user: User,
        entry_id: str,
        secret: str,
        *,
        revision: int | None = None,
        mutation: str | None = None,
        **material,
    ) -> dict:
        """Atomically give one entry a new token/account version; its policy and peers are untouched."""

        def action():
            entry = self.owned(user.id, entry_id, revision)
            self.seal(user, entry, {"secret": secret, **material})
            return self.touch(entry, "replaced")

        request = {"replace": entry_id, "revision": revision, "secret": secret, **material}
        with self.lock, self.transaction():
            return self.once(user.id, mutation, request, action)

    def edit(self, user_id: int, entry_id: str, *, revision: int | None = None, mutation: str | None = None, **policy):
        """Change name, enabled or filters; stored material and versions are untouched."""
        changes = {
            key: json.dumps(value) if key in ("roles", "profiles") else int(value) if key == "enabled" else value
            for key, value in policy.items()
            if key in ("name", "enabled", "roles", "profiles") and value is not None
        }

        def action():
            entry = self.owned(user_id, entry_id, revision)
            toggled = "enabled" in changes and changes["enabled"] != entry["enabled"]
            reason = ("enabled" if changes["enabled"] else "disabled") if toggled else "edited"
            return self.touch(entry, reason, **changes)

        request = {"edit": entry_id, "revision": revision, **changes}
        with self.lock, self.transaction():
            return self.once(user_id, mutation, request, action)

    def reorder(self, user_id: int, provider: str, entries: list[str], *, revision: int, mutation: str | None = None):
        """Commit an exact new priority order of the provider's current entries."""
        if provider not in PROVIDERS:
            raise VaultError(404, "unknown provider")

        def action():
            current, existing = self.order(user_id, provider)
            if current != revision or sorted(entries) != sorted(existing):
                raise VaultError(409, CONFLICT)
            self.arrange(user_id, provider, entries, "reordered")
            return self.group(user_id, provider)

        request = {"reorder": provider, "revision": revision, "entries": entries}
        with self.lock, self.transaction():
            return self.once(user_id, mutation, request, action)

    def remove(self, user_id: int, entry_id: str, revision: int) -> dict:
        """Tombstone an entry and revoke only its material; repeating the removal changes nothing."""
        with self.lock, self.transaction():
            try:
                entry = self.owned(user_id, entry_id, revision)
            except VaultError as error:
                if error.status != 410:
                    raise
                return self.view(
                    self.db.execute("SELECT * FROM credential_entries WHERE entry_id=?", (entry_id,)).fetchone()
                )
            route = entry["route"]
            self.destroy(entry_id, "revoked")
            version = self.db.execute(
                "SELECT MAX(version) FROM credential_metadata WHERE entry_id=?", (entry_id,)
            ).fetchone()[0]
            self.db.execute(
                "UPDATE credentials SET status='revoked',updated=? WHERE user_id=? AND route=? AND version=?",
                (self.now(), user_id, route, version),
            )
            self.db.execute(
                "UPDATE credential_metadata SET status_reason='revoked' WHERE user_id=? AND route=? AND version=?",
                (user_id, route, version),
            )
            self.event(user_id, route, version, "revoked", "revoked", "revoked")
            remaining = [e for e in self.order(user_id, entry["provider"])[1] if e != entry_id]
            return self.touch(entry, "removed", remaining, removed=self.now())

    def destroy(self, entry_id: str, reason="revoked") -> None:
        """Retire one entry's usable versions before deleting their sealed material, within the caller's transaction."""
        for row in self.db.execute(
            "SELECT c.user_id,c.route,c.version FROM credentials c JOIN credential_metadata m ON c.user_id=m.user_id"
            " AND c.route=m.route AND c.version=m.version WHERE m.entry_id=? AND c.ciphertext IS NOT NULL",
            (entry_id,),
        ).fetchall():
            key = (row["user_id"], row["route"], row["version"])
            self.db.execute(
                "UPDATE credential_metadata SET retired=COALESCE(retired,?),status_reason=?"
                " WHERE user_id=? AND route=? AND version=?",
                (self.now(), reason, *key),
            )
            if reason == "replaced":
                self.event(*key, "retired", "retired", reason)
            self.db.execute(
                "UPDATE credentials SET wrapped_key=NULL,nonce=NULL,ciphertext=NULL WHERE user_id=? AND route=? AND version=?",
                key,
            )

    def view(self, entry: sqlite3.Row) -> dict:
        """Owner-facing entry policy with its current record's nonsecret metadata."""
        entries = self.order(entry["user_id"], entry["provider"])[1]
        version = self.db.execute(
            "SELECT MAX(version) FROM credential_metadata WHERE entry_id=?", (entry["entry_id"],)
        ).fetchone()[0]
        value = {
            key: entry[key]
            for key in ("entry_id", "provider", "route", "name", "revision", "created", "updated", "removed")
        }
        value.update(
            kind=ROUTES[entry["route"]][1],
            enabled=bool(entry["enabled"]),
            roles=json.loads(entry["roles"]),
            profiles=json.loads(entry["profiles"]),
            position=entries.index(entry["entry_id"]) if entry["entry_id"] in entries else None,
            record=self.record(entry["user_id"], entry["route"], version),
        )
        return value

    def group(self, user_id: int, provider: str) -> dict:
        """One provider's list revision and entries in priority order."""
        revision, entries = self.order(user_id, provider)
        rows = {
            row["entry_id"]: row
            for row in self.db.execute(
                "SELECT * FROM credential_entries WHERE user_id=? AND provider=?", (user_id, provider)
            )
        }
        return {"provider": provider, "revision": revision, "entries": [self.view(rows[e]) for e in entries]}

    def listing(self, user_id: int) -> dict:
        """The owner's ordered provider lists and the types a new entry may take; never sealed columns."""
        with self.lock:
            return {
                "providers": [self.group(user_id, provider) for provider in PROVIDERS],
                "types": [
                    {"route": r, "provider": p, "kind": k, "label": label} for r, (p, k, label) in ROUTES.items()
                ],
            }

    def policy(self, user_id: int, cutoff: datetime) -> dict[str, dict]:
        """Each provider's list revision and entry policy in force at `cutoff`."""
        chosen = {}
        for row in self.db.execute(
            "SELECT provider,revision,at,document FROM credential_policy WHERE user_id=? ORDER BY revision", (user_id,)
        ):
            if not row["at"] or datetime.fromisoformat(row["at"]) <= cutoff:
                chosen[row["provider"]] = {"revision": row["revision"], "entries": json.loads(row["document"])}
        return chosen

    def metadata(self, user_id: int, at: str | None = None) -> dict:
        """Private nonsecret status of each entry's current version with its list policy, now or as of `at`.

        Unknown legacy evidence stays unknown. List revision 0 marks a boundary before entries existed.
        """
        cutoff = datetime.fromisoformat(instant(at)) if at else datetime.fromtimestamp(self.clock(), UTC)
        with self.lock:
            rows = self.db.execute(
                "SELECT c.user_id,c.route,c.version,c.status,c.created,c.updated,m.models,m.retired,m.entry_id,"
                "c.ciphertext IS NOT NULL AS usable,m.kind,m.status_reason,m.validated,m.expires_at,m.expiry_known,"
                "m.token_revision FROM credentials c JOIN credential_metadata m "
                "ON c.user_id=m.user_id AND c.route=m.route AND c.version=m.version "
                "WHERE c.user_id=? ORDER BY c.version",
                (user_id,),
            ).fetchall()
            lists = self.policy(user_id, cutoff)
            entries = {
                row["entry_id"]: row
                for row in self.db.execute("SELECT * FROM credential_entries WHERE user_id=?", (user_id,))
            }
        latest = {}
        for row in rows:
            if datetime.fromisoformat(row["created"].replace("Z", "+00:00")) > cutoff:
                continue
            status = row["status"]
            if at:
                if row["retired"]:
                    status = (
                        "valid" if cutoff < datetime.fromisoformat(row["retired"].replace("Z", "+00:00")) else "revoked"
                    )
                elif not row["usable"]:
                    status = "unknown"
            elif not row["usable"]:
                status = "revoked" if status == "revoked" else "invalid"
            if row["expiry_known"] and row["expires_at"] and cutoff >= datetime.fromisoformat(row["expires_at"]):
                status = "invalid"
            entry = entries[row["entry_id"]]
            listed = lists.get(entry["provider"], {}).get("entries", [])
            position = next((i for i, e in enumerate(listed) if e["entry_id"] == row["entry_id"]), None)
            policy = (
                listed[position]
                if position is not None
                else {
                    "name": entry["name"],
                    "enabled": False,
                    "roles": json.loads(entry["roles"]),
                    "profiles": json.loads(entry["profiles"]),
                }
            )
            latest[row["entry_id"]] = {
                "record_id": record_id(user_id, row["route"], row["version"]),
                "user_id": str(user_id),
                "route": row["route"],
                "version": row["version"],
                "status": status,
                "models": json.loads(row["models"]) if row["models"] else None,
                "created": row["created"],
                "updated": row["updated"],
                "kind": row["kind"],
                "status_reason": row["status_reason"],
                "validated": row["validated"],
                "expires_at": row["expires_at"] if row["expiry_known"] else None,
                "expiry_known": bool(row["expiry_known"]),
                "token_revision": row["token_revision"],
                "provider": entry["provider"],
                **policy,
                "entry_id": row["entry_id"],
                "position": position,
                "removed": position is None,
            }
        return {
            "history_complete": all(r["status"] != "unknown" for r in latest.values()),
            "credentials": list(latest.values()),
            "lists": {
                provider: {"revision": value["revision"], "entries": [e["entry_id"] for e in value["entries"]]}
                for provider, value in lists.items()
            },
        }

    def record(self, user_id: int, route: str, version: int) -> dict[str, object]:
        """Public owner metadata of one version; account headers remain inside the sealed payload."""
        row = self.db.execute(
            "SELECT c.route,c.status,c.fingerprint,c.version,c.updated,m.kind,m.status_reason,m.validated,m.expires_at,"
            "m.expiry_known,m.last_used,m.account_label,m.verified_account,m.models "
            "FROM credentials c LEFT JOIN credential_metadata m ON c.user_id=m.user_id AND c.route=m.route AND c.version=m.version"
            " WHERE c.user_id=? AND c.route=? AND c.version=?",
            (user_id, route, version),
        ).fetchone()
        value = dict(row)
        value["record_id"] = record_id(user_id, route, version)
        value["expiry_known"] = bool(value["expiry_known"])
        value["models"] = json.loads(value["models"]) if value["models"] else None
        if (
            value["expiry_known"]
            and value["expires_at"]
            and self.clock() >= datetime.fromisoformat(value["expires_at"]).timestamp()
        ):
            value["status"], value["status_reason"] = "invalid", "expired"
        return value

    def resolve(self, user_id: int, route: str, identifier: str, version: int) -> dict:
        """Return one exact version only to the gateway listener; no selection or fallback occurs here."""
        if identifier != record_id(user_id, route, version) or route not in ROUTES:
            raise VaultError(404, "credential not found")
        result = None
        with self.lock, self.transaction():
            row = self.db.execute(
                "SELECT c.*,m.key_id,m.expires_at,m.expiry_known,m.envelope_format,m.token_revision "
                "FROM credentials c JOIN credential_metadata m ON c.user_id=m.user_id AND c.route=m.route AND c.version=m.version "
                "WHERE c.user_id=? AND c.route=? AND c.version=?",
                (user_id, route, version),
            ).fetchone()
            if row is None:
                raise VaultError(404, "credential not found")
            if row["status"] == "valid" and row["ciphertext"] is not None:
                expired = (
                    row["expiry_known"]
                    and row["expires_at"]
                    and self.clock() >= datetime.fromisoformat(row["expires_at"]).timestamp()
                )
                if expired:
                    self.db.execute(
                        "UPDATE credentials SET status='invalid',wrapped_key=NULL,nonce=NULL,ciphertext=NULL,updated=?"
                        " WHERE user_id=? AND route=? AND version=?",
                        (self.now(), user_id, route, version),
                    )
                    self.db.execute(
                        "UPDATE credential_metadata SET status_reason='expired',retired=COALESCE(retired,?)"
                        " WHERE user_id=? AND route=? AND version=?",
                        (self.now(), user_id, route, version),
                    )
                    self.event(user_id, route, version, "unusable", "invalid", "expired")
                else:
                    try:
                        material = self.envelope.open(
                            Sealed(row["wrapped_key"], row["nonce"], row["ciphertext"]),
                            aad(user_id, route, version),
                            row["key_id"],
                        )
                        payload = (
                            json.loads(material)
                            if row["envelope_format"] == 2
                            else {"secret": material, "account": None}
                        )
                    except (ValueError, UnicodeError, InvalidTag, KeyError):
                        raise VaultError(503, "credential material unavailable") from None
                    result = {
                        "record_id": identifier,
                        "user_id": str(user_id),
                        "route": route,
                        "version": version,
                        "token_revision": row["token_revision"],
                        "secret": payload["secret"],
                        "account": payload.get("account"),
                        "expires_at": row["expires_at"] if row["expiry_known"] else None,
                    }
                    self.db.execute(
                        "UPDATE credential_metadata SET last_used=? WHERE user_id=? AND route=? AND version=?",
                        (self.now(), user_id, route, version),
                    )
        if result is None:
            raise VaultError(410, "credential unavailable")
        return result

    def rotate(self, envelope: Envelope) -> int:
        """Atomically rewrap data keys; preserve token bytes, versions, validation and retirement evidence."""
        changed = 0
        with self.lock:
            with self.transaction():
                rows = self.db.execute(
                    "SELECT c.user_id,c.route,c.version,c.wrapped_key,c.nonce,c.ciphertext,m.key_id FROM credentials c "
                    "JOIN credential_metadata m ON c.user_id=m.user_id AND c.route=m.route AND c.version=m.version "
                    "WHERE c.ciphertext IS NOT NULL",
                ).fetchall()
                for row in rows:
                    if row["key_id"] == envelope.current:
                        continue
                    try:
                        sealed = envelope.rewrap(
                            Sealed(row["wrapped_key"], row["nonce"], row["ciphertext"]),
                            aad(row["user_id"], row["route"], row["version"]),
                            row["key_id"],
                        )
                    except (InvalidTag, KeyError):
                        raise ConfigError("wrapping key rotation unavailable") from None
                    self.db.execute(
                        "UPDATE credentials SET wrapped_key=? WHERE user_id=? AND route=? AND version=?",
                        (sealed.wrapped_key, row["user_id"], row["route"], row["version"]),
                    )
                    self.db.execute(
                        "UPDATE credential_metadata SET key_id=? WHERE user_id=? AND route=? AND version=?",
                        (envelope.current, row["user_id"], row["route"], row["version"]),
                    )
                    self.event(row["user_id"], row["route"], row["version"], "rewrapped", "valid", "key_rotation")
                    changed += 1
            self.envelope = envelope
        return changed

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Commit lifecycle events and material/status changes together, or roll everything back."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")


class SecretBody(BaseModel):
    """An owner-supplied candidate; no owner, URL, refresh or trusted-expiry fields."""

    secret: str
    account_id: str | None = Field(default=None, description="Candidate ChatGPT account header, not verified identity.")


Mutation = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{16,64}$", description="Owner-chosen replay identity.")]
Name = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[^\x00-\x1f\x7f]*\S[^\x00-\x1f\x7f]*$")]
Roles = Annotated[list[Annotated[str, Field(pattern=r"^[a-z_]{1,32}$")]], Field(max_length=16)]
Revision = Annotated[int, Field(ge=0, strict=True)]


class ProfileRef(BaseModel):
    """One target's profile key; the canonical upstream repository keeps equal keys on other targets apart."""

    target: str = Field(pattern=r"^[\w.-]{1,100}/[\w.-]{1,100}$")
    profile: str = Field(min_length=1, max_length=100, pattern=r"^[\x21-\x7e]+$")


Profiles = Annotated[list[ProfileRef], Field(max_length=64)]


class AddBody(SecretBody):
    """A new list entry; empty filters match all of the provider's work."""

    route: str
    name: Name
    roles: Roles = []
    profiles: Profiles = []
    mutation_id: Mutation


class EditBody(BaseModel):
    """Policy changes to one entry at the revision the owner last read."""

    revision: Revision
    name: Name | None = None
    enabled: bool | None = None
    roles: Roles | None = None
    profiles: Profiles | None = None
    mutation_id: Mutation


class ReplaceBody(SecretBody):
    """A replacement token/account for one entry at the revision the owner last read."""

    revision: Revision
    mutation_id: Mutation


class OrderBody(BaseModel):
    """The provider list's exact new order at the list revision the owner last read."""

    revision: Revision
    entries: list[Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]]
    mutation_id: Mutation


def candidate(route: str, body: SecretBody) -> dict:
    """Check a supplied token and its route's account header; the pair is sealed as one version."""
    secret = body.secret.strip()
    # Keys go into request headers: only printable ASCII without whitespace is a plausible key.
    if not secret or not all(33 <= ord(c) <= 126 for c in secret):
        raise VaultError(422, "secret is empty or has unsupported characters")
    if route != "chatgpt-subscription":
        if body.account_id is not None:
            raise VaultError(422, "this route does not use an account ID")
        return {"secret": secret}
    if not body.account_id or not all(33 <= ord(c) <= 126 for c in body.account_id):
        raise VaultError(422, "ChatGPT account ID is required and must be a valid header value")
    return {
        "secret": secret,
        "account": {"chatgpt_account_id": body.account_id},
        "account_label": "Supplied ChatGPT account " + digest(body.account_id)[:8],
    }


def distinct(values: list) -> list:
    """Drop repeated filter selections, keeping first-seen order."""
    return [value for index, value in enumerate(values) if value not in values[:index]]


def create_app(
    store: Store,
    identity: Identity,
    service_token: str | None = None,
    private_tokens: tuple[str, ...] = (),
) -> FastAPI:
    app = FastAPI(title="Concurro credentials vault", docs_url=None, redoc_url=None, openapi_url=None)

    protect(app)

    def user(authorization: Annotated[str | None, Header()] = None) -> User:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise VaultError(401, "missing bearer token")
        if any(
            hmac.compare_digest(token.strip().encode(), value.encode())
            for value in (service_token, *private_tokens)
            if value
        ):
            raise VaultError(401, "private credential cannot act as a user")
        return identity.resolve(token.strip())

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    def service(authorization: Annotated[str | None, Header()] = None):
        """Authenticate nonsecret metadata/events without enabling the separate resolver principal."""
        if not service_token or not hmac.compare_digest(
            (authorization or "").encode(), ("Bearer " + service_token).encode()
        ):
            raise VaultError(401, "invalid service credential")

    @app.get("/metadata/events", dependencies=[Depends(service)])
    def metadata_events(after: int = 0):
        """Expose the existing immutable nonsecret feed to the service scheduler only."""
        return store.events(after)

    @app.get("/metadata/{user_id}")
    def metadata(user_id: int, at: str | None = None, authorization: Annotated[str | None, Header()] = None) -> dict:
        """Read nonsecret metadata only with the separately configured service bearer."""
        service(authorization)
        if user_id < 1:
            raise VaultError(422, "invalid user identity")
        return store.metadata(user_id, at)

    @app.get("/credentials")
    def list_credentials(caller: Annotated[User, Depends(user)]) -> dict:
        return store.listing(caller.id)

    @app.post("/credentials")
    def add_credential(body: AddBody, caller: Annotated[User, Depends(user)]) -> dict:
        if body.route not in ROUTES:
            raise VaultError(422, "unsupported credential kind")
        secret = candidate(body.route, body)
        return store.add(
            caller,
            body.route,
            secret.pop("secret"),
            name=body.name.strip(),
            roles=distinct(body.roles),
            profiles=distinct([profile.model_dump() for profile in body.profiles]),
            mutation=body.mutation_id,
            **secret,
        )

    @app.put("/credentials/order/{provider}")
    def reorder_credentials(provider: str, body: OrderBody, caller: Annotated[User, Depends(user)]) -> dict:
        return store.reorder(caller.id, provider, body.entries, revision=body.revision, mutation=body.mutation_id)

    @app.patch("/credentials/{entry_id}")
    def edit_credential(entry_id: str, body: EditBody, caller: Annotated[User, Depends(user)]) -> dict:
        policy = {
            "name": body.name.strip() if body.name is not None else None,
            "enabled": body.enabled,
            "roles": distinct(body.roles) if body.roles is not None else None,
            "profiles": distinct([p.model_dump() for p in body.profiles]) if body.profiles is not None else None,
        }
        if all(value is None for value in policy.values()):
            raise VaultError(422, "no changes supplied")
        return store.edit(caller.id, entry_id, revision=body.revision, mutation=body.mutation_id, **policy)

    @app.put("/credentials/{entry_id}/secret")
    def replace_secret(entry_id: str, body: ReplaceBody, caller: Annotated[User, Depends(user)]) -> dict:
        secret = candidate(store.route(caller.id, entry_id), body)
        return store.replace(
            caller, entry_id, secret.pop("secret"), revision=body.revision, mutation=body.mutation_id, **secret
        )

    @app.delete("/credentials/{entry_id}")
    def remove_credential(entry_id: str, revision: int, caller: Annotated[User, Depends(user)]) -> dict:
        return store.remove(caller.id, entry_id, revision)

    return app


class RecordRef(BaseModel):
    """The frozen exact identity a future admitted gateway request supplies, never a user-selected payer."""

    user_id: int = Field(gt=0, strict=True, description="Stable GitHub owner ID.")
    route: str = Field(description="Trusted configured credential route.")
    record_id: str = Field(pattern=r"^[0-9a-f]{64}$", description="Opaque immutable record identity.")
    version: int = Field(gt=0, strict=True, description="Exact version frozen at admission.")


def protect(app: FastAPI) -> None:
    """Use the same fixed-error boundary on both listeners; request validation must never echo secrets."""

    @app.exception_handler(VaultError)
    async def refused(_: Request, error: VaultError) -> JSONResponse:
        """Return only the vault's bounded public error vocabulary."""
        headers = {"WWW-Authenticate": "Bearer"} if error.status == 401 else None
        return JSONResponse({"error": error.reason}, status_code=error.status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def malformed(_: Request, __: RequestValidationError) -> JSONResponse:
        """Suppress submitted field values from schema errors."""
        return JSONResponse({"error": "malformed request"}, status_code=422)


def create_resolver(store: Store, token: str) -> FastAPI:
    """A separate gateway-principal listener; user/metadata credentials cannot reach its material API."""

    def gateway(authorization: Annotated[str | None, Header()] = None):
        """Authenticate locally without ever forwarding a private bearer to GitHub."""
        scheme, _, supplied = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token or not hmac.compare_digest(supplied.encode(), token.encode()):
            raise VaultError(401, "invalid gateway credential")

    app = FastAPI(
        title="Concurro private credential resolver",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(gateway)],
    )
    protect(app)

    @app.post("/resolve")
    def resolve(body: RecordRef):
        """Resolve only the recorded version; responses must not be cached by intermediaries."""
        material = store.resolve(body.user_id, body.route, body.record_id, body.version)
        return JSONResponse(material, headers={"Cache-Control": "no-store"})

    @app.get("/events")
    def events(after: int = 0):
        """Read nonsecret lifecycle events; the future Exchange consumer persists its own cursor."""
        return store.events(after)

    return app


class ListenerApps:
    """Select an API using the trusted accepted socket's port, never a client header or URL."""

    def __init__(self, public, private=None, private_port=None):
        """Bind one public application and, optionally, one separately addressed private listener."""
        self.public, self.private, self.private_port = public, private, private_port

    async def __call__(self, scope, receive, send):
        """Lifespan belongs to the public app; neither application creates listener-specific resources."""
        address = scope.get("server")
        target = self.private if self.private and address and address[1] == self.private_port else self.public
        await target(scope, receive, send)


def main(argv=None) -> None:
    """Serve the legacy public API, optionally a private listener, or explicitly rotate wrapping keys."""
    import uvicorn

    parser = argparse.ArgumentParser(prog="concurro-vault")
    parser.add_argument("--rotate", action="store_true", help="rewrap live records under the configured active key")
    args = parser.parse_args(argv or [])
    os.umask(0o077)
    state = Path(os.environ.get("VAULT_STATE_DIR", "/data"))
    key_file = os.environ.get("VAULT_MASTER_KEY_FILE", "/run/secrets/vault-master-key")
    keyring = os.environ.get("VAULT_KEYRING_FILE")
    try:
        envelope = load_envelope(key_file, keyring)
        service_path = os.environ.get("VAULT_SERVICE_TOKEN_FILE")
        service_token = load_master_key(service_path).hex() if service_path else None
        gateway_path = os.environ.get("VAULT_RESOLVER_TOKEN_FILE")
        gateway_token = load_master_key(gateway_path).hex() if gateway_path else None
        if gateway_token and gateway_token == service_token:
            raise ConfigError("metadata and gateway principals must differ")
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        with ExitStack() as resources:
            store = Store(
                state / "vault.db",
                envelope,
                manifest=lambda: load_manifest(os.environ.get("VAULT_ORDER_MANIFEST_FILE")),
            )
            resources.callback(store.close)
            if store.migration:
                print(json.dumps({"adopted_lists": store.migration}), flush=True)
            if args.rotate:
                if not keyring:
                    raise ConfigError("rotation requires a named keyring")
                print(json.dumps({"rewrapped": store.rotate(envelope)}))
                return
            public = create_app(store, Identity(http_get), service_token, (gateway_token,) if gateway_token else ())
            host, _, port = os.environ.get("VAULT_LISTEN", "0.0.0.0:8090").rpartition(":")
            sockets = [resources.enter_context(socket.create_server((host, int(port))))]
            private, private_port = None, None
            if gateway_token:
                private_host, _, value = os.environ.get("VAULT_RESOLVER_LISTEN", "127.0.0.1:8091").rpartition(":")
                private_port = int(value)
                if private_port == int(port):
                    raise ConfigError("vault listener ports must differ")
                sockets.append(resources.enter_context(socket.create_server((private_host, private_port))))
                private = create_resolver(store, gateway_token)
            server = uvicorn.Server(
                uvicorn.Config(ListenerApps(public, private, private_port), server_header=False, proxy_headers=False)
            )
            server.run(sockets=sockets)
    except (ConfigError, OSError, ValueError) as error:
        reason = str(error) if isinstance(error, ConfigError) else "vault startup failed"
        raise SystemExit("vault: " + reason) from None


if __name__ == "__main__":
    main(sys.argv[1:])
