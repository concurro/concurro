"""Dispatch due work: one tick per task, bounded per repository and overall, fair across repositories."""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from controller import Outcome

log = logging.getLogger("concurro.service")


def fair(items, running, limits):
    """Due keys to start, in order: round-robin across repositories, oldest first within each.

    `items` are due work items, oldest first; `running` holds the keys ticking now.
    Neither limit is ever exceeded.
    """
    queues = {}
    for item in items:
        if item.key not in running:
            queues.setdefault(item.key[0], []).append(item.key)
    busy = {}
    for key in running:
        busy[key[0]] = busy.get(key[0], 0) + 1
    capacity = limits.global_ticks - len(running)
    selected = []
    for index in range(max(map(len, queues.values()), default=0)):
        for repository, keys in queues.items():
            if capacity <= 0:
                return selected
            if index < len(keys) and busy.get(repository, 0) < limits.per_repository:
                selected.append(keys[index])
                busy[repository] = busy.get(repository, 0) + 1
                capacity -= 1
    return selected


class Scheduler:
    """Acquire a generation before each tick and release it with the tick's outcome."""

    def __init__(self, registry, gateway, worker, limits, clock=time.time, executor=None, name="worker"):
        """`worker.run(key, generation)` returns an `Outcome` or the stop that ended the tick."""
        self.registry, self.gateway, self.worker, self.limits = registry, gateway, worker, limits
        self.clock, self.name = clock, name
        self.executor = executor or ThreadPoolExecutor(limits.global_ticks, thread_name_prefix="concurro-tick")
        self.guard = threading.Condition()
        self.running = {}

    def dispatch(self):
        """Start every due task the limits allow; return the keys started.

        Keys with a live owner, a write in flight or an unresolved write are refused by
        `acquire` and stay queued; selection continues past them until the limits are
        filled or no due key is left, so a refused key never holds a slot.
        """
        items = self.registry.due(self.clock())
        tried, started = set(), []
        while True:
            with self.guard:
                selected = fair([item for item in items if item.key not in tried], self.running, self.limits)
            if not selected:
                return started
            for key in selected:
                tried.add(key)
                generation = self.gateway.acquire(key, self.name)
                if generation is None:
                    continue
                with self.guard:
                    self.running[key] = generation
                started.append(key)
                self.executor.submit(self.tick, key, generation)

    def tick(self, key, generation):
        """Run one tick and always end its generation; a stop records no outcome."""
        result = None
        try:
            result = self.worker.run(key, generation)
        except BaseException:  # noqa: B036  (a crashed tick must still end its generation)
            log.exception("tick of %s#%s crashed", *key)
        finally:
            try:
                self.gateway.release(key, generation, result if isinstance(result, Outcome) else None)
            finally:
                with self.guard:
                    self.running.pop(key, None)
                    self.guard.notify_all()

    def wait(self, timeout=None):
        """Wait until no tick is running; return whether that happened in time."""
        with self.guard:
            return self.guard.wait_for(lambda: not self.running, timeout)

    def shutdown(self):
        """Start no further ticks and wait for the running ones to finish."""
        self.executor.shutdown(wait=True)
