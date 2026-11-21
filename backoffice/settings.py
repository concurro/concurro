"""Backoffice settings: a JSON file outside the repository, validated before anything starts."""

import ipaddress
import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import private
from service import settings as service_settings
from service.list_index import read_token
from service.settings import CHECKOUT
from service.targets import Target

FIELDS = {
    "public_url",
    "client_id",
    "client_secret_path",
    "reader",
    "targets",
    "diagnostics_dir",
    "read_cache_dir",
    "vault_url",
    "static_dir",
    "service_url",
    "auth_state_dir",
    "auth_key_path",
    "lists_token_path",
    "relay_server",
    "relay_download_url",
    "task_api_url",
    "command_proof_key_path",
    "viewer_api_bearer_path",
}
REQUIRED = {"public_url", "client_id", "client_secret_path", "reader", "targets"}
DNS_NAME = re.compile(
    r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
)


class SettingsError(ValueError):
    """The settings file is missing or invalid; the text names only the field."""


@dataclass(frozen=True)
class Reader:
    """The read-only GitHub App the backoffice reads with; `service.identity.Identity` mints its tokens."""

    app_id: int
    private_key_path: Path
    targets: tuple[Target, ...]


@dataclass(frozen=True)
class Settings:
    """`public_url` is the external base the edge serves the backoffice under, e.g. `https://<host>/concurro`."""

    public_url: str
    client_id: str
    client_secret: str
    targets: tuple[Target, ...]
    reader: Reader
    diagnostics_dir: Path | None = None
    # Rebuildable issue snapshots; separate from the service registry and diagnostics.
    read_cache_dir: Path | None = None
    vault_url: str | None = None
    static_dir: Path | None = None
    # The service's internal address for commands; defaults to `http://concurro-service:8787`.
    service_url: str | None = None
    # Optional encrypted GitHub logout retry state; neither field changes provider credentials.
    auth_state_dir: Path | None = None
    auth_key_path: Path | None = None
    # The shared bearer for the service's list index; setting it switches list freshness to the index.
    lists_token: str | None = None
    # Public `host:port` Sponsor relays connect to, shown in the portal's launch command.
    relay_server: str | None = None
    # Where owners download Sponsor; the portal says none is configured when absent.
    relay_download_url: str | None = None
    # Paired M3 command bridge: the Python session authority signs for the Rust task API.
    task_api_url: str | None = None
    command_proof_key_path: Path | None = None
    # Read-scoped credential for the separate viewer-authority listener.
    viewer_api_bearer: str | None = None

    @property
    def base_path(self) -> str:
        """The path prefix of `public_url`, without a trailing slash."""
        return urlsplit(self.public_url).path.rstrip("/")

    def relay_command(self, *arguments: str) -> str | None:
        """A copyable `concurro-relay` command naming the configured relay address, if any."""
        if not self.relay_server:
            return None
        return shlex.join(["concurro-relay", *arguments, "--server", self.relay_server])

    @property
    def secure(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def callback_url(self) -> str:
        return self.public_url.rstrip("/") + "/auth/callback"


def require(condition: object, field: str) -> None:
    if not condition:
        raise SettingsError("invalid settings field: " + field)


def relay_address(value: object) -> bool:
    """Whether `value` is `host:port` with a DNS name, IPv4 address or bracketed IPv6 address."""
    if not isinstance(value, str):
        return False
    try:
        parts = urlsplit("//" + value)
        port = parts.port
    except ValueError:
        return False
    host = parts.hostname or ""
    if parts.netloc != value or parts.username is not None or not port or ":" not in value:
        return False
    if value.startswith("["):
        try:
            return isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address)
        except ValueError:
            return False
    return bool(DNS_NAME.fullmatch(host))


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
    reader = data["reader"]
    require(isinstance(reader, dict) and set(reader) == {"app_id", "private_key_path"}, "reader")
    try:
        app_id = service_settings.integer(reader["app_id"], "reader.app_id")
        reader_key, _ = service_settings.secret(reader["private_key_path"], "reader.private_key_path")
    except service_settings.SettingsError as error:
        raise SettingsError(str(error)) from None
    optional_path = {
        k: Path(data[k]) if data.get(k) else None
        for k in ("diagnostics_dir", "static_dir", "auth_state_dir", "auth_key_path")
    }
    require(bool(optional_path["auth_state_dir"]) == bool(optional_path["auth_key_path"]), "auth_state")
    if optional_path["auth_state_dir"]:
        try:
            private.outside(optional_path["auth_state_dir"], (CHECKOUT,))
            key = bytes.fromhex(private.read(optional_path["auth_key_path"], (CHECKOUT,)).decode().strip())
            require(len(key) == 32, "auth_key_path")
        except Exception:
            raise SettingsError("invalid settings field: auth_state") from None
    lists_token = None
    if data.get("lists_token_path"):
        try:
            lists_token = read_token(data["lists_token_path"])
        except Exception:
            raise SettingsError("invalid settings field: lists_token_path") from None
    cache_dir = data.get("read_cache_dir", os.environ.get("CONCURRO_READ_CACHE_DIR"))
    if cache_dir == "":
        cache_dir = None
    require(cache_dir is None or isinstance(cache_dir, str) and bool(cache_dir), "read_cache_dir")
    # Absent or empty means not configured; anything else must be a valid address.
    relay_server = None if data.get("relay_server") in (None, "") else data["relay_server"]
    require(relay_server is None or relay_address(relay_server), "relay_server")
    download = None if data.get("relay_download_url") in (None, "") else data["relay_download_url"]
    require(
        download is None
        or isinstance(download, str)
        and urlsplit(download).scheme == "https"
        and bool(urlsplit(download).netloc)
        and not any(c.isspace() for c in download),
        "relay_download_url",
    )
    task_api_url = data.get("task_api_url") or None
    proof_key_path = data.get("command_proof_key_path") or None
    require(bool(task_api_url) == bool(proof_key_path), "task_api_command_bridge")
    require(
        task_api_url is None
        or isinstance(task_api_url, str)
        and urlsplit(task_api_url).scheme in ("http", "https")
        and bool(urlsplit(task_api_url).netloc),
        "task_api_url",
    )
    if proof_key_path:
        try:
            private.read(proof_key_path, (CHECKOUT,))
        except Exception:
            raise SettingsError("invalid settings field: command_proof_key_path") from None
    viewer_api_bearer = None
    if data.get("viewer_api_bearer_path"):
        try:
            viewer_api_bearer = private.read(data["viewer_api_bearer_path"], (CHECKOUT,)).decode().strip()
            require(viewer_api_bearer, "viewer_api_bearer_path")
        except Exception:
            raise SettingsError("invalid settings field: viewer_api_bearer_path") from None
    return Settings(
        public_url=url.rstrip("/"),
        client_id=data["client_id"],
        client_secret=secret,
        targets=tuple(targets),
        reader=Reader(app_id, reader_key, tuple(targets)),
        read_cache_dir=Path(cache_dir) if cache_dir else None,
        vault_url=data.get("vault_url") or None,
        service_url=data.get("service_url") or None,
        lists_token=lists_token,
        relay_server=relay_server,
        relay_download_url=download,
        task_api_url=task_api_url,
        command_proof_key_path=Path(proof_key_path) if proof_key_path else None,
        viewer_api_bearer=viewer_api_bearer,
        **optional_path,
    )
