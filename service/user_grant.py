"""The reviewer account's user-to-server token for the service App: stored, refreshed and bound by numeric ID.

The token carries the intersection of the App's and the account's permissions; gateway
admission, not this credential, limits it to review publication.
"""

import http.client
import json
import os
import threading
import time
from urllib.parse import urlencode

import oauth_grant
from identities import Actor

from . import transport
from .identity import IdentityError

GRANT = "reviewer-grant.json"
HOST = "github.com"
TOKEN_PATH = "/login/oauth/access_token"
DEVICE_PATH = "/login/device/code"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
REFRESH_MARGIN = 300
LIMIT = 100_000


def post(connection, path, fields):
    """One form POST to GitHub's OAuth endpoints; the parsed JSON reply, with sanitized failures."""
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "concurro-service",
    }
    try:
        response = transport.exchange(
            connection, HOST, "POST", path, headers, urlencode(fields).encode(), LIMIT, transport.TIMEOUT
        )
        return response.status if response.sent else None, response.json() if response.sent else None
    except (OSError, http.client.HTTPException, ValueError):
        return None, None


def account(connection, token):
    """The account a token belongs to, resolved from GitHub's `/user`."""
    headers = {"Authorization": "Bearer " + token, **transport.headers()}
    try:
        response = transport.exchange(
            connection, transport.API, "GET", "/user", headers, None, LIMIT, transport.TIMEOUT
        )
        user = response.json() if response.sent and response.status == 200 else None
    except (OSError, http.client.HTTPException, ValueError):
        user = None
    if not isinstance(user, dict) or type(user.get("id")) is not int or not isinstance(user.get("login"), str):
        raise IdentityError("GitHub did not confirm the reviewer account")
    return Actor(user["login"], user["id"], viewer(lambda body: graphql(connection, token, body)))


VIEWER = {"query": "query { viewer { login id } }"}


def viewer(request):
    """The requesting account's GraphQL node ID, as author fields report it; None when unavailable."""
    try:
        found = ((request(VIEWER) or {}).get("data") or {}).get("viewer") or {}
    except Exception:
        return None
    return found["id"] if isinstance(found.get("id"), str) and found["id"] else None


def graphql(connection, token, body):
    """One GraphQL read with a user token."""
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json", **transport.headers()}
    response = transport.exchange(
        connection, transport.API, "POST", "/graphql", headers, json.dumps(body).encode(), LIMIT, transport.TIMEOUT
    )
    return response.json() if response.sent and response.status == 200 else None


def record(fields, now, actor):
    """The stored form of a token response; absent expiries stay absent."""
    token, lifetime, refresh, refresh_lifetime = fields
    return {
        "token": token,
        "expires": None if lifetime is None else now + lifetime,
        "refresh_token": refresh,
        "refresh_expires": None if refresh_lifetime is None else now + refresh_lifetime,
        "actor": actor.record(),
    }


def save(path, grant):
    """Replace the stored grant atomically, owner-only."""
    temporary = path.with_name(path.name + ".new")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(grant, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def authorize(reviewer, state_dir, connection=transport.connect, clock=time.time, sleep=time.sleep, show=print):
    """Device-flow authorization of the configured account; stores its grant and returns its actor.

    The operator approves the shown code while signed in as the reviewer account. A grant for
    any other account is discarded.
    """
    status, device = post(connection, DEVICE_PATH, {"client_id": reviewer.client_id})
    if status != 200 or not isinstance(device, dict) or not isinstance(device.get("device_code"), str):
        raise IdentityError("GitHub refused the device authorization request; enable device flow for the App")
    show(f"Signed in as the reviewer account, open {device['verification_uri']} and enter {device['user_code']}")
    interval, deadline = int(device.get("interval", 5)), clock() + int(device.get("expires_in", 900))
    while clock() < deadline:
        sleep(interval)
        fields = {"client_id": reviewer.client_id, "device_code": device["device_code"], "grant_type": DEVICE_GRANT}
        status, payload = post(connection, TOKEN_PATH, fields)
        pending = payload.get("error") if isinstance(payload, dict) else None
        if pending == "authorization_pending":
            continue
        if pending == "slow_down":
            interval += 5
            continue
        try:
            granted = oauth_grant.parse(status or 0, payload)
        except oauth_grant.GrantError:
            raise IdentityError("GitHub refused the reviewer authorization") from None
        actor = account(connection, granted[0])
        if actor.id != reviewer.user_id:
            raise IdentityError("the authorizing account is not the configured reviewer")
        save(state_dir / GRANT, record(granted, clock(), actor))
        return actor
    raise IdentityError("reviewer authorization expired before it was approved")


class UserGrant:
    """A transport credential backed by the stored grant; refresh is serialized and saved before use."""

    def __init__(self, reviewer, state_dir, connection=transport.connect, clock=time.time):
        """Load the stored grant and require it to belong to the configured numeric account."""
        self.reviewer, self.path = reviewer, state_dir / GRANT
        self.connection, self.clock = connection, clock
        self.lock = threading.Lock()
        self.grant = self.read()
        if self.grant is None:
            raise IdentityError("no reviewer grant; run `authorize-reviewer` as the reviewer account")

    def read(self):
        """The stored grant when it is readable and bound to the configured account, else None."""
        try:
            if self.path.stat().st_mode & 0o077:
                return None
            grant = json.loads(self.path.read_text())
            actor = Actor.parse(grant["actor"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        valid = isinstance(grant.get("token"), str) and grant["token"] and actor.id == self.reviewer.user_id
        return grant if valid else None

    @property
    def actor(self):
        """The bound account: numeric and node IDs from setup, login for display."""
        return Actor.parse(self.grant["actor"])

    def get(self):
        """A usable token; refreshes shortly before expiry, never for a non-expiring token."""
        with self.lock:
            grant = self.grant
            if grant["expires"] is None or grant["expires"] - REFRESH_MARGIN > self.clock():
                return grant["token"]
            # A new authorization may have replaced the file since this process read it.
            stored = self.read()
            if stored and stored["token"] != grant["token"]:
                self.grant = stored
                return stored["token"]
            if not grant["refresh_token"] or grant["refresh_expires"] <= self.clock():
                raise IdentityError("reviewer grant expired; authorize the reviewer again")
            secret = self.reviewer.client_secret_path.read_text().strip()
            fields = {
                "client_id": self.reviewer.client_id,
                "client_secret": secret,
                "grant_type": "refresh_token",
                "refresh_token": grant["refresh_token"],
            }
            status, payload = post(self.connection, TOKEN_PATH, fields)
            try:
                granted = oauth_grant.parse(status or 0, payload)
            except oauth_grant.GrantError:
                # The refresh token is single-use: a lost reply can require a new authorization.
                raise IdentityError("reviewer grant could not be refreshed") from None
            if granted[2] is None:
                raise IdentityError("reviewer grant refresh was incomplete")
            self.grant = record(granted, self.clock(), self.actor)
            save(self.path, self.grant)
            return self.grant["token"]

    def drop(self, token):
        """After GitHub refused the token, refresh on the next use when a refresh is possible."""
        with self.lock:
            if self.grant["token"] == token and self.grant["refresh_token"]:
                self.grant = {**self.grant, "expires": 0}
