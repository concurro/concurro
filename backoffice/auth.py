"""GitHub App user sign-in: one-use `state` and PKCE bound to the starting browser, server-side sessions.

Sessions live in memory; a restart signs everyone out. The user's GitHub token never reaches the browser.
"""

import base64
import hashlib
import secrets
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"
LOGIN_SECONDS = 600
# GitHub App user tokens expire after 8 hours unless expiry is disabled for the App.
DEFAULT_TOKEN_SECONDS = 8 * 3600


class AuthError(Exception):
    """Sign-in failed; the text is safe to show."""


@dataclass(frozen=True)
class User:
    id: int
    login: str
    name: str
    avatar_url: str


@dataclass
class Session:
    token: str
    user: User
    expires: float


@dataclass
class PendingLogin:
    state: str
    verifier: str
    next: str
    expires: float


@dataclass
class Sessions:
    """Pending sign-ins keyed by a browser-bound cookie, and signed-in sessions keyed by another."""

    clock: object = time.time
    logins: dict[str, PendingLogin] = field(default_factory=dict)
    sessions: dict[str, Session] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def now(self) -> float:
        return self.clock()  # type: ignore[operator]

    def begin(self, next_path: str) -> tuple[str, PendingLogin]:
        """A new pending sign-in and the cookie value that binds it to this browser."""
        cookie = secrets.token_urlsafe(32)
        login = PendingLogin(
            secrets.token_urlsafe(32), secrets.token_urlsafe(48), next_path, self.now() + LOGIN_SECONDS
        )
        with self.lock:
            self.logins = {k: v for k, v in self.logins.items() if v.expires > self.now()}
            self.logins[cookie] = login
        return cookie, login

    def complete(self, cookie: str | None, state: str | None) -> PendingLogin:
        """Consume the pending sign-in only when the callback's `state` matches this browser's."""
        with self.lock:
            login = self.logins.pop(cookie or "", None)
        if login is None or login.expires <= self.now() or not state or not secrets.compare_digest(login.state, state):
            raise AuthError("sign-in expired or was started in another browser; please sign in again")
        return login

    def create(self, token: str, user: User, lifetime: float) -> str:
        sid = secrets.token_urlsafe(32)
        with self.lock:
            self.sessions = {k: v for k, v in self.sessions.items() if v.expires > self.now()}
            self.sessions[sid] = Session(token, user, self.now() + lifetime)
        return sid

    def get(self, sid: str | None) -> Session | None:
        with self.lock:
            session = self.sessions.get(sid or "")
        return session if session and session.expires > self.now() else None

    def end(self, sid: str | None) -> None:
        with self.lock:
            self.sessions.pop(sid or "", None)


def challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def authorize_url(client_id: str, callback: str, login: PendingLogin) -> str:
    query = {
        "client_id": client_id,
        "redirect_uri": callback,
        "state": login.state,
        "code_challenge": challenge(login.verifier),
        "code_challenge_method": "S256",
    }
    return AUTHORIZE_URL + "?" + urlencode(query)


class GitHubOAuth:
    """The two GitHub calls sign-in needs; `client` is injectable for tests."""

    def __init__(self, client_id: str, client_secret: str, client: httpx.Client | None = None):
        self.client_id, self.client_secret = client_id, client_secret
        self.client = client or httpx.Client(timeout=20)

    def exchange(self, code: str, callback: str, verifier: str) -> tuple[str, float]:
        """The user token and its lifetime in seconds."""
        try:
            response = self.client.post(
                TOKEN_URL,
                headers={"Accept": "application/json"},
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": code,
                    "redirect_uri": callback,
                    "code_verifier": verifier,
                },
            )
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise AuthError("GitHub sign-in is unavailable; try again") from None
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if response.status_code != 200 or not isinstance(token, str):
            raise AuthError("GitHub refused the sign-in; please sign in again")
        lifetime = payload.get("expires_in")
        return token, float(lifetime) if isinstance(lifetime, int) and lifetime > 0 else DEFAULT_TOKEN_SECONDS

    def user(self, token: str) -> User:
        try:
            response = self.client.get(
                USER_URL, headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"}
            )
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise AuthError("GitHub is unavailable; try again") from None
        if response.status_code != 200 or not isinstance(data, dict) or type(data.get("id")) is not int:
            raise AuthError("GitHub did not confirm your account")
        return User(data["id"], data["login"], data.get("name") or data["login"], data.get("avatar_url") or "")
