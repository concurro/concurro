"""Concurro's credentials vault: per-user provider secrets, stored write-only.

Callers present a GitHub user token; the vault resolves the user itself through
`GET https://api.github.com/user`. Each secret is validated with a cheap provider call, then
sealed with a fresh data key that the master key wraps (envelope encryption, AES-256-GCM).
Public endpoints never return secrets. Only the separately authenticated gateway listener resolves exact versions.
"""

import argparse
import hashlib
import hmac
import http.client
import json
import os
import runpy
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
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, BinaryIO
from urllib.parse import urlencode, urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

PROTOCOL_PATH = Path(__file__).with_name("provider_protocol.py")
if not PROTOCOL_PATH.exists():
    PROTOCOL_PATH = Path(__file__).resolve().parents[1] / "provider_protocol.py"
PROTOCOL = runpy.run_path(str(PROTOCOL_PATH))
SUBSCRIPTIONS = {route for route, value in PROTOCOL["ROUTES"].items() if value["kind"] == "subscription"}
MAX_EVENT = PROTOCOL["MAX_EVENT"]
VALIDATION_SECONDS = PROTOCOL["VALIDATION_SECONDS"]
# One fixed-origin streaming candidate response, closed after validation.
ProbeTransport = Callable[[str, Mapping[str, str], dict], AbstractContextManager[tuple[int, str, BinaryIO]]]


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


@contextmanager
def http_probe(url: str, headers: Mapping[str, str], body: dict) -> Iterator[tuple[int, str, BinaryIO]]:
    """Bound connected reads; late DNS/TCP/TLS completion cannot send a token or publish material."""
    target = urlsplit(url)
    connection_type = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(target.hostname, target.port, timeout=VALIDATION_SECONDS)
    deadline = time.monotonic() + VALIDATION_SECONDS
    expired, channel = threading.Event(), []

    def expire():
        """Interrupt a slow header or partial-line read without waiting for socket inactivity."""
        expired.set()
        if channel:
            try:
                channel[0].shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    timer = threading.Timer(VALIDATION_SECONDS, expire)
    timer.daemon = True
    timer.start()
    try:
        connection.connect()
        channel.append(connection.sock)
        if expired.is_set():
            raise TimeoutError
        connection.request(
            "POST",
            target.path + (("?" + target.query) if target.query else ""),
            json.dumps(body).encode(),
            {"User-Agent": "concurro-vault", **headers},
        )
        with connection.getresponse() as reply:
            yield reply.status, reply.getheader("Content-Type", ""), reply
        if expired.is_set() or time.monotonic() >= deadline:
            raise TimeoutError
    except (TimeoutError, OSError, http.client.HTTPException):
        raise Unavailable("provider validation unavailable") from None
    finally:
        timer.cancel()
        connection.close()


@dataclass(frozen=True)
class Validated:
    """Material proven by validation; account identity and expiry remain unverified."""

    models: list[str] | None  # Exact models proved, or legacy unknown API coverage.
    account: dict | None = None  # Account header material sealed with this token/version.
    account_label: str | None = None  # Opaque owner-visible hint, never authenticated identity.


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
class Route:
    """A credential route and the provider call that proves a secret works."""

    url: str
    headers: Callable[[str], dict[str, str]]


ROUTES = {
    "anthropic-api-key": Route(
        "https://api.anthropic.com/v1/models",
        lambda secret: {"x-api-key": secret, "anthropic-version": "2023-06-01"},
    ),
    "openai-api-key": Route(
        "https://api.openai.com/v1/models",
        lambda secret: {"Authorization": f"Bearer {secret}"},
    ),
}


class Validator:
    """Checks a secret against its provider: 2xx is valid, 401/403 invalid, anything else unavailable."""

    def __init__(self, transport: Transport, routes: Mapping[str, Route] = ROUTES, probe: ProbeTransport = http_probe):
        self.transport, self.routes, self.probe = transport, routes, probe

    def validate(
        self, route: str, secret: str, models: list[str] | None = None, account_id: str | None = None
    ) -> Validated:
        """Validate the entire candidate before publishing any replacement or model coverage."""
        if route not in SUBSCRIPTIONS:
            valid, available = self.models(route, secret)
            if not valid:
                raise VaultError(422, "provider rejected the secret")
            return Validated(available)
        if not models or any(not isinstance(model, str) or not model.strip() or secret in model for model in models):
            raise VaultError(422, "select models to validate")
        selected = list(dict.fromkeys(model.strip() for model in models))
        account = None
        if route == "chatgpt-subscription":
            if not account_id or not all(33 <= ord(c) <= 126 for c in account_id):
                raise VaultError(422, "ChatGPT account ID is required and must be a valid header value")
            account = {"chatgpt_account_id": account_id}
        elif account_id is not None:
            raise VaultError(422, "this route does not use an account ID")
        for model in selected:
            url, headers, body = PROTOCOL["probe_request"](route, model, secret, account_id)
            deadline = time.monotonic() + VALIDATION_SECONDS
            try:
                with self.probe(url, headers, body) as (status, media, response):
                    if status in (400, 401, 403, 404, 422):
                        raise VaultError(422, "provider rejected the candidate or selected model")
                    if not 200 <= status < 300:
                        raise Unavailable("provider validation unavailable")
                    observation = PROTOCOL["Observation"](route, status)
                    if "text/event-stream" not in media.lower():
                        raw = response.read(MAX_EVENT + 1)
                        if len(raw) > MAX_EVENT:
                            raise Unavailable("provider validation did not complete")
                        observation.accept(json.loads(raw))
                    else:
                        frame = bytearray()
                        while chunk := response.readline(MAX_EVENT + 1):
                            if time.monotonic() >= deadline:
                                raise Unavailable("provider validation did not complete")
                            frame.extend(chunk)
                            if len(frame) > MAX_EVENT:
                                raise Unavailable("provider validation did not complete")
                            if chunk not in (b"\n", b"\r\n"):
                                continue
                            value, error = PROTOCOL["event_value"](frame)
                            if observation.accept(value) or error:
                                raise Unavailable("provider validation did not complete")
                            frame.clear()
                        if frame:
                            observation.ambiguous = True
                    result = observation.finish()
                    if time.monotonic() >= deadline or not result["completed"] or result["category"] is not None:
                        raise Unavailable("provider validation did not complete")
            except (ValueError, UnicodeError, TimeoutError, OSError, http.client.HTTPException):
                raise Unavailable("provider validation did not complete") from None
        label = "Supplied ChatGPT account " + digest(account_id)[:8] if account_id else None
        return Validated(selected, account, label)

    def valid(self, route: str, secret: str) -> bool:
        return self.models(route, secret)[0]

    def models(self, route: str, secret: str) -> tuple[bool, list[str] | None]:
        """Validate a complete provider model listing; malformed/cyclic pagination stays unknown."""
        target = self.routes[route]
        parameters: dict[str, int | str] = {"limit": 1000} if route == "anthropic-api-key" else {}
        models, cursors = set(), set()
        while True:
            url = target.url + (("&" if "?" in target.url else "?") + urlencode(parameters) if parameters else "")
            status, body = self.transport(url, target.headers(secret))
            if status in (401, 403):
                return False, None
            if not 200 <= status < 300:
                raise Unavailable(f"provider validation returned {status}")
            try:
                payload = json.loads(body)
                values = payload.get("data")
                if not isinstance(values, list) or any(
                    not isinstance(v, dict) or not isinstance(v.get("id"), str) or not v["id"] or secret in v["id"]
                    for v in values
                ):
                    return True, None
                models.update(v["id"] for v in values)
                more = payload.get("has_more", None if route == "anthropic-api-key" else False)
                if more is False:
                    return True, sorted(models)
                cursor = payload.get("last_id")
                if (
                    more is not True
                    or not isinstance(cursor, str)
                    or not cursor
                    or cursor in cursors
                    or secret in cursor
                ):
                    return True, None
                cursors.add(cursor)
                parameters["after_id"] = cursor
            except (ValueError, TypeError, AttributeError):
                return True, None


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
)
"""

ROUTE_KINDS = {
    "openai-api-key": "api_key",
    "anthropic-api-key": "api_key",
    "chatgpt-subscription": "subscription",
    "claude-subscription": "subscription",
}


class Store:
    """Versioned owner/route records, encrypted material and atomic nonsecret lifecycle events."""

    def __init__(self, path: str | Path, envelope: Envelope, clock: Callable[[], float] = time.time):
        """Migrate additively without guessing legacy validation or retirement evidence."""
        self.envelope, self.clock = envelope, clock
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA secure_delete=ON")
        self.lock = threading.RLock()
        with self.transaction():
            self.db.execute(SCHEMA)
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS credential_metadata (user_id INTEGER,route TEXT,version INTEGER,"
                "models TEXT,retired TEXT,PRIMARY KEY(user_id,route,version))"
            )
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(credential_metadata)")}
            for name, definition in (
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
            ):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE credential_metadata ADD COLUMN {name} {definition}")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS credential_events (sequence INTEGER PRIMARY KEY AUTOINCREMENT,"
                "id TEXT NOT NULL UNIQUE,record_id TEXT NOT NULL,user_id INTEGER NOT NULL,route TEXT NOT NULL,"
                "version INTEGER NOT NULL,type TEXT NOT NULL,status TEXT NOT NULL,reason TEXT,at TEXT NOT NULL)"
            )
            self.db.execute("CREATE TABLE IF NOT EXISTS vault_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            schema = self.db.execute("SELECT value FROM vault_meta WHERE key='schema'").fetchone()
            if schema and schema[0] not in ("2", "3"):
                raise ConfigError("unsupported vault schema")
            legacy = (
                self.db.execute("SELECT * FROM credentials ORDER BY user_id,route,version").fetchall()
                if schema is None
                else []
            )
            for row in legacy:
                self.db.execute(
                    "INSERT OR IGNORE INTO credential_metadata(user_id,route,version,kind) VALUES(?,?,?,?)",
                    (row["user_id"], row["route"], row["version"], ROUTE_KINDS.get(row["route"])),
                )
                self.db.execute(
                    "UPDATE credential_metadata SET kind=COALESCE(kind,?) WHERE user_id=? AND route=? AND version=?",
                    (ROUTE_KINDS.get(row["route"]), row["user_id"], row["route"], row["version"]),
                )
                self.event(
                    row["user_id"],
                    row["route"],
                    row["version"],
                    "snapshot",
                    row["status"] if row["ciphertext"] is not None else "retired",
                    "legacy_snapshot",
                    "migration:" + record_id(row["user_id"], row["route"], row["version"]),
                )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS credential_fences(user_id INTEGER NOT NULL,route TEXT NOT NULL,revision INTEGER NOT NULL,PRIMARY KEY(user_id,route))"
            )
            self.db.execute(
                "INSERT OR IGNORE INTO credential_fences SELECT user_id,route,MAX(sequence) FROM credential_events GROUP BY user_id,route"
            )
            self.db.execute(
                "INSERT INTO vault_meta VALUES('schema','3') ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )

    def now(self) -> str:
        """A UTC instant from the injected clock."""
        return datetime.fromtimestamp(self.clock(), UTC).isoformat(timespec="seconds")

    def close(self) -> None:
        """Close this store's connection."""
        self.db.close()

    def event(self, user_id, route, version, kind, status, reason=None, identifier=None):
        """Append an immutable secret-free event in the caller's write transaction."""
        self.db.execute(
            "INSERT OR IGNORE INTO credential_events(id,record_id,user_id,route,version,type,status,reason,at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (
                identifier or uuid.uuid4().hex,
                record_id(user_id, route, version),
                user_id,
                route,
                version,
                kind,
                status,
                reason,
                self.now(),
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

    def listing(self, user_id: int) -> list[dict[str, object]]:
        """Only the owner's latest route/version metadata, never sealed columns or account secrets."""
        with self.lock:
            rows = self.db.execute(
                "SELECT route,MAX(version) AS version FROM credentials WHERE user_id=? GROUP BY route ORDER BY route",
                (user_id,),
            ).fetchall()
            return [self.entry(user_id, row["route"], row["version"]) for row in rows]

    def revision(self, user_id: int, route: str) -> int:
        """Read the owner/route mutation fence before any candidate validation I/O."""
        with self.lock:
            row = self.db.execute(
                "SELECT revision FROM credential_fences WHERE user_id=? AND route=?", (user_id, route)
            ).fetchone()
            return row[0] if row else 0

    def advance_revision(self, user_id: int, route: str) -> None:
        """Fence older candidates inside the current replacement/revoke transaction."""
        self.db.execute(
            "INSERT INTO credential_fences VALUES(?,?,1) ON CONFLICT(user_id,route) DO UPDATE SET revision=revision+1",
            (user_id, route),
        )

    def put(
        self,
        user: User,
        route: str,
        secret: str,
        models: list[str] | None = None,
        *,
        account: dict | None = None,
        account_label: str | None = None,
        verified_account: str | None = None,
        expires_at: str | None = None,
        expiry_known=False,
        expected_revision: int | None = None,
    ) -> dict[str, object]:
        """Store already-validated material; replacement retires only this owner's previous route version."""
        if route not in ROUTE_KINDS:
            raise VaultError(422, "unsupported credential kind")
        if expiry_known and expires_at is None or expires_at is not None and not expiry_known:
            raise VaultError(422, "expiry requires trusted evidence")
        expiry = instant(expires_at) if expires_at else None
        if expiry and datetime.fromisoformat(expiry).timestamp() <= self.clock():
            raise VaultError(422, "credential is already expired")
        now = self.now()
        with self.lock, self.transaction():
            if expected_revision is not None and self.revision(user.id, route) != expected_revision:
                raise VaultError(409, "credential changed during validation; refresh status before retrying")
            self.advance_revision(user.id, route)
            version = self.db.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM credentials WHERE user_id=? AND route=?", (user.id, route)
            ).fetchone()[0]
            material = json.dumps({"secret": secret, "account": account}, separators=(",", ":")) if account else secret
            sealed = self.envelope.seal(material, aad(user.id, route, version))
            self.destroy(user.id, route, "replaced")
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
                "INSERT INTO credential_metadata(user_id,route,version,models,kind,account_label,verified_account,"
                "status_reason,validated,expires_at,expiry_known,key_id,envelope_format)"
                " VALUES(?,?,?,?,?,?,?,'validated',?,?,?,?,?)",
                (
                    user.id,
                    route,
                    version,
                    json.dumps(models),
                    ROUTE_KINDS[route],
                    account_label,
                    verified_account,
                    now,
                    expiry,
                    int(expiry_known),
                    self.envelope.current,
                    2 if account else 1,
                ),
            )
            self.event(user.id, route, version, "validated", "valid", "validated")
            return self.entry(user.id, route, version)

    def revoke(self, user_id: int, route: str) -> dict[str, object] | None:
        """Atomically revoke the latest version and destroy material; replay creates no second event."""
        with self.lock, self.transaction():
            self.advance_revision(user_id, route)
            row = self.db.execute(
                "SELECT version,status,ciphertext FROM credentials WHERE user_id=? AND route=?"
                " ORDER BY version DESC LIMIT 1",
                (user_id, route),
            ).fetchone()
            if row is None:
                return None
            if row["status"] != "revoked":
                self.destroy(user_id, route, "revoked")
                self.db.execute(
                    "UPDATE credentials SET status='revoked',updated=? WHERE user_id=? AND route=? AND version=?",
                    (self.now(), user_id, route, row["version"]),
                )
                self.db.execute(
                    "UPDATE credential_metadata SET status_reason='revoked' WHERE user_id=? AND route=? AND version=?",
                    (user_id, route, row["version"]),
                )
                self.event(user_id, route, row["version"], "revoked", "revoked", "revoked")
            return self.entry(user_id, route, row["version"])

    def sealed(self, user_id: int, route: str) -> tuple[int, Sealed] | None:
        """The latest usable ciphertext, retained for migration/rotation tooling only."""
        with self.lock:
            row = self.db.execute(
                "SELECT version,wrapped_key,nonce,ciphertext FROM credentials "
                "WHERE user_id=? AND route=? AND ciphertext IS NOT NULL ORDER BY version DESC LIMIT 1",
                (user_id, route),
            ).fetchone()
        return (row["version"], Sealed(row["wrapped_key"], row["nonce"], row["ciphertext"])) if row else None

    def metadata(self, user_id: int, at: str | None = None) -> dict:
        """Private nonsecret status/model history; unknown legacy evidence stays unknown."""
        cutoff = datetime.fromisoformat(instant(at)) if at else datetime.fromtimestamp(self.clock(), UTC)
        with self.lock:
            rows = self.db.execute(
                "SELECT c.user_id,c.route,c.version,c.status,c.created,c.updated,m.models,m.retired,"
                "c.ciphertext IS NOT NULL AS usable,m.kind,m.status_reason,m.validated,m.expires_at,m.expiry_known,"
                "m.token_revision FROM credentials c LEFT JOIN credential_metadata m "
                "ON c.user_id=m.user_id AND c.route=m.route AND c.version=m.version "
                "WHERE c.user_id=? ORDER BY c.version",
                (user_id,),
            ).fetchall()
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
            latest[row["route"]] = {
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
            }
        return {
            "history_complete": all(r["status"] != "unknown" for r in latest.values()),
            "credentials": list(latest.values()),
        }

    def destroy(self, user_id: int, route: str, reason="revoked") -> None:
        """Retire usable versions before deleting sealed material, within the caller's transaction."""
        versions = self.db.execute(
            "SELECT version FROM credentials WHERE user_id=? AND route=? AND ciphertext IS NOT NULL", (user_id, route)
        ).fetchall()
        for row in versions:
            self.db.execute(
                "UPDATE credential_metadata SET retired=COALESCE(retired,?),status_reason=?"
                " WHERE user_id=? AND route=? AND version=?",
                (self.now(), reason, user_id, route, row["version"]),
            )
            if reason == "replaced":
                self.event(user_id, route, row["version"], "retired", "retired", reason)
        self.db.execute(
            "UPDATE credentials SET wrapped_key=NULL,nonce=NULL,ciphertext=NULL WHERE user_id=? AND route=?",
            (user_id, route),
        )

    def entry(self, user_id: int, route: str, version: int) -> dict[str, object]:
        """Public owner metadata only; account headers remain inside the sealed payload."""
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
        if identifier != record_id(user_id, route, version) or route not in ROUTE_KINDS:
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
    models: list[str] | None = Field(
        default=None, description="Selected model set, each probed once before replacement."
    )
    account_id: str | None = Field(default=None, description="Candidate ChatGPT account header, not verified identity.")


def create_app(
    store: Store,
    identity: Identity,
    validator: Validator,
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

    def known(route: str) -> str:
        if route not in validator.routes and route not in SUBSCRIPTIONS:
            raise VaultError(404, "unknown route")
        return route

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
    def list_credentials(caller: Annotated[User, Depends(user)]) -> list[dict[str, object]]:
        return store.listing(caller.id)

    @app.put("/credentials/{route}")
    def put_credential(
        route: Annotated[str, Depends(known)], body: SecretBody, caller: Annotated[User, Depends(user)]
    ) -> dict[str, object]:
        secret = body.secret.strip()
        # Keys go into request headers: only printable ASCII without whitespace is a plausible key.
        if not secret or not all(33 <= ord(c) <= 126 for c in secret):
            raise VaultError(422, "secret is empty or has unsupported characters")
        revision = store.revision(caller.id, route)
        validated = validator.validate(route, secret, body.models, body.account_id)
        return store.put(
            caller,
            route,
            secret,
            validated.models,
            account=validated.account,
            account_label=validated.account_label,
            expected_revision=revision,
        )

    @app.delete("/credentials/{route}")
    def revoke_credential(
        route: Annotated[str, Depends(known)], caller: Annotated[User, Depends(user)]
    ) -> dict[str, object]:
        entry = store.revoke(caller.id, route)
        if entry is None:
            raise VaultError(404, "no such credential")
        return entry

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
            store = Store(state / "vault.db", envelope)
            resources.callback(store.close)
            if args.rotate:
                if not keyring:
                    raise ConfigError("rotation requires a named keyring")
                print(json.dumps({"rewrapped": store.rotate(envelope)}))
                return
            public = create_app(
                store, Identity(http_get), Validator(http_get), service_token, (gateway_token,) if gateway_token else ()
            )
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
