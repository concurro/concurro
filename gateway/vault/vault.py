"""Concurro's credentials vault: per-user provider secrets, stored write-only.

Callers present a GitHub user token; the vault resolves the user itself through
`GET https://api.github.com/user`. Each secret is validated with a cheap provider call, then
sealed with a fresh data key that the master key wraps (envelope encryption, AES-256-GCM).
No endpoint returns a secret, and secrets and tokens are never logged.
"""

import hashlib
import json
import os
import sqlite3
import stat
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

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


def load_master_key(path: str | Path) -> bytes:
    """Read the 32-byte master key, hex-encoded in a regular file only its owner (this process) can access."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise ConfigError(f"master key file {path} is missing") from None
    except OSError as error:
        raise ConfigError(f"master key file {path} cannot be opened: {error.strerror}") from None
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError(f"master key file {path} is not a regular file")
        if info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise ConfigError(f"master key file {path} must be owned by uid {os.geteuid()} with mode 0400 or 0600")
        text = file.read().strip()
    try:
        key = bytes.fromhex(text.decode("ascii"))
    except ValueError:
        key = b""
    if len(key) != 32:
        raise ConfigError(f"master key file {path} must hold 32 bytes as 64 hex characters")
    return key


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

    def __init__(self, transport: Transport, routes: Mapping[str, Route] = ROUTES):
        self.transport, self.routes = transport, routes

    def valid(self, route: str, secret: str) -> bool:
        target = self.routes[route]
        status, _ = self.transport(target.url, target.headers(secret))
        if 200 <= status < 300:
            return True
        if status in (401, 403):
            return False
        raise Unavailable(f"provider validation returned {status}")


@dataclass(frozen=True)
class Sealed:
    wrapped_key: bytes
    nonce: bytes
    ciphertext: bytes


class Envelope:
    """AES-256-GCM envelope encryption; `aad` binds a ciphertext to its record."""

    def __init__(self, master: bytes):
        self.master = AESGCM(master)

    def seal(self, secret: str, aad: bytes) -> Sealed:
        data_key = AESGCM.generate_key(bit_length=256)
        nonce, wrap_nonce = os.urandom(NONCE), os.urandom(NONCE)
        ciphertext = AESGCM(data_key).encrypt(nonce, secret.encode(), aad)
        return Sealed(wrap_nonce + self.master.encrypt(wrap_nonce, data_key, aad), nonce, ciphertext)

    def open(self, sealed: Sealed, aad: bytes) -> str:
        wrap_nonce, wrapped = sealed.wrapped_key[:NONCE], sealed.wrapped_key[NONCE:]
        data_key = self.master.decrypt(wrap_nonce, wrapped, aad)
        return AESGCM(data_key).decrypt(sealed.nonce, sealed.ciphertext, aad).decode()


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

LISTING = "route, status, fingerprint, version, updated"


class Store:
    """Versioned credential records keyed by (user_id, route) in SQLite (WAL).

    Only a route's latest version keeps ciphertext; storing a new version or revoking destroys
    older ciphertext.
    """

    def __init__(self, path: str | Path, envelope: Envelope):
        self.envelope = envelope
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute(SCHEMA)
        self.lock = threading.Lock()

    def close(self) -> None:
        self.db.close()

    def listing(self, user_id: int) -> list[dict[str, object]]:
        with self.lock:
            rows = self.db.execute(
                f"SELECT {LISTING} FROM credentials c WHERE user_id = ? AND version = "
                "(SELECT MAX(version) FROM credentials WHERE user_id = c.user_id AND route = c.route) ORDER BY route",
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def put(self, user: User, route: str, secret: str) -> dict[str, object]:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self.lock, self.transaction():
            latest = self.db.execute(
                "SELECT COALESCE(MAX(version), 0) FROM credentials WHERE user_id = ? AND route = ?", (user.id, route)
            ).fetchone()[0]
            version = latest + 1
            sealed = self.envelope.seal(secret, aad(user.id, route, version))
            self.destroy(user.id, route)
            self.db.execute(
                "INSERT INTO credentials VALUES (?, ?, ?, ?, 'valid', ?, ?, ?, ?, ?, ?)",
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
            return self.entry(user.id, route, version)

    def revoke(self, user_id: int, route: str) -> dict[str, object] | None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self.lock, self.transaction():
            row = self.db.execute(
                "SELECT MAX(version) FROM credentials WHERE user_id = ? AND route = ?", (user_id, route)
            ).fetchone()
            if row[0] is None:
                return None
            self.destroy(user_id, route)
            self.db.execute(
                "UPDATE credentials SET status = 'revoked', updated = ? "
                "WHERE user_id = ? AND route = ? AND version = ? AND status != 'revoked'",
                (now, user_id, route, row[0]),
            )
            return self.entry(user_id, route, row[0])

    def sealed(self, user_id: int, route: str) -> tuple[int, Sealed] | None:
        """The latest version's ciphertext, if it still has one."""
        with self.lock:
            row = self.db.execute(
                "SELECT version, wrapped_key, nonce, ciphertext FROM credentials "
                "WHERE user_id = ? AND route = ? AND ciphertext IS NOT NULL ORDER BY version DESC LIMIT 1",
                (user_id, route),
            ).fetchone()
        return (row["version"], Sealed(row["wrapped_key"], row["nonce"], row["ciphertext"])) if row else None

    def destroy(self, user_id: int, route: str) -> None:
        self.db.execute(
            "UPDATE credentials SET wrapped_key = NULL, nonce = NULL, ciphertext = NULL WHERE user_id = ? AND route = ?",
            (user_id, route),
        )

    def entry(self, user_id: int, route: str, version: int) -> dict[str, object]:
        row = self.db.execute(
            f"SELECT {LISTING} FROM credentials WHERE user_id = ? AND route = ? AND version = ?",
            (user_id, route, version),
        ).fetchone()
        return dict(row)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")


class SecretBody(BaseModel):
    secret: str


def create_app(store: Store, identity: Identity, validator: Validator) -> FastAPI:
    app = FastAPI(title="Concurro credentials vault", docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(VaultError)
    async def refused(_: Request, error: VaultError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if error.status == 401 else None
        return JSONResponse({"error": error.reason}, status_code=error.status, headers=headers)

    # FastAPI's default body errors echo the submitted input, which may be a secret.
    @app.exception_handler(RequestValidationError)
    async def malformed(_: Request, __: RequestValidationError) -> JSONResponse:
        return JSONResponse({"error": "malformed request"}, status_code=422)

    def user(authorization: Annotated[str | None, Header()] = None) -> User:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise VaultError(401, "missing bearer token")
        return identity.resolve(token.strip())

    def known(route: str) -> str:
        if route not in validator.routes:
            raise VaultError(404, "unknown route")
        return route

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

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
        if not validator.valid(route, secret):
            raise VaultError(422, "provider rejected the secret")
        return store.put(caller, route, secret)

    @app.delete("/credentials/{route}")
    def revoke_credential(
        route: Annotated[str, Depends(known)], caller: Annotated[User, Depends(user)]
    ) -> dict[str, object]:
        entry = store.revoke(caller.id, route)
        if entry is None:
            raise VaultError(404, "no such credential")
        return entry

    return app


def main() -> None:
    import uvicorn

    os.umask(0o077)
    state = Path(os.environ.get("VAULT_STATE_DIR", "/data"))
    key_file = os.environ.get("VAULT_MASTER_KEY_FILE", "/run/secrets/vault-master-key")
    host, _, port = os.environ.get("VAULT_LISTEN", "0.0.0.0:8090").rpartition(":")
    try:
        envelope = Envelope(load_master_key(key_file))
    except ConfigError as error:
        raise SystemExit(f"vault: {error}") from None
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    store = Store(state / "vault.db", envelope)
    app = create_app(store, Identity(http_get), Validator(http_get))
    uvicorn.run(app, host=host, port=int(port), server_header=False)


if __name__ == "__main__":
    main()
