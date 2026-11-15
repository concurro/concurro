"""Live updates: relay the service's list index changes to signed-in browsers.

One thread holds the service's changes feed open. Each change first makes the saved data it
outdates due, then goes to every subscribed browser stream, which sends on only what its user
may see. Nothing is replayed to a browser: a stream that opens, or that missed something,
is told to reload.
"""

import asyncio
import json
import logging
import threading

from .commands import IndexUnavailable

log = logging.getLogger("concurro.backoffice")

# Changes one browser stream may have waiting; a slower one is told to reload instead.
QUEUED = 256
# The longest pause between attempts while the service gives no usable answer.
RETRY_SECONDS = 30
# A comment line this often keeps an idle stream open through proxies.
KEEPALIVE_SECONDS = 20
RELOAD = {"kind": "reload", "repository": None, "issue": None}


class Relay:
    """The feed's one consumer and the browser streams it serves."""

    def __init__(self, feed, read_cache=None):
        """`feed(incarnation, since)` is one request to the service's changes feed."""
        self.feed, self.read_cache = feed, read_cache
        self.lock, self.stopped, self.thread = threading.Lock(), threading.Event(), None
        self.subscribers: dict[asyncio.Queue, asyncio.AbstractEventLoop] = {}

    def start(self):
        self.thread = threading.Thread(target=self.run, name="list-changes", daemon=True)
        self.thread.start()

    def stop(self):
        """Stop relaying; a feed request in flight is abandoned, not awaited."""
        self.stopped.set()

    def run(self):
        """Follow the feed from an in-memory cursor; without one the service says to resynchronize."""
        incarnation, since, delay = None, None, 1
        while not self.stopped.is_set():
            try:
                answer = self.feed(incarnation, since)
                changes, cursor = answer["changes"], (answer["incarnation"], answer["sequence"])
                if not isinstance(changes, list) or not isinstance(cursor[0], str) or type(cursor[1]) is not int:
                    raise TypeError
                if answer.get("resynchronize"):
                    self.apply(None, None, None)
                for change in changes:
                    self.apply(change["repository"], change["kind"], change["item"])
            except (IndexUnavailable, KeyError, TypeError):
                # The same cursor is asked again: nothing of a lost or unusable answer was applied twice.
                self.stopped.wait(delay)
                delay = min(delay * 2, RETRY_SECONDS)
                continue
            (incarnation, since), delay = cursor, 1

    def apply(self, repository, kind, item):
        """Make what one change outdates due, then tell the browsers.

        A change without a kind says signals of that repository may have been lost, and one
        without a repository that the feed could not be continued: everything saved for them
        is due. A `tasks` change is Concurro's progress on one issue, whose saved snapshot is
        made due without the settling a command starts. Lists follow their own revisions.
        """
        issue = int(item) if isinstance(item, str) and item.isdecimal() else None
        if kind is None:
            if self.read_cache is not None:
                self.read_cache.outdate("", None if repository is None else [repository])
            event = RELOAD | {"repository": repository}
        else:
            if kind == "tasks" and issue is not None and self.read_cache is not None:
                self.read_cache.outdate(str(issue), [repository], exact=True)
            event = {"kind": kind, "repository": repository, "issue": issue}
        with self.lock:
            subscribers = list(self.subscribers.items())
        for waiting, loop in subscribers:
            try:
                loop.call_soon_threadsafe(self.put, waiting, event)
            except RuntimeError:
                # Its event loop is gone; the stream's own exit unsubscribes it.
                pass

    @staticmethod
    def put(waiting, event):
        """Queue one event for a stream; one that fell behind gets a single reload instead."""
        try:
            waiting.put_nowait(event)
        except asyncio.QueueFull:
            while not waiting.empty():
                waiting.get_nowait()
            waiting.put_nowait(RELOAD)

    def subscribe(self):
        """A queue of the events from now on, for the running event loop."""
        waiting = asyncio.Queue(QUEUED)
        with self.lock:
            self.subscribers[waiting] = asyncio.get_running_loop()
        return waiting

    def unsubscribe(self, waiting):
        with self.lock:
            self.subscribers.pop(waiting, None)


def shown(event, targets):
    """The event as one user may receive it, or None when it concerns no target they can see.

    `targets` are the configured targets visible to the user. The event names the target pages
    it concerns by their upstream, never a repository the user cannot see.
    """
    repository = event["repository"]
    if repository is None:
        return RELOAD | {"targets": [target.upstream for target in targets]} if targets else None
    concerned = [
        target.upstream for target in targets if repository.lower() in (r.lower() for r in target.repositories)
    ]
    return {**event, "targets": concerned} if concerned else None


async def stream(relay, visible, keepalive=KEEPALIVE_SECONDS):
    """Server-sent events for one signed-in session.

    `visible()` returns the targets the session's user may see now, or None once the session
    has ended, which ends the stream. It is asked for every event, so it must be cheap while
    its access decisions are cached.
    """
    waiting = relay.subscribe()
    try:
        if await asyncio.to_thread(visible) is None:
            return
        # The browser reloads its views itself whenever its stream opens: nothing is replayed.
        yield "retry: 5000\n\n"
        while True:
            try:
                event = await asyncio.wait_for(waiting.get(), keepalive)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            targets = await asyncio.to_thread(visible)
            if targets is None:
                return
            sent = shown(event, targets)
            if sent is not None:
                yield f"event: {'reload' if sent['kind'] == 'reload' else 'change'}\ndata: {json.dumps(sent)}\n\n"
    finally:
        relay.unsubscribe(waiting)
