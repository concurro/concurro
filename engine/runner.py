"""A runner job's host loop: trade the job's OIDC token for its session, then run the target's turns.

Runs on a GitHub-hosted runner of the target, from the Concurro commit the session pins. It holds
the job's OIDC request token and the session's runner token; agent containers get only the
gateway key. Turns are model turns or calls on the controller's workbenches, which live here
(`bench.Benches`). Turns run concurrently up to `--capacity`; the loop calls the exchange at least
every HEARTBEAT seconds, and ends the session once no turn has arrived for `--idle` seconds, or
BENCH_IDLE seconds while a workbench is open.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback
import urllib.request

from bench import Benches
from mirror import Mirror
from relay import ExchangeError, Runner
import turns

AUDIENCE = "concurro-gateway"
HEARTBEAT = 30
# A develop turn and a local review may run between two calls on a workbench.
BENCH_IDLE = 3600


def oidc_token():
    """A fresh OIDC token for this job, with Concurro's audience."""
    request = urllib.request.Request(os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"] + "&audience=" + AUDIENCE,
                                     headers={"Authorization": "bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)["value"]


def log(message):
    print(time.strftime("%H:%M:%S"), message, flush=True)


class Loop:
    """Take turns while they come; run each in its own directory; report every result."""

    def __init__(self, runner, key, image, mirror, root, capacity, idle, limits, benches=None):
        self.runner, self.key, self.image, self.mirror, self.root = runner, key, image, mirror, Path(root)
        self.capacity, self.idle, self.limits = capacity, idle, limits
        self.benches = benches
        self.running, self.guard = 0, threading.Lock()

    def serve(self):
        last = time.monotonic()
        with ThreadPoolExecutor(self.capacity) as pool:
            while True:
                with self.guard:
                    free = self.capacity - self.running
                try:
                    taken = self.runner.next(free, HEARTBEAT if free else 0)
                except ExchangeError as error:
                    if error.status == 410:
                        log("the session has ended")
                        return
                    log(f"exchange call failed ({error.status}); retrying")
                    time.sleep(5)
                    continue
                for turn in taken:
                    with self.guard:
                        self.running += 1
                    pool.submit(self.run, turn)
                with self.guard:
                    busy = self.running
                idle = BENCH_IDLE if self.benches and self.benches.open else self.idle
                if taken or busy:
                    last = time.monotonic()
                elif time.monotonic() - last > idle:
                    log("idle; ending the session")
                    return
                if not free:
                    time.sleep(HEARTBEAT)

    def run(self, turn):
        """Run one turn and report it; a turn that cannot run reports a failure, never silence."""
        identifier, payload = turn["id"], turn["payload"]
        bench = payload.get("kind") == "bench"
        directory = None if bench else turns.directory(self.root)
        log(f"turn {identifier}: " + (f"workbench {payload.get('op')}" if bench else
                                      f"{payload['request']['stage']} ({payload['request']['provider']})"))
        try:
            try:
                if bench:
                    if self.benches is None:
                        raise RuntimeError("this session runs no workbenches")
                    result = self.benches.handle(payload)
                else:
                    result = turns.execute(payload, self.image, self.key, self.mirror, directory, self.limits)
            except Exception as error:
                log(f"turn {identifier} could not run: {type(error).__name__}: {error}")
                traceback.print_exc(file=sys.stdout)
                result = {"outcome": "failed", "response": None, "workspace": None, "retained": None}
            log(f"turn {identifier}: {result['outcome']}")
            for attempt in range(5):
                try:
                    self.runner.complete(identifier, result)
                    break
                except ExchangeError as error:
                    if error.status in (409, 410):
                        log(f"turn {identifier}: result not accepted ({error.status})")
                        break
                    time.sleep(5 * (attempt + 1))
        finally:
            if directory is not None:
                turns.remove(directory)
            with self.guard:
                self.running -= 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True)
    parser.add_argument("--gateway", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--execution-image", help="the trusted workbench's image; without one, no workbenches")
    parser.add_argument("--root", required=True)
    parser.add_argument("--capacity", type=int, default=3)
    parser.add_argument("--idle", type=int, default=600)
    parser.add_argument("--memory", default="7g")
    parser.add_argument("--cpus", type=int, default=4)
    args = parser.parse_args()
    runner = Runner(args.gateway, args.session)
    grant = runner.key(oidc_token())
    log(f"session {args.session} started; models {grant['models']}")
    root = Path(args.root)
    mirror = Mirror(root / "mirrors", args.repository)
    limits = turns.Limits(memory=args.memory, cpus=args.cpus, pids=8192, tmp="4g")
    benches = None
    if args.execution_image:
        os.environ["CONCURRO_EXECUTION_IMAGE"] = args.execution_image
        benches = Benches(args.session, mirror)
    try:
        Loop(runner, grant["key"], args.image, mirror, root / "turns", args.capacity, args.idle, limits, benches).serve()
    finally:
        if benches is not None:
            benches.close_all()
        try:
            runner.end()
        except ExchangeError as error:
            log(f"ending the session failed ({error.status}); the exchange ends it when its heartbeat lapses")


if __name__ == "__main__":
    main()
