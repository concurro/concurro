"""Backoffice settings: a JSON file outside the repository, validated before anything starts."""

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from service.targets import Target

FIELDS = {"public_url", "client_id", "client_secret_path", "targets", "diagnostics_dir", "vault_url", "static_dir"}
REQUIRED = {"public_url", "client_id", "client_secret_path", "targets"}


class SettingsError(ValueError):
    """The settings file is missing or invalid; the text names only the field."""


@dataclass(frozen=True)
class Settings:
    """`public_url` is the external base the edge serves the backoffice under, e.g. `https://<host>/concurro`."""

    public_url: str
    client_id: str
    client_secret: str
    targets: tuple[Target, ...]
    diagnostics_dir: Path | None = None
    vault_url: str | None = None
    static_dir: Path | None = None

    @property
    def base_path(self) -> str:
        """The path prefix of `public_url`, without a trailing slash."""
        return urlsplit(self.public_url).path.rstrip("/")

    @property
    def secure(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def callback_url(self) -> str:
        return self.public_url.rstrip("/") + "/auth/callback"


def require(condition: object, field: str) -> None:
    if not condition:
        raise SettingsError("invalid settings field: " + field)


def load(path: str | Path) -> Settings:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        raise SettingsError("settings file is missing or unreadable") from None
    require(isinstance(data, dict) and REQUIRED <= set(data) <= FIELDS, "fields")
    url = data["public_url"]
    require(isinstance(url, str) and urlsplit(url).scheme in ("http", "https") and urlsplit(url).netloc, "public_url")
    require(isinstance(data["client_id"], str) and data["client_id"], "client_id")
    try:
        secret = Path(data["client_secret_path"]).read_text().strip()
    except (OSError, TypeError):
        raise SettingsError("invalid settings field: client_secret_path") from None
    require(secret, "client_secret_path")
    require(isinstance(data["targets"], list) and data["targets"], "targets")
    targets = []
    for entry in data["targets"]:
        require(isinstance(entry, dict) and set(entry) == {"upstream", "fork", "issues"}, "targets")
        require(entry["issues"] in ("upstream", "fork", "both"), "targets.issues")
        targets.append(Target(**entry))
    optional_path = {k: Path(data[k]) if data.get(k) else None for k in ("diagnostics_dir", "static_dir")}
    return Settings(
        public_url=url.rstrip("/"),
        client_id=data["client_id"],
        client_secret=secret,
        targets=tuple(targets),
        vault_url=data.get("vault_url") or None,
        **optional_path,
    )
