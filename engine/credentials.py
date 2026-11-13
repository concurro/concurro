"""Credential identities: each model call records which route it used; no secret is ever part of one."""

import re
from dataclasses import asdict, dataclass

from policy import Blocked, digest

KINDS = ("org", "contributed")


@dataclass(frozen=True)
class Credential:
    """Non-secret identity recorded in audit state; material is obtained separately."""

    id: str
    provider: str
    kind: str
    adapter: str
    account: str | None = None
    route: str | None = None  # Configured personal route; absent on legacy org identities.
    record_id: str | None = None  # Opaque immutable vault record, never its material.
    version: int | None = None  # User replacement version, separate from token revision.

    def record(self):
        """Serializable identity for state and transport; never contains secret material."""
        value = asdict(self)
        if self.kind == "org":
            for key in ("route", "record_id", "version"):
                value.pop(key)
        return value

    @classmethod
    def parse(cls, record):
        """Rebuild an identity from transport, rejecting anything but the exact fields."""
        legacy = {"id", "provider", "kind", "adapter", "account"}
        fields = legacy if isinstance(record, dict) and record.get("kind") == "org" else set(cls.__dataclass_fields__)
        if not isinstance(record, dict) or set(record) != fields:
            raise Blocked("invalid credential identity")
        credential = cls(**record)
        if (
            not all(isinstance(v, str) and v for v in (credential.id, credential.provider, credential.adapter))
            or credential.kind not in KINDS
            or credential.account is not None
            and not isinstance(credential.account, str)
        ):
            raise Blocked("invalid credential identity")
        if credential.kind == "contributed" and (
            not isinstance(credential.account, str)
            or not re.fullmatch(r"[1-9][0-9]*", credential.account)
            or not isinstance(credential.route, str)
            or not credential.route
            or not isinstance(credential.record_id, str)
            or not re.fullmatch(r"[0-9a-f]{64}", credential.record_id)
            or type(credential.version) is not int
            or credential.version < 1
            or credential.id
            != digest(["contributed", credential.account, credential.route, credential.record_id, credential.version])
        ):
            raise Blocked("invalid contributed credential identity")
        return credential
