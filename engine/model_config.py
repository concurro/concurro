"""Named model profiles and explicit role assignments, independent of CLI harnesses."""

from dataclasses import asdict, dataclass
from functools import lru_cache
import json
import re

HARNESSES = {"anthropic": "claude", "openai": "codex"}
EFFORTS = {
    "anthropic": {"low", "medium", "high", "xhigh", "max"},
    "openai": {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"},
}
LEGACY_ROLES = {"triage": "qc", "planner": "planner", "implementer": "implementer", "pr_reviewers": "reviewers"}
LEGACY_FIELDS = {"qc", "qc_model", "planner", "implementer", "reviewers"}
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


@dataclass(frozen=True)
class ModelProfile:
    """A vendor model and its explicit reasoning effort."""

    provider: str
    model: str
    effort: str

    @property
    def harness(self):
        """The CLI adapter used to invoke this vendor."""
        return HARNESSES[self.provider]


@dataclass(frozen=True)
class AgentAssignments:
    """Profile names assigned to each pipeline role."""

    triage: str
    planner: str
    plan_reviewers: tuple[str, ...]
    implementer: str
    pr_reviewers: tuple[str, ...]


@dataclass(frozen=True)
class ResolvedAgent:
    """A stable profile identity together with the settings used for its invocation."""

    name: str
    profile: ModelProfile

    def record(self):
        """Serializable identity for audit, votes and session binding."""
        return {"profile": self.name, **asdict(self.profile), "harness": self.profile.harness}


@dataclass(frozen=True)
class ModelConfiguration:
    """Validated immutable profiles and assignments shared by all consumers."""

    profiles: tuple[ResolvedAgent, ...]
    agents: AgentAssignments

    def resolve(self, name):
        """Resolve a configured profile; provider names are never implicit aliases."""
        for agent in self.profiles:
            if agent.name == name:
                return agent
        raise ValueError("unknown model profile: " + str(name))


@lru_cache(maxsize=128)
def _parse(serialized):
    """Parse each distinct model configuration once without retaining a mutable config."""
    data = json.loads(serialized)
    models, roles = data["models"], data["agents"]
    if not isinstance(models, dict) or not models:
        raise ValueError("models must be a non-empty map of named profiles")
    profiles = []
    for name, value in models.items():
        if not NAME.fullmatch(name) or not isinstance(value, dict) or set(value) != {"provider", "model", "effort"}:
            raise ValueError("invalid model profile fields or name")
        provider, model, effort = (value[k] for k in ("provider", "model", "effort"))
        if not isinstance(provider, str) or provider not in HARNESSES:
            raise ValueError("unknown model provider")
        if not isinstance(model, str) or not model.strip() or model != model.strip() or len(model) > 200:
            raise ValueError("model profile requires an explicit model ID")
        if not isinstance(effort, str) or effort not in EFFORTS[provider]:
            raise ValueError("unsupported effort for model provider")
        profiles.append(ResolvedAgent(name, ModelProfile(provider, model, effort)))
    if not isinstance(roles, dict) or set(roles) != set(AgentAssignments.__dataclass_fields__):
        raise ValueError("agents must assign triage, planner, plan_reviewers, implementer and pr_reviewers")
    selected = {}
    for role, value in roles.items():
        if role.endswith("reviewers"):
            if (
                not isinstance(value, list)
                or len(value) < 2
                or not all(isinstance(v, str) for v in value)
                or len(set(value)) != len(value)
            ):
                raise ValueError(role + " requires at least two distinct model profiles")
            selected[role] = tuple(value)
        else:
            if not isinstance(value, str):
                raise ValueError(role + " must name one model profile")
            selected[role] = value
        names = value if isinstance(value, list) else [value]
        if any(name not in models for name in names):
            raise ValueError("agent role references an unknown model profile")
        if role.endswith("reviewers"):
            identities = {(models[name]["provider"], models[name]["model"]) for name in names}
            if len(identities) != len(names):
                raise ValueError(role + " requires distinct provider/model identities; aliases and effort do not count")
    return ModelConfiguration(tuple(profiles), AgentAssignments(**selected))


def parse(config):
    """Validate named configuration, refusing ambiguous mixtures with legacy assignments."""
    if LEGACY_FIELDS & config.keys():
        raise ValueError("named agents cannot be mixed with legacy role assignments")
    return _parse(json.dumps({"models": config.get("models"), "agents": config.get("agents")}, sort_keys=True))


def assigned(config, role):
    """Return a role's profile names, retaining the legacy configuration interface."""
    if "agents" in config:
        return getattr(parse(config).agents, role)
    return config[LEGACY_ROLES[role]]


def resolve(config, name, stage, reviewer=None):
    """Resolve an invocation; legacy configurations retain their recorded model selection."""
    if "agents" in config:
        return parse(config).resolve(name)
    model = (reviewer or {}).get("model")
    if model is None:
        model = config.get("qc_model") if stage in ("intake", "plan_qc") else None
        model = model or config["models"][name]
    vendor = {"claude": "anthropic", "codex": "openai"}.get(name)
    if vendor is None:
        raise ValueError("the runner supports Claude and Codex only")
    return ResolvedAgent(name, ModelProfile(vendor, model, "medium" if name == "claude" else "low"))
