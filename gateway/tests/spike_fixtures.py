"""Fake-only services and pinned Docker runtime for the opt-in sponsorship spike.

All credential values are synthetic. Nothing reads deployment settings or CLI login files.
"""

import hashlib
import http.client
import itertools
import json
import re
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from spike_observer import observe

IMAGE = (
    "cr.agentgateway.dev/agentgateway:v1.7.0@sha256:60216aafb03d37f94e952ff3d51d2f630a65b10ecaebdc0a864882f92610c45b"
)
CLI_VERSIONS = {"codex": "0.163.0", "claude": "2.1.301"}
SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def request(url, method="GET", body=None, headers=()):
    """Make a bounded local HTTP request while preserving repeated header pairs."""
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=15)
    data = None if body is None else json.dumps(body).encode()
    connection.putrequest(method, parsed.path or "/")
    if data is not None:
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(len(data)))
    for name, value in headers:
        connection.putheader(name, value)
    connection.endheaders(data)
    try:
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read().decode()
    finally:
        connection.close()


def response_value(model, answer):
    """A complete fake Responses result used by HTTP clients and native Codex."""
    return {
        "id": "resp_" + uuid.uuid4().hex,
        "object": "response",
        "created_at": 1,
        "model": model,
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "output": [
            {
                "type": "message",
                "id": "msg_" + uuid.uuid4().hex,
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": json.dumps({"answer": answer}), "annotations": []}],
            }
        ],
        "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
    }


def response_events(value):
    """Emit the Responses lifecycle expected by the pinned native CLI."""
    item = value["output"][0]
    text = item["content"][0]["text"]
    return [
        {"type": "response.created", "response": {**value, "status": "in_progress", "output": [], "usage": None}},
        {"type": "response.output_item.added", "output_index": 0, "item": {**item, "content": []}},
        {
            "type": "response.content_part.added",
            "item_id": item["id"],
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        },
        {
            "type": "response.output_text.delta",
            "item_id": item["id"],
            "output_index": 0,
            "content_index": 0,
            "delta": text,
        },
        {
            "type": "response.output_text.done",
            "item_id": item["id"],
            "output_index": 0,
            "content_index": 0,
            "text": text,
        },
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": value},
    ]


def claude_events(body, answer):
    """Return synthetic Anthropic SSE, using the CLI's structured-output tool when present."""
    structured = any(tool.get("name") == "StructuredOutput" for tool in body.get("tools", []))
    tool = structured
    content = (
        {"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex, "name": "StructuredOutput", "input": {}}
        if tool
        else {"type": "text", "text": ""}
    )
    delta = (
        {"type": "input_json_delta", "partial_json": json.dumps({"answer": answer})}
        if tool
        else {"type": "text_delta", "text": json.dumps({"answer": answer})}
    )
    message = {
        "id": "msg_" + uuid.uuid4().hex,
        "type": "message",
        "role": "assistant",
        "content": [],
        "model": body["model"],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 2, "output_tokens": 0},
    }
    return [
        {"type": "message_start", "message": message},
        {"type": "content_block_start", "index": 0, "content_block": content},
        {"type": "content_block_delta", "index": 0, "delta": delta},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use" if tool else "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 3},
        },
        {"type": "message_stop"},
    ]


def scan_paths(paths, needles):
    """Find exact synthetic credential markers in actual artifact bytes, returning paths only."""
    return [path for path in paths if any(needle in path.read_bytes() for needle in needles)]


class FakeServices:
    """Thread-safe synthetic vault/resolver/provider state; not production sponsorship logic."""

    def __init__(self):
        """Create an empty fake inventory and request journal."""
        self.lock = threading.Lock()
        self.records, self.credentials, self.pending, self.calls, self.resolutions = {}, {}, {}, [], []
        self.observations = []
        self.outage = False
        self.blocked = threading.Event()
        self.release = threading.Event()
        self.flood_stopped = threading.Event()
        self.flood_written = 0

    def resolve(self, session):
        """Snapshot a fake session/turn identity at authorization, never a client sponsor header."""
        with self.lock:
            record = self.records.get(session)
            if self.outage:
                return 503, {"error": "fixture unavailable"}
            if record is None or record.get("revoked") or record.get("expired"):
                return 403, {"error": "fixture denied"}
            identifier = str(uuid.uuid4())
            frozen = {**record, "session": session, "request": identifier}
            self.pending[identifier] = frozen
            self.resolutions.append({k: frozen[k] for k in ("session", "turn", "request", "account", "route")})
            return 200, {
                "token": record["token"],
                "account": record["account"],
                "route": record["route"],
                "request": identifier,
                "turn": record["turn"],
            }

    def provider(self, handler, body):
        """Check the actual upstream credential/header pairing and return a controlled response."""
        identifier = handler.headers.get("x-phase0-request", "")
        with self.lock:
            token = handler.headers.get("authorization", "")
            # An external provider sees credentials, not the observer's private correlation header.
            # Token-indexed association is fake-server control only; observer attribution uses its own frozen snapshot.
            association = self.pending.get(identifier) or next(
                (value for value in reversed(list(self.pending.values())) if "Bearer " + value["token"] == token), {}
            )
            record = dict(association)
            credential = self.credentials.get(token.removeprefix("Bearer "), {})
            actual_route = next(
                (
                    route
                    for prefix, route in (("/codex/", "chatgpt"), ("/openai/", "openai-api"), ("/anthropic/", "claude"))
                    if handler.path.startswith(prefix)
                ),
                "operator",
            )
            valid = bool(credential) and credential["route"] == actual_route
            if actual_route == "chatgpt":
                valid = valid and handler.headers.get("chatgpt-account-id") == credential.get("account")
            self.calls.append(
                {
                    "request": identifier,
                    "session": record.get("session"),
                    "turn": record.get("turn"),
                    "account": credential.get("account"),
                    "selected_account": record.get("account"),
                    "route": actual_route,
                    "path": handler.path,
                    "auth_matches": valid,
                    "model": body.get("model"),
                    "body": body,
                    "header_values": {
                        k.lower(): handler.headers.get_all(k)
                        for k in handler.headers
                        if k.lower() not in ("authorization", "x-api-key")
                    },
                    "headers": {
                        k.lower(): v
                        for k, v in handler.headers.items()
                        if k.lower() not in ("authorization", "x-api-key")
                    },
                }
            )
        if not valid:
            return handler.json_reply(401, {"error": {"type": "invalid_api_key", "message": "fixture rejected"}})
        mode = record.get("mode", "ok")
        if mode in ("auth", "quota", "rate", "unknown"):
            code, error = {
                "auth": (401, "invalid_api_key"),
                "quota": (429, "insufficient_quota"),
                "rate": (429, "rate_limit_error"),
                "unknown": (503, "fixture_unavailable"),
            }[mode]
            return handler.json_reply(
                code,
                {
                    "error": {
                        "type": error,
                        "code": error,
                        "message": "FAKE_PROVIDER_DETAIL_SENTINEL " + record.get("token", ""),
                    }
                },
                {"Retry-After": "2"} if mode == "rate" else {},
            )
        if handler.path.endswith("count_tokens"):
            return handler.json_reply(200, {"input_tokens": 2})
        if mode in ("json_invalid", "json_large"):
            raw = b"not JSON" if mode == "json_invalid" else b"x" * 65537
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(raw)))
            handler.end_headers()
            handler.wfile.write(raw)
            return
        answer = next(iter(re.findall(r"MEMORY_[A-Z]+", json.dumps(body))), "fixture")
        if body.get("stream"):
            events = (
                claude_events(body, answer)
                if "/anthropic/" in handler.path
                else response_events(response_value(body["model"], answer))
            )
            if mode in ("stream_error", "stream_error_unterminated"):
                events = [
                    {
                        "type": "error",
                        "error": {
                            "type": "rate_limit_error",
                            "message": "FAKE_PROVIDER_DETAIL_SENTINEL " + record.get("token", ""),
                        },
                    }
                ]
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Connection", "close")
            handler.end_headers()
            if mode in ("sse_invalid", "sse_large"):
                raw = b"data: not JSON\n\n" if mode == "sse_invalid" else b"data: " + b"x" * 65537 + b"\n\n"
                handler.wfile.write(raw)
                handler.close_connection = True
                return
            if mode == "disconnect":
                events = events[:1]
            if mode == "flood":
                handler.connection.settimeout(3)
                events = itertools.chain(
                    events[:1],
                    ({"type": "response.output_text.delta", "delta": "x" * 16384} for _ in range(2048)),
                    events[-1:],
                )
            try:
                for index, event in enumerate(events):
                    ending = "" if mode == "stream_error_unterminated" else "\n\n"
                    handler.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + ending).encode())
                    handler.wfile.flush()
                    if mode == "flood":
                        self.flood_written += 1
                    if mode == "delay" and index == 0:
                        self.blocked.set()
                        self.release.wait(10)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            finally:
                if mode == "flood":
                    self.flood_stopped.set()
            handler.close_connection = True
            return
        if "/anthropic/" in handler.path:
            return handler.json_reply(
                200,
                {
                    "id": "msg_phase0",
                    "type": "message",
                    "role": "assistant",
                    "model": body["model"],
                    "content": [{"type": "text", "text": json.dumps({"answer": answer})}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 2, "output_tokens": 3},
                },
            )
        return handler.json_reply(200, response_value(body["model"], answer))


class FakeHandler(BaseHTTPRequestHandler):
    """Serve fake-only resolver and provider endpoints on loopback."""

    protocol_version = "HTTP/1.1"

    def do_POST(self):
        """Read one local request and dispatch without real provider traffic."""
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = json.loads(raw or b"{}")
        if self.path.startswith("/observe/"):
            return observe(self, body)
        if self.path == "/resolve":
            status, value = self.server.state.resolve(body.get("session"))
            return self.json_reply(
                status, value, {"x-phase0-" + key: str(item) for key, item in value.items()} if status == 200 else {}
            )
        return self.server.state.provider(self, body)

    def json_reply(self, status, value, headers=None):
        """Send a complete JSON response and its exact length."""
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        """Do not log credential-bearing requests."""


@contextmanager
def fake_server(state):
    """Serve loopback fake endpoints and always join their listener thread."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
    server.state = state
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        state.release.set()
        server.shutdown()
        server.server_close()
        thread.join(5)


def configuration(port, observer=False):
    """Build the test-only native resolver configuration; all upstreams are fake endpoints."""
    host = f"host.docker.internal:{port}"
    models = []
    for route, name, provider, path in (
        ("chatgpt", "gpt-*", "openAI", "codex"),
        ("openai-api", "gpt-*", "openAI", "openai"),
        ("claude", "claude-*", "anthropic", "anthropic"),
    ):
        headers = {"x-phase0-request": "extauthz.request"}
        if route == "chatgpt":
            headers["chatgpt-account-id"] = "extauthz.account"
        if route == "claude":
            headers["anthropic-beta"] = (
                '"anthropic-beta" in request.headers ? '
                '(type(request.headers["anthropic-beta"]) == list ? request.headers["anthropic-beta"].join(",") : '
                'request.headers["anthropic-beta"]) + ",oauth-2025-04-20" : "oauth-2025-04-20"'
            )
        models.append(
            {
                "id": route,
                "name": name,
                "provider": provider,
                "passthrough": "detect",
                "matches": [{"headers": [{"name": "x-concurro-plan", "value": {"exact": route}}]}],
                "params": {"baseUrl": f"http://{host}/" + ("observe/" if observer else "") + path},
                "auth": {
                    "key": {
                        "expression": "extauthz.token",
                        "location": {"header": {"name": "authorization", "prefix": "Bearer "}},
                    }
                },
                "requestTransformation": {
                    "set": headers,
                    "remove": [
                        "x-api-key",
                        "x-concurro-plan",
                        "x-concurro-sponsor",
                        "x-concurro-account",
                        "x-internal-auth",
                        "x-phase0-token",
                        "x-phase0-account",
                        "x-phase0-route",
                        "x-phase0-turn",
                    ],
                },
            }
        )
    # A poison fallback detects any accidental route-selection fallthrough without contacting a provider.
    models.extend(
        {
            "id": "operator-" + name,
            "name": name,
            "provider": provider,
            "passthrough": "detect",
            "params": {"baseUrl": f"http://{host}/operator"},
            "auth": {"key": "fake-operator-poison"},
        }
        for name, provider in (("gpt-*", "openAI"), ("claude-*", "anthropic"))
    )
    result = {
        "config": {
            "adminAddr": "0.0.0.0:15000",
            "statsAddr": "0.0.0.0:15020",
            "storage": {"mode": "hybrid"},
            "database": {"url": "sqlite:///tmp/gateway.db"},
            "logging": {"format": "json", "level": "info,agent_llm=error"},
        },
        "frontendPolicies": {
            "accessLog": {
                "remove": ["error"],
                "add": {
                    "phase0_session": 'apiKey["concurro.session"]',
                    "phase0_turn": "extauthz.turn",
                    "phase0_request": "extauthz.request",
                },
            }
        },
        "llm": {
            "port": 4000,
            "policies": {
                "apiKey": {"mode": "strict", "keys": []},
                "extAuthz": {
                    "host": host,
                    "protocol": {
                        "http": {
                            "path": '"/resolve"',
                            "addRequestHeaders": {":method": '"POST"', "content-type": '"application/json"'},
                            "body": '{"session": apiKey["concurro.session"]}',
                            "metadata": {
                                key: 'response.headers["x-phase0-' + key + '"]'
                                for key in ("token", "account", "route", "request", "turn")
                            },
                        }
                    },
                },
                "transformations": {"request": {"set": {"x-concurro-plan": "extauthz.route"}}},
            },
            "models": models,
        },
    }

    if observer:
        result["config"]["logging"]["database"] = {"url": "sqlite:///tmp/requests.db"}
        result["frontendPolicies"]["accessLog"]["database"] = {"llm": "metadata"}
    return result


class DockerGateway:
    """Own one disposable pinned-image container; never invoke the deployment Compose stack."""

    def __init__(self, root, fake_port, docker_host=None, observer=False):
        """Allocate configuration/output paths and a unique container name."""
        self.root = Path(root)
        self.name = "concurro-phase0-" + uuid.uuid4().hex[:12]
        self.command = ["docker", "--config", str(self.root / "docker-config")]
        if docker_host:
            self.command += ["--host", docker_host]
        self.config = configuration(fake_port, observer)
        self.config_path = self.root / "gateway.json"
        self.config_path.write_text(json.dumps(self.config, indent=2) + "\n")
        self.logs = ""

    def docker(self, *args, check=True):
        """Run a bounded Docker CLI command without implicit login/config discovery."""
        return subprocess.run(self.command + list(args), check=check, capture_output=True, text=True, timeout=60)

    def __enter__(self):
        """Start the exact image with only loopback-published ephemeral ports."""
        self.identity = json.loads(self.docker("image", "inspect", IMAGE).stdout)[0]
        self.docker(
            "run",
            "--rm",
            "--detach",
            "--name",
            self.name,
            "--publish",
            "127.0.0.1::4000",
            "--publish",
            "127.0.0.1::15000",
            "--volume",
            str(self.config_path) + ":/config.json:ro",
            IMAGE,
            "-f",
            "/config.json",
        )
        try:
            bindings = json.loads(self.docker("inspect", self.name).stdout)[0]["NetworkSettings"]["Ports"]
            self.inference = "http://127.0.0.1:" + bindings["4000/tcp"][0]["HostPort"]
            self.admin = "http://127.0.0.1:" + bindings["15000/tcp"][0]["HostPort"]
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    if request(self.admin + "/api/config/resources/llm.apiKey")[0] == 200:
                        return self
                except (OSError, http.client.HTTPException):
                    pass
                time.sleep(0.1)
            raise RuntimeError("isolated gateway did not become ready")
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def request_database(self):
        """Copy the quiescent fake-only request log and WAL before the disposable container exits."""
        paths = []
        for suffix in ("", "-wal", "-shm"):
            path = self.root / ("requests.db" + suffix)
            copied = self.docker("cp", self.name + ":/tmp/requests.db" + suffix, str(path), check=False)
            if copied.returncode == 0:
                paths.append(path)
        return paths

    def __exit__(self, *_args):
        """Preserve gateway logs before removing only this experiment's container."""
        logs = self.docker("logs", self.name, check=False)
        self.logs = logs.stdout + "\n" + logs.stderr
        if not (self.root / "gateway.log").exists():
            (self.root / "gateway.log").write_text(logs.stdout)
            (self.root / "gateway.stderr.log").write_text(logs.stderr)
        self.docker("rm", "--force", self.name, check=False)


def file_digest(path):
    """Return an artifact's SHA-256 for the reproducibility manifest."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
