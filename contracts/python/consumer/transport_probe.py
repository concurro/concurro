"""Consume one JetStream message for the cross-language exit test."""

import asyncio
import json
import os
import sys

import nats
from nats.js.api import ConsumerConfig

from consumer import Action, Consumer, Outcome, delivery_key, stream_identity


async def main() -> None:
    """Record one event effect and acknowledge its delivery."""
    url, database_path = sys.argv[1:3]
    expected = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    stop_before_ack = len(sys.argv) > 4 and sys.argv[4] == "stop-before-ack"
    mode = os.getenv("CONCURRO_PROBE_MODE", "done")
    consumer = Consumer(database_path, "python-transport", 3)
    consumer.db.execute("CREATE TABLE IF NOT EXISTS effect (event_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")

    def handle(conn, event):
        if mode == "retry":
            return Outcome.RETRY, "injected retry"
        if mode == "reject":
            return Outcome.REJECT, "injected rejection"
        if mode == "hold":
            return Outcome.HOLD, "ready"
        payload = json.loads(event.model_dump_json())
        for optional in ("causation", "entity", "revision"):
            if payload.get(optional) is None:
                payload.pop(optional, None)
        conn.execute(
            "INSERT INTO effect VALUES (?, ?)",
            (str(event.id.root), json.dumps(payload)),
        )
        return Outcome.DONE, ""

    action = None
    if mode == "release":
        consumer.db.execute("BEGIN IMMEDIATE")
        consumer.release("ready")
        consumer.db.execute("COMMIT")
        action = consumer.process_released(handle)
    client = await nats.connect(url)
    stream_created, _ = await stream_identity(client, "INGRESS")
    subscription = await client.jetstream().pull_subscribe(
        "github.>",
        durable="python-transport",
        stream="INGRESS",
        config=ConsumerConfig(ack_wait=0.5, max_deliver=-1),
    )
    seen = 0
    progress = 0
    count_effects = mode == "volume"
    deadline = asyncio.get_running_loop().time() + 120
    while progress < expected:
        if count_effects and asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"recorded {progress}/{expected} distinct effects")
        messages = await subscription.fetch(min(expected - progress, 100), timeout=10)
        for message in messages:
            key = delivery_key(message, stream_created)
            action = consumer.process(message.data, key, handle)
            if stop_before_ack:
                consumer.close()
                raise SystemExit(17)
            if action is Action.ACK:
                await message.ack()
            elif action is Action.TERM:
                await message.term()
            else:
                await message.nak(delay=consumer.retry_delay)
            seen += 1
            if not count_effects:
                progress = seen
        if count_effects:
            progress = consumer.db.execute("SELECT count(*) FROM effect").fetchone()[0]
    await client.flush()
    row = consumer.db.execute("SELECT payload FROM effect").fetchone()
    print(
        json.dumps(
            {
                "action": action.value,
                "deliveries": seen,
                "count": consumer.db.execute("SELECT count(*) FROM effect").fetchone()[0],
                "parked": consumer.db.execute("SELECT count(*) FROM parked").fetchone()[0],
                "held": consumer.db.execute("SELECT count(*) FROM held").fetchone()[0],
                "event": json.loads(row[0]) if row else None,
            }
        )
    )
    consumer.close()
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
