"""Adapt checked GitHub bus facts to the service's existing webhook routing."""

import asyncio
import json
import logging
import threading
from datetime import datetime
from urllib.request import Request, urlopen

from .ingress import NotServed

log = logging.getLogger("concurro.service")


def webhook_payload(event):
    """Rebuild only the GitHub fields the router and list index consume."""
    family = ".".join(event.type.split(".")[:2]).removeprefix("github.")
    data = event.model_dump(mode="json", by_alias=True)["data"]
    repository = data["repository"]
    payload = {
        "repository": {
            "id": repository["id"],
            "full_name": repository["full_name"],
            "owner": {"id": repository["owner_id"]},
        },
        "installation": {"id": data["installation_id"]},
        "sender": data["sender"],
    }
    if action := data.get("action"):
        payload["action"] = action
    if family == "issues":
        issue = dict(data["issue"])
        if issue.pop("is_pull_request"):
            issue["pull_request"] = {}
        payload["issue"] = issue
        for field in ("label", "assignee"):
            if field in data:
                payload[field] = data[field]
        if "changes" in data:
            payload["changes"] = {
                key: {"from": value}
                for key, value in (
                    ("title", data["changes"].get("title_from")),
                    ("body", data["changes"].get("body_from")),
                )
                if value is not None
            }
        if "new_issue" in data:
            target = data["new_issue"]
            payload["new_issue"] = {"number": target["number"], "repository": {"id": target["repository_id"]}}
    elif family == "issue_comment":
        issue = dict(data["issue"])
        if issue.pop("is_pull_request"):
            issue["pull_request"] = {}
        payload.update(issue=issue, comment=data["comment"])
        if data.get("previous_body") is not None:
            payload["changes"] = {"body": {"from": data["previous_body"]}}
    elif family == "pull_request":
        pull = dict(data["pull_request"])
        head = dict(pull["head"])
        repository_id = head.pop("repository_id")
        full_name = head.pop("full_name")
        head["repo"] = {"id": repository_id, "full_name": full_name} if repository_id is not None else None
        pull["head"] = head
        payload["pull_request"] = pull
        for field in ("requested_reviewer", "requested_team"):
            if data.get(field) is not None:
                payload[field] = data[field]
    elif family == "pull_request_review":
        payload["review"] = data["review"]
        payload["pull_request"] = {
            "number": data["pull_request"]["number"],
            "head": {"sha": data["pull_request"]["head_sha"]},
        }
    elif family == "pull_request_review_comment":
        payload.update(comment=data["comment"], pull_request=data["pull_request"])
        if data.get("previous_body") is not None:
            payload["changes"] = {"body": {"from": data["previous_body"]}}
    elif family == "pull_request_review_thread":
        payload.update(thread=data["thread"], pull_request=data["pull_request"])
    elif family in ("check_run", "check_suite"):
        check = {
            key: value
            for key, value in data.items()
            if key
            not in (
                "delivery_id",
                "mapping_revision",
                "received_at",
                "installation_id",
                "repository",
                "sender",
                "action",
            )
        }
        check["pull_requests"] = [{"number": number} for number in check["pull_requests"]]
        if family == "check_run":
            check["app"] = {"id": check.pop("app_id")} if check.get("app_id") is not None else None
            check["check_suite"] = (
                {"id": check.pop("check_suite_id")} if check.get("check_suite_id") is not None else None
            )
        elif check.get("app_slug") is not None:
            check["app"] = {"slug": check.pop("app_slug")}
        payload[family] = check
    elif family == "workflow_run":
        run = {
            key: value
            for key, value in data.items()
            if key
            not in (
                "delivery_id",
                "mapping_revision",
                "received_at",
                "installation_id",
                "repository",
                "sender",
                "action",
            )
        }
        run["pull_requests"] = [{"number": number} for number in run["pull_requests"]]
        payload["workflow_run"] = run
    elif family in ("push", "status"):
        payload.update(
            {
                key: value
                for key, value in data.items()
                if key
                not in (
                    "delivery_id",
                    "mapping_revision",
                    "received_at",
                    "installation_id",
                    "repository",
                    "sender",
                    "action",
                )
            }
        )
    else:
        raise ValueError(f"unsupported GitHub family: {family}")
    return family, payload


def route_event(ingress, event, list_index=None):
    """Route one checked bus fact through the existing Python decisions."""
    family, payload = webhook_payload(event)
    delivery = event.model_dump(mode="json", by_alias=True)["data"]["delivery_id"]
    outcome = ingress.route(delivery, family, payload)
    if list_index is not None:
        name = payload["repository"]["full_name"]
        repository = ingress.repositories[name.lower()]
        list_index.apply_delivery(delivery, family, repository, payload)
    return outcome


class BusWorker:
    """Consume GitHub and coverage facts with a durable local receipt log."""

    def __init__(self, settings, ingress, list_index, reconciler, stopping):
        self.settings = settings
        self.ingress = ingress
        self.list_index = list_index
        self.reconciler = reconciler
        self.stopping = stopping
        self.thread = None

    def start(self):
        """Start one reconnecting bus reader alongside the HTTP path."""
        self.thread = threading.Thread(target=self.run, name="github-bus", daemon=True)
        self.thread.start()

    def run(self):
        """Reconnect after transport errors without stopping the service daemon."""
        while not self.stopping.is_set():
            try:
                asyncio.run(self.session())
            except Exception as error:
                log.warning("GitHub bus disconnected: %s", type(error).__name__)
            self.stopping.wait(1)

    def replay(self, floor, target_created):
        """Ask ingress to republish every retained row after a lost floor."""
        token = self.settings.bus_replay_bearer_path.read_text().strip()
        request = Request(
            self.settings.bus_replay_url.rstrip("/") + "/v1/replays",
            data=json.dumps(
                {"floor": {"stream_created": floor[0], "sequence": floor[1]}, "target_created": target_created}
            ).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=15) as response:
            return json.load(response)

    def handle(self, db, event, created, sequence):
        """Apply a GitHub event or coverage fact before the consumer commits its receipt."""
        from consumer import Outcome

        if event.type == "ingress.coverage.gap":
            if not db.execute(
                "SELECT 1 FROM applied_gaps WHERE consumer = ? AND event_id = ?", ("service", str(event.id.root))
            ).fetchone():
                self.reconciler.poll(strict=True)
                self.list_index.coverage_gap()
                db.execute("INSERT INTO applied_gaps VALUES (?, ?)", ("service", str(event.id.root)))
        elif event.type == "ingress.coverage.advanced":
            if sequence == 0:
                return Outcome.DONE, "released coverage does not prove current stream progress"
            db.execute(
                "INSERT INTO coverage_fact VALUES (?, ?, ?, ?) ON CONFLICT(consumer) DO UPDATE SET "
                "covered_until = excluded.covered_until, stream_created = excluded.stream_created, "
                "stream_sequence = excluded.stream_sequence",
                ("service", event.data.covered_until.root, created, sequence),
            )
        elif event.type.startswith("github."):
            try:
                route_event(self.ingress, event, self.list_index)
            except NotServed:
                return Outcome.DONE, "other repository"
            except (KeyError, TypeError, ValueError) as error:
                return Outcome.REJECT, f"bus adapter: {error}"
        else:
            return Outcome.REJECT, "unsupported event"
        return Outcome.DONE, ""

    async def session(self):
        """Read a durable pull consumer and compare its floor with the stream head."""
        import nats
        from consumer import Action, Consumer, decode_bus_event, delivery_key, inspect_consumer, stream_identity
        from nats.errors import TimeoutError as NatsTimeoutError
        from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

        credentials = json.loads(self.settings.bus_credentials_path.read_text())
        client = await nats.connect(
            self.settings.bus_url,
            user=credentials["user"],
            password=credentials["password"],
            max_reconnect_attempts=-1,
        )
        consumer = Consumer(
            self.settings.state_dir / "github-bus.sqlite",
            "service",
            self.settings.bus_max_attempts,
            decode_bus_event,
            backoff_seconds=self.settings.bus_backoff_seconds,
        )
        consumer.db.execute(
            "CREATE TABLE IF NOT EXISTS coverage_fact (consumer TEXT PRIMARY KEY, covered_until TEXT NOT NULL, "
            "stream_created TEXT NOT NULL, stream_sequence INTEGER NOT NULL)"
        )
        self.list_index.consumer_completeness(False)
        try:
            jetstream = client.jetstream()
            subscription = await jetstream.pull_subscribe(
                ">",
                durable="service",
                stream="GITHUB",
                config=ConsumerConfig(
                    durable_name="service",
                    ack_policy=AckPolicy.EXPLICIT,
                    deliver_policy=DeliverPolicy.ALL,
                    filter_subjects=["github.>", "ingress.>"],
                ),
            )
            replayed_floor = None
            replay_generation = None
            replay_requested_at = None
            recovery_coverage = None
            session_coverage_confirmed = False
            applied_coverage = consumer.db.execute(
                "SELECT covered_until, stream_created, stream_sequence FROM coverage_fact WHERE consumer = ?",
                ("service",),
            ).fetchone()
            while not self.stopping.is_set():
                created, first_sequence, floor = await inspect_consumer(client, jetstream, "GITHUB", "service")
                lost = consumer.observe_stream(created, first_sequence, floor)
                if lost and (lost != replayed_floor or created != replay_generation):
                    self.list_index.consumer_completeness(False)
                    applied_coverage = None
                    recovery_coverage = None
                    session_coverage_confirmed = False
                    answer = await asyncio.to_thread(self.replay, lost, created)
                    if answer.get("unrecoverable") is not None:
                        await asyncio.to_thread(self.reconciler.poll, strict=True)
                        await asyncio.to_thread(self.list_index.coverage_gap)
                    replayed_floor = lost
                    replay_generation = created
                    replay_requested_at = datetime.fromisoformat(answer["requested_at"])
                if (
                    lost
                    and recovery_coverage is not None
                    and recovery_coverage[0] == created
                    and floor >= recovery_coverage[1]
                ):
                    consumer.resolve_loss(created, floor)
                    replayed_floor = None
                    replay_generation = None
                    replay_requested_at = None
                    recovery_coverage = None
                coverage = consumer.db.execute(
                    "SELECT covered_until, stream_created, stream_sequence FROM coverage_fact WHERE consumer = ?",
                    ("service",),
                ).fetchone()
                coverage_ready = bool(coverage and consumer.coverage_applied(coverage[1], coverage[2]))
                if coverage and coverage != applied_coverage and coverage_ready:
                    session_coverage_confirmed = True
                prior_ok = bool(
                    session_coverage_confirmed
                    and applied_coverage
                    and applied_coverage[1] == created
                    and consumer.coverage_applied(applied_coverage[1], applied_coverage[2])
                )
                complete = bool(session_coverage_confirmed and coverage and (prior_ok or coverage_ready))
                self.list_index.consumer_completeness(complete)
                if complete and coverage_ready and coverage != applied_coverage:
                    self.list_index.covered(coverage[0])
                    applied_coverage = coverage
                boundary = applied_coverage if prior_ok else coverage
                try:
                    messages = await subscription.fetch(1, timeout=1)
                except NatsTimeoutError:
                    messages = []
                if messages and (await stream_identity(client, "GITHUB"))[0] != created:
                    raise RuntimeError("stream incarnation changed after fetching deliveries")
                for message in messages:
                    action = consumer.process(
                        message.data,
                        delivery_key(message, created),
                        lambda db, event: self.handle(db, event, created, message.metadata.sequence.stream),
                    )
                    if action is Action.ACK:
                        await message.ack()
                        if message.subject == "ingress.coverage.gap":
                            log.info("ingress coverage gap reconciled: %s", message.metadata.sequence.stream)
                        if replayed_floor and message.subject == "ingress.coverage.advanced":
                            occurred_at = json.loads(message.data).get("occurred_at")
                            if occurred_at and datetime.fromisoformat(occurred_at) > replay_requested_at:
                                recovery_coverage = (created, message.metadata.sequence.stream)
                    elif action is Action.TERM:
                        await message.term()
                    else:
                        await message.nak(delay=consumer.retry_delay)
                    if complete and boundary and not consumer.coverage_applied(boundary[1], boundary[2]):
                        self.list_index.consumer_completeness(False)
                while consumer.process_released(lambda db, event: self.handle(db, event, created, 0)) is not None:
                    pass
                if complete and boundary and not consumer.coverage_applied(boundary[1], boundary[2]):
                    self.list_index.consumer_completeness(False)
        finally:
            self.list_index.consumer_completeness(False)
            consumer.close()
            await client.close()
