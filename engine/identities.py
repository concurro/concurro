"""Authorship contract: which GitHub identity may author each kind of published event."""

import re
from dataclasses import dataclass

from policy import Blocked

# Control-plane events (audit and plan artifacts, state pointers, queue cursors, health
# notices, merges) are authored only by the controller identity.
CONTROL_EVENTS = frozenset({"state", "merge"})
EVENTS = CONTROL_EVENTS | {"conversation"}
# GitHub App slugs are lowercase alphanumerics and inner hyphens; their bot login appends [bot].
BOT_LOGIN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\[bot\]")
ROLE = re.compile(r"[a-z][a-z0-9-]{0,31}(?::[a-z0-9][a-z0-9.-]{0,63})*")


@dataclass(frozen=True)
class Actor:
    """One GitHub account: its display login, REST numeric ID and GraphQL node ID.

    The two IDs are separate bindings of the account resolved at setup, never derived from
    each other. A descriptor without an ID is a legacy login-bound record and matches by login.
    """

    login: str
    id: int | None = None
    node: str | None = None

    @classmethod
    def parse(cls, value):
        """A descriptor from its stored record, or from a legacy bare login."""
        if isinstance(value, cls):
            return value
        if isinstance(value, str) and value:
            return cls(value)
        if (
            not isinstance(value, dict)
            or not set(value) <= {"login", "id", "node"}
            or not isinstance(value.get("login"), str)
            or not value["login"]
            or value.get("id") is not None
            and (type(value["id"]) is not int or value["id"] < 1)
            or value.get("node") is not None
            and (not isinstance(value["node"], str) or not value["node"])
        ):
            raise Blocked("invalid actor descriptor")
        return cls(value["login"], value.get("id"), value.get("node"))

    def record(self):
        """The stored form, carried in receipts and caches."""
        return {"login": self.login, "id": self.id, "node": self.node}

    @property
    def logins(self):
        """The login as REST and GraphQL spell it; GraphQL names a bot without `[bot]`."""
        return (self.login, self.login.removesuffix("[bot]"))

    def rest(self, user):
        """Whether a REST `user` object is this account: by numeric ID when both sides have one."""
        if not isinstance(user, dict):
            return False
        if self.id is not None and type(user.get("id")) is int:
            return user["id"] == self.id
        return user.get("login") == self.login

    def graphql(self, author):
        """Whether a GraphQL `author` object is this account: by node ID when both sides have one."""
        if not isinstance(author, dict):
            return False
        if self.node is not None and isinstance(author.get("id"), str):
            return author["id"] == self.node
        return author.get("login") in self.logins


# GraphQL author selection carrying the stable node ID beside the login.
AUTHOR = "author { login ... on Node { id } }"


class Identities:
    """Map each event type and role to exactly one expected author login."""

    def __init__(self, config, publishers=None):
        """The controller is `bot_login`; optional publishers author conversation events by role."""
        self.controller = config["bot_login"]
        self.publishers = dict(publishers or {})
        if self.controller in self.publishers.values() or not all(
            isinstance(k, str) and ROLE.fullmatch(k) and isinstance(v, str) and BOT_LOGIN.fullmatch(v)
            for k, v in self.publishers.items()
        ):
            raise Blocked("conversation publishers must be distinct [bot] identities")

    def author(self, event, role=None):
        """Return the only login whose content of this type and role is trusted."""
        if event not in EVENTS:
            raise Blocked("unknown publication event type")
        if event in CONTROL_EVENTS or role is None:
            return self.controller
        return self.publishers.get(role, self.controller)

    def ours(self, login):
        """Whether a login is any Concurro identity, so its comments are never human input."""
        return login == self.controller or login in self.publishers.values()
