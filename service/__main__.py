"""`serve` runs the daemon; every other command is a request to it over the control channel."""

import argparse
import json
import logging
import os
import pwd
import signal
import subprocess
import sys
import threading
import time
from dataclasses import replace
from urllib.parse import quote

import delegation
import private
from identities import Actor
from policy import Blocked

from . import control, credential_order, ingress, retro, settings as settings_module, user_grant
from .actions import Actions
from .bus import BusWorker
from .cutover import Cutover
from .diagnostics import Store
from .errors import GatewayStop, RegistryError
from .gateway import MutationGateway
from .identity import Identity, IdentityError
from .list_index import ListIndex, Lists, read_token
from .reconciler import Reconciler
from .registry import Registry
from .scheduler import Scheduler
from .settings import CHECKOUT, SettingsError
from .shadow import Metadata, Shadow
from .targets import review_targets, verify
from .transport import Transport
from .webhook_recovery import DeliveryBridge, Recovery
from .worker import Worker, operator

POLL_SECONDS = 300
DISPATCH_SECONDS = 1
LISTEN = "127.0.0.1:8787"
REQUEST_FIELDS = {
    "resolve": ("intent", "disposition", "evidence", "operator"),
    "recover-review": ("intent", "operator"),
    "evaluate": ("repository", "issue", "provider"),
    "adopt": ("repository", "issues"),
    "release": ("repository", "issue"),
}

log = logging.getLogger("concurro.service")


class StartupError(RuntimeError):
    """A host check failed; the service does not start."""


def host_checks(settings):
    """Settings with the engine resolved: the checkout is clean at its HEAD, without target execution on this host.

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
        secret = private.read(settings.webhook_secret_path, (CHECKOUT,)).strip()
        self.identity = Identity(settings)
        bot_login = self.identity.verify()
        repositories = sorted({repository for target in settings.targets for repository in target.repositories})
        issue_repositories = sorted(
            {repository for target in settings.targets for repository in target.issue_repositories}
        )
        installations = {repository: self.identity.installation(repository) for repository in repositories}
        readers = {r: Transport(r, self.identity.credential(r, "read"), time.time) for r in repositories}
        verify(settings.targets, readers.__getitem__)
        writers = {r: Transport(r, self.identity.credential(r, "write"), time.time) for r in repositories}
        app = app_actor(readers[repositories[0]], bot_login)
        # Standalone reviews are published with the reviewer account's own grant, one credential for every upstream.
        reviews, reviewer, as_reviewer = {}, None, {}
        if settings.reviewer:
            grant = user_grant.UserGrant(settings.reviewer, settings.state_dir)
            reviews, reviewer = review_targets(settings.targets), grant.actor
            as_reviewer = {r: Transport(r, grant, time.time) for r in reviews}
            log.info("reviewing pull requests as %s", reviewer.login)
        identities = [app, *([reviewer] if reviewer else [])]
        actors = {"app": app.record(), **({"reviewer": reviewer.record()} if reviewer else {})}
        self.registry = Registry.open(settings.state_dir, settings.registry_id, initialize=self.initialize)
        gateway = MutationGateway(
            self.registry,
            writers.__getitem__,
            readers.__getitem__,
            bot_login,
            app=app,
            reviewer=reviewer,
            reviewer_transport=as_reviewer.__getitem__ if reviewer else None,
        )
        self.reconciler = Reconciler(
            self.registry,
            readers.__getitem__,
            issue_repositories,
            identities,
            command_comment=delegation.marked,
            review_repositories=sorted(reviews),
        )
        diagnostics = Store(settings.diagnostics_dir) if settings.diagnostics_dir else None
        delegation_key = (
            private.read(settings.delegation_key_path, (CHECKOUT,)).strip() if settings.delegation_key_path else None
        )
        worker = Worker(
            settings,
            gateway,
            self.registry,
            self.reconciler,
            readers.__getitem__,
            bot_login,
            diagnostics=diagnostics,
            delegation_key=delegation_key,
            actors=actors,
            reviewer_reader=as_reviewer.__getitem__ if reviewer else None,
        )
        worker.sponsorship = Shadow(
            self.registry,
            readers.__getitem__,
            worker._operator,
            Metadata(settings.vault_metadata_url, settings.vault_service_token_path)
            if settings.vault_metadata_url
            else None,
            delegation_key,
        )
        self.reconciler.policy_observer = worker.observe_policies
        worker.observe_policies()
        self.reconciler.cleanup = worker.cleanup_runners
        self.list_index = ListIndex(settings.state_dir)
        self.list_index.start(
            {r: readers[r].request("GET", f"repos/{r}")["id"] for r in repositories},
            rows={
                "runs": {target.fork for target in settings.targets},
                "issues": set(issue_repositories),
                "pulls": {target.upstream for target in settings.targets},
            },
            reader=readers.__getitem__,
            sponsored=self.registry.sponsor_issues,
            linked=self.registry.linked_pulls,
        )
        self.registry.changed = lambda kind, repository, issue: self.list_index.signal(repository, kind, issue)
        if diagnostics:
            diagnostics.changed = lambda repository, issue: self.list_index.signal(repository, "tasks", issue)
        lists_token = read_token(settings.lists_token_path) if settings.lists_token_path else None
        self.recovery = Recovery(
            self.identity, self.registry, self.reconciler, installations, list_index=self.list_index
        )
        self.scheduler = Scheduler(self.registry, gateway, worker, settings.limits)
        cutover = Cutover(settings, self.identity, gateway, self.registry, readers.__getitem__, worker.load)
        actions = Actions(
            delegation_key,
            self.registry,
            readers.__getitem__,
            writers.__getitem__,
            settings.targets,
            projection=worker.sponsorship.view,
        )
        self.ingress = ingress.Ingress(
            secret,
            self.registry,
            installations,
            identities,
            issue_repositories=issue_repositories,
            command_comment=delegation.marked,
            list_index=self.list_index,
            reviewer=reviewer,
            review_targets=reviews,
        )
        self.http = ingress.server(
            self.ingress,
            *self.listen,
            actions=actions,
            lists=Lists(self.list_index, lists_token),
            delivery_bridge=DeliveryBridge(self.identity, read_token(settings.delivery_recovery_bearer_path))
            if settings.delivery_recovery_bearer_path
            else None,
        )
        self.control = control.server(
            control.Control(self.registry, self.reconciler, cutover, worker.evaluate, gateway), settings.state_dir
        )
        for name, target in (("ingress", self.http.serve_forever), ("control", self.control.serve_forever)):
            threading.Thread(target=target, name=name, daemon=True).start()
        self.bus_worker = None
        if settings.bus_url is not None:
            self.bus_worker = BusWorker(settings, self.ingress, self.list_index, self.reconciler, self.stopping)
            self.bus_worker.start()
        log.info("serving %d repositories as %s on %s:%d", len(repositories), bot_login, *self.listen)

    def run(self):
        """Dispatch due work and recover missed webhook deliveries until asked to stop."""
        poller, observer, next_poll = None, None, 0.0
        try:
            while not self.stopping.wait(DISPATCH_SECONDS):
                self.scheduler.dispatch()
                if not (observer and observer.is_alive()) and self.registry.observations():
                    observer = threading.Thread(
                        target=self.reconciler.observe_pending, name="observations", daemon=True
                    )
                    observer.start()
                if time.monotonic() >= next_poll and not (poller and poller.is_alive()):
                    poller = threading.Thread(target=self.recovery.poll, name="webhook-recovery", daemon=True)
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
        # Held feed requests answer at once instead of outliving the listener.
        self.list_index.release()
        self.http.shutdown()
        self.control.shutdown()
        self.scheduler.shutdown()
        if self.bus_worker is not None:
            self.bus_worker.thread.join(timeout=5)
        self.list_index.close()
        self.identity.close()
        self.registry.close()


def retro_sources(settings):
    """Live retrospective sources: App read transports, run diagnostics and the exchange's operator channel."""

    def build(target):
        identity = Identity(settings)
        bot_login = identity.verify()
        readers = {r: Transport(r, identity.credential(r, "read"), time.time) for r in target.repositories}
        return retro.Live(readers, settings.diagnostics_dir, operator(settings)), bot_login

    return build


def app_actor(reader, bot_login):
    """The App's account with its REST numeric ID and GraphQL node ID; either stays unset when unavailable."""
    try:
        numeric = reader.request("GET", "users/" + quote(bot_login)).get("id")
    except Exception:
        numeric = None
    node = user_grant.viewer(lambda body: reader.request("POST", "graphql", body))
    return Actor(bot_login, numeric if type(numeric) is int else None, node)


def authorize_reviewer(settings):
    """Store the reviewer account's grant from an interactive device authorization."""
    if settings.reviewer is None:
        raise StartupError("settings name no reviewer")
    actor = user_grant.authorize(settings.reviewer, settings.state_dir)
    print("stored the reviewer grant for", actor.login)
    return 0


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
    fields = REQUEST_FIELDS.get(arguments.command, ()) if isinstance(arguments.command, str) else ()
    return {"command": arguments.command, **{field: getattr(arguments, field) for field in fields}}


def parser():
    """The command line."""
    root = argparse.ArgumentParser(prog="python3 -m service", description=__doc__)
    root.add_argument("--settings", required=True, help="private settings file")
    commands = root.add_subparsers(dest="command", required=True)
    serve_command = commands.add_parser("serve", help="run the daemon")
    serve_command.add_argument("--initialize", action="store_true", help="create a new, empty registry")
    serve_command.add_argument("--listen", default=LISTEN, help="webhook address, HOST:PORT")
    serve_command.add_argument("--poll-seconds", type=int, default=POLL_SECONDS, help="App webhook recovery interval")
    commands.add_parser("initialize", help="create a new, empty registry and exit, without starting the daemon")
    commands.add_parser(
        "authorize-reviewer", help="authorize the configured reviewer account for the App and store its grant"
    )
    commands.add_parser("status", help="show every task the registry knows")
    parked = commands.add_parser("parked", help="inspect or resolve parked GitHub bus events")
    parked_commands = parked.add_subparsers(dest="parked_action", required=True)
    parked_commands.add_parser("list")
    parked_commands.add_parser("show").add_argument("parking_id", type=int)
    replay = parked_commands.add_parser("replay")
    replay.add_argument("parking_id", type=int)
    replay.add_argument("operator")
    discard = parked_commands.add_parser("discard")
    discard.add_argument("parking_id", type=int)
    discard.add_argument("operator")
    discard.add_argument("reason")
    commands.add_parser("reconcile-once", help="poll every target now")
    commands.add_parser(
        "credential-order-manifest",
        help="print sponsors' current personal credential-type selections for the vault's list migration;"
        " reads the registry directly, with or without a running daemon",
    )
    retro_collect = commands.add_parser(
        "retro-collect",
        help="snapshot a target's tasks and write retrospective dossiers and metrics, with or without a running daemon",
    )
    retro_collect.add_argument("target", help="the target's upstream or fork repository")
    retro_collect.add_argument("--since", required=True, help="window start, ISO date or time")
    retro_collect.add_argument("--until", help="window end, ISO date or time; default now")
    retro_collect.add_argument("--snapshot", required=True, help="new directory for the frozen reads")
    retro_collect.add_argument("--output", required=True, help="new directory for run.json and dossiers")
    adopt = commands.add_parser("adopt", help="move tasks from the Actions controller to this service")
    adopt.add_argument("repository")
    adopt.add_argument("issues", nargs="*", type=int)
    release = commands.add_parser("release", help="hand an idle task back to the Actions controller")
    release.add_argument("repository")
    release.add_argument("issue", type=int)
    evaluation = commands.add_parser(
        "evaluate", help="run four fresh design-review fixtures on GitHub runners (uses model budget)"
    )
    evaluation.add_argument("repository")
    evaluation.add_argument("issue", type=int)
    evaluation.add_argument("provider", help="model profile name (legacy configs: claude or codex)")
    resolve = commands.add_parser("resolve", help="record what happened to a write of unknown outcome")
    resolve.add_argument("intent")
    resolve.add_argument("disposition", choices=("applied", "not-applied"))
    resolve.add_argument("evidence")
    resolve.add_argument("--operator", default=pwd.getpwuid(os.getuid()).pw_name)
    recovery = commands.add_parser(
        "recover-review", help="reconcile an uncertain pending-review edit or confirmed submission"
    )
    recovery.add_argument("intent")
    recovery.add_argument("--operator", default=pwd.getpwuid(os.getuid()).pw_name)
    return root


def main():
    """Validate settings, then serve or send one command to the daemon."""
    arguments = parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        settings = settings_module.load(arguments.settings)
        if arguments.command == "serve":
            return serve(arguments, settings)
        if arguments.command == "authorize-reviewer":
            return authorize_reviewer(settings)
        if arguments.command == "initialize":
            Registry.open(settings.state_dir, settings.registry_id, initialize=True).close()
            print("initialized the registry in", settings.state_dir)
            return 0
        if arguments.command == "parked":
            from consumer import Consumer

            consumer = Consumer(settings.state_dir / "github-bus.sqlite", "service", 5)
            try:
                result = consumer.parked_command(
                    arguments.parked_action,
                    getattr(arguments, "parking_id", None),
                    getattr(arguments, "operator", None),
                    getattr(arguments, "reason", None),
                )
            finally:
                consumer.close()
            print(json.dumps(result, indent=2))
            return 0
        if arguments.command == "credential-order-manifest":
            registry = Registry.inspect(settings.state_dir)
            try:
                print(json.dumps(credential_order.manifest(registry), indent=2))
            finally:
                registry.close()
            return 0
        if arguments.command == "retro-collect":
            try:
                run = retro.capture(
                    settings,
                    arguments.target,
                    arguments.since,
                    arguments.until,
                    arguments.snapshot,
                    arguments.output,
                    retro_sources(settings),
                )
            except ValueError as error:
                print(f"retro: {error}", file=sys.stderr)
                return 1
            print(retro.describe(run))
            return 0
        reply = control.request(settings.state_dir, command(arguments))
    except (
        SettingsError,
        StartupError,
        RegistryError,
        IdentityError,
        Blocked,
        ConnectionError,
        GatewayStop,
        ValueError,
    ) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(reply.get("result") if reply["ok"] else {"error": reply["error"]}, indent=2))
    return 0 if reply["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
