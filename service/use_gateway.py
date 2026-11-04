"""Move a service settings file to the inference gateway: `python3 -m service.use_gateway --help`.

Drops per-target provider credentials, any engine pin (the service then runs its checkout's
HEAD, which runner sessions must match) and, for runners, the local images; adds the `gateway`
block, and replaces the file only once the result validates.
"""

import argparse
import json
import os
from pathlib import Path
import sys

from . import settings as settings_module

WORKFLOW_REF = "concurro/concurro/.github/workflows/agent.yml@refs/heads/main"


def migrate(data, url, token, executor="runner", workflow_ref=WORKFLOW_REF, models=("claude-*", "gpt-*"),
            budget=20_000_000, headers=None):
    """The settings with the gateway block in place of provider credentials and the engine pin."""
    # With runners, this host runs no containers, so it names no images.
    dropped = {"engine"} | ({"agent_image", "execution_image"} if executor == "runner" else set())
    data = {k: v for k, v in data.items() if k not in dropped}
    data["targets"] = [{"repository": t["repository"]} for t in data["targets"]]
    data["gateway"] = {"url": url, "operator_token_path": str(token), "executor": executor,
                       "workflow_ref": workflow_ref if executor == "runner" else None, "models": list(models),
                       "budget": budget, "headers": {"codex": {"x-concurro-plan": "chatgpt"}} if headers is None else headers}
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m service.use_gateway", description=__doc__)
    parser.add_argument("--settings", required=True, type=Path, help="private settings file, rewritten in place")
    parser.add_argument("--url", required=True, help="gateway base URL, e.g. https://host/concurro")
    parser.add_argument("--operator-token", required=True, type=Path, help="owner-only file holding the operator token")
    parser.add_argument("--executor", choices=("runner", "host"), default="runner")
    parser.add_argument("--workflow-ref", default=WORKFLOW_REF)
    arguments = parser.parse_args(argv)
    path = arguments.settings
    data = migrate(json.loads(path.read_text()), arguments.url, arguments.operator_token.resolve(), arguments.executor,
                   arguments.workflow_ref)
    staged = path.with_name(path.name + ".new")
    descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(data, handle, indent=2)
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
