"""Move a service settings file to the inference gateway: `python3 -m service.use_gateway --help`.

Drops per-target provider credentials, any engine pin (the service then runs its checkout's
HEAD, which runner sessions must match) and, for runners, the local images; adds the `gateway`
block, and replaces the file only once the result validates.
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from . import settings as settings_module

WORKFLOW_REF = "concurro/concurro/.github/workflows/agent.yml@refs/heads/deployed"


def migrate(
    data,
    url,
    token,
    executor="runner",
    workflow_ref=WORKFLOW_REF,
    models=("claude-*", "gpt-*"),
    budget=20_000_000,
    headers=None,
    upstream=None,
    fork=None,
    issues="fork",
):
    """The settings with the gateway block in place of provider credentials and the engine pin."""
    # With runners, this host runs no containers, so it names no images.
    dropped = {"engine"} | ({"agent_image", "execution_image"} if executor == "runner" else set())
    data = {k: v for k, v in data.items() if k not in dropped}
    if upstream is not None or fork is not None:
        if not upstream or not fork or len(data["targets"]) != 1:
            raise ValueError("provide both --upstream and --fork for a single-target migration")
        data["targets"] = [{"upstream": upstream, "fork": fork, "issues": issues}]
    elif any(set(t) != {"upstream", "fork", "issues"} for t in data["targets"]):
        raise ValueError(
            "legacy targets need explicit --upstream and --fork; multi-target settings must be edited explicitly"
        )
    data["gateway"] = {
        "url": url,
        "operator_token_path": str(token),
        "executor": executor,
        "workflow_ref": workflow_ref if executor == "runner" else None,
        "models": list(models),
        "budget": budget,
        "headers": {"codex": {"x-concurro-plan": "chatgpt"}} if headers is None else headers,
    }
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m service.use_gateway", description=__doc__)
    parser.add_argument("--settings", required=True, type=Path, help="private settings file, rewritten in place")
    parser.add_argument("--url", required=True, help="gateway base URL, e.g. https://host/concurro")
    parser.add_argument("--operator-token", required=True, type=Path, help="owner-only file holding the operator token")
    parser.add_argument("--executor", choices=("runner",), default="runner")
    parser.add_argument("--workflow-ref", default=WORKFLOW_REF)
    parser.add_argument("--upstream", help="upstream repository for a single legacy target")
    parser.add_argument("--fork", help="existing fork for a single legacy target")
    parser.add_argument("--issues", choices=("upstream", "fork", "both"), default="fork")
    arguments = parser.parse_args(argv)
    path = arguments.settings
    try:
        data = migrate(
            json.loads(path.read_text()),
            arguments.url,
            arguments.operator_token.resolve(),
            arguments.executor,
            arguments.workflow_ref,
            upstream=arguments.upstream,
            fork=arguments.fork,
            issues=arguments.issues,
        )
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    staged = Path(temporary)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(data, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        settings_module.load(staged)
    except settings_module.SettingsError as error:
        staged.unlink()
        print(error, file=sys.stderr)
        return 1
    os.replace(staged, path)
    print("updated", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
