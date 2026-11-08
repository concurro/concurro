"""One native GitHub runner role: claim, trusted setup heartbeat, sequential turns and cleanup."""

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import urllib.request

from mirror import Mirror, git_environment
from relay import ExchangeError, Runner
from policy import runner_bound
from vocab import TurnOutcome
import turns

AUDIENCE = "concurro-gateway"
HEARTBEAT = 30


def oidc_token():
    """Request fresh GitHub proof of this job for the exchange."""
    request = urllib.request.Request(
        os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"] + "&audience=" + AUDIENCE,
        headers={"Authorization": "bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)["value"]


def log(message):
    """Write lifecycle diagnostics without payloads or credentials."""
    print(time.strftime("%H:%M:%S"), message, flush=True)


def heartbeat(client, stopped, interval=HEARTBEAT):
    """Renew while setup or a turn runs; loss of the session stops native processes."""
    last = time.monotonic()
    while not stopped.is_set():
        try:
            client.next(0, 0)
            last = time.monotonic()
        except ExchangeError as error:
            if error.status in (401, 403, 409, 410) or time.monotonic() - last >= 150:
                stopped.set()
                return
        stopped.wait(interval)


class Loop:
    """Run sequential turns in a stable workspace, with a separate heartbeat during long turns."""

    def __init__(self, runner, key, mirror, root, idle=600):
        self.runner, self.key, self.mirror, self.root = runner, key, mirror, Path(root)
        self.idle, self.stopped = idle, threading.Event()

    def serve(self):
        """Serve only this bound role until idle, revoked or expired."""
        last = time.monotonic()
        thread = threading.Thread(target=heartbeat, args=(self.runner, self.stopped), daemon=True)
        thread.start()
        try:
            while not self.stopped.is_set():
                try:
                    taken = self.runner.next(1, HEARTBEAT)
                except ExchangeError as error:
                    if error.status in (401, 403, 409, 410):
                        return
                    self.stopped.wait(5)
                    continue
                for turn in taken:
                    if not self.stopped.is_set():
                        self.run(turn)
                        if runner_bound(turn["payload"]["request"]["stage"]):
                            return  # Long-running turns have their own job clock.
                    last = time.monotonic()
                if not taken and time.monotonic() - last > self.idle:
                    return
        finally:
            self.stopped.set()
            thread.join(35)

    def run(self, turn):
        """Execute and acknowledge one turn; the provider journal remains recoverable in its result."""
        identifier, payload = turn["id"], turn["payload"]
        # Stable across turns and replacement jobs: Claude's transcript lookup includes the cwd.
        directory = self.root / ("turn-" + "0" * 32)
        directory.mkdir(parents=True, mode=0o700)
        log(f"turn {identifier}: {payload['request']['stage']} ({payload['request']['provider']})")
        try:
            try:
                result = turns.execute(payload, self.key, self.mirror, directory, self.stopped)
            except Exception as error:
                log(f"turn {identifier} could not run ({type(error).__name__}); details suppressed")
                result = {"outcome": TurnOutcome.FAILED, "response": None, "workspace": None, "retained": None}
            for attempt in range(5):
                if self.stopped.is_set():
                    return
                try:
                    self.runner.complete(identifier, result)
                    return
                except ExchangeError as error:
                    if error.status in (401, 403, 409, 410):
                        self.stopped.set()
                        return
                    self.stopped.wait(5 * (attempt + 1))
            # Do not accept more work when the result's acknowledgment is uncertain.
            self.stopped.set()
        finally:
            turns.remove(directory)


def save_grant(root, grant):
    """Persist the claimed session locally before starting setup; never print its tokens."""
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    with os.fdopen(os.open(root / "grant.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump(grant, stream)


def load_grant(root, client):
    """Restore the claimed session for the following workflow steps."""
    grant = json.loads((root / "grant.json").read_text())
    client.token = grant["token"]
    return grant


def stop_helper(root):
    """Stop the detached setup heartbeat after its successor has renewed the session."""
    path = root / "heartbeat.pid"
    if path.exists():
        try:
            os.kill(int(path.read_text()), signal.SIGTERM)
        except ProcessLookupError:
            pass
        path.unlink()


def checkout(repository, sha, path):
    """Materialize an exact public commit without persisting a GitHub credential."""
    environment = git_environment()
    for command in (
        ["git", "init", "--quiet", str(path)],
        ["git", "-C", str(path), "fetch", "--quiet", "--depth=1", "https://github.com/" + repository + ".git", sha],
        ["git", "-C", str(path), "checkout", "--quiet", "--detach", "FETCH_HEAD"],
    ):
        subprocess.run(command, check=True, capture_output=True, env=environment, timeout=1800)


def prepare(grant, config, base):
    """Use centrally bound repositories and SHAs, not dispatch inputs, for trusted target setup."""
    binding = grant["binding"]
    checkout(binding["fork"], binding["config_sha"], config)
    checkout(binding["upstream"], binding["base"], base)
    setup = config / ".concurro/setup/action.yml"
    if setup.is_symlink() or any(p.is_symlink() for p in (setup.parent, setup.parent.parent)):
        raise ValueError("target setup must be a regular composite action")
    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write(f"setup={'true' if setup.is_file() else 'false'}\nbase={base}\n")


def end_session(root, client):
    """Forget the grant only after confirmed retirement; an ended session is already clean."""
    try:
        client.end()
    except ExchangeError as error:
        if error.status != 410:
            raise
    (root / "grant.json").unlink(missing_ok=True)


def renew_before_serve(client):
    """Retry a transient handover failure while the setup helper continues renewing."""
    for attempt in range(5):
        try:
            client.next(0, 0)
            return
        except ExchangeError as error:
            if error.status in (401, 403, 409, 410) or attempt == 4:
                raise
            time.sleep(5)


def main():
    """Workflow entry points; claim starts the setup heartbeat before tool installation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("claim", "heartbeat", "prepare", "serve", "end"))
    parser.add_argument("--session", required=True)
    parser.add_argument("--gateway", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--config")
    parser.add_argument("--base")
    parser.add_argument("--idle", type=int, default=600)
    args = parser.parse_args()
    root, client = Path(args.root).resolve(), Runner(args.gateway, args.session)
    if args.command == "claim":
        grant = client.key(oidc_token())
        if not grant.get("binding"):
            client.end()
            raise ValueError("session has no task/role binding")
        save_grant(root, grant)
        helper = subprocess.Popen(
            [
                sys.executable,
                __file__,
                "heartbeat",
                "--session",
                args.session,
                "--gateway",
                args.gateway,
                "--root",
                str(root),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        (root / "heartbeat.pid").write_text(str(helper.pid))
        return
    if args.command == "end" and not (root / "grant.json").exists():
        return
    grant = load_grant(root, client)
    if args.command == "heartbeat":
        stopped = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stopped.set())
        heartbeat(client, stopped)
    elif args.command == "prepare":
        if not args.config or not args.base:
            parser.error("prepare needs --config and --base")
        prepare(grant, Path(args.config).resolve(), Path(args.base).resolve())
    elif args.command == "serve":
        # Renew first, then hand off the heartbeat with no gap after long setup.
        renew_before_serve(client)
        stop_helper(root)
        binding = grant["binding"]
        mirror = Mirror(root / "mirrors", binding["upstream"], sources=(binding["fork"],))
        try:
            Loop(client, grant["key"], mirror, root / "turns", args.idle).serve()
        finally:
            try:
                end_session(root, client)
            except ExchangeError:
                pass  # The always() step retries; exchange expiry remains the final fallback.
    elif args.command == "end":
        try:
            end_session(root, client)
        finally:
            stop_helper(root)


if __name__ == "__main__":
    main()
