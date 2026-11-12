"""Operator settings outside any repository, validated strictly before anything else starts."""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import private
from policy import Blocked

from . import ENGINE
from .targets import Target, enrollment

CHECKOUT = ENGINE.parent
CONTROLLER = re.compile(r"service:[a-z0-9-]{1,32}")
COMMIT = re.compile(r"[0-9a-f]{40}")
REPOSITORY = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}")
REGISTRY = re.compile(r"[A-Za-z0-9._-]{1,64}")
FIELDS = {
    "controller_id",
    "engine",
    "app_id",
    "private_key_path",
    "webhook_secret_path",
    "state_dir",
    "registry_id",
    "targets",
    "limits",
    "gateway",
    "diagnostics_dir",
    "delegation_key_path",
    "vault_metadata_url",
    "vault_service_token_path",
}
OPTIONAL = {"engine", "diagnostics_dir", "delegation_key_path", "vault_metadata_url", "vault_service_token_path"}
GATEWAY_FIELDS = {"url", "operator_token_path", "executor", "workflow_ref", "models", "budget", "headers"}
GATEWAY_URL = re.compile(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~-]+)*")
WORKFLOW_REF = re.compile(
    r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}/\.github/workflows/[A-Za-z0-9._-]+\.ya?ml"
    r"@refs/(?:heads|tags)/[A-Za-z0-9._/-]{1,100}"
)
MODEL_PATTERN = re.compile(r"[A-Za-z0-9.*_:-]{1,100}")
HEADER = re.compile(r"[A-Za-z0-9-]{1,64}")
PROVIDERS = ("claude", "codex")


class SettingsError(ValueError):
    """The settings file is missing, unsafe or invalid; the text names only the field."""


@dataclass(frozen=True)
class Gateway:
    """Central inference and task-bound GitHub runner dispatch settings."""

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
    """Validated settings; an optional engine pin resolves to the clean checkout at startup."""

    controller_id: str
    engine: str | None
    app_id: int
    private_key_path: Path
    webhook_secret_path: Path
    state_dir: Path
    registry_id: str
    targets: tuple
    limits: Limits
    gateway: Gateway
    # Run diagnostics are recorded here when set (see `service.diagnostics`).
    diagnostics_dir: Path | None = None
    # Signs commands the backoffice delivers (`engine/delegation.py`); without it they are refused.
    delegation_key_path: Path | None = None
    vault_metadata_url: str | None = None  # Private nonsecret metadata listener.
    vault_service_token_path: Path | None = None  # Separate service bearer, never a user token.


def require(condition, field):
    """Refuse one field without echoing its value."""
    if not condition:
        raise SettingsError("invalid settings field: " + field)


def absolute(value, field):
    """A canonical absolute path outside the engine checkout.

    Canonical means no symlink anywhere along it and no `.` or `..` component, so each
    path names exactly one location.
    """
    require(isinstance(value, str) and value and Path(value).is_absolute() and os.path.realpath(value) == value, field)
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
    require(isinstance(data, dict) and FIELDS - OPTIONAL <= set(data) <= FIELDS, "fields")
    require(isinstance(data["controller_id"], str) and CONTROLLER.fullmatch(data["controller_id"]), "controller_id")
    require("engine" not in data or isinstance(data["engine"], str) and COMMIT.fullmatch(data["engine"]), "engine")
    require(isinstance(data["registry_id"], str) and REGISTRY.fullmatch(data["registry_id"]), "registry_id")
    # Every secret file is distinct by filesystem identity, not by spelling.
    key, key_identity = secret(data["private_key_path"], "private_key_path")
    webhook, webhook_identity = secret(data["webhook_secret_path"], "webhook_secret_path")
    require(webhook_identity != key_identity, "webhook_secret_path")
    targets, identities = [], {key_identity, webhook_identity}
    require(isinstance(data["targets"], list) and data["targets"], "targets")
    for entry in data["targets"]:
        require(isinstance(entry, dict) and set(entry) == {"upstream", "fork", "issues"}, "targets")
        for name in ("upstream", "fork"):
            require(isinstance(entry[name], str) and REPOSITORY.fullmatch(entry[name]), "targets." + name)
        require(entry["issues"] in ("upstream", "fork", "both"), "targets.issues")
        require(entry["upstream"].lower() != entry["fork"].lower(), "targets.fork")
        targets.append(Target(**entry))
    try:
        enrollment(targets)
    except ValueError:
        raise SettingsError("invalid settings field: targets") from None
    delegation = None
    if "delegation_key_path" in data:
        delegation, delegation_identity = secret(data["delegation_key_path"], "delegation_key_path")
        require(delegation_identity not in identities, "delegation_key_path")
        identities.add(delegation_identity)
    metadata_url, metadata_token = data.get("vault_metadata_url"), None
    require((metadata_url is None) == (data.get("vault_service_token_path") is None), "vault metadata")
    if metadata_url is not None:
        require(
            isinstance(metadata_url, str) and re.fullmatch(r"https?://[A-Za-z0-9.-]+(?::[0-9]{1,5})?", metadata_url),
            "vault_metadata_url",
        )
        metadata_token, metadata_identity = secret(data["vault_service_token_path"], "vault_service_token_path")
        require(metadata_identity not in identities, "vault_service_token_path")
        identities.add(metadata_identity)
    gateway = gateway_settings(data["gateway"], identities)
    limits = data["limits"]
    require(isinstance(limits, dict) and set(limits) == {"global", "per_repository"}, "limits")
    total = integer(limits["global"], "limits.global")
    require(integer(limits["per_repository"], "limits.per_repository") <= total, "limits.per_repository")
    return Settings(
        controller_id=data["controller_id"],
        engine=data.get("engine"),
        app_id=integer(data["app_id"], "app_id"),
        private_key_path=key,
        webhook_secret_path=webhook,
        state_dir=state(data["state_dir"]),
        registry_id=data["registry_id"],
        targets=tuple(targets),
        limits=Limits(total, limits["per_repository"]),
        gateway=gateway,
        diagnostics_dir=absolute(data["diagnostics_dir"], "diagnostics_dir") if "diagnostics_dir" in data else None,
        delegation_key_path=delegation,
        vault_metadata_url=metadata_url,
        vault_service_token_path=metadata_token,
    )


def gateway_settings(data, identities):
    """The `gateway` block; its operator token is a secret file distinct from every other."""
    require(isinstance(data, dict) and set(data) == GATEWAY_FIELDS, "gateway")
    require(isinstance(data["url"], str) and GATEWAY_URL.fullmatch(data["url"]), "gateway.url")
    token, identity = secret(data["operator_token_path"], "gateway.operator_token_path")
    require(identity not in identities, "gateway.operator_token_path")
    require(data["executor"] == "runner", "gateway.executor")
    workflow = data["workflow_ref"]
    require(isinstance(workflow, str) and WORKFLOW_REF.fullmatch(workflow), "gateway.workflow_ref")
    models = data["models"]
    require(
        isinstance(models, list) and models and all(isinstance(m, str) and MODEL_PATTERN.fullmatch(m) for m in models),
        "gateway.models",
    )
    headers = data["headers"]
    require(
        isinstance(headers, dict)
        and set(headers) <= set(PROVIDERS)
        and all(
            isinstance(h, dict)
            and all(
                isinstance(k, str) and HEADER.fullmatch(k) and isinstance(v, str) and v.isprintable() and len(v) <= 200
                for k, v in h.items()
            )
            for h in headers.values()
        ),
        "gateway.headers",
    )
    return Gateway(
        data["url"],
        token,
        data["executor"],
        workflow,
        tuple(models),
        integer(data["budget"], "gateway.budget"),
        headers,
    )
