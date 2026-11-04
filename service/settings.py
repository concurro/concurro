"""Operator settings outside any repository, validated strictly before anything else starts."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re

from policy import IMAGE, Blocked
import private

from . import ENGINE

CHECKOUT = ENGINE.parent
CONTROLLER = re.compile(r"service:[a-z0-9-]{1,32}")
COMMIT = re.compile(r"[0-9a-f]{40}")
REPOSITORY = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}")
REGISTRY = re.compile(r"[A-Za-z0-9._-]{1,64}")
FIELDS = {"controller_id", "engine", "app_id", "private_key_path", "webhook_secret_path", "state_dir",
          "registry_id", "targets", "agent_image", "execution_image", "limits", "gateway"}
GATEWAY_FIELDS = {"url", "operator_token_path", "executor", "workflow_ref", "models", "budget", "headers"}
GATEWAY_URL = re.compile(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~-]+)*")
WORKFLOW_REF = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}/\.github/workflows/[A-Za-z0-9._-]+\.ya?ml"
                          r"@refs/(?:heads|tags)/[A-Za-z0-9._/-]{1,100}")
MODEL_PATTERN = re.compile(r"[A-Za-z0-9.*_:-]{1,100}")
HEADER = re.compile(r"[A-Za-z0-9-]{1,64}")
PROVIDERS = ("claude", "codex")


class SettingsError(ValueError):
    """The settings file is missing, unsafe or invalid; the text names only the field."""


@dataclass(frozen=True)
class Target:
    """One enabled repository."""

    repository: str


@dataclass(frozen=True)
class Gateway:
    """The inference gateway every model call goes through, and where turns run.

    `executor` is `runner` (GitHub-hosted runners of the target, started by `repository_dispatch`
    and running `workflow_ref`) or `host` (this host's Docker). `models` are the patterns a
    session key allows, `budget` its token budget, and `headers` the route-selecting headers
    each provider's CLI sends.
    """

    url: str
    operator_token_path: Path
    executor: str
    workflow_ref: str | None
    models: tuple
    budget: int
    headers: dict


@dataclass(frozen=True)
class Limits:
    """Concurrent ticks, process-wide and per repository."""

    global_ticks: int
    per_repository: int


@dataclass(frozen=True)
class Settings:
    """Validated operator settings; host checks (engine HEAD, local images) run when `serve` starts.

    `engine` is None when the settings leave it to the checkout's HEAD, resolved at startup.
    """

    controller_id: str
    engine: str | None
    app_id: int
    private_key_path: Path
    webhook_secret_path: Path
    state_dir: Path
    registry_id: str
    targets: tuple
    agent_image: str
    execution_image: str
    limits: Limits
    gateway: Gateway


def require(condition, field):
    """Refuse one field without echoing its value."""
    if not condition:
        raise SettingsError("invalid settings field: " + field)


def absolute(value, field):
    """A canonical absolute path outside the engine checkout.

    Canonical means no symlink anywhere along it and no `.` or `..` component, so each
    path names exactly one location.
    """
    require(isinstance(value, str) and value and Path(value).is_absolute()
            and os.path.realpath(value) == value, field)
    try:
        return private.outside(value, (CHECKOUT,))
    except Blocked:
        raise SettingsError("invalid settings field: " + field) from None


def secret(value, field):
    """An owner-only regular file outside the engine checkout, with no other hard link; returns its identity."""
    path = absolute(value, field)
    try:
        private.read(path)
        info = path.lstat()
    except (OSError, Blocked):
        raise SettingsError("invalid settings field: " + field) from None
    require(info.st_nlink == 1, field)
    return path, (info.st_dev, info.st_ino)


def state(value):
    """The registry's existing owner-only directory."""
    path = absolute(value, "state_dir")
    try:
        return private.directory(path)
    except (OSError, Blocked):
        raise SettingsError("invalid settings field: state_dir") from None


def integer(value, field, low=1):
    """A positive integer, not a boolean."""
    require(type(value) is int and value >= low, field)
    return value


def load(path):
    """Read and validate a private settings file in a private directory."""
    path = Path(path)
    require(path.is_absolute() and os.path.realpath(path) == str(path), "settings path")
    try:
        private.directory(path.parent)
        data = json.loads(private.read(path, (CHECKOUT,)))
    except (OSError, ValueError, Blocked):
        raise SettingsError("settings file is missing, unreadable or not owner-only") from None
    require(isinstance(data, dict) and set(data) in (FIELDS, FIELDS - {"engine"}), "fields")
    require(isinstance(data["controller_id"], str) and CONTROLLER.fullmatch(data["controller_id"]), "controller_id")
    require("engine" not in data or isinstance(data["engine"], str) and COMMIT.fullmatch(data["engine"]), "engine")
    require(isinstance(data["registry_id"], str) and REGISTRY.fullmatch(data["registry_id"]), "registry_id")
    for field in ("agent_image", "execution_image"):
        require(isinstance(data[field], str) and IMAGE.fullmatch(data[field]), field)
    # Every secret file is distinct by filesystem identity, not by spelling.
    key, key_identity = secret(data["private_key_path"], "private_key_path")
    webhook, webhook_identity = secret(data["webhook_secret_path"], "webhook_secret_path")
    require(webhook_identity != key_identity, "webhook_secret_path")
    targets, repositories, identities = [], set(), {key_identity, webhook_identity}
    require(isinstance(data["targets"], list) and data["targets"], "targets")
    for entry in data["targets"]:
        require(isinstance(entry, dict) and set(entry) == {"repository"}, "targets")
        repository = entry["repository"]
        require(isinstance(repository, str) and REPOSITORY.fullmatch(repository)
                and repository.lower() not in repositories, "targets.repository")
        repositories.add(repository.lower())
        targets.append(Target(repository))
    gateway = gateway_settings(data["gateway"], identities)
    limits = data["limits"]
    require(isinstance(limits, dict) and set(limits) == {"global", "per_repository"}, "limits")
    total = integer(limits["global"], "limits.global")
    require(integer(limits["per_repository"], "limits.per_repository") <= total, "limits.per_repository")
    return Settings(
        controller_id=data["controller_id"], engine=data.get("engine"), app_id=integer(data["app_id"], "app_id"),
        private_key_path=key, webhook_secret_path=webhook,
        state_dir=state(data["state_dir"]), registry_id=data["registry_id"],
        targets=tuple(targets), agent_image=data["agent_image"], execution_image=data["execution_image"],
        limits=Limits(total, limits["per_repository"]), gateway=gateway)


def gateway_settings(data, identities):
    """The `gateway` block; its operator token is a secret file distinct from every other."""
    require(isinstance(data, dict) and set(data) == GATEWAY_FIELDS, "gateway")
    require(isinstance(data["url"], str) and GATEWAY_URL.fullmatch(data["url"]), "gateway.url")
    token, identity = secret(data["operator_token_path"], "gateway.operator_token_path")
    require(identity not in identities, "gateway.operator_token_path")
    require(data["executor"] in ("runner", "host"), "gateway.executor")
    workflow = data["workflow_ref"]
    require(workflow is None and data["executor"] == "host"
            or isinstance(workflow, str) and WORKFLOW_REF.fullmatch(workflow), "gateway.workflow_ref")
    models = data["models"]
    require(isinstance(models, list) and models and all(isinstance(m, str) and MODEL_PATTERN.fullmatch(m)
                                                        for m in models), "gateway.models")
    headers = data["headers"]
    require(isinstance(headers, dict) and set(headers) <= set(PROVIDERS) and all(
        isinstance(h, dict) and all(isinstance(k, str) and HEADER.fullmatch(k) and isinstance(v, str)
                                    and v.isprintable() and len(v) <= 200 for k, v in h.items())
        for h in headers.values()), "gateway.headers")
    return Gateway(data["url"], token, data["executor"], workflow, tuple(models), integer(data["budget"], "gateway.budget"),
                   headers)
