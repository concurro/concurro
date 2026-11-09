"""Concurro's key exchange and turn relay, beside agentgateway.

Runs in the gateway's network namespace, the only process that reaches the gateway's
unauthenticated admin API. A runner job trades its GitHub Actions OIDC token for its session's
key and a runner token, then takes the turns the operator (the Concurro service) queues for its
target and returns their results. Only bound runner sessions receive inference keys.
A key is deleted when its session ends, its deadline passes, its runner stops calling, or
reconciliation finds it orphaned.
"""

import argparse
import base64
import fcntl
import hashlib
import hmac
import json
import os
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
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ISSUER = "https://token.actions.githubusercontent.com"
JWKS_URL = ISSUER + "/.well-known/jwks"
# Clock skew tolerated on time claims, and the oldest token accepted.
SKEW, MAX_AGE = 60, 600
# Requests served at once, and how long one connection may stay idle.
# Long polls hold a request each: runners waiting for turns and the operator waiting for results.
MAX_REQUESTS, REQUEST_TIMEOUT = 64, 60
# Turn payloads and results carry repository text and retained transcripts; only an authenticated
# submission or result may be that large, and bodies held at once share BODY_BUDGET.
MAX_BODY, SMALL_BODY, BODY_BUDGET = 48 * 1024 * 1024, 64 * 1024, 160 * 1024 * 1024
POLL_SECONDS = 1
# A dispatched session must start within START_SECONDS; a started one calls at least every HEARTBEAT_SECONDS.
START_SECONDS, HEARTBEAT_SECONDS = 1200, 180
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
SESSION_PATH = re.compile(
    rf"/exchange/sessions/(?P<session>{UUID})/(?:(?P<action>key|end|next)|turns/(?P<turn>{UUID}))"
)
OPERATOR_PATH = re.compile(rf"(?P<kind>sessions|turns)/(?P<id>{UUID})(?:/(?P<action>end))?")
# Mirrors engine.protocol.DIGEST_PATTERN; this image ships as a single file.
RUNNER_TOKEN = re.compile(r"[0-9a-f]{64}")
LABEL = re.compile(r"[A-Za-z0-9._:/#-]{1,200}")
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
    lines = [base64.b64encode(info)[i : i + 64].decode() for i in range(0, len(base64.b64encode(info)), 64)]
    return "-----BEGIN PUBLIC KEY-----\n" + "\n".join(lines) + "\n-----END PUBLIC KEY-----\n"


def verify_rs256(pem, message, signature):
    """Whether `signature` is a valid RS256 signature of `message` under `pem`."""
    with tempfile.TemporaryDirectory() as directory:
        key, sig = Path(directory, "key.pem"), Path(directory, "sig")
        key.write_text(pem)
        sig.write_bytes(signature)
        result = subprocess.run(
            ["openssl", "dgst", "-sha256", "-verify", str(key), "-signature", str(sig)],
            input=message,
            capture_output=True,
            timeout=15,
        )
    return result.returncode == 0


JWKS_REFRESH_SECONDS = 60


class Keys:
    """GitHub's OIDC signing keys, refreshed when an unknown key ID appears (at most once a minute)."""

    def __init__(self, fetch=None):
        self.fetch = fetch or (lambda: json.load(urllib.request.urlopen(JWKS_URL, timeout=15)))
        self.keys, self.fetched, self.guard = {}, 0.0, threading.Lock()

    def pem(self, kid):
        with self.guard:
            if kid not in self.keys and time.time() - self.fetched > JWKS_REFRESH_SECONDS:
                self.keys = {k["kid"]: jwk_pem(k) for k in self.fetch()["keys"] if k.get("kty") == "RSA"}
                self.fetched = time.time()
            return self.keys.get(kid)


def _now(now):
    """Use the supplied observation time, sampling the clock only when absent."""
    return time.time() if now is None else now


def claims(token, audience, keys, now=None):
    """Verify a GitHub Actions OIDC token's signature and standard claims; return its claims."""
    now = _now(now)
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
    if (
        payload.get("iss") != ISSUER
        or audience not in (aud if isinstance(aud, list) else [aud])
        or not isinstance(payload.get("exp"), (int, float))
        or payload["exp"] < now - SKEW
        or payload.get("nbf", 0) > now + SKEW
        or not now - MAX_AGE <= payload.get("iat", 0) <= now + SKEW
    ):
        raise Refused(401, "token issuer, audience or time claims are invalid")
    if not isinstance(payload.get("jti"), str) or not payload["jti"]:
        raise Refused(401, "token has no ID")
    return payload


ACTIVATION_ATTEMPTS = 50
ACTIVATION_POLL_SECONDS = 0.2


class Gateway:
    """agentgateway's admin API (loopback, unauthenticated) and its inference port, for activation checks."""

    def __init__(self, admin, inference):
        self.admin, self.inference = admin.rstrip("/"), inference.rstrip("/")

    def call(self, method, url, body=None, headers=None):
        request = urllib.request.Request(
            url,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json", **(headers or {})},
        )
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
        value = {
            "key": key,
            "metadata": {"name": name, "concurro.session": name},
            "allowedModels": models,
            "budgets": [
                {"name": "session", "limit": {"unit": "Tokens", "amount": budget}, "onBudgetExceeded": "Block"}
            ],
        }
        status, text = self.call(
            "PUT", self.admin + "/api/config/resources/llm.apiKey", {"resources": [{"value": value}]}
        )
        if status >= 300:
            raise Refused(502, f"gateway refused the key ({status})")
        key_id = self.find(key)
        if not key_id:
            raise Refused(502, "gateway did not record the key")
        self.activated(key)
        return key_id

    def activated(self, key):
        """Wait until a key is accepted for traffic; new keys reach the traffic path asynchronously."""
        for _ in range(ACTIVATION_ATTEMPTS):
            if self.call("GET", self.inference + "/v1/models", headers={"Authorization": "Bearer " + key})[0] != 401:
                return
            time.sleep(ACTIVATION_POLL_SECONDS)
        raise Refused(504, "gateway never activated the key")

    def list(self):
        status, text = self.call("GET", self.admin + "/api/config/resources/llm.apiKey")
        if status != 200:
            raise Refused(502, f"gateway key listing failed ({status})")
        return json.loads(text).get("resources", [])

    def delete(self, key_id):
        """Delete a key; True if it is gone (already absent counts)."""
        return self.call("DELETE", self.admin + "/api/config/resources/llm.apiKey/" + key_id)[0] in (200, 404)


def _expected_claims(expected):
    """Require known string claims, with actor_id optional and empty strings permitted."""
    if (
        not isinstance(expected, dict)
        or set(expected) - set(EXPECTED) - {"actor_id"}
        or not set(EXPECTED) <= set(expected)
        or not all(isinstance(v, str) for v in expected.values())
    ):
        raise ValueError("unknown, missing or non-string expected claims")


def session_binding(value):
    """Validate the controller-issued task, role and immutable setup/code binding."""
    fields = {"repository", "issue", "role", "generation", "config_sha", "upstream", "fork", "base", "phase"}
    repo = r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+"
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or type(value["issue"]) is not int
        or value["issue"] < 1
        or type(value["generation"]) is not int
        or value["generation"] < 1
        or any(
            not isinstance(value[k], str) or re.fullmatch(repo, value[k]) is None
            for k in ("repository", "upstream", "fork")
        )
        # Mirrors engine.protocol.SHA_PATTERN; keep the single-file exchange independent.
        or any(
            not isinstance(value[k], str) or re.fullmatch(r"[0-9a-f]{40}", value[k]) is None
            for k in ("base", "config_sha")
        )
        or not isinstance(value["role"], str)
        or re.fullmatch(r"(intake|plan|plan_qc|develop|review):(claude|codex)", value["role"]) is None
        or not isinstance(value["phase"], str)
        or not re.fullmatch(r"[A-Za-z0-9:_-]{1,128}", value["phase"])
    ):
        raise ValueError("invalid runner task/role binding")
    return json.dumps(value, sort_keys=True)


def staffing(expected, binding, models, budget, lease):
    """A reusable job identity, excluding its unique per-session OIDC ref."""
    claims = {k: v for k, v in expected.items() if k != "ref"}
    return hashlib.sha256(
        json.dumps([claims, binding, models, budget, float(lease)], sort_keys=True).encode()
    ).hexdigest()


SESSION_COLUMNS = (
    "expected",
    "models",
    "budget",
    "deadline",
    "run_id",
    "key",
    "key_id",
    "ended",
    "kind",
    "token",
    "seen",
    "created",
    "binding",
    "staffing",
)
MAX_CAPACITY = 8
MAX_WAIT_SECONDS = 50
FINISHED_RETENTION_SECONDS = 86400


class Exchange:
    """Sessions, their keys, their run bindings and the turns they serve, kept durably in SQLite.

    A `runner` session is served to one GitHub Actions run, proven by its OIDC token; a `host`
    session is a key minted directly for the operator (the Concurro service). Turns are queued by
    the operator for a target and served to a live runner session of that target.
    """

    def __init__(self, database, gateway, keys, audience):
        self.db = sqlite3.connect(database, check_same_thread=False, isolation_level=None)
        self.gateway, self.keys, self.audience = gateway, keys, audience
        self.guard, self.lockfile = threading.Lock(), database + ".lock"
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, expected TEXT NOT NULL, models TEXT NOT NULL,
                budget INTEGER NOT NULL, deadline REAL NOT NULL, run_id TEXT, key TEXT, key_id TEXT,
                ended REAL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS seen (jti TEXT PRIMARY KEY, exp REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS turns (id TEXT PRIMARY KEY, expected TEXT NOT NULL, models TEXT NOT NULL,
                budget INTEGER NOT NULL, lease REAL NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
                session TEXT, result TEXT, created REAL NOT NULL, started REAL, finished REAL, digest TEXT,
                staff TEXT, pin TEXT);""")
        # Columns added since a table was first created; older databases gain them in place.
        self.add_columns(
            "sessions",
            (
                ("kind", "TEXT NOT NULL DEFAULT 'runner'"),
                ("token", "TEXT"),
                ("seen", "REAL"),
                ("refusal", "TEXT"),
                ("binding", "TEXT"),
                ("staffing", "TEXT"),
            ),
        )
        self.add_columns(
            "turns", (("digest", "TEXT"), ("staff", "TEXT"), ("pin", "TEXT"), ("binding", "TEXT"), ("staffing", "TEXT"))
        )

    def add_columns(self, table, columns):
        present = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
        for column, kind in columns:
            if column not in present:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    @contextmanager
    def locked(self):
        """Serialize lifecycle operations across threads and processes (the server and operator commands)."""
        with self.guard, open(self.lockfile, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    @contextmanager
    def _transaction(self):
        """Commit local writes atomically; the caller holds locked(), with no nesting or gateway I/O."""
        if self.db.in_transaction:
            raise RuntimeError("exchange transactions must not nest")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            yield
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                try:
                    self.db.execute("ROLLBACK")
                except BaseException:
                    pass
            raise

    @staticmethod
    def validate(models, budget, lease):
        if (
            not isinstance(models, list)
            or not models
            or not all(isinstance(m, str) and m for m in models)
            or type(budget) is not int
            or budget <= 0
            or not isinstance(lease, (int, float))
            or lease <= 0
        ):
            raise ValueError("a session needs models, a positive budget and a positive lease")

    def issue(self, expected, models, budget, lease, binding=None):
        """Record a runner session the operator authorizes; returns its ID."""
        _expected_claims(expected)
        self.validate(models, budget, lease)
        with self.locked():
            return self.create(expected, models, budget, lease, binding=binding)

    def create(self, expected, models, budget, lease, kind="runner", binding=None):
        session, now = str(uuid.uuid4()), time.time()
        bound = session_binding(binding) if binding is not None else None
        group = staffing(expected, binding, models, budget, lease) if bound else None
        if bound:
            expected = {**expected, "event_name": "workflow_dispatch", "ref": "refs/heads/concurro/runs/" + session}
        self.db.execute(
            "INSERT INTO sessions (id, expected, models, budget, deadline, created, kind, binding, staffing) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                session,
                json.dumps(expected, sort_keys=True),
                json.dumps(models),
                budget,
                now + lease,
                now,
                kind,
                bound,
                group,
            ),
        )
        return session

    def row(self, session):
        """Read a session in its stable persisted-column order; caller holds the lock."""
        found = self.db.execute(
            "SELECT " + ", ".join(SESSION_COLUMNS) + " FROM sessions WHERE id = ?",
            (session,),
        ).fetchone()
        if not found:
            raise Refused(404, "unknown session")
        return dict(zip(SESSION_COLUMNS, found))

    def refuse(self, session, status, reason):
        """Refuse a runner and record why, so the operator can see why a session never started."""
        self.db.execute("UPDATE sessions SET refusal = ? WHERE id = ?", (reason, session))
        raise Refused(status, reason)

    def authorize(self, session, token, now):
        """Check a token against a live runner session, binding the session to the token's run on first use."""
        payload = claims(token, self.audience, self.keys, now)
        record = self.row(session)
        if record["kind"] != "runner":
            raise Refused(403, "not a runner session")
        if record["ended"] or record["deadline"] < now:
            raise Refused(403, "session has ended")
        expected = json.loads(record["expected"])
        wrong = sorted(k for k, v in expected.items() if str(payload.get(k)) != v)
        if {"repository_id", "repository_owner_id"} & set(wrong):
            # Anyone can present a token from another repository; only the target's own runs are reported.
            raise Refused(403, "token does not match the session")
        if wrong:
            self.refuse(session, 403, "token does not match the session: " + ", ".join(wrong))
        if payload.get("runner_environment") != "github-hosted":
            self.refuse(session, 403, "only GitHub-hosted runners are served")
        run = f"{payload.get('run_id')}/{payload.get('run_attempt')}"
        if str(payload.get("run_attempt")) != "1" or record["run_id"] not in (None, run):
            self.refuse(session, 403, "session is bound to another run")
        self.db.execute("DELETE FROM seen WHERE exp < ?", (now - SKEW,))
        try:
            self.db.execute("INSERT INTO seen (jti, exp) VALUES (?, ?)", (payload["jti"], payload["exp"]))
        except sqlite3.IntegrityError:
            raise Refused(401, "token already used") from None
        if record["run_id"] is None:
            self.db.execute("UPDATE sessions SET run_id = ? WHERE id = ? AND run_id IS NULL", (run, session))
        return record

    def key(self, session, token, now=None):
        """The session's key and runner token, minted on first request; a retry from the bound run gets the same."""
        now = _now(now)
        with self.locked():
            record = self.authorize(session, token, now)
            if not record["binding"]:
                raise Refused(403, "session has no task/role binding")
            key, runner = record["key"], record["token"] or secrets.token_hex(32)
            self.db.execute("UPDATE sessions SET token = ?, seen = ? WHERE id = ?", (runner, now, session))
            reply = {
                "key": None,
                "token": runner,
                "models": json.loads(record["models"]),
                "deadline": record["deadline"],
                "binding": json.loads(record["binding"]) if record["binding"] else None,
            }
            if key and record["key_id"]:
                return reply | {"key": key}
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
            return reply | {"key": key}

    def runner(self, session, token, now):
        """The live runner session that `token` proves; every runner call is also its heartbeat. The lock is held."""
        record = self.row(session)
        if record["kind"] != "runner" or not record["token"] or not hmac.compare_digest(record["token"], token):
            raise Refused(401, "invalid runner token")
        if record["ended"] or record["deadline"] < now:
            raise Refused(410, "session has ended")
        self.db.execute("UPDATE sessions SET seen = ? WHERE id = ?", (now, session))
        return record

    def prove(self, session, token):
        """Check a runner token before its request body is read."""
        with self.locked():
            self.runner(session, token, time.time())

    def next(self, session, token, capacity, wait, now=None):
        """Up to `capacity` queued turns of the session's target, waiting up to `wait` seconds for one."""
        if (
            type(capacity) is not int
            or not 0 <= capacity <= MAX_CAPACITY
            or not isinstance(wait, (int, float))
            or not 0 <= wait <= MAX_WAIT_SECONDS
        ):
            raise Refused(400, "invalid capacity or wait")
        stop = time.monotonic() + wait
        while True:
            with self.locked():
                current = _now(now)
                record = self.runner(session, token, current)
                busy = self.db.execute(
                    "SELECT 1 FROM turns WHERE session = ? AND status = 'running'", (session,)
                ).fetchone()
                turns = (
                    []
                    if not capacity or busy or not record["binding"]
                    else self.db.execute(
                        "SELECT id, payload FROM turns WHERE status = 'queued' AND staffing = ? AND (pin IS NULL OR pin = ?) "
                        "ORDER BY created LIMIT ?",
                        (record["staffing"], session, min(capacity, 1)),
                    ).fetchall()
                )
                for turn, _ in turns:
                    self.db.execute(
                        "UPDATE turns SET status = 'running', session = ?, started = ? WHERE id = ?",
                        (session, current, turn),
                    )
            if turns or time.monotonic() >= stop:
                return {"turns": [{"id": turn, "payload": json.loads(payload)} for turn, payload in turns]}
            time.sleep(POLL_SECONDS)

    def complete(self, session, token, turn, result, now=None):
        """Record a running turn's result; a turn no longer running for this session is refused."""
        now = _now(now)
        if not isinstance(result, dict):
            raise Refused(400, "a result must be an object")
        with self.locked():
            record = self.runner(session, token, now)
            if not record["binding"]:
                raise Refused(409, "legacy runner session has no task binding")
            changed = self.db.execute(
                "UPDATE turns SET status = 'done', result = ?, finished = ? "
                "WHERE id = ? AND session = ? AND status = 'running'",
                (json.dumps(result), now, turn, session),
            ).rowcount
            if not changed:
                prior = self.db.execute(
                    "SELECT result FROM turns WHERE id = ? AND session = ? AND status = 'done'", (turn, session)
                ).fetchone()
                if prior and json.loads(prior[0]) == result:
                    return
                raise Refused(409, "turn is not running in this session or its result conflicts")

    def end(self, session, token=None, runner=None, now=None):
        """End a session and delete its key: by OIDC token, by runner token, or (neither) by the operator."""
        now = _now(now)
        with self.locked():
            if runner is not None:
                record = self.runner(session, runner, now)
            elif token is not None:
                record = self.authorize(session, token, now)
            else:
                record = self.row(session)
            self.retire(session, record, now)

    def _lose_session_turns(self, session, now):
        """Lose running and pinned queued work while the caller holds the lifecycle lock."""
        self.db.execute(
            "UPDATE turns SET status = 'lost', finished = ? WHERE (session = ? AND status = 'running') "
            "OR (pin = ? AND status = 'queued')",
            (now, session, session),
        )

    def _lose_turn(self, turn, now):
        """Lose one turn while the caller holds the lifecycle lock."""
        self.db.execute("UPDATE turns SET status = 'lost', finished = ? WHERE id = ?", (now, turn))

    def retire(self, session, record, now):
        """End a session, then delete its key; a failed deletion is retried by reconciliation."""
        with self._transaction():
            self.db.execute("UPDATE sessions SET ended = COALESCE(ended, ?) WHERE id = ?", (now, session))
            self._lose_session_turns(session, now)
        key_id = record["key_id"] or (self.gateway.find(record["key"]) if record["key"] else None)
        if key_id and not self.gateway.delete(key_id):
            raise Refused(502, "gateway did not delete the key; reconciliation will retry")
        self.forget(session)

    def forget(self, session):
        """Drop an ended session's key once the gateway no longer has it."""
        self.db.execute("UPDATE sessions SET key = NULL, key_id = NULL WHERE id = ?", (session,))

    def live(self, record, now):
        """Whether a runner session can still serve turns: dispatched recently, or bound and heartbeating."""
        if record["ended"] or record["deadline"] < now:
            return False
        if record["run_id"] is None:
            return now - record["created"] < START_SECONDS
        return now - (record["seen"] or record["created"]) < HEARTBEAT_SECONDS

    def observation(self, turn):
        """Operator-only timing from the stored turn; caller holds the exchange lock."""
        status, created, started, finished, session = self.db.execute(
            "SELECT status, created, started, finished, session FROM turns WHERE id = ?", (turn,)
        ).fetchone()
        runner = self.row(session) if session and status in ("lost", "cancelled") else None
        return {
            "turn": turn,
            "status": status,
            "created": created,
            "started": started,
            "finished": finished,
            **({"runner": {"session": session, "run_id": runner["run_id"]}} if runner else {}),
        }

    def submit(self, turn, expected, models, budget, lease, payload, pin=None, now=None, binding=None):
        """Queue a turn under the caller's ID; returns it and a new session ID to dispatch, if one is needed.

        A turn `pin`ned to a runner session (one holding its state, such as a workbench) is served
        only to that session, and is lost with it.

        Submitting the same turn again returns its original admission, so a caller that lost the
        reply can recover it: the session it was given while that session is unbound, or its
        status once it was withdrawn. A different turn under a used ID is refused.
        """
        if not isinstance(turn, str) or not re.fullmatch(UUID, turn):
            raise ValueError("a turn ID must be a UUID")
        if pin is not None and (not isinstance(pin, str) or not re.fullmatch(UUID, pin)):
            raise ValueError("a pin must be a session ID")
        _expected_claims(expected)
        self.validate(models, budget, lease)
        bound = session_binding(binding)
        if expected.get("event_name") != "workflow_dispatch" or not expected.get("actor_id"):
            raise ValueError("runner turns require workflow_dispatch and the App actor")
        if not isinstance(payload, dict) or payload.get("kind") == "bench":
            raise ValueError("only model turns are supported")
        request = payload.get("request", {})
        if binding["role"] != str(request.get("stage")) + ":" + str(request.get("provider")):
            raise ValueError("turn does not match its role")
        group = staffing(expected, binding, models, budget, lease)
        now = _now(now)
        digest = hashlib.sha256(
            json.dumps([expected, models, budget, lease, payload, pin, binding], sort_keys=True).encode()
        ).hexdigest()
        with self.locked():
            found = self.db.execute("SELECT digest, status, staff, result FROM turns WHERE id = ?", (turn,)).fetchone()
            if found and found[0] is None:
                # Withdrawn before its admission arrived.
                return {**self.observation(turn), "dispatch": None}
            if found and found[0] != digest:
                raise Refused(409, "the turn ID is in use")
            if found:
                staff = found[2] and self.row(found[2])
                pending = staff and staff["run_id"] is None and self.live(staff, now)
                admitted = {**self.observation(turn), "dispatch": found[2] if pending else None}
                return {**admitted, "result": json.loads(found[3])} if found[1] == "done" else admitted
            if pin is not None:
                record = self.row(pin)
                if record["kind"] != "runner" or record["staffing"] != group:
                    raise Refused(409, "the pinned session serves another target")
            with self._transaction():
                self.db.execute(
                    "INSERT INTO turns (id, expected, models, budget, lease, payload, status, created, digest, pin, binding, staffing) "
                    "VALUES (?,?,?,?,?,?,'queued',?,?,?,?,?)",
                    (
                        turn,
                        json.dumps(expected, sort_keys=True),
                        json.dumps(models),
                        budget,
                        lease,
                        json.dumps(payload),
                        now,
                        digest,
                        pin,
                        bound,
                        group,
                    ),
                )
                staff = self.staffed(turn, now)
                self.db.execute("UPDATE turns SET staff = ? WHERE id = ?", (staff, turn))
            return {**self.observation(turn), "dispatch": staff}

    def staffed(self, turn, now):
        """For a queued turn whose target has no live runner session, issue one and return it to dispatch. The lock is held."""
        found = self.db.execute(
            "SELECT expected, models, budget, lease, status, pin, binding, staffing FROM turns WHERE id = ?", (turn,)
        ).fetchone()
        if not found or found[4] != "queued" or found[5] or not found[6]:
            return None
        expected, models, budget, lease, _, _, bound, group = found
        for row in self.db.execute(
            "SELECT id FROM sessions WHERE kind = 'runner' AND ended IS NULL AND staffing = ?", (group,)
        ).fetchall():
            if self.live(self.row(row[0]), now):
                return None
        return self.create(json.loads(expected), json.loads(models), budget, lease, binding=json.loads(bound))

    def poll(self, turn, wait, now=None):
        """A turn's status, waiting up to `wait` seconds for it to finish.

        A queued turn whose target has no live session gets one: its ID comes back as `dispatch`,
        with the latest refusal of a runner for that target since the turn was queued.
        """
        if not isinstance(wait, (int, float)) or not 0 <= wait <= MAX_WAIT_SECONDS:
            raise Refused(400, "invalid wait")
        stop = time.monotonic() + wait
        while True:
            with self.locked():
                current = _now(now)
                found = self.db.execute(
                    "SELECT status, session, result, expected, created, pin, staffing FROM turns WHERE id = ?", (turn,)
                ).fetchone()
                if not found:
                    raise Refused(404, "unknown turn")
                status, session, result, expected, created, pin, group = found
                if group is None and status in ("queued", "running"):
                    self._lose_turn(turn, current)
                    status = "lost"
                if status == "queued" and pin and not self.live(self.row(pin), current):
                    # Only its session could serve it.
                    self._lose_turn(turn, current)
                    status = "lost"
                if status == "done":
                    return {**self.observation(turn), "result": json.loads(result)}
                if status == "running":
                    record = self.row(session)
                    if not self.live(record, current):
                        try:
                            self.retire(session, record, current)
                        except Refused:
                            pass
                        status = "lost"
                if status not in ("queued", "running"):
                    return self.observation(turn)
                if time.monotonic() >= stop:
                    if status == "running":
                        return self.observation(turn)
                    refusal = self.db.execute(
                        "SELECT refusal FROM sessions WHERE kind = 'runner' AND staffing = ? AND created >= ? "
                        "AND refusal IS NOT NULL ORDER BY created DESC LIMIT 1",
                        (group, created),
                    ).fetchone()
                    return {
                        **self.observation(turn),
                        "dispatch": self.staffed(turn, current),
                        "refusal": refusal[0] if refusal else None,
                    }
            time.sleep(POLL_SECONDS)

    def cancel(self, turn, now=None):
        """Withdraw a queued or running turn; a runner's later result for it is refused."""
        now = _now(now)
        with self.locked():
            self.db.execute(
                "UPDATE turns SET status = 'cancelled', finished = ? WHERE id = ? AND status IN ('queued', 'running')",
                (now, turn),
            )
            # An unknown turn may still be in flight: its admission must find it withdrawn.
            self.db.execute(
                "INSERT OR IGNORE INTO turns (id, expected, models, budget, lease, payload, status, created, "
                "finished) VALUES (?, '', '[]', 0, 0, '{}', 'cancelled', ?, ?)",
                (turn, now, now),
            )

    def reconcile(self, now=None):
        """End dead sessions; delete keys of ended or expired sessions and gateway keys no session owns.

        Returns what was deleted. Finished turns are dropped after a day.
        """
        now = _now(now)
        with self.locked():
            for (session,) in self.db.execute(
                "SELECT id FROM sessions WHERE kind = 'runner' AND ended IS NULL"
            ).fetchall():
                record = self.row(session)
                if not self.live(record, now):
                    with self._transaction():
                        self.db.execute("UPDATE sessions SET ended = ? WHERE id = ?", (now, session))
                        self._lose_session_turns(session, now)
            self.db.execute(
                "DELETE FROM turns WHERE finished IS NOT NULL AND finished < ?", (now - FINISHED_RETENTION_SECONDS,)
            )
            live, retired = set(), {}
            for session, key, deadline, ended in self.db.execute(
                "SELECT id, key, deadline, ended FROM sessions WHERE key IS NOT NULL"
            ).fetchall():
                if ended or deadline < now:
                    self.db.execute("UPDATE sessions SET ended = COALESCE(ended, ?) WHERE id = ?", (now, session))
                    retired[key] = session
                else:
                    live.add(key)
            removed, kept = [], set()
            for resource in self.gateway.list():
                key = resource["value"].get("key")
                if key in live:
                    continue
                if self.gateway.delete(resource["id"]):
                    removed.append(resource["value"].get("metadata", {}).get("concurro.session") or resource["id"])
                else:
                    kept.add(key)
            for key, session in retired.items():
                if key not in kept:
                    self.forget(session)
            return removed


class Budget:
    """Bytes of request bodies held at once; a request that cannot reserve its share soon is turned away."""

    def __init__(self, total):
        self.free, self.condition = total, threading.Condition()

    @contextmanager
    def reserve(self, size, wait=5):
        with self.condition:
            if not self.condition.wait_for(lambda: self.free >= size, wait):
                raise Refused(503, "busy")
            self.free -= size
        try:
            yield
        finally:
            with self.condition:
                self.free += size
                self.condition.notify_all()


class Handler(BaseHTTPRequestHandler):
    """Serve the runner and operator APIs using this server's dependencies."""

    timeout = REQUEST_TIMEOUT

    def do_GET(self):
        self.handle_request("GET")

    def do_POST(self):
        self.handle_request("POST")

    def do_DELETE(self):
        self.handle_request("DELETE")

    def handle_request(self, method):
        auth = self.headers.get("Authorization", "")
        bearer = auth[7:] if auth.startswith("Bearer ") else None
        path, _, query = self.path.partition("?")
        try:
            # Authenticate first: only a proven caller's submission or result may be large.
            if path.startswith("/exchange/operator/"):
                if (
                    not self.server.operator_digest
                    or not bearer
                    or not hmac.compare_digest(hashlib.sha256(bearer.encode()).digest(), self.server.operator_digest)
                ):
                    raise Refused(401, "operator token required")
                limit = MAX_BODY if path == "/exchange/operator/turns" else SMALL_BODY
                handle = lambda body: self.operator(method, path[len("/exchange/operator/") :], query, body)
            else:
                match = SESSION_PATH.fullmatch(path)
                if not match or method != "POST":
                    raise Refused(404, "not found")
                if not bearer:
                    raise Refused(401, "a bearer token is required")
                limit = SMALL_BODY
                if match["turn"]:
                    self.server.exchange.prove(match["session"], bearer)
                    limit = MAX_BODY
                handle = lambda body: self.runner(match["session"], match["action"], match["turn"], bearer, body)
            size = int(self.headers.get("Content-Length") or 0) if method == "POST" else 0
            if not 0 <= size <= limit:
                raise Refused(413, "request too large")
            with self.server.body_budget.reserve(size):
                reply = handle(self.body(size) if method == "POST" else {})
            self.reply(200, reply)
        except Refused as error:
            self.reply(error.status, {"error": str(error)})
        except ValueError as error:
            self.reply(400, {"error": str(error)})
        except Exception:
            self.reply(500, {"error": "internal error"})

    def runner(self, session, action, turn, bearer, body):
        if action == "key":
            return self.server.exchange.key(session, bearer)
        if action == "end":
            # A runner token is 64 hex characters; anything else is an OIDC token.
            runner = RUNNER_TOKEN.fullmatch(bearer)
            self.server.exchange.end(session, token=None if runner else bearer, runner=bearer if runner else None)
            return {}
        if action == "next":
            return self.server.exchange.next(session, bearer, body.get("capacity", 1), body.get("wait", 0))
        self.server.exchange.complete(session, bearer, turn, body.get("result"))
        return {}

    def operator(self, method, path, query, body):
        wait = float(dict(p.partition("=")[::2] for p in query.split("&") if p).get("wait", 0))
        if method == "POST" and path == "turns":
            return self.server.exchange.submit(
                body.get("turn"),
                body.get("expected"),
                body.get("models"),
                body.get("budget"),
                body.get("lease"),
                body.get("payload"),
                body.get("pin"),
                binding=body.get("binding"),
            )
        match = OPERATOR_PATH.fullmatch(path)
        if match and match["kind"] == "sessions" and method == "POST" and match["action"] == "end":
            self.server.exchange.end(match["id"])
            return {}
        if match and match["kind"] == "turns" and not match["action"]:
            if method == "GET":
                return self.server.exchange.poll(match["id"], wait)
            if method == "DELETE":
                self.server.exchange.cancel(match["id"])
                return {}
        raise Refused(404, "not found")

    def body(self, size):
        data = self.rfile.read(size) if size else b"{}"
        parsed = json.loads(data)
        if not isinstance(parsed, dict):
            raise Refused(400, "a JSON object is required")
        return parsed

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

    def __init__(self, address, handler):
        """Give each server its own request slots before accepting connections."""
        self.slots = threading.BoundedSemaphore(MAX_REQUESTS)
        super().__init__(address, handler)

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


def server(exchange, address, operator=None):
    """Build an HTTP server with its own exchange, operator credentials and body budget."""
    digest = hashlib.sha256(operator.encode()).digest() if operator else None
    budget = Budget(BODY_BUDGET)
    host, port = address.rsplit(":", 1)
    instance = Bounded((host, int(port)), Handler)
    instance.exchange = exchange
    instance.operator_digest = digest
    instance.body_budget = budget
    return instance


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
    sub.add_parser("list")
    sub.add_parser("turns")
    end = sub.add_parser("end")
    end.add_argument("session")
    sub.add_parser("reconcile")
    args = parser.parse_args()
    env = os.environ.get
    exchange = Exchange(
        env("EXCHANGE_DB", "/data/exchange.db"),
        Gateway(env("EXCHANGE_ADMIN", "http://127.0.0.1:15000"), env("EXCHANGE_INFERENCE", "http://127.0.0.1:4000")),
        Keys(),
        env("EXCHANGE_AUDIENCE", "concurro-gateway"),
    )
    if args.command == "serve":
        threading.Thread(target=reconciling, args=(exchange,), daemon=True).start()
        server(exchange, env("EXCHANGE_LISTEN", "0.0.0.0:8080"), env("EXCHANGE_OPERATOR_TOKEN") or None).serve_forever()
    elif args.command == "list":
        for row in exchange.db.execute(
            "SELECT id, kind, run_id, key IS NOT NULL, deadline, ended, refusal FROM sessions"
        ):
            print(json.dumps(dict(zip(("session", "kind", "run", "has_key", "deadline", "ended", "refusal"), row))))
    elif args.command == "turns":
        for row in exchange.db.execute("SELECT id, status, session, created, started, finished FROM turns"):
            print(json.dumps(dict(zip(("turn", "status", "session", "created", "started", "finished"), row))))
    elif args.command == "end":
        exchange.end(args.session)
    elif args.command == "reconcile":
        print(exchange.reconcile())


if __name__ == "__main__":
    main()
