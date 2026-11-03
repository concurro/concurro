"""`serve` runs the daemon; every other command is a request to it over the control channel."""

import argparse
from dataclasses import replace
import json
import logging
import os
import pwd
import signal
import subprocess
import sys
import threading
import time

from policy import Blocked
import private

from . import control, ingress, settings as settings_module
from .cutover import Cutover
from .errors import GatewayStop, RegistryError
from .gateway import MutationGateway
from .identity import Identity, IdentityError
from .reconciler import Reconciler
from .registry import Registry
from .scheduler import Scheduler
from .settings import CHECKOUT, SettingsError
from .transport import Transport
from .worker import Worker

POLL_SECONDS = 300
DISPATCH_SECONDS = 1
LISTEN = "127.0.0.1:8787"

log = logging.getLogger("concurro.service")


class StartupError(RuntimeError):
    """A host check failed; the service does not start."""


def host_checks(settings):
    """Settings with the engine resolved: the checkout is clean at its HEAD, and both images exist locally.

    Settings that name an engine commit require the checkout to be at it; without one, the
    service serves the checkout's HEAD.
    """
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=CHECKOUT, capture_output=True, text=True, timeout=30)
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=CHECKOUT, capture_output=True, text=True, timeout=30)
    if head.returncode or dirty.returncode or dirty.stdout.strip():
        raise StartupError("the engine checkout must be clean")
    engine = head.stdout.strip()
    if settings.engine not in (None, engine):
        raise StartupError("the engine checkout must be at the configured engine commit")
    for image in (settings.agent_image, settings.execution_image):
        if subprocess.run(["docker", "image", "inspect", image], capture_output=True, timeout=30).returncode:
            raise StartupError("a configured image is unavailable locally")
    return replace(settings, engine=engine)


class Daemon:
    """One process: ingress, reconciler, scheduler, worker pool, gateway and control channel."""

    def __init__(self, settings, initialize=False, listen=LISTEN, poll_seconds=POLL_SECONDS):
        self.settings, self.initialize, self.poll_seconds = settings, initialize, poll_seconds
        host, _, port = listen.rpartition(":")
        self.listen = (host, int(port))
        self.stopping = threading.Event()

    def start(self):
        """Check the host, verify the App, open the registry and start serving."""
        self.settings = settings = host_checks(self.settings)
        log.info("serving engine %s", settings.engine)
        # Process-wide values, fixed at startup; nothing per task enters the environment.
        reports = settings.state_dir / "reports"
        reports.mkdir(mode=0o700, exist_ok=True)
        os.environ.update(CONCURRO_AGENT_IMAGE=settings.agent_image, CONCURRO_EXECUTION_IMAGE=settings.execution_image,
                          CONCURRO_LOCAL_REPORT_DIR=str(reports))
        secret = private.read(settings.webhook_secret_path, (CHECKOUT,)).strip()
        self.identity = Identity(settings)
        bot_login = self.identity.verify()
        repositories = [target.repository for target in settings.targets]
        installations = {repository: self.identity.installation(repository) for repository in repositories}
        readers = {r: Transport(r, self.identity.credential(r, "read"), time.time) for r in repositories}
        writers = {r: Transport(r, self.identity.credential(r, "write"), time.time) for r in repositories}
        self.registry = Registry.open(settings.state_dir, settings.registry_id, initialize=self.initialize)
        gateway = MutationGateway(self.registry, writers.__getitem__, readers.__getitem__, bot_login)
        self.reconciler = Reconciler(self.registry, readers.__getitem__, repositories, {bot_login})
        worker = Worker(settings, gateway, self.registry, self.reconciler, readers.__getitem__, bot_login)
        self.scheduler = Scheduler(self.registry, gateway, worker, settings.limits)
        cutover = Cutover(settings, self.identity, gateway, self.registry, readers.__getitem__, worker.load)
        self.http = ingress.server(ingress.Ingress(secret, self.registry, installations, {bot_login}), *self.listen)
        self.control = control.server(control.Control(self.registry, self.reconciler, cutover), settings.state_dir)
        for name, target in (("ingress", self.http.serve_forever), ("control", self.control.serve_forever)):
            threading.Thread(target=target, name=name, daemon=True).start()
        log.info("serving %d repositories as %s on %s:%d", len(repositories), bot_login, *self.listen)

    def run(self):
        """Dispatch due work and poll for missed input until asked to stop."""
        poller, next_poll = None, 0.0
        try:
            while not self.stopping.wait(DISPATCH_SECONDS):
                self.scheduler.dispatch()
                if time.monotonic() >= next_poll and not (poller and poller.is_alive()):
                    poller = threading.Thread(target=self.reconciler.poll, name="poll", daemon=True)
                    poller.start()
                    next_poll = time.monotonic() + self.poll_seconds
        except GatewayStop as error:
            # Durable state failed: scheduling and mutation admission stop entirely.
            log.error("stopping: %s", error)
        finally:
            self.stop()

    def stop(self):
        """Stop accepting work, let running ticks finish, then revoke tokens and release the registry."""
        self.stopping.set()
        self.http.shutdown()
        self.control.shutdown()
        self.scheduler.shutdown()
        self.identity.close()
        self.registry.close()


def serve(arguments, settings):
    """Run the daemon in the foreground; the first SIGINT or SIGTERM drains running ticks."""
    daemon = Daemon(settings, arguments.initialize, arguments.listen, arguments.poll_seconds)
    daemon.start()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: daemon.stopping.set())
    daemon.run()
    return 0


def command(arguments):
    """The control request for a client command."""
    if arguments.command == "resolve":
        return {"command": "resolve", "intent": arguments.intent, "disposition": arguments.disposition,
                "evidence": arguments.evidence, "operator": arguments.operator}
    if arguments.command == "adopt":
        return {"command": "adopt", "repository": arguments.repository, "issues": arguments.issues}
    if arguments.command == "release":
        return {"command": "release", "repository": arguments.repository, "issue": arguments.issue}
    return {"command": arguments.command}


def parser():
    """The command line."""
    root = argparse.ArgumentParser(prog="python3 -m service", description=__doc__)
    root.add_argument("--settings", required=True, help="private settings file")
    commands = root.add_subparsers(dest="command", required=True)
    serve_command = commands.add_parser("serve", help="run the daemon")
    serve_command.add_argument("--initialize", action="store_true", help="create a new, empty registry")
    serve_command.add_argument("--listen", default=LISTEN, help="webhook address, HOST:PORT")
    serve_command.add_argument("--poll-seconds", type=int, default=POLL_SECONDS)
    commands.add_parser("status", help="show every task the registry knows")
    commands.add_parser("reconcile-once", help="poll every target now")
    adopt = commands.add_parser("adopt", help="move tasks from the Actions controller to this service")
    adopt.add_argument("repository")
    adopt.add_argument("issues", nargs="*", type=int)
    release = commands.add_parser("release", help="hand an idle task back to the Actions controller")
    release.add_argument("repository")
    release.add_argument("issue", type=int)
    resolve = commands.add_parser("resolve", help="record what happened to a write of unknown outcome")
    resolve.add_argument("intent")
    resolve.add_argument("disposition", choices=("applied", "not-applied"))
    resolve.add_argument("evidence")
    resolve.add_argument("--operator", default=pwd.getpwuid(os.getuid()).pw_name)
    return root


def main():
    """Validate settings, then serve or send one command to the daemon."""
    arguments = parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        settings = settings_module.load(arguments.settings)
        if arguments.command == "serve":
            return serve(arguments, settings)
        reply = control.request(settings.state_dir, command(arguments))
    except (SettingsError, StartupError, RegistryError, IdentityError, Blocked, ConnectionError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(reply.get("result") if reply["ok"] else {"error": reply["error"]}, indent=2))
    return 0 if reply["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
