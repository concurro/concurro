"""Authorship contract: which GitHub identity may author each kind of published event."""

import re

from policy import Blocked

# Control-plane events (audit and plan artifacts, state pointers, queue cursors, health
# notices, merges) are authored only by the controller identity.
CONTROL_EVENTS = frozenset({"state", "merge"})
EVENTS = CONTROL_EVENTS | {"conversation"}
# GitHub App slugs are lowercase alphanumerics and inner hyphens; their bot login appends [bot].
BOT_LOGIN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\[bot\]")
ROLE = re.compile(r"[a-z][a-z0-9-]{0,31}(?::[a-z0-9][a-z0-9.-]{0,63})*")


class Identities:
    """Map each event type and role to exactly one expected author login."""

    def __init__(self, config, publishers=None):
        """The controller is `bot_login`; optional publishers author conversation events by role."""
        self.controller = config["bot_login"]
        self.publishers = dict(publishers or {})
        if self.controller in self.publishers.values() or not all(
                isinstance(k, str) and ROLE.fullmatch(k) and isinstance(v, str) and BOT_LOGIN.fullmatch(v)
                for k, v in self.publishers.items()):
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
