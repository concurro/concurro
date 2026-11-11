"""Load and validate a target's pinned configuration and bundle."""

import tempfile
from dataclasses import dataclass
from pathlib import Path

from bundle import CHARTER, TARGET_INSTRUCTIONS, Bundle
from controller import validate_config
from github import NotFound
from model_config import parse as parse_models
from policy import Blocked

from .github_reads import content, head
from .settings import CHECKOUT
from .target_config import read_remote


@dataclass(frozen=True)
class Target:
    """One repository's validated configuration and bundle at its default-branch head."""

    config: dict
    bundle: Bundle
    config_sha: str


def load_target(target, reader, engine, bot_login, engine_root=CHECKOUT, config_sha=None):
    """Read JSON or YAML `.concurro/` configuration at the default-branch head, through the read token.

    The target must name the verified App. Its `engine` pin is
    optional: without one, it runs whatever engine the operator serves; with one, only that. Files are staged in a private temporary directory only while the
    bundle reads them.
    """
    repository = target.fork
    sha = config_sha or head(reader, repository)
    config = read_remote(lambda path: content(reader, repository, path, sha))
    if not isinstance(config, dict):
        raise Blocked("target configuration must be an object")
    validate_config(config)
    active = (
        {a.profile.harness for a in parse_models(config).profiles}
        if "agents" in config
        else {config["planner"], config["implementer"], config["qc"], *config["reviewers"]}
    )
    if not active <= {"claude", "codex"}:
        raise Blocked("the MVP runner supports Claude and Codex roles only")
    if config["repository"].lower() != repository.lower():
        raise Blocked("target configuration names another repository")
    if "engine" in config and config["engine"] != engine:
        raise Blocked("target pins another engine commit")
    if config["bot_login"] != bot_login:
        raise Blocked("target bot_login differs from the controller App")
    paths = [CHARTER]
    with tempfile.TemporaryDirectory(prefix="concurro-target-") as staging:
        root = Path(staging)
        root.chmod(0o700)
        for path in paths + sorted(TARGET_INSTRUCTIONS):
            try:
                text = content(reader, repository, path, sha)
            except NotFound:
                if path in TARGET_INSTRUCTIONS:
                    continue
                raise
            file = root / path
            file.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            file.write_text(text, encoding="utf-8")
            file.chmod(0o600)
        bundle = Bundle(engine_root, target=root)
    return Target(config, bundle, sha)
