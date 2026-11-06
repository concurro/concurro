"""Convert a legacy target config to the native-runner contract, writing a separate reviewed file."""

import argparse
from copy import deepcopy
import json
from pathlib import Path

from controller import validate_config
from .target_config import read_local

REMOVED = {"execution_profiles", "execution_profile", "required_checks", "merge_enabled",
           "local_reviewer", "local_reviewer_model", "max_local_repairs", "max_local_seconds",
           "max_check_seconds", "max_session_seconds", "local_review_model", "max_local_fix_rounds",
           "local_timeout_seconds", "local_check_timeout_seconds", "max_validation_repairs"}


def migrate(config):
    """Preserve target policy and model choices; refuse custom profiles needing a human translation."""
    data = deepcopy(config)
    profiles = data.get("execution_profiles", [data["execution_profile"]] if "execution_profile" in data else [])
    if profiles not in ([], ["rust"]):
        raise ValueError("translate custom profiles into target setup, instructions and path policy before migrating")
    for key in REMOVED:
        data.pop(key, None)
    paths = data["paths"]
    if profiles == ["rust"]:
        paths["protected"] = list(dict.fromkeys([*paths.get("protected", []), "**/rust-toolchain*", "**/rustfmt.toml"]))
        paths["generated"] = list(dict.fromkeys([*paths.get("generated", []), "**/Cargo.lock"]))
    validate_config(data)
    return data


def main(argv=None):
    """Write a new configuration without replacing the input or an existing output."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    data = migrate(read_local(args.config))
    with args.output.open("x") as handle:
        handle.write(json.dumps(data, indent=2) + "\n")


if __name__ == "__main__":
    main()
