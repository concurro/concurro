"""Owner-only local control channel: operator commands are requests to the running daemon."""

import json
import logging
import os
from pathlib import Path
import socket
import socketserver

from policy import Blocked

from .cutover import CutoverError
from .errors import GatewayStop
from .identity import IdentityError

SOCKET = "control.sock"
REQUEST_LIMIT = 65_536
TIMEOUT = 600

log = logging.getLogger("concurro.service")


class Control:
    """Serve `status`, `resolve`, `reconcile-once`, `adopt` and `release` against the daemon's own registry."""

    def __init__(self, registry, reconciler, cutover):
        self.registry, self.reconciler, self.cutover = registry, reconciler, cutover

    def handle(self, request):
        """One command's JSON reply; refusals carry a short reason and change nothing."""
        try:
            return {"ok": True, "result": self.run(request)}
        except (ValueError, KeyError, TypeError, Blocked, CutoverError, IdentityError, GatewayStop) as error:
            return {"ok": False, "error": str(error) or type(error).__name__}

    def run(self, request):
        command = request["command"]
        if command == "status":
            return self.registry.status()
        if command == "resolve":
            return self.resolve(request["intent"], request["disposition"], request["evidence"], request["operator"])
        if command == "reconcile-once":
            return [list(key) for key in self.reconciler.poll()]
        if command == "adopt":
            return self.cutover.adopt(request["repository"], tuple(number(n) for n in request.get("issues", ())))
        if command == "release":
            self.cutover.release(request["repository"], number(request["issue"]))
            return None
        raise ValueError("unknown command")

    def resolve(self, intent, disposition, evidence, operator):
        """Record the operator's disposition; resolving the last unknown write makes the task due again."""
        record = self.registry.mutation(intent)
        if record is None:
            raise ValueError("no mutation with that intent ID")
        self.registry.resolve(intent, disposition, evidence, operator)
        return {"repository": record.key[0], "issue": record.key[1]}


def number(value):
    """A positive issue number."""
    if type(value) is not int or value <= 0:
        raise ValueError("issue numbers must be positive integers")
    return value


class Handler(socketserver.StreamRequestHandler):
    """One JSON request line, one JSON reply line."""

    control = None

    def handle(self):
        line = self.rfile.readline(REQUEST_LIMIT + 1)
        try:
            if len(line) > REQUEST_LIMIT:
                raise ValueError("request too large")
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
        except ValueError as error:
            reply = {"ok": False, "error": str(error)}
        else:
            reply = self.control.handle(request)
        self.wfile.write(json.dumps(reply).encode() + b"\n")


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def server(control, state_dir):
    """A control server on a socket in the private state directory; the caller runs and stops it.

    The directory's owner-only mode is the access boundary; the daemon holds the state
    lock, so a leftover socket is stale and replaced.
    """
    path = Path(state_dir) / SOCKET
    path.unlink(missing_ok=True)
    handler = type("ControlHandler", (Handler,), {"control": control})
    instance = Server(str(path), handler)
    os.chmod(path, 0o600)
    return instance


def request(state_dir, payload):
    """Send one command to the running daemon and return its reply."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(TIMEOUT)
        try:
            client.connect(str(Path(state_dir) / SOCKET))
        except OSError:
            raise ConnectionError("the service is not running") from None
        client.sendall(json.dumps(payload).encode() + b"\n")
        with client.makefile("rb") as stream:
            line = stream.readline()
    if not line:
        raise ConnectionError("the service closed the connection")
    return json.loads(line)
