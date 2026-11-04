"""Credential identities: each model call records which route it used; no secret is ever part of one."""

from dataclasses import asdict, dataclass

from policy import Blocked

KINDS = ("org", "contributed")


@dataclass(frozen=True)
class Credential:
    """Non-secret identity recorded in audit state; material is obtained separately."""

    id: str
    provider: str
    kind: str
    adapter: str
    account: str | None = None

    def record(self):
        """Serializable identity for state and transport; never contains secret material."""
        return asdict(self)

    @classmethod
    def parse(cls, record):
        """Rebuild an identity from transport, rejecting anything but the exact fields."""
        if not isinstance(record, dict) or set(record) != set(cls.__dataclass_fields__):
            raise Blocked("invalid credential identity")
        credential = cls(**record)
        if (not all(isinstance(v, str) and v for v in (credential.id, credential.provider, credential.adapter))
                or credential.kind not in KINDS
                or credential.account is not None and not isinstance(credential.account, str)):
            raise Blocked("invalid credential identity")
        return credential
