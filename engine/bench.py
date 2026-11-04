"""The trusted workbench on a runner session: the controller's handle and the runner's side.

The controller builds and tests candidates in a `LocalWorkspace`. With runner executors that
workbench lives on one of the target's runner sessions, so the service never runs candidate
code: each workbench call is a turn pinned to the session that opened it. The runner builds the
source archives from its mirror at the pinned commits; the controller still checks the files it
reads against GitHub's.
"""

import re
import threading
import time
import uuid

from github import Transient
from local import LocalWorkspace
from policy import Blocked

OPS = ("open", "files", "configure_validation", "prepare", "checkpoint", "scope", "run_checks", "export", "close")
# Turn timeouts: opening fetches history and stages two archives; other calls are bounded by their own limits.
OPEN_SECONDS, CALL_SECONDS = 1800, 900
BENCH = re.compile(r"[0-9a-f]{32}")
SHA = re.compile(r"[0-9a-f]{40}")


class BenchLost(Transient):
    """The runner session holding the workbench is gone, or its call's outcome is unknown; retry later."""

    TEXT = {"unavailable": "The runner holding the workbench was lost; retry later."}


class RemoteBench:
    """A controller `workspace_factory` whose workbenches live on the target's runner sessions."""

    # The controller passes commits, not archives: the runner builds them.
    commits = True

    def __init__(self, executor):
        self.executor = executor

    def __call__(self, config, parent, branch, execution, base):
        return RemoteWorkspace(self.executor, {"config": config, "parent": parent, "branch": branch,
                                               "execution": execution, "base": base})


class RemoteWorkspace:
    """The `LocalWorkspace` interface, each call relayed to the runner session holding the workbench."""

    def __init__(self, executor, opening):
        self.executor, self.opening = executor, opening
        self.id, self.session = uuid.uuid4().hex, None

    def __enter__(self):
        self.session = self.call("open", self.opening, OPEN_SECONDS)["session"]
        return self

    def __exit__(self, *_):
        try:
            self.call("close", {}, CALL_SECONDS)
        except Exception:
            # The runner closes its workbenches when its session ends.
            pass

    def call(self, op, args, timeout):
        turn = {"kind": "bench", "bench": self.id, "op": op, "args": args, "timeout": int(timeout)}
        result = self.executor.run(turn, "bench:" + op, pin=self.session)
        response = result.get("response") if result.get("outcome") == "ok" else None
        if not isinstance(response, dict) or not set(response) & {"value", "error"}:
            raise BenchLost("unavailable")
        if "error" in response:
            raise Blocked("workbench: " + str(response["error"])[:500])
        return response["value"]

    def files(self, paths):
        return self.call("files", {"paths": list(paths)}, CALL_SECONDS)

    def configure_validation(self, spec, originals, scope):
        self.call("configure_validation", {"spec": spec, "originals": originals, "scope": scope}, CALL_SECONDS)

    def prepare(self, selected, timeout):
        self.call("prepare", {"selected": list(selected), "timeout": timeout}, timeout + CALL_SECONDS)

    def checkpoint(self, edits):
        return self.call("checkpoint", {"edits": edits}, CALL_SECONDS)

    def scope(self, packages):
        self.call("scope", {"packages": packages}, CALL_SECONDS)

    def run_checks(self, required, requested, deadline):
        seconds = max(0.0, deadline - time.monotonic())
        return self.call("run_checks", {"required": required, "requested": requested, "seconds": seconds},
                         seconds + CALL_SECONDS)

    def export(self):
        return self.call("export", {}, CALL_SECONDS)


class Benches:
    """A runner session's open workbenches, served from its mirror."""

    def __init__(self, session, mirror, factory=LocalWorkspace):
        self.session, self.mirror, self.factory = session, mirror, factory
        self.open, self.guard = {}, threading.Lock()

    def handle(self, payload):
        """One call's turn result: its value, or the error that refused it."""
        try:
            response = {"value": self.dispatch(payload)}
        except (Blocked, ValueError, KeyError, TypeError) as error:
            response = {"error": f"{type(error).__name__}: {error}"}
        return {"outcome": "ok", "response": response, "workspace": None, "retained": None}

    def dispatch(self, payload):
        bench, op, args = payload.get("bench"), payload.get("op"), payload.get("args")
        if not isinstance(bench, str) or not BENCH.fullmatch(bench) or op not in OPS or not isinstance(args, dict):
            raise ValueError("invalid workbench call")
        if op == "open":
            return self.start(bench, args)
        with self.guard:
            workspace = self.open.get(bench)
        if workspace is None:
            raise Blocked("unknown workbench")
        if op == "close":
            self.close(bench)
            return None
        if op == "files":
            return workspace.files(args["paths"])
        if op == "configure_validation":
            return workspace.configure_validation(args["spec"], args["originals"], args["scope"])
        if op == "prepare":
            return workspace.prepare(args["selected"], args["timeout"])
        if op == "checkpoint":
            return workspace.checkpoint(args["edits"])
        if op == "scope":
            return workspace.scope(args["packages"])
        if op == "run_checks":
            return workspace.run_checks(args["required"], args["requested"], time.monotonic() + float(args["seconds"]))
        return workspace.export()

    def start(self, bench, args):
        with self.guard:
            if bench in self.open:
                return {"session": self.session}
        parent, base, config = args["parent"], args["base"], args["config"]
        if not SHA.fullmatch(str(parent)) or not SHA.fullmatch(str(base)):
            raise ValueError("workbench commits must be full SHAs")
        self.mirror.borrow("bench-" + bench, config["base"], sorted({parent, base}))
        try:
            archive = self.archive(parent)
            workspace = self.factory(config, archive, args["branch"], execution=args["execution"],
                                     base_archive=archive if parent == base else self.archive(base))
            workspace.__enter__()
        except BaseException:
            self.mirror.release("bench-" + bench)
            raise
        with self.guard:
            self.open[bench] = workspace
        return {"session": self.session}

    def archive(self, sha):
        """A single-root source archive of one commit, as GitHub serves them."""
        result = self.mirror.git("archive", "--format=tar.gz", "--prefix=source/", sha, timeout=600)
        if result.returncode:
            raise Blocked("the mirror could not archive a pinned commit")
        return result.stdout

    def close(self, bench):
        with self.guard:
            workspace = self.open.pop(bench, None)
        if workspace is not None:
            try:
                workspace.close()
            finally:
                self.mirror.release("bench-" + bench)

    def close_all(self):
        for bench in list(self.open):
            self.close(bench)
