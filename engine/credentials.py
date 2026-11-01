"""Credential resolution: each model call selects a credential identity before any secret is read."""

from dataclasses import asdict, dataclass
import os

from policy import Blocked, PROVIDER_CREDENTIALS

# How each environment credential is held; names match the service design's adapter table.
ADAPTERS = {
    "CODEX_API_KEY": "direct-api-key",
    "ANTHROPIC_API_KEY": "direct-api-key",
    "CLAUDE_CODE_OAUTH_TOKEN": "long-lived-token",
    "COPILOT_GITHUB_TOKEN": "long-lived-token",
}
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


class EnvironmentCredentials:
    """One operator credential per provider, read from the process environment."""

    def __init__(self, environ=None):
        """Default to this process's environment, as the Actions and pilot paths provide it."""
        self.environ = os.environ if environ is None else environ

    def resolve(self, provider, stage, task):
        """Select the provider's only configured credential; stage and task serve future policies."""
        names = [n for n in PROVIDER_CREDENTIALS.get(provider, ()) if self.environ.get(n)]
        if len(names) != 1:
            raise Blocked(f"configure exactly one inference credential for {provider}")
        return Credential("env:" + names[0], provider, "org", ADAPTERS[names[0]])

    def material(self, credential):
        """Return container environment only for the credential this resolver currently selects."""
        if self.resolve(credential.provider, None, None) != credential:
            raise Blocked("selected credential is no longer available")
        name = credential.id.removeprefix("env:")
        return {name: self.environ[name]}
