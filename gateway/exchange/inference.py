"""Private request attribution and bounded provider observations beside the native gateway."""

import http.client
import json
import runpy
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import closing
from http.server import BaseHTTPRequestHandler
from pathlib import Path

PROTOCOL_PATH = Path(__file__).with_name("provider_protocol.py")
if not PROTOCOL_PATH.exists():
    PROTOCOL_PATH = Path(__file__).resolve().parents[1] / "provider_protocol.py"
PROTOCOL = runpy.run_path(str(PROTOCOL_PATH))
UPSTREAMS, MAX_EVENT = PROTOCOL["UPSTREAMS"], PROTOCOL["MAX_EVENT"]
Observation, category, tokens = PROTOCOL["Observation"], PROTOCOL["category"], PROTOCOL["tokens"]
event_value = PROTOCOL["event_value"]

PRIVATE = ("x-concurro-", "x-phase0-", "x-internal-")
HOP = {
    "host",
    "connection",
    "transfer-encoding",
    "content-length",
    "keep-alive",
    "proxy-authorization",
    "proxy-authenticate",
    "trailer",
    "upgrade",
}
ERRORS = frozenset({"auth", "quota_exhausted", "rate_limited", "budget_denied"})


class Inference:
    """Durable request-start snapshots, sanitized outbox and exact native-log joins."""

    def __init__(self, exchange, native_database=None, upstreams=None):
        """Share the Exchange lifecycle transaction instead of adding another authority store."""
        self.exchange, self.native_database = exchange, native_database
        self.upstreams = dict(UPSTREAMS if upstreams is None else upstreams)
        self.import_guard = threading.Lock()
        self.connections_guard = threading.Lock()
        self.connections = {}
        exchange.db.executescript("""
            CREATE TABLE IF NOT EXISTS inference_requests(id TEXT PRIMARY KEY,session TEXT NOT NULL,turn TEXT,binding TEXT NOT NULL,created REAL NOT NULL,resolved INTEGER NOT NULL DEFAULT 0,claimed INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS inference_observations(sequence INTEGER PRIMARY KEY AUTOINCREMENT,request TEXT NOT NULL,source TEXT NOT NULL,body TEXT NOT NULL,UNIQUE(request,source));
        """)
        exchange.add_columns("inference_requests", (("model", "TEXT"), ("repository", "TEXT"), ("issue", "INTEGER")))
        exchange.add_columns("inference_observations", (("turn", "TEXT"), ("repository", "TEXT"), ("issue", "INTEGER")))
        exchange.db.executescript("""
            UPDATE inference_observations SET turn=json_extract(body,'$.turn'),repository=json_extract(body,'$.binding.repository'),issue=json_extract(body,'$.binding.issue') WHERE turn IS NULL;
            CREATE INDEX IF NOT EXISTS inference_turn ON inference_observations(turn,sequence);
            CREATE INDEX IF NOT EXISTS inference_task ON inference_observations(repository,issue,sequence);
            UPDATE inference_requests SET repository=json_extract(binding,'$.repository'),issue=json_extract(binding,'$.issue') WHERE repository IS NULL;
            CREATE INDEX IF NOT EXISTS inference_request_task ON inference_requests(repository,issue);
            CREATE INDEX IF NOT EXISTS inference_request_turn ON inference_requests(turn);
            CREATE TABLE IF NOT EXISTS native_cursor(id INTEGER PRIMARY KEY,identity TEXT NOT NULL,last_row INTEGER NOT NULL);
        """)
        with exchange.locked(), exchange._transaction():
            self.settle_ended_locked()

    def start(self, key, method, path):
        """Authenticate a live virtual key before freezing its sole active turn, never client headers."""
        ex = self.exchange
        with ex.locked():
            record = (
                ex.db.execute("SELECT id FROM sessions WHERE key=?", (key,)).fetchone()
                if isinstance(key, str)
                else None
            )
            if record is None:
                return {"request": "", "funding": "org"}  # Native API-key authentication still decides admission.
            session = record[0]
            row = ex.row(session)
            if row["ended"] or row["deadline"] <= time.time():
                raise ex.refusal_type(403, "session unavailable")
            binding = json.loads(row["binding"] or "{}")
            if binding.get("funding") != "contributed":
                return {"request": "", "funding": "org"}
            ex.check_session_funding(session, row, time.time())
            # Model listing and key activation perform no inference, secret lookup or task pause.
            if method == "GET" and path == "/v1/models":
                return {"request": "", "funding": "probe"}
            active = ex.db.execute(
                "SELECT id,binding,models FROM turns WHERE session=? AND status='running'", (session,)
            ).fetchall()
            if len(active) != 1 or active[0][1] != row["binding"]:
                raise ex.refusal_type(403, "no unique admitted active turn")
            if self.failure(active[0][0]):
                raise ex.refusal_type(403, "provider condition ended this turn")
            identifier = str(uuid.uuid4())
            ex.db.execute(
                "INSERT INTO inference_requests(id,session,turn,binding,created,model,repository,issue) VALUES(?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    session,
                    active[0][0],
                    row["binding"],
                    time.time(),
                    json.loads(active[0][2])[0],
                    binding["repository"],
                    binding["issue"],
                ),
            )
            return {"request": identifier, "funding": "contributed"}

    def snapshot(self, identifier):
        """Load an immutable request identity; callers hold the lifecycle lock."""
        row = self.exchange.db.execute(
            "SELECT session,turn,binding,resolved,claimed,model FROM inference_requests WHERE id=?", (identifier,)
        ).fetchone()
        if row is None:
            raise self.exchange.refusal_type(403, "unknown private request")
        return {
            "request": identifier,
            "session": row[0],
            "turn": row[1],
            "binding": json.loads(row[2]),
            "resolved": row[3],
            "claimed": row[4],
            "model": row[5],
        }

    def resolve(self, identifier, session):
        """Resolve this session's frozen version anew on every native inference request."""
        ex = self.exchange
        with ex.locked():
            snap = self.snapshot(identifier)
            row = ex.row(session)
            if snap["session"] != session or row["ended"] or row["deadline"] <= time.time():
                raise ex.refusal_type(403, "request session unavailable")
            ex.check_session_funding(session, row, time.time())
            if snap["resolved"]:
                raise ex.refusal_type(409, "request already resolved")
            if not ex.db.execute(
                "SELECT 1 FROM turns WHERE id=? AND session=? AND status='running'", (snap["turn"], session)
            ).fetchone():
                raise ex.refusal_type(403, "request turn ended")
            ex.db.execute("UPDATE inference_requests SET resolved=-1 WHERE id=?", (identifier,))
        bound = snap["binding"]
        try:
            material = ex.vault.resolve(bound)
        except ex.refusal_type as error:
            self.finish(
                identifier,
                "resolver",
                {
                    "category": "auth" if error.status in (404, 410) else "unknown",
                    "usage": None,
                    "completed": False,
                    "status": 403 if error.status in (404, 410) else 503,
                    "retry_after": None,
                },
            )
            raise
        required = {
            "user_id": bound["sponsor"],
            "record_id": bound["credential"],
            "route": bound["route"],
            "version": bound["credential_version"],
        }
        if (
            not isinstance(material, dict)
            or any(material.get(k) != v for k, v in required.items())
            or not isinstance(material.get("secret"), str)
            or not material["secret"]
            or any(c in material["secret"] for c in "\r\n")
        ):
            self.finish(identifier, "resolver", {"category": "unknown", "status": 503, "completed": False})
            raise ex.refusal_type(503, "resolver identity incomplete")
        account = material.get("account") or ""
        if isinstance(account, dict) and bound["route"] == "chatgpt-subscription":
            account = account.get("chatgpt_account_id")
        if (
            not isinstance(account, str)
            or any(c in account for c in "\r\n")
            or bound["route"] == "chatgpt-subscription"
            and not account
        ):
            self.finish(identifier, "resolver", {"category": "unknown", "status": 503, "completed": False})
            raise ex.refusal_type(503, "credential account metadata unavailable")
        with ex.locked():
            row = ex.row(session)
            active = ex.db.execute(
                "SELECT 1 FROM turns WHERE id=? AND session=? AND status='running'", (snap["turn"], session)
            ).fetchone()
            if not active or row["ended"] or row["deadline"] <= time.time() or not ex.allowed(bound):
                self.finish_locked(identifier, "resolver", {"category": "unknown", "status": 403, "completed": False})
                raise ex.refusal_type(403, "request authority ended during resolution")
            ex.db.execute("UPDATE inference_requests SET resolved=1 WHERE id=?", (identifier,))
        return {"token": material["secret"], "account": account, "route": bound["route"], "request": identifier}

    def claim(self, identifier, route):
        """Allow the loopback observer to consume one successfully resolved request exactly once."""
        ex = self.exchange
        with ex.locked():
            snap = self.snapshot(identifier)
            if snap["binding"]["route"] != route or snap["resolved"] != 1 or snap["claimed"]:
                raise ex.refusal_type(403, "observer request unavailable")
            ex.db.execute("UPDATE inference_requests SET claimed=1 WHERE id=?", (identifier,))
            row = ex.row(snap["session"])
            active = ex.db.execute(
                "SELECT 1 FROM turns WHERE id=? AND session=? AND status='running'", (snap["turn"], snap["session"])
            ).fetchone()
            if not active or row["ended"] or row["deadline"] <= time.time() or not ex.allowed(snap["binding"]):
                raise ex.refusal_type(403, "observer turn ended")
            return {**snap, "deadline": row["deadline"]}

    def attach(self, snapshot, stream):
        """Bind one live upstream socket to the existing session/turn authority before sending bytes."""
        ex = self.exchange
        with ex.locked():
            row = ex.row(snapshot["session"])
            active = ex.db.execute(
                "SELECT 1 FROM turns WHERE id=? AND session=? AND status='running'",
                (snapshot["turn"], snapshot["session"]),
            ).fetchone()
            if not active or row["ended"] or row["deadline"] <= time.time() or not ex.allowed(snapshot["binding"]):
                raise ex.refusal_type(403, "upstream authority ended")
            with self.connections_guard:
                self.connections[snapshot["request"]] = (snapshot["session"], snapshot["turn"], stream)

    def detach(self, identifier):
        """Forget a completed socket without changing its durable request evidence."""
        with self.connections_guard:
            self.connections.pop(identifier, None)

    def stop(self, session=None, identifier=None, turn=None):
        """Interrupt only matching admitted sockets on retirement or their frozen deadline."""
        with self.connections_guard:
            for request, (owner, active_turn, stream) in list(self.connections.items()):
                if (
                    identifier is not None
                    and request != identifier
                    or session is not None
                    and owner != session
                    or turn is not None
                    and active_turn != turn
                ):
                    continue
                try:
                    stream.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                stream.close()
                self.connections.pop(request, None)

    def finish(self, identifier, source, result):
        """Append sanitized metadata under the existing lifecycle lock."""
        with self.exchange.locked():
            self.finish_locked(identifier, source, result)

    def finish_locked(self, identifier, source, result):
        """Append one immutable source observation; callers hold the lifecycle lock/transaction."""
        ex = self.exchange
        snap = self.snapshot(identifier)
        value = {k: result.get(k) for k in ("status", "category", "usage", "completed", "retry_after")}
        value["category"] = value["category"] if value["category"] in ERRORS | {"unknown", None} else "unknown"
        value["usage"] = tokens(value["usage"])
        value.update({k: snap[k] for k in ("request", "session", "turn", "binding", "model")})
        value["source"] = source
        ex.db.execute(
            "INSERT OR IGNORE INTO inference_observations(request,source,body,turn,repository,issue) VALUES(?,?,?,?,?,?)",
            (
                identifier,
                source,
                json.dumps(value, sort_keys=True),
                snap["turn"],
                snap["binding"]["repository"],
                snap["binding"]["issue"],
            ),
        )

    def settle_ended_locked(self):
        """Close missing evidence only after durable owner retirement, retaining late native joins."""
        requests = self.exchange.db.execute(
            "SELECT r.id FROM inference_requests r JOIN sessions s ON s.id=r.session "
            "WHERE s.ended IS NOT NULL AND NOT EXISTS(SELECT 1 FROM inference_observations o WHERE o.request=r.id)"
        ).fetchall()
        for (identifier,) in requests:
            self.finish_locked(identifier, "native_gap", {"category": "unknown", "completed": False})

    def import_native(self):
        """Join append-ordered native rows in one transaction, retaining the cursor with observations."""
        ex = self.exchange
        if not self.native_database:
            return "disabled"
        with self.import_guard:
            with ex.locked():
                cursor = ex.db.execute("SELECT identity,last_row FROM native_cursor WHERE id=1").fetchone()
            try:
                stat = Path(self.native_database).stat()
                identity = f"{stat.st_dev}:{stat.st_ino}"
                with closing(
                    sqlite3.connect("file:" + str(self.native_database) + "?mode=ro", uri=True, timeout=1)
                ) as db:
                    maximum = db.execute("SELECT COALESCE(MAX(rowid),0) FROM request_logs").fetchone()[0]
                    after = cursor[1] if cursor and cursor[0] == identity and cursor[1] <= maximum else 0
                    rows = db.execute(
                        "SELECT rowid,attributes_json FROM request_logs WHERE rowid>? ORDER BY rowid", (after,)
                    ).fetchall()
            except (OSError, sqlite3.DatabaseError):
                with ex.locked(), ex._transaction():
                    self.settle_ended_locked()
                return "unavailable"
            with ex.locked(), ex._transaction():
                for _, raw in rows:
                    try:
                        value = json.loads(raw)
                        identifier = value.get("concurro_request") if isinstance(value, dict) else None
                        if not isinstance(identifier, str) or not identifier:
                            continue
                        self.finish_locked(
                            identifier,
                            "native",
                            {
                                "status": value.get("http.status"),
                                "category": "budget_denied" if value.get("concurro_native_budget") is True else None,
                                "usage": None,
                                "completed": False,
                                "retry_after": None,
                            },
                        )
                    except (ValueError, TypeError, ex.refusal_type):
                        continue
                high = rows[-1][0] if rows else after
                ex.db.execute("INSERT OR REPLACE INTO native_cursor VALUES(1,?,?)", (identity, high))
                self.settle_ended_locked()
            return "available"

    def pending(self, turn=None, repository=None, issue=None):
        """Unjoined native requests cannot authorize a replacement paid attempt during log outages."""
        query = "SELECT r.id FROM inference_requests r WHERE NOT EXISTS(SELECT 1 FROM inference_observations o WHERE o.request=r.id)"
        parameters = ()
        if turn is not None:
            query += " AND r.turn=?"
            parameters = (turn,)
        else:
            query += " AND NOT EXISTS(SELECT 1 FROM turns t WHERE t.id=r.turn AND t.status='running') AND r.repository=? AND r.issue=?"
            parameters = (repository, issue)
        return self.exchange.db.execute(query, parameters).fetchone() is not None

    def failure(self, turn):
        """Return a trusted terminal condition before generic CLI failure or any paid restart."""
        for sequence, raw in self.exchange.db.execute(
            "SELECT sequence,body FROM inference_observations WHERE turn=? ORDER BY sequence", (turn,)
        ):
            value = json.loads(raw)
            if value["turn"] == turn and value["category"] in ERRORS:
                return {"sequence": sequence, **value}
        return None

    def events(self, repository, issue):
        """Read only this task's indexed durable evidence, including late completions."""
        with self.exchange.locked():
            return [
                {"sequence": sequence, **json.loads(raw)}
                for sequence, raw in self.exchange.db.execute(
                    "SELECT sequence,body FROM inference_observations WHERE repository=? AND issue=? ORDER BY sequence",
                    (repository, issue),
                )
            ]


class Handler(BaseHTTPRequestHandler):
    """Loopback-only native start/resolve hooks and a fixed-route streaming observation hop."""

    protocol_version = "HTTP/1.1"
    timeout = 60

    def do_POST(self):
        """Authenticate hooks with the virtual key or unguessable native-only request nonce."""
        store = self.server.inference
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 <= size <= self.server.max_body or self.headers.get("Transfer-Encoding"):
                return self.reply(413, {"error": {"type": "unknown", "message": "unsupported request framing"}})
            with self.server.body_budget.reserve(size):
                body = self.rfile.read(size)
                if len(body) != size:
                    return self.reply(400, {"error": {"type": "unknown", "message": "incomplete request"}})
                if self.path == "/start":
                    value = json.loads(body)
                    supplied = value.get("authorization", "")
                    api_key = value.get("api_key", "")
                    token = supplied[7:] if isinstance(supplied, str) and supplied.startswith("Bearer ") else None
                    if isinstance(api_key, str) and api_key:
                        if token and token != api_key:
                            return self.reply(403, {"error": {"type": "unknown", "message": "ambiguous gateway key"}})
                        token = api_key
                    result = store.start(token, value.get("method"), value.get("path"))
                    return self.reply(200, {}, {"x-concurro-" + k: v for k, v in result.items()})
                if self.path == "/resolve":
                    value = json.loads(body)
                    result = store.resolve(value.get("request"), value.get("session"))
                    return self.reply(200, {}, {"x-concurro-" + k: v for k, v in result.items()})
                route, sep, path = self.path.lstrip("/").partition("/")
                if not sep or route not in store.upstreams:
                    return self.reply(404, {"error": {"type": "unknown", "message": "route unavailable"}})
                snap = store.claim(self.headers.get("x-concurro-request"), route)
                self.forward(snap, store.upstreams[route] + "/" + path, body)
        except self.server.inference.exchange.refusal_type as error:
            self.reply(error.status, {"error": {"type": "unknown", "message": "private request refused"}})
        except (ValueError, KeyError, TypeError):
            self.reply(400, {"error": {"type": "unknown", "message": "invalid private request"}})
        except (OSError, http.client.HTTPException):
            self.close_connection = True

    def reply(self, status, value, headers=None):
        """Send fixed errors or private response headers without logging credential material."""
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.close_connection = True
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def forward(self, snapshot, url, body):
        """Relay bounded JSON/SSE with backpressure; close upstream promptly on cancellation."""
        parsed = urllib.parse.urlsplit(url)
        cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        connection = cls(parsed.hostname, parsed.port, timeout=max(1, snapshot["deadline"] - time.time()))
        excluded = (
            HOP | {"origin", "chatgpt-account-id", "accept-encoding"}
            if snapshot["binding"]["route"] != "chatgpt-subscription"
            else HOP | {"origin", "accept-encoding"}
        )
        headers = {
            k.lower(): v
            for k, v in self.headers.items()
            if k.lower() not in excluded and not k.lower().startswith(PRIVATE)
        }
        if self.headers.get_all("anthropic-beta"):
            headers["anthropic-beta"] = ", ".join(self.headers.get_all("anthropic-beta"))
        headers["accept-encoding"] = "identity"
        observer = Observation(snapshot["binding"]["route"], 502)
        timer = None
        try:
            connection.connect()
            self.server.inference.attach(snapshot, connection.sock)
            timer = threading.Timer(
                max(0, snapshot["deadline"] - time.time()),
                self.server.inference.stop,
                kwargs={"identifier": snapshot["request"]},
            )
            timer.daemon = True
            timer.start()
            connection.request("POST", parsed.path + ("?" + parsed.query if parsed.query else ""), body, headers)
            response = connection.getresponse()
            observer = Observation(snapshot["binding"]["route"], response.status, response.getheader("Retry-After"))
            if response.getheader("Content-Encoding", "identity").lower() not in ("identity", ""):
                observer.ambiguous = True
                return self.reply(502, {"error": {"type": "unknown", "message": "unsupported provider encoding"}})
            if "text/event-stream" not in response.getheader("Content-Type", ""):
                raw = response.read(MAX_EVENT + 1)
                try:
                    value = json.loads(raw) if len(raw) <= MAX_EVENT else None
                except (ValueError, UnicodeError):
                    value = None
                if value is None:
                    observer.ambiguous = True
                    return self.reply(502, {"error": {"type": "unknown", "message": "unsupported provider response"}})
                failed = observer.accept(value)
                if failed or response.status >= 400:
                    observer.result["category"] = category(value, observer.route) or "unknown"
                    return self.reply(
                        response.status,
                        {"error": {"type": observer.result["category"], "message": "provider request failed"}},
                    )
                return self.reply(response.status, value)
            self.connection.settimeout(10)
            self.send_response(response.status)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            event = bytearray()
            while chunk := response.readline(MAX_EVENT + 1):
                event.extend(chunk)
                if len(event) > MAX_EVENT:
                    observer.ambiguous = True
                    break  # Opaque bytes can contain errors; never leak uninspected detail.
                if chunk not in (b"\n", b"\r\n"):
                    continue
                try:
                    value, error_event = event_value(event)
                except (ValueError, UnicodeError):
                    observer.ambiguous = True
                    break
                if observer.accept(value) or error_event:
                    observer.result["category"] = observer.result["category"] or "unknown"
                    error = {
                        "type": "error",
                        "error": {"type": observer.result["category"], "message": "provider request failed"},
                    }
                    if snapshot["binding"]["route"] in ("openai-api-key", "chatgpt-subscription"):
                        error = {
                            "type": "error",
                            "code": observer.result["category"],
                            "message": "provider request failed",
                            "param": None,
                            "sequence_number": 0,
                        }
                    event = bytearray(("event: error\ndata: " + json.dumps(error) + "\n\n").encode())
                self.wfile.write(event)
                self.wfile.flush()
                event.clear()
                if observer.result["category"]:
                    break
            if event:
                observer.ambiguous = True
        except (OSError, http.client.HTTPException):
            observer.ambiguous = True
        finally:
            self.close_connection = True
            if timer:
                timer.cancel()
            self.server.inference.detach(snapshot["request"])
            connection.close()
            self.server.inference.finish(snapshot["request"], "provider", observer.finish())

    def log_message(self, *_args):
        """Never log keys, private response headers, provider bodies or caller paths."""
