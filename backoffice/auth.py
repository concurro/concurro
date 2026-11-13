"""Browser-bound GitHub sign-in and server-only, single-flight user-token refresh.

Sessions remain in memory: restarting signs users out. Provider credentials never enter this store.
"""

import base64
import hashlib
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import quote, urlencode

import httpx

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"
LOGIN_SECONDS = 600
DEFAULT_TOKEN_SECONDS = 8 * 3600
REFRESH_MARGIN = 60


class AuthError(Exception):
    """A fixed sign-in failure, optionally with an ambiguous remote refresh outcome."""

    def __init__(self, message: str, uncertain: bool = False):
        """Keep remote response bodies and token values outside exceptions."""
        super().__init__(message)
        self.uncertain = uncertain


class TokenCredential(Protocol):
    """The existing transport get/drop contract, shared by GitHub and private BFF clients."""

    def get(self) -> str:
        """Return the current server-side access token."""
        ...

    def drop(self, token: str) -> None:
        """Invalidate only the refused revision, never a concurrently refreshed token."""
        ...


@dataclass(frozen=True)
class User:
    """The stable authenticated GitHub identity, without token material."""

    id: int
    login: str
    name: str
    avatar_url: str


@dataclass(frozen=True)
class TokenGrant:
    """A GitHub-issued pair; absent expiry/refresh fields stay absent."""

    token: str = field(repr=False)  # Access token, never returned to the browser.
    expires_in: float | None = None  # Trusted access-token lifetime, when GitHub supplies it.
    refresh_token: str | None = field(default=None, repr=False)  # Server-only rotating refresh token.
    refresh_expires_in: float | None = None  # Trusted refresh-token lifetime.


@dataclass
class Session:
    """One browser session and its serialized access-token revision."""

    token: str = field(repr=False)
    user: User
    expires: float
    access_expires: float | None = None  # Access expiry is separate from the browser/refresh lifetime.
    refresh_token: str | None = field(default=None, repr=False)
    refresh_expires: float | None = None
    closed: bool = False  # A logout fence checked again after refresh I/O.
    refreshing: bool = False  # Lets logout retain an honest pending/unknown remote status.
    credential: TokenCredential | None = field(default=None, repr=False)
    orphan: Callable[[TokenGrant | None, bool], None] | None = field(
        default=None, repr=False
    )  # Cleanup callback for a refresh finishing after logout.
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


@dataclass
class PendingLogin:
    """A one-use PKCE transaction bound to the initiating browser cookie."""

    state: str
    verifier: str = field(repr=False)
    next: str
    expires: float


@dataclass
class Sessions:
    """Pending logins and memory-only sessions; refresh is serialized per session."""

    clock: object = time.time
    logins: dict[str, PendingLogin] = field(default_factory=dict, repr=False)
    sessions: dict[str, Session] = field(default_factory=dict, repr=False)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    oauth: "GitHubOAuth | None" = field(
        default=None, repr=False
    )  # Injected by the app, including in transport credentials.

    def now(self) -> float:
        """Read the injected wall clock."""
        return self.clock()  # type: ignore[operator]

    def begin(self, next_path: str) -> tuple[str, PendingLogin]:
        """Create a browser-bound, expiring login transaction."""
        cookie = secrets.token_urlsafe(32)
        login = PendingLogin(
            secrets.token_urlsafe(32), secrets.token_urlsafe(48), next_path, self.now() + LOGIN_SECONDS
        )
        with self.lock:
            self.logins = {k: v for k, v in self.logins.items() if v.expires > self.now()}
            self.logins[cookie] = login
        return cookie, login

    def complete(self, cookie: str | None, state: str | None) -> PendingLogin:
        """Consume a transaction only in its initiating browser."""
        with self.lock:
            login = self.logins.pop(cookie or "", None)
        if login is None or login.expires <= self.now() or not state or not secrets.compare_digest(login.state, state):
            raise AuthError("sign-in expired or was started in another browser; please sign in again")
        return login

    def create(self, token: str, user: User, lifetime: float, *, grant: TokenGrant | None = None) -> str:
        """Keep legacy local lifetimes; refresh-capable sessions use GitHub's actual refresh expiry."""
        sid, now = secrets.token_urlsafe(32), self.now()
        span = grant.refresh_expires_in if grant and grant.refresh_token else lifetime
        session = Session(
            token,
            user,
            now + float(span or lifetime),
            now + grant.expires_in if grant and grant.expires_in is not None else (None if grant else now + lifetime),
            grant.refresh_token if grant else None,
            now + grant.refresh_expires_in if grant and grant.refresh_expires_in is not None else None,
        )
        session.credential = UserCredential(self, sid)
        with self.lock:
            for old_sid, old in list(self.sessions.items()):
                if old.expires <= now and not old.refreshing:
                    old.closed = True
                    self.sessions.pop(old_sid)
            self.sessions[sid] = session
        return sid

    def get(self, sid: str | None) -> Session | None:
        """Refresh once when needed; a late response cannot reopen a logged-out session."""
        with self.lock:
            session = self.sessions.get(sid or "")
            if session is None or session.closed:
                return None
            if session.expires <= self.now():
                session.closed = True
                self.sessions.pop(sid or "", None)
                return None
        if (
            not session.refresh_token
            or session.access_expires is None
            or session.access_expires - REFRESH_MARGIN > self.now()
        ):
            return session
        with session.lock:
            with self.lock:
                if session.closed or self.sessions.get(sid or "") is not session:
                    return None
                if session.access_expires is not None and session.access_expires - REFRESH_MARGIN > self.now():
                    return session
                if not self.oauth or session.refresh_expires is None or session.refresh_expires <= self.now():
                    session.closed = True
                    self.sessions.pop(sid or "", None)
                    return None
                session.refreshing = True
            granted, failure = None, None
            try:
                granted = self.oauth.refresh(session.refresh_token)
            except AuthError as error:
                failure = error
            with self.lock:
                if failure:
                    session.closed = True
                    self.sessions.pop(sid or "", None)
                if not session.closed and granted is not None:
                    assert granted.expires_in is not None and granted.refresh_expires_in is not None
                    now = self.now()
                    session.token = granted.token
                    session.access_expires = now + granted.expires_in
                    session.refresh_token = granted.refresh_token
                    session.refresh_expires = now + granted.refresh_expires_in
                    session.expires = session.refresh_expires
                orphan = session.orphan if session.closed else None
                session.refreshing = False
            if orphan:
                orphan(granted, bool(failure and failure.uncertain))
            if failure:
                raise failure
            return None if session.closed else session

    def end(self, sid: str | None, orphan=None, persist=None) -> Session | None:
        """End local authorization before any remote I/O and fence an in-flight refresh."""
        with self.lock:
            session = self.sessions.pop(sid or "", None)
            if session:
                session.closed = True
                session.orphan = orphan
                if persist:
                    persist(session)
            return session


class UserCredential:
    """Refresh-aware implementation of the existing transport credential contract."""

    def __init__(self, sessions: Sessions, sid: str):
        """Keep only a server-side session reference."""
        self.sessions, self.sid = sessions, sid

    def get(self) -> str:
        """Read a current token or require a fresh browser sign-in."""
        session = self.sessions.get(self.sid)
        if session is None:
            raise AuthError("sign in with GitHub again")
        return session.token

    def drop(self, token: str) -> None:
        """A refused old token cannot invalidate a newer concurrent refresh."""
        with self.sessions.lock:
            session = self.sessions.sessions.get(self.sid)
            if session and session.token == token:
                if session.refresh_token:
                    session.access_expires = 0
                else:
                    session.closed = True
                    self.sessions.sessions.pop(self.sid, None)


class BearerAuth(httpx.Auth):
    """Refresh-aware bearer forwarding; only a definite authentication refusal is replayed."""

    def __init__(self, credential: str | TokenCredential):
        """Accept existing fixed tokens as well as browser-session credentials."""
        self.credential = credential

    def auth_flow(self, request):
        """Retry once after 401, before these private APIs admit any write or provider validation."""
        token = self.credential if isinstance(self.credential, str) else self.credential.get()
        request.headers["Authorization"] = "Bearer " + token
        response = yield request
        if response.status_code == 401 and not isinstance(self.credential, str):
            self.credential.drop(token)
            replacement = self.credential.get()
            if replacement != token:
                response.read()
                request.headers["Authorization"] = "Bearer " + replacement
                yield request


def challenge(verifier: str) -> str:
    """The S256 PKCE challenge."""
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def authorize_url(client_id: str, callback: str, login: PendingLogin) -> str:
    """Preserve App configuration: refresh is used when GitHub supplies it, without forcing new scopes."""
    return (
        AUTHORIZE_URL
        + "?"
        + urlencode(
            {
                "client_id": client_id,
                "redirect_uri": callback,
                "state": login.state,
                "code_challenge": challenge(login.verifier),
                "code_challenge_method": "S256",
            }
        )
    )


class GitHubOAuth:
    """GitHub code exchange, single-use refresh and single-token revocation; all errors are sanitized."""

    def __init__(self, client_id: str, client_secret: str, client: httpx.Client | None = None):
        """Inject an HTTP client for protocol tests without real GitHub credentials."""
        self.client_id, self.client_secret = client_id, client_secret
        self.client = client or httpx.Client(timeout=20)

    def tokens(self, data: dict) -> TokenGrant:
        """Accept only GitHub's actual response fields; never guess provider token lifetimes."""
        try:
            response = self.client.post(
                TOKEN_URL,
                headers={"Accept": "application/json"},
                data={"client_id": self.client_id, "client_secret": self.client_secret, **data},
            )
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise AuthError("GitHub sign-in is unavailable; please sign in again", uncertain=True) from None
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if response.status_code != 200 or not isinstance(token, str) or not token:
            raise AuthError("GitHub refused the sign-in; please sign in again", uncertain=response.status_code >= 500)
        lifetime, refresh, refresh_lifetime = (
            payload.get("expires_in"),
            payload.get("refresh_token"),
            payload.get("refresh_token_expires_in"),
        )
        if lifetime is not None and (type(lifetime) is not int or lifetime <= 0):
            raise AuthError("GitHub returned an incomplete sign-in; please sign in again", uncertain=True)
        if refresh is not None and (
            not isinstance(refresh, str)
            or not refresh
            or type(refresh_lifetime) is not int
            or refresh_lifetime <= 0
            or lifetime is None
        ):
            raise AuthError("GitHub returned an incomplete sign-in; please sign in again", uncertain=True)
        return TokenGrant(token, lifetime, refresh, refresh_lifetime)

    def exchange(self, code: str, callback: str, verifier: str) -> TokenGrant:
        """Exchange the browser's one-use code using its own PKCE verifier."""
        return self.tokens({"code": code, "redirect_uri": callback, "code_verifier": verifier})

    def refresh(self, token: str) -> TokenGrant:
        """Exchange a GitHub refresh token once; callers serialize it and never replay ambiguity."""
        grant = self.tokens({"grant_type": "refresh_token", "refresh_token": token})
        if not grant.refresh_token or grant.expires_in is None or grant.refresh_expires_in is None:
            raise AuthError("GitHub returned an incomplete refresh; please sign in again", uncertain=True)
        return grant

    def revoke(self, token: str) -> bool:
        """Revoke this access token only; confirm invalidity after an ambiguous/replayed deletion."""
        url = "https://api.github.com/applications/" + quote(self.client_id, safe="") + "/token"
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        try:
            response = self.client.request(
                "DELETE", url, auth=(self.client_id, self.client_secret), headers=headers, json={"access_token": token}
            )
            if response.status_code == 204:
                return True
            if response.status_code in (404, 422):
                checked = self.client.post(
                    url, auth=(self.client_id, self.client_secret), headers=headers, json={"access_token": token}
                )
                return checked.status_code == 404
        except httpx.HTTPError:
            pass
        return False

    def user(self, token: str) -> User:
        """Resolve the stable account directly from GitHub, never from browser-supplied identity."""
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
