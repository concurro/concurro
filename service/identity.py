"""The controller App's identity: a JWT, and installation tokens narrowed to one repository and use."""

import base64
from contextlib import contextmanager
from datetime import datetime
import http.client
import json
import subprocess
import threading
import time

import private

from . import transport
from .targets import permissions

READ = {"contents": "read", "issues": "read", "pull_requests": "read", "checks": "read", "administration": "read"}
WRITE = {"contents": "write", "issues": "write", "pull_requests": "write"}
CUTOVER = {"actions": "read", "actions_variables": "read", "environments": "read"}
KINDS = {"read": READ, "write": WRITE, "cutover": CUTOVER}
JWT_SECONDS = 540
JWT_MARGIN = 60
TOKEN_MARGIN = 600
APP_LIMIT = 1_000_000


class IdentityError(RuntimeError):
    """A token could not be minted or verified; the text never contains credentials."""


def segment(value):
    """Encode a JWT segment without padding."""
    return base64.urlsafe_b64encode(value).rstrip(b"=")


def sign(key_path, message):
    """RS256 over `message` with the App's private key, as the pilot does."""
    result = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(key_path)], input=message, capture_output=True, timeout=15
    )
    if result.returncode:
        raise IdentityError("GitHub App key could not sign authentication request")
    return result.stdout


class Credential:
    """One repository's cached token of one kind, as a transport needs it."""

    def __init__(self, identity, repository, kind):
        self.identity, self.repository, self.kind = identity, repository, kind

    def get(self):
        """A valid token, minting one when none is cached or it nears expiry."""
        return self.identity.token(self.repository, self.kind)

    def drop(self, token):
        """Forget the token after GitHub refused it."""
        self.identity.drop(self.repository, self.kind, token)


class Identity:
    """Mint, verify, cache and revoke the controller App's installation tokens."""

    def __init__(self, settings, connection=transport.connect, clock=time.time, signer=sign):
        """`settings` supplies `app_id` and `private_key_path`; the key is checked on each signature."""
        self.app_id, self.key_path = settings.app_id, settings.private_key_path
        self.permissions = permissions(settings.targets) if hasattr(settings, "targets") else None
        self.connection, self.clock, self.signer = connection, clock, signer
        self.guard = threading.Lock()
        self.locks = {}
        self.tokens = {}
        self.installations = {}
        self.jwt = None
        self.bot_login = None

    def api(self, method, path, token, data=None):
        """One App API call with in-memory credentials and sanitized errors."""
        headers = {
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": transport.API_VERSION,
            "User-Agent": transport.USER_AGENT,
        }
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        try:
            response = transport.exchange(
                self.connection, transport.API, method, "/" + path, headers, body, APP_LIMIT, transport.TIMEOUT
            )
        except (OSError, http.client.HTTPException):
            raise IdentityError("GitHub App connection failed") from None
        if not response.sent:
            raise IdentityError("GitHub App connection failed")
        if not 200 <= response.status < 300:
            raise IdentityError(f"GitHub App request failed (HTTP {response.status})")
        try:
            return response.json()
        except ValueError:
            raise IdentityError("GitHub App response unreadable") from None

    def app_jwt(self):
        """A signed App JWT, re-signed shortly before it expires."""
        with self.guard:
            now = int(self.clock())
            if self.jwt and self.jwt[1] - JWT_MARGIN > now:
                return self.jwt[0]
            private.read(self.key_path)
            claims = {"iat": now - 60, "exp": now + JWT_SECONDS, "iss": str(self.app_id)}
            message = segment(b'{"alg":"RS256","typ":"JWT"}') + b"." + segment(json.dumps(claims).encode())
            self.jwt = ((message + b"." + segment(self.signer(self.key_path, message))).decode(), now + JWT_SECONDS)
            return self.jwt[0]

    def verify(self):
        """Record the App's bot login; the worker requires each target's `bot_login` to equal it."""
        slug = self.api("GET", "app", self.app_jwt()).get("slug")
        if not isinstance(slug, str) or not slug:
            raise IdentityError("GitHub App identity unavailable")
        self.bot_login = slug + "[bot]"
        return self.bot_login

    def installation(self, repository):
        """The App's installation on `repository`: selected repositories only, owned by its owner."""
        if repository not in self.installations:
            found = self.api("GET", f"repos/{repository}/installation", self.app_jwt())
            if found.get("repository_selection") != "selected":
                raise IdentityError("install the App on selected repositories, not all repositories")
            if found.get("account", {}).get("login", "").lower() != repository.split("/")[0].lower():
                raise IdentityError("App installation owner differs from the repository owner")
            self.installations[repository] = found["id"]
        return self.installations[repository]

    def mint(self, repository, kind):
        """A new token for one repository with exactly the kind's permissions; revoked if it differs."""
        if self.permissions is not None and kind in ("read", "write"):
            wanted = self.permissions.get(repository, {}).get(kind)
            if not wanted:
                raise IdentityError("repository is not enabled for this token use")
        else:
            wanted = KINDS[kind]
        granted = self.api(
            "POST",
            f"app/installations/{self.installation(repository)}/access_tokens",
            self.app_jwt(),
            {"repositories": [repository.split("/")[1]], "permissions": wanted},
        )
        token = granted.get("token")
        if not isinstance(token, str) or not token:
            raise IdentityError("GitHub App token unavailable")
        try:
            permissions = {k: v for k, v in granted.get("permissions", {}).items() if k != "metadata"}
            if permissions != wanted:
                raise IdentityError("App token permissions differ from the requested least-privilege scope")
            listed = self.api("GET", "installation/repositories?per_page=100", token)
            if listed.get("total_count") != 1 or [
                r.get("full_name", "").lower() for r in listed.get("repositories", [])
            ] != [repository.lower()]:
                raise IdentityError("App token repository scope differs from the requested repository")
            expires = datetime.fromisoformat(granted["expires_at"]).timestamp()
        except BaseException as error:
            self.revoke(token)
            if isinstance(error, (KeyError, TypeError, ValueError)):
                raise IdentityError("GitHub App token response unreadable") from None
            raise
        return token, expires

    def lock(self, repository, kind):
        """One lock per repository and kind, so concurrent callers mint once."""
        with self.guard:
            return self.locks.setdefault((repository, kind), threading.Lock())

    def token(self, repository, kind):
        """A cached token valid for at least `TOKEN_MARGIN` more seconds; cutover tokens are never cached."""
        if kind not in ("read", "write"):
            raise IdentityError("only read and write tokens are cached")
        with self.lock(repository, kind):
            cached = self.tokens.get((repository, kind))
            if cached and cached[1] - TOKEN_MARGIN > self.clock():
                return cached[0]
            # A replaced token is not revoked: another thread may still be using it.
            self.tokens[(repository, kind)] = self.mint(repository, kind)
            return self.tokens[(repository, kind)][0]

    def drop(self, repository, kind, token):
        """Forget a token GitHub refused, unless it was already replaced."""
        with self.lock(repository, kind):
            if self.tokens.get((repository, kind), (None,))[0] == token:
                del self.tokens[(repository, kind)]

    def credential(self, repository, kind):
        """The transport's view of one cached token."""
        if kind not in ("read", "write"):
            raise IdentityError("only read and write tokens are cached")
        return Credential(self, repository, kind)

    @contextmanager
    def cutover(self, repository):
        """A token for the `adopt` checks only, revoked when the command ends."""
        token, _ = self.mint(repository, "cutover")
        try:
            yield token
        finally:
            self.revoke(token)

    def revoke(self, token):
        """Revoke one token, best effort; it otherwise keeps GitHub's normal expiry."""
        try:
            self.api("DELETE", "installation/token", token)
        except IdentityError:
            pass

    def close(self):
        """Revoke every cached token."""
        with self.guard:
            tokens, self.tokens = list(self.tokens.values()), {}
        for token, _ in tokens:
            self.revoke(token)
