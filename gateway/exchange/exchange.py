"""Concurro's key exchange: a GitHub Actions OIDC token in, one agentgateway session key out.

Runs beside the gateway in its network namespace, the only process that reaches the gateway's
unauthenticated admin API. Sessions are issued by the operator (later, the Concurro service);
a runner job trades its OIDC token for its session's key, and the key is deleted when the
session ends, its deadline passes, or reconciliation finds it orphaned.
"""

import argparse
import base64
from contextlib import contextmanager
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

ISSUER = "https://token.actions.githubusercontent.com"
JWKS_URL = ISSUER + "/.well-known/jwks"
# Clock skew tolerated on time claims, and the oldest token accepted.
SKEW, MAX_AGE = 60, 600
# Requests served at once, and how long one connection may stay idle.
MAX_REQUESTS, REQUEST_TIMEOUT = 16, 30
SESSION_PATH = re.compile(r"/exchange/sessions/([0-9a-f-]{36})/(key|end)")
EXPECTED = ("repository_id", "repository_owner_id", "job_workflow_ref", "job_workflow_sha", "event_name", "ref")


class Refused(Exception):
    """A request the exchange will not serve; `status` is its HTTP status."""

    def __init__(self, status, reason):
        super().__init__(reason)
        self.status = status


def b64decode(segment):
    """Decode base64url without padding."""
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def der_length(size):
    """An ASN.1 DER length."""
    if size < 0x80:
        return bytes([size])
    body = size.to_bytes((size.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def der(tag, body):
    """One ASN.1 DER element."""
    return bytes([tag]) + der_length(len(body)) + body


def der_integer(value):
    """A non-negative ASN.1 INTEGER."""
    body = value.to_bytes((value.bit_length() + 8) // 8, "big")
    return der(0x02, body)


def jwk_pem(jwk):
    """An RSA JWK as a PEM SubjectPublicKeyInfo, for `openssl dgst -verify`."""
    n = int.from_bytes(b64decode(jwk["n"]), "big")
    e = int.from_bytes(b64decode(jwk["e"]), "big")
    rsa = der(0x30, der_integer(n) + der_integer(e))
    algorithm = der(0x30, der(0x06, bytes.fromhex("2a864886f70d010101")) + der(0x05, b""))
    info = der(0x30, algorithm + der(0x03, b"\x00" + rsa))
    lines = [base64.b64encode(info)[i:i + 64].decode() for i in range(0, len(base64.b64encode(info)), 64)]
    return "-----BEGIN PUBLIC KEY-----\n" + "\n".join(lines) + "\n-----END PUBLIC KEY-----\n"


def verify_rs256(pem, message, signature):
    """Whether `signature` is a valid RS256 signature of `message` under `pem`."""
    with tempfile.TemporaryDirectory() as directory:
        key, sig = Path(directory, "key.pem"), Path(directory, "sig")
        key.write_text(pem)
        sig.write_bytes(signature)
        result = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(key), "-signature", str(sig)],
                                input=message, capture_output=True, timeout=15)
    return result.returncode == 0


class Keys:
    """GitHub's OIDC signing keys, refreshed when an unknown key ID appears (at most once a minute)."""

    def __init__(self, fetch=None):
        self.fetch = fetch or (lambda: json.load(urllib.request.urlopen(JWKS_URL, timeout=15)))
        self.keys, self.fetched, self.guard = {}, 0.0, threading.Lock()

    def pem(self, kid):
        with self.guard:
            if kid not in self.keys and time.time() - self.fetched > 60:
                self.keys = {k["kid"]: jwk_pem(k) for k in self.fetch()["keys"] if k.get("kty") == "RSA"}
                self.fetched = time.time()
            return self.keys.get(kid)


def claims(token, audience, keys, now=None):
    """Verify a GitHub Actions OIDC token's signature and standard claims; return its claims."""
    now = time.time() if now is None else now
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
        header, payload = json.loads(b64decode(header_b64)), json.loads(b64decode(payload_b64))
        signature = b64decode(signature_b64)
    except (ValueError, TypeError):
        raise Refused(401, "malformed token") from None
    pem = keys.pem(header.get("kid")) if header.get("alg") == "RS256" else None
    if not pem or not verify_rs256(pem, (header_b64 + "." + payload_b64).encode(), signature):
        raise Refused(401, "invalid token signature")
    aud = payload.get("aud")
    if (payload.get("iss") != ISSUER or audience not in (aud if isinstance(aud, list) else [aud])
            or not isinstance(payload.get("exp"), (int, float)) or payload["exp"] < now - SKEW
            or payload.get("nbf", 0) > now + SKEW or not now - MAX_AGE <= payload.get("iat", 0) <= now + SKEW):
        raise Refused(401, "token issuer, audience or time claims are invalid")
    if not isinstance(payload.get("jti"), str) or not payload["jti"]:
        raise Refused(401, "token has no ID")
    return payload


class Gateway:
    """agentgateway's admin API (loopback, unauthenticated) and its inference port, for activation checks."""

    def __init__(self, admin, inference):
        self.admin, self.inference = admin.rstrip("/"), inference.rstrip("/")

    def call(self, method, url, body=None, headers=None):
        request = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode()
        except (urllib.error.URLError, OSError):
            raise Refused(502, "gateway unreachable") from None

    def find(self, key):
        """The gateway ID of a key value, or None."""
        return next((r["id"] for r in self.list() if r["value"].get("key") == key), None)

    def create(self, key, name, models, budget):
        """Create a session key; return its gateway ID once the key is accepted for traffic."""
        value = {"key": key, "metadata": {"name": name, "concurro.session": name}, "allowedModels": models,
                 "budgets": [{"name": "session", "limit": {"unit": "Tokens", "amount": budget},
                              "onBudgetExceeded": "Block"}]}
        status, text = self.call("PUT", self.admin + "/api/config/resources/llm.apiKey", {"resources": [{"value": value}]})
        if status >= 300:
            raise Refused(502, f"gateway refused the key ({status})")
        key_id = self.find(key)
        if not key_id:
            raise Refused(502, "gateway did not record the key")
        self.activated(key)
        return key_id

    def activated(self, key):
        """Wait until a key is accepted for traffic; new keys reach the traffic path asynchronously."""
        for _ in range(50):
            if self.call("GET", self.inference + "/v1/models", headers={"Authorization": "Bearer " + key})[0] != 401:
                return
            time.sleep(0.2)
        raise Refused(504, "gateway never activated the key")

    def list(self):
        status, text = self.call("GET", self.admin + "/api/config/resources/llm.apiKey")
        if status != 200:
            raise Refused(502, f"gateway key listing failed ({status})")
        return json.loads(text).get("resources", [])

    def delete(self, key_id):
        """Delete a key; True if it is gone (already absent counts)."""
        return self.call("DELETE", self.admin + "/api/config/resources/llm.apiKey/" + key_id)[0] in (200, 404)


class Exchange:
    """Sessions, their keys and their run bindings, kept durably in SQLite."""

    def __init__(self, database, gateway, keys, audience):
        self.db = sqlite3.connect(database, check_same_thread=False, isolation_level=None)
        self.gateway, self.keys, self.audience = gateway, keys, audience
        self.guard, self.lockfile = threading.Lock(), database + ".lock"
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, expected TEXT NOT NULL, models TEXT NOT NULL,
                budget INTEGER NOT NULL, deadline REAL NOT NULL, run_id TEXT, key TEXT, key_id TEXT,
                ended REAL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS seen (jti TEXT PRIMARY KEY, exp REAL NOT NULL);""")

    @contextmanager
    def locked(self):
        """Serialize lifecycle operations across threads and processes (the server and operator commands)."""
        with self.guard, open(self.lockfile, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def issue(self, expected, models, budget, lease):
        """Record a session the operator authorizes; returns its ID."""
        if set(expected) - set(EXPECTED) - {"actor_id"} or not all(isinstance(v, str) for v in expected.values()):
            raise ValueError("unknown or non-string expected claims")
        if not models or type(budget) is not int or budget <= 0 or lease <= 0:
            raise ValueError("a session needs models, a positive budget and a positive lease")
        session = str(uuid.uuid4())
        with self.locked():
            self.db.execute("INSERT INTO sessions (id, expected, models, budget, deadline, created) VALUES (?,?,?,?,?,?)",
                            (session, json.dumps(expected), json.dumps(models), budget, time.time() + lease, time.time()))
        return session

    def row(self, session):
        found = self.db.execute("SELECT expected, models, budget, deadline, run_id, key, key_id, ended FROM sessions "
                                "WHERE id = ?", (session,)).fetchone()
        if not found:
            raise Refused(404, "unknown session")
        return dict(zip(("expected", "models", "budget", "deadline", "run_id", "key", "key_id", "ended"), found))

    def authorize(self, session, token, now):
        """Check a token against a live session, binding the session to the token's run on first use."""
        payload = claims(token, self.audience, self.keys, now)
        record = self.row(session)
        if record["ended"] or record["deadline"] < now:
            raise Refused(403, "session has ended")
        expected = json.loads(record["expected"])
        if any(str(payload.get(k)) != v for k, v in expected.items()):
            raise Refused(403, "token does not match the session")
        if payload.get("runner_environment") != "github-hosted":
            raise Refused(403, "only GitHub-hosted runners are served")
        run = f"{payload.get('run_id')}/{payload.get('run_attempt')}"
        if str(payload.get("run_attempt")) != "1" or record["run_id"] not in (None, run):
            raise Refused(403, "session is bound to another run")
        self.db.execute("DELETE FROM seen WHERE exp < ?", (now - SKEW,))
        try:
            self.db.execute("INSERT INTO seen (jti, exp) VALUES (?, ?)", (payload["jti"], payload["exp"]))
        except sqlite3.IntegrityError:
            raise Refused(401, "token already used") from None
        if record["run_id"] is None:
            self.db.execute("UPDATE sessions SET run_id = ? WHERE id = ? AND run_id IS NULL", (run, session))
        return record

    def key(self, session, token, now=None):
        """The session's key, minted on first request; a retry from the bound run gets the same key."""
        now = time.time() if now is None else now
        with self.locked():
            record = self.authorize(session, token, now)
            key = record["key"]
            if key and record["key_id"]:
                return {"key": key, "models": json.loads(record["models"]), "deadline": record["deadline"]}
            if not key:
                key = "sk-agw-" + secrets.token_hex(24)
                # Durable before the gateway sees it, so reconciliation can always find it.
                self.db.execute("UPDATE sessions SET key = ? WHERE id = ?", (key, session))
            # A retry after a partial creation adopts the key if the gateway already has it.
            key_id = self.gateway.find(key)
            if key_id:
                self.gateway.activated(key)
            else:
                key_id = self.gateway.create(key, session, json.loads(record["models"]), record["budget"])
            self.db.execute("UPDATE sessions SET key_id = ? WHERE id = ?", (key_id, session))
            return {"key": key, "models": json.loads(record["models"]), "deadline": record["deadline"]}

    def end(self, session, token=None, now=None):
        """End a session and delete its key. `token` is required unless the operator ends it."""
        now = time.time() if now is None else now
        with self.locked():
            record = self.authorize(session, token, now) if token is not None else self.row(session)
            self.retire(session, record, now)

    def retire(self, session, record, now):
        """End a session, then delete its key; a failed deletion is retried by reconciliation."""
        self.db.execute("UPDATE sessions SET ended = COALESCE(ended, ?) WHERE id = ?", (now, session))
        key_id = record["key_id"] or (self.gateway.find(record["key"]) if record["key"] else None)
        if key_id and not self.gateway.delete(key_id):
            raise Refused(502, "gateway did not delete the key; reconciliation will retry")

    def reconcile(self, now=None):
        """Delete keys of ended or expired sessions and gateway keys no session owns; return what was deleted."""
        now = time.time() if now is None else now
        with self.locked():
            live = set()
            for session, key, deadline, ended in self.db.execute(
                    "SELECT id, key, deadline, ended FROM sessions WHERE key IS NOT NULL").fetchall():
                if ended or deadline < now:
                    self.db.execute("UPDATE sessions SET ended = COALESCE(ended, ?) WHERE id = ?", (now, session))
                else:
                    live.add(key)
            removed = []
            for resource in self.gateway.list():
                if resource["value"].get("key") not in live and self.gateway.delete(resource["id"]):
                    removed.append(resource["value"].get("metadata", {}).get("concurro.session") or resource["id"])
            return removed


def server(exchange, address):
    """The runner-facing HTTP API: POST /exchange/sessions/<id>/key and /end, with the OIDC token as bearer."""

    class Handler(BaseHTTPRequestHandler):
        timeout = REQUEST_TIMEOUT

        def do_POST(self):
            match = SESSION_PATH.fullmatch(self.path)
            auth = self.headers.get("Authorization", "")
            try:
                if not match:
                    raise Refused(404, "not found")
                if not auth.startswith("Bearer "):
                    raise Refused(401, "a bearer OIDC token is required")
                session, action = match.groups()
                body = exchange.key(session, auth[7:]) if action == "key" else (exchange.end(session, auth[7:]) or {})
                self.reply(200, body)
            except Refused as error:
                self.reply(error.status, {"error": str(error)})
            except Exception:
                self.reply(500, {"error": "internal error"})

        def reply(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):
            # Paths and statuses only; tokens and keys never reach the log.
            sys.stderr.write("exchange %s %s\n" % (self.command, fmt % args))

    class Bounded(ThreadingHTTPServer):
        """Serves at most MAX_REQUESTS at once; further connections are answered 503 and closed."""
        daemon_threads = True
        slots = threading.BoundedSemaphore(MAX_REQUESTS)

        def process_request(self, request, client_address):
            if not self.slots.acquire(blocking=False):
                try:
                    request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                finally:
                    self.shutdown_request(request)
                return
            super().process_request(request, client_address)

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                self.slots.release()

    host, port = address.rsplit(":", 1)
    return Bounded((host, int(port)), Handler)


def reconciling(exchange, interval=60):
    """Reconcile at startup and then periodically; a failed pass is retried on the next one."""
    while True:
        try:
            removed = exchange.reconcile()
            if removed:
                print("reconciled: deleted keys of", removed, flush=True)
        except Exception as error:
            print("reconciliation failed:", type(error).__name__, error, flush=True)
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    issue = sub.add_parser("issue")
    issue.add_argument("--repository-id", required=True)
    issue.add_argument("--repository-owner-id", required=True)
    issue.add_argument("--job-workflow-ref", required=True)
    issue.add_argument("--job-workflow-sha", required=True)
    issue.add_argument("--event-name", required=True)
    issue.add_argument("--ref", required=True)
    issue.add_argument("--actor-id")
    issue.add_argument("--models", required=True, help="comma-separated model patterns")
    issue.add_argument("--budget", type=int, required=True, help="token budget")
    issue.add_argument("--lease-hours", type=float, default=6)
    sub.add_parser("list")
    end = sub.add_parser("end")
    end.add_argument("session")
    sub.add_parser("reconcile")
    args = parser.parse_args()
    env = os.environ.get
    exchange = Exchange(env("EXCHANGE_DB", "/data/exchange.db"),
                        Gateway(env("EXCHANGE_ADMIN", "http://127.0.0.1:15000"), env("EXCHANGE_INFERENCE", "http://127.0.0.1:4000")),
                        Keys(), env("EXCHANGE_AUDIENCE", "concurro-gateway"))
    if args.command == "serve":
        threading.Thread(target=reconciling, args=(exchange,), daemon=True).start()
        server(exchange, env("EXCHANGE_LISTEN", "0.0.0.0:8080")).serve_forever()
    elif args.command == "issue":
        expected = {k: getattr(args, k) for k in EXPECTED}
        if args.actor_id:
            expected["actor_id"] = args.actor_id
        print(exchange.issue(expected, args.models.split(","), args.budget, args.lease_hours * 3600))
    elif args.command == "list":
        for row in exchange.db.execute("SELECT id, run_id, key_id IS NOT NULL, deadline, ended FROM sessions"):
            print(json.dumps(dict(zip(("session", "run", "has_key", "deadline", "ended"), row))))
    elif args.command == "end":
        exchange.end(args.session)
    elif args.command == "reconcile":
        print(exchange.reconcile())


if __name__ == "__main__":
    main()
