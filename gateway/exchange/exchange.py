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
import runpy
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
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

INFERENCE = runpy.run_path(str(Path(__file__).with_name("inference.py")))
Inference, InferenceHandler = INFERENCE["Inference"], INFERENCE["Handler"]

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
    rf"/exchange/sessions/(?P<session>{UUID})/(?:(?P<action>engine|key|end|next)|turns/(?P<turn>{UUID}))"
)
OPERATOR_PATH = re.compile(rf"(?P<kind>sessions|turns)/(?P<id>{UUID})(?:/(?P<action>end))?")
# Mirrors engine.protocol.DIGEST_PATTERN; this image ships as a single file.
RUNNER_TOKEN = re.compile(r"[0-9a-f]{64}")
LABEL = re.compile(r"[A-Za-z0-9._:/#-]{1,200}")
# The launcher workflow is authenticated by its ref; its commit is free to differ from the engine's.
EXPECTED = ("repository_id", "repository_owner_id", "job_workflow_ref", "event_name", "ref")
OPTIONAL_EXPECTED = {"actor_id", "job_workflow_sha"}
SHA = re.compile(r"[0-9a-f]{40}")  # Mirrors engine.protocol.SHA_PATTERN.


class Refused(Exception):
    """A request the exchange will not serve; `status` is its HTTP status."""

    def __init__(self, status, reason, code=None, transition=None):
        """Carry a private protocol code and exact proven-unapplied transition when supplied."""
        super().__init__(reason)
        self.status, self.code, self.transition = status, code, transition


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

    def __init__(self, admin, inference, contributed=False):
        """Enable explicit contributed adapters only with the private inference listener wired."""
        self.admin, self.inference = admin.rstrip("/"), inference.rstrip("/")
        self.contributed_routes = tuple(sorted(set(ROUTES) & set(INFERENCE["UPSTREAMS"]))) if contributed else ()

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

    contributed_routes = ()  # Org-only adapters advertise no contributed routes.

    def create(self, key, name, models, budget, binding=None):
        """Create a native model/budget key whose funding metadata is assigned only by Exchange."""
        if binding and binding.get("funding") == "contributed" and binding.get("route") not in self.contributed_routes:
            raise Refused(503, "contributed route adapter is unavailable")
        value = {
            "key": key,
            "metadata": {
                "name": name,
                "concurro.session": name,
                **({"concurro.funding": "contributed"} if binding and binding.get("funding") == "contributed" else {}),
            },
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
    """Require known string claims, with actor_id and a launcher commit optional and empty strings permitted."""
    if (
        not isinstance(expected, dict)
        or set(expected) - set(EXPECTED) - OPTIONAL_EXPECTED
        or not set(EXPECTED) <= set(expected)
        or not all(isinstance(v, str) for v in expected.values())
    ):
        raise ValueError("unknown, missing or non-string expected claims")


def session_binding(value):
    """Validate the controller-issued task, role and immutable setup/code binding.

    `engine` is the commit the runner must run; sessions stored before it existed lack it and
    cannot be claimed.
    """
    fields = {"repository", "issue", "role", "generation", "config_sha", "upstream", "fork", "base", "phase"}
    versioned = isinstance(value, dict) and "version" in value
    if versioned:
        fields |= FUNDING_FIELDS
    repo = r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+"
    if (
        not isinstance(value, dict)
        or set(value) - {"engine"} != fields
        or type(value["issue"]) is not int
        or value["issue"] < 1
        or type(value["generation"]) is not int
        or value["generation"] < 1
        or any(
            not isinstance(value[k], str) or re.fullmatch(repo, value[k]) is None
            for k in ("repository", "upstream", "fork")
        )
        or any(
            not isinstance(value[k], str) or SHA.fullmatch(value[k]) is None
            for k in {"base", "config_sha", "engine"} & set(value)
        )
        or not isinstance(value["role"], str)
        or re.fullmatch(r"(intake|plan|plan_qc|develop|review):(claude|codex)", value["role"]) is None
        or not isinstance(value["phase"], str)
        or not re.fullmatch(r"[A-Za-z0-9:_-]{1,128}", value["phase"])
    ):
        raise ValueError("invalid runner task/role binding")
    if versioned:
        personal = ("sponsorship_provider", "sponsor", "epoch", "grant_id", "route", "credential_version")
        if (
            type(value["version"]) is not int
            or value["version"] != 2
            or value["funding"] not in ("org", "contributed")
            or not isinstance(value["credential"], str)
            or not value["credential"]
        ):
            raise ValueError("invalid funding binding")
        if value["funding"] == "org":
            if any(value[key] is not None for key in personal):
                raise ValueError("organization binding cannot name a personal payer")
        elif (
            not isinstance(value["route"], str)
            or ROUTES.get(value["route"], {}).get("sponsorship_provider") != value["sponsorship_provider"]
            or not isinstance(value["sponsor"], str)
            or not re.fullmatch(r"[1-9][0-9]*", value["sponsor"])
            or type(value["epoch"]) is not int
            or value["epoch"] < 1
            or not isinstance(value["grant_id"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["grant_id"])
            or not re.fullmatch(r"[0-9a-f]{64}", value["credential"])
            or type(value["credential_version"]) is not int
            or value["credential_version"] < 1
        ):
            raise ValueError("invalid contributed binding")
    return json.dumps(value, sort_keys=True)


def staffing(expected, binding, models, budget, lease):
    """A reusable job identity, excluding its unique per-session OIDC ref."""
    claims = {k: v for k, v in expected.items() if k != "ref"}
    return hashlib.sha256(
        json.dumps([claims, binding, models, budget, float(lease)], sort_keys=True).encode()
    ).hexdigest()


# The image copies this same leaf module; source checkouts resolve the repository copy.
CATALOG = Path(__file__).with_name("credential_routes.py")
if not CATALOG.is_file():
    CATALOG = Path(__file__).resolve().parents[2] / "engine" / "credential_routes.py"
ROUTES = runpy.run_path(str(CATALOG))["ROUTES"]
FUNDING_FIELDS = {
    "version",
    "funding",
    "sponsorship_provider",
    "sponsor",
    "epoch",
    "grant_id",
    "route",
    "credential",
    "credential_version",
}


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
MAX_WAIT_SECONDS = 50
FINISHED_RETENTION_SECONDS = 86400


class Vault:
    """Keep metadata auth separate from the isolated gateway principal used for events and resolution."""

    def __init__(self, metadata_url, metadata_token, events_url, events_token):
        """Use the Exchange's existing env-file secret transport with distinct private principals."""
        if not all(isinstance(v, str) and v for v in (metadata_url, metadata_token, events_url, events_token)):
            raise ValueError("incomplete Exchange vault configuration")
        if (
            any(not re.fullmatch(r"[0-9a-fA-F]{64}", v) for v in (metadata_token, events_token))
            or metadata_token.lower() == events_token.lower()
        ):
            raise ValueError("Exchange vault principals must be distinct 32-byte tokens")
        self.metadata_url, self.events_url = metadata_url.rstrip("/"), events_url.rstrip("/")
        self.metadata_token, self.events_token = metadata_token.lower(), events_token.lower()

    def get(self, url, path, token):
        """Return JSON over a private bearer channel without exposing remote bodies in failures."""
        request = urllib.request.Request(url + path, headers={"Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.load(response)
        except (OSError, ValueError, urllib.error.URLError):
            raise Refused(503, "vault metadata or events unavailable") from None

    def read(self, actor):
        """Read one owner's current metadata with the principal that cannot resolve secrets."""
        value = self.get(self.metadata_url, "/metadata/" + str(int(actor)), self.metadata_token)
        if not isinstance(value, dict) or not isinstance(value.get("credentials"), list):
            raise Refused(503, "vault metadata incomplete")
        fields = {"user_id", "record_id", "route", "version", "status", "models", "kind", "expiry_known", "expires_at"}
        if any(not isinstance(record, dict) or record.get("user_id") != str(actor) for record in value["credentials"]):
            raise Refused(503, "vault metadata identity mismatch")
        return [{key: item.get(key) for key in fields} for item in value["credentials"]]

    def resolve(self, binding):
        """Use the isolated gateway principal for one exact frozen record, never metadata auth."""
        request = urllib.request.Request(
            self.events_url + "/resolve",
            method="POST",
            data=json.dumps(
                {
                    "user_id": int(binding["sponsor"]),
                    "route": binding["route"],
                    "record_id": binding["credential"],
                    "version": binding["credential_version"],
                }
            ).encode(),
            headers={"Authorization": "Bearer " + self.events_token, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise Refused(
                error.code if error.code in (404, 410) else 503, "credential resolution unavailable"
            ) from None
        except (OSError, ValueError):
            raise Refused(503, "credential resolution unavailable") from None

    def events(self, after):
        """Read nonsecret lifecycle events on the isolated gateway-principal listener only."""
        return self.get(self.events_url, "/events?after=" + str(after), self.events_token)


class Exchange:
    """Sessions, their keys, their run bindings and the turns they serve, kept durably in SQLite.

    A `runner` session is served to one GitHub Actions run, proven by its OIDC token; a `host`
    session is a key minted directly for the operator (the Concurro service). Turns are queued by
    the operator for a target and served to a live runner session of that target.
    """

    refusal_type = Refused

    def __init__(self, database, gateway, keys, audience, vault=None):
        self.db = sqlite3.connect(database, check_same_thread=False, isolation_level=None)
        self.gateway, self.keys, self.audience = gateway, keys, audience
        self.vault = vault
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
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sponsorship (repository TEXT,issue INTEGER,provider TEXT,generation INTEGER,epoch INTEGER,slot TEXT,PRIMARY KEY(repository,issue,provider));
            CREATE TABLE IF NOT EXISTS sponsorship_transitions (id TEXT PRIMARY KEY,digest TEXT NOT NULL,response TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sponsorship_revocations (repository TEXT,issue INTEGER,provider TEXT,actor TEXT,through_epoch INTEGER,reason TEXT,PRIMARY KEY(repository,issue,provider,actor));
        """)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS funding_generations(repository TEXT,issue INTEGER,generation INTEGER,PRIMARY KEY(repository,issue));
            CREATE TABLE IF NOT EXISTS credential_revocations(record_id TEXT,version INTEGER,actor TEXT,route TEXT,sequence INTEGER,PRIMARY KEY(record_id,version));
            CREATE TABLE IF NOT EXISTS vault_cursor(id INTEGER PRIMARY KEY,sequence INTEGER NOT NULL);
            INSERT OR IGNORE INTO vault_cursor VALUES(1,0);
        """)
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

        self.inference = Inference(self)

    def capabilities(self):
        """Advertise only implemented adapter routes with a working retirement-event channel."""
        routes = sorted(set(getattr(self.gateway, "contributed_routes", ())) & ROUTES.keys()) if self.vault else []
        if routes:
            self.consume_events()
        return {"version": 1, "org": True, "contributed_routes": routes}

    def observations(self, repository, issue):
        """Read durable task evidence and unresolved native requests without waking or retrying work."""
        native_status = self.inference.import_native()
        with self.locked():
            pending = self.inference.pending(repository=repository, issue=issue)
        return {"events": self.inference.events(repository, issue), "pending": pending, "native_status": native_status}

    def advance_generation(self, repository, issue, generation):
        """Mirror task ownership monotonically inside the caller's transaction, never a second authority."""
        row = self.db.execute(
            "SELECT generation FROM funding_generations WHERE repository=? AND issue=?", (repository, issue)
        ).fetchone()
        if row and generation < row[0]:
            raise Refused(409, "task ownership generation is stale")
        self.db.execute(
            "INSERT INTO funding_generations VALUES(?,?,?) ON CONFLICT(repository,issue) DO UPDATE SET generation=MAX(generation,excluded.generation)",
            (repository, issue, generation),
        )

    def allowed(self, binding):
        """Check only recorded authority and retirement cutoffs; graceful takeover keeps old epochs valid."""
        if not binding or binding.get("funding") != "contributed":
            return True
        task = binding["repository"], binding["issue"]
        generation = self.db.execute(
            "SELECT generation FROM funding_generations WHERE repository=? AND issue=?", task
        ).fetchone()
        if generation and binding["generation"] < generation[0]:
            return False
        revoked = self.db.execute(
            "SELECT through_epoch FROM sponsorship_revocations WHERE repository=? AND issue=? AND provider=? AND actor=?",
            (*task, binding["sponsorship_provider"], binding["sponsor"]),
        ).fetchone()
        if revoked and binding["epoch"] <= revoked[0]:
            return False
        return not self.db.execute(
            "SELECT 1 FROM credential_revocations WHERE record_id=? AND version=? AND actor=? AND route=?",
            (binding["credential"], binding["credential_version"], binding["sponsor"], binding["route"]),
        ).fetchone()

    def retire_revoked(self, now):
        """Retire affected contributed work after durable fences; failed key cleanup reconciles normally."""
        for identifier, encoded in self.db.execute(
            "SELECT id,binding FROM sessions WHERE ended IS NULL AND binding IS NOT NULL"
        ).fetchall():
            if not self.allowed(json.loads(encoded)):
                try:
                    self.retire(identifier, self.row(identifier), now)
                except Refused:
                    pass
        for identifier, encoded in self.db.execute(
            "SELECT id,binding FROM turns WHERE status='queued' AND binding IS NOT NULL"
        ).fetchall():
            if not self.allowed(json.loads(encoded)):
                self._lose_turn(identifier, now)

    def consume_events(self):
        """Commit cursor and exact-version retirement together; never schedule tasks or new model work."""
        if not self.vault:
            return
        while True:
            with self.locked():
                cursor = self.db.execute("SELECT sequence FROM vault_cursor WHERE id=1").fetchone()[0]
            page = self.vault.events(cursor)
            if (
                not isinstance(page, dict)
                or not isinstance(page.get("events"), list)
                or type(page.get("next")) is not int
                or type(page.get("more")) is not bool
            ):
                raise Refused(503, "vault event page incomplete")
            sequence = cursor
            for event in page["events"]:
                if (
                    not isinstance(event, dict)
                    or type(event.get("sequence")) is not int
                    or event["sequence"] <= sequence
                    or not isinstance(event.get("record_id"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", event["record_id"])
                    or type(event.get("user_id")) is not int
                    or event["user_id"] < 1
                    or type(event.get("version")) is not int
                    or event["version"] < 1
                    or event.get("route") not in ROUTES
                    or event.get("type") not in ("validated", "retired", "revoked", "unusable", "rewrapped", "snapshot")
                    or event.get("status") not in ("valid", "invalid", "revoked", "retired")
                ):
                    raise Refused(503, "vault event identity incomplete")
                sequence = event["sequence"]
            if page["next"] != sequence or page["more"] and sequence == cursor:
                raise Refused(503, "vault event cursor incomplete")
            with self.locked():
                with self._transaction():
                    if self.db.execute("SELECT sequence FROM vault_cursor WHERE id=1").fetchone()[0] != cursor:
                        continue
                    for event in page["events"]:
                        if (
                            event["type"] in ("retired", "revoked", "unusable")
                            or event["type"] == "snapshot"
                            and event["status"] != "valid"
                        ):
                            self.db.execute(
                                "INSERT OR IGNORE INTO credential_revocations VALUES(?,?,?,?,?)",
                                (
                                    event["record_id"],
                                    event["version"],
                                    str(event["user_id"]),
                                    event["route"],
                                    event["sequence"],
                                ),
                            )
                    self.db.execute("UPDATE vault_cursor SET sequence=? WHERE id=1", (sequence,))
                self.retire_revoked(_now(None))
            if not page["more"]:
                return

    def check_session_funding(self, session, record, now):
        """Refuse revoked recorded work without selecting another payer or touching org sessions."""
        if record["binding"] and not self.allowed(json.loads(record["binding"])):
            try:
                self.retire(session, record, now)
            except Refused:
                pass
            raise Refused(410, "session funding authority ended")

    def metadata_for(self, binding, models):
        """Check a new admission's exact owner/route/version and model evidence outside the lifecycle lock."""
        if binding.get("funding") != "contributed":
            return
        if binding["route"] not in self.capabilities()["contributed_routes"]:
            raise Refused(503, "contributed route adapter is unavailable")
        rows = self.vault.read(binding["sponsor"])
        record = next(
            (
                row
                for row in rows
                if row.get("route") == binding["route"]
                and row.get("record_id") == binding["credential"]
                and row.get("version") == binding["credential_version"]
                and row.get("user_id") == binding["sponsor"]
            ),
            None,
        )
        if record is None or record.get("status") != "valid" or record.get("kind") != ROUTES[binding["route"]]["kind"]:
            raise Refused(410, "credential version is unavailable")
        if record.get("expiry_known"):
            try:
                expired = datetime.fromisoformat(record["expires_at"].replace("Z", "+00:00")).timestamp() <= time.time()
            except (ValueError, TypeError, KeyError, AttributeError):
                raise Refused(503, "credential expiry evidence incomplete") from None
            if expired:
                raise Refused(410, "credential version expired")
        if not isinstance(record.get("models"), list) or any(model not in record["models"] for model in models):
            raise Refused(409, "credential model coverage is unavailable")

    def current_grant(self, binding, admission):
        """Enforce the selected fence at insertion, initializing only from the operator's frozen checkpoint grant."""
        if binding.get("funding") != "contributed":
            return
        key = binding["repository"], binding["issue"], binding["sponsorship_provider"]
        row = self.db.execute(
            "SELECT generation,epoch,slot FROM sponsorship WHERE repository=? AND issue=? AND provider=?", key
        ).fetchone()
        slot = json.loads(row[2]) if row else admission.get("grant")
        if (
            not isinstance(slot, dict)
            or slot.get("status") != "active"
            or slot.get("sponsor_id") != binding["sponsor"]
            or slot.get("epoch") != binding["epoch"]
            or slot.get("grant_id") != binding["grant_id"]
            or slot.get("provenance") != "explicit_comment"
            and not (
                slot.get("provenance") in ("task_start", "label") and slot.get("source_funding_mode") == "contributed"
            )
        ):
            raise Refused(409, "provider grant differs from current sponsorship fence")
        if row and binding["generation"] < row[0] or not self.allowed(binding):
            raise Refused(409, "provider funding authority ended")
        self.db.execute(
            "INSERT INTO sponsorship VALUES(?,?,?,?,?,?) ON CONFLICT(repository,issue,provider) DO UPDATE SET generation=MAX(generation,excluded.generation)",
            (*key, binding["generation"], binding["epoch"], json.dumps(slot, sort_keys=True)),
        )

    @contextmanager
    def minting(self):
        """Serialize key creation separately so lifecycle revocations can proceed during activation I/O."""
        with open(self.lockfile + ".keys", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def sponsorship(self, request):
        """Apply replay-safe provider fences/cutoffs atomically; org-funded sessions are untouched."""
        requests = request.get("transitions", [request])
        if not isinstance(requests, list) or not requests or len(requests) > 2:
            raise ValueError("invalid sponsorship transitions")
        fields = {
            "repository",
            "issue",
            "provider",
            "generation",
            "expected_epoch",
            "slot",
            "reason",
            "actor",
            "cutoff",
            "transition",
        }
        for value in requests:
            if not isinstance(value, dict) or set(value) != fields:
                raise ValueError("invalid sponsorship transition")
            slot = value["slot"]
            if (
                not isinstance(value["repository"], str)
                or not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", value["repository"])
                or type(value["issue"]) is not int
                or value["issue"] < 1
                or value["provider"] not in ("openai", "anthropic")
                or type(value["generation"]) is not int
                or value["generation"] < 1
                or type(value["expected_epoch"]) is not int
                or value["expected_epoch"] < -1
                or not isinstance(slot, dict)
                or type(slot.get("epoch")) is not int
                or slot["epoch"] < 0
                or slot.get("status") not in ("active", "unassigned", "withdrawn")
                or slot.get("sponsor_id") is not None
                and (not isinstance(slot["sponsor_id"], str) or not slot["sponsor_id"].isdecimal())
                or slot.get("status") == "active"
                and (not slot.get("sponsor_id") or not re.fullmatch(r"[0-9a-f]{64}", slot.get("grant_id", "")))
                or value["reason"] not in ("initialize", "sponsor", "withdraw", "permission_loss", "cutover")
                or not isinstance(value["transition"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", value["transition"])
                or value["cutoff"] is not None
                and (
                    type(value["cutoff"]) is not int
                    or value["cutoff"] < 0
                    or not isinstance(value["actor"], str)
                    or not value["actor"].isdecimal()
                )
            ):
                raise ValueError("invalid sponsorship transition")
        with self.locked():
            replies = []
            with self._transaction():
                for value in requests:
                    encoded = json.dumps(value, sort_keys=True)
                    digest = hashlib.sha256(encoded.encode()).hexdigest()
                    old = self.db.execute(
                        "SELECT digest,response FROM sponsorship_transitions WHERE id=?", (value["transition"],)
                    ).fetchone()
                    if old:
                        if old[0] != digest:
                            raise Refused(409, "sponsorship transition identity conflict")
                        replies.append(json.loads(old[1]))
                        continue
                    try:
                        self.advance_generation(value["repository"], value["issue"], value["generation"])
                    except Refused as error:
                        raise Refused(
                            error.status, str(error), "stale_sponsorship_generation", value["transition"]
                        ) from None
                    key = value["repository"], value["issue"], value["provider"]
                    row = self.db.execute(
                        "SELECT generation,epoch,slot FROM sponsorship WHERE repository=? AND issue=? AND provider=?",
                        key,
                    ).fetchone()
                    expected = row[1] if row else -1
                    slot = value["slot"]
                    if expected != value["expected_epoch"] or row and value["generation"] < row[0]:
                        raise Refused(409, "sponsorship fence conflict")
                    if expected == -1:
                        valid = value["reason"] == "initialize" and slot["epoch"] == (
                            1 if slot.get("sponsor_id") else 0
                        )
                    else:
                        same = json.loads(row[2]) == slot
                        valid = (
                            slot["epoch"] == expected + 1 or same and value["reason"] in ("withdraw", "permission_loss")
                        )
                    if not valid or value["cutoff"] is not None and value["cutoff"] > max(expected, 0):
                        raise Refused(409, "sponsorship epoch conflict")
                    self.db.execute(
                        "INSERT OR REPLACE INTO sponsorship VALUES (?,?,?,?,?,?)",
                        (*key, value["generation"], slot["epoch"], json.dumps(slot, sort_keys=True)),
                    )
                    if value["cutoff"] is not None:
                        self.db.execute(
                            "INSERT INTO sponsorship_revocations VALUES (?,?,?,?,?,?) ON CONFLICT(repository,issue,provider,actor) DO UPDATE SET through_epoch=MAX(through_epoch,excluded.through_epoch),reason=excluded.reason",
                            (*key, value["actor"], value["cutoff"], value["reason"]),
                        )
                    reply = {"transition": value["transition"], "epoch": slot["epoch"]}
                    self.db.execute(
                        "INSERT INTO sponsorship_transitions VALUES (?,?,?)",
                        (value["transition"], digest, json.dumps(reply)),
                    )
                    replies.append(reply)
            self.retire_revoked(_now(None))
            return {"transitions": replies}

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
        if isinstance(binding, dict) and binding.get("funding") == "contributed":
            raise Refused(409, "contributed sessions require an admitted turn")
        with self.locked():
            return self.create(expected, models, budget, lease, binding=binding)

    def create(self, expected, models, budget, lease, kind="runner", binding=None):
        session, now = str(uuid.uuid4()), time.time()
        bound = session_binding(binding) if binding is not None else None
        if kind != "runner" and binding and binding.get("funding") == "contributed":
            raise ValueError("host sessions cannot use contributed credentials")
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
        self.check_session_funding(session, record, now)
        if record["run_id"] is None:
            self.db.execute("UPDATE sessions SET run_id = ? WHERE id = ? AND run_id IS NULL", (run, session))
        return record

    def bound_engine(self, session, record):
        """The engine commit the session's runner must run."""
        if not record["binding"]:
            raise Refused(403, "session has no task/role binding")
        engine = json.loads(record["binding"]).get("engine")
        if not engine:
            self.refuse(session, 403, "session has no engine binding")
        return engine

    def engine(self, session, token, now=None):
        """Where an authenticated launcher finds the engine to run: its own repository, at the session's commit."""
        now = _now(now)
        with self.locked():
            record = self.authorize(session, token, now)
            engine = self.bound_engine(session, record)
            # The run is bound from here on, so this is its first heartbeat: the claim follows the download.
            self.db.execute("UPDATE sessions SET seen = ? WHERE id = ?", (now, session))
            workflow = json.loads(record["expected"])["job_workflow_ref"]
            return {"repository": "/".join(workflow.split("/")[:2]), "engine": engine}

    def key(self, session, token, engine=None, now=None):
        """Mint once outside the lifecycle lock; recheck retirement after activation without resetting budget."""
        with self.locked():
            contributed = json.loads(self.row(session)["binding"] or "{}").get("funding") == "contributed"
        if contributed:
            self.consume_events()
        clock = now
        now = _now(clock)
        with self.minting():
            with self.locked():
                record = self.authorize(session, token, now)
                if self.bound_engine(session, record) != engine:
                    self.refuse(session, 403, "runner engine differs from the session's")
                bound = json.loads(record["binding"])
                contributed = bound.get("funding") == "contributed"
                if contributed and bound["route"] not in getattr(self.gateway, "contributed_routes", ()):
                    raise Refused(503, "contributed route adapter is unavailable")
                key, runner = record["key"], record["token"] or secrets.token_hex(32)
                self.db.execute("UPDATE sessions SET token = ?, seen = ? WHERE id = ?", (runner, now, session))
                reply = {
                    "key": None,
                    "token": runner,
                    "models": json.loads(record["models"]),
                    "deadline": record["deadline"],
                    "binding": bound,
                }
                if key and record["key_id"]:
                    return reply | {"key": key}
                if not key:
                    key = "sk-agw-" + secrets.token_hex(24)
                    self.db.execute("UPDATE sessions SET key = ? WHERE id = ?", (key, session))
                if not contributed:
                    key_id = self.activate_key(session, record, key, bound)
                    self.db.execute("UPDATE sessions SET key_id = ? WHERE id = ?", (key_id, session))
                    return reply | {"key": key}
            try:
                key_id = self.activate_key(session, record, key, bound)
            except BaseException:
                with self.locked():
                    current = self.row(session)
                    if current["ended"] or not self.allowed(bound):
                        self.db.execute("UPDATE sessions SET key=? WHERE id=?", (key, session))
                        try:
                            self.retire(session, self.row(session), now)
                        except Refused:
                            pass
                raise
            with self.locked():
                # Retirement during activation may have cleared the key before creation completed.
                if self.row(session)["key"] != key:
                    self.db.execute("UPDATE sessions SET key = ? WHERE id = ?", (key, session))
                self.db.execute("UPDATE sessions SET key_id = ? WHERE id = ?", (key_id, session))
                current = self.row(session)
                if current["ended"] or current["deadline"] < _now(clock) or not self.allowed(bound):
                    try:
                        self.retire(session, current, _now(clock))
                    except Refused:
                        pass
                    raise Refused(410, "session authority ended during key activation")
                return reply | {"key": key}

    def activate_key(self, session, record, key, binding):
        """Adopt a partially activated key with its existing budget; never create a replacement on retry."""
        if binding.get("funding") == "contributed":
            self.metadata_for(binding, json.loads(record["models"]))
        key_id = self.gateway.find(key)
        if key_id:
            self.gateway.activated(key)
            return key_id
        arguments = {"binding": binding} if binding.get("funding") == "contributed" else {}
        return self.gateway.create(key, session, json.loads(record["models"]), record["budget"], **arguments)

    def runner(self, session, token, now):
        """The live runner session that `token` proves; every runner call is also its heartbeat. The lock is held."""
        record = self.row(session)
        if record["kind"] != "runner" or not record["token"] or not hmac.compare_digest(record["token"], token):
            raise Refused(401, "invalid runner token")
        if record["ended"] or record["deadline"] < now:
            raise Refused(410, "session has ended")
        self.check_session_funding(session, record, now)
        self.db.execute("UPDATE sessions SET seen = ? WHERE id = ?", (now, session))
        return record

    def prove(self, session, token):
        """Check a runner token before its request body is read."""
        with self.locked():
            self.runner(session, token, time.time())

    def next(self, session, token, capacity, wait, now=None):
        """Claim one turn when ready; legacy capacity zero pauses claims, positive values mean ready."""
        if (
            type(capacity) is not int
            or capacity < 0
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
                        "ORDER BY created LIMIT 1",
                        (record["staffing"], session),
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
        self.inference.settle_ended_locked()

    def _lose_turn(self, turn, now):
        """Lose one turn while the caller holds the lifecycle lock."""
        self.db.execute("UPDATE turns SET status = 'lost', finished = ? WHERE id = ?", (now, turn))

    def retire(self, session, record, now):
        """End a session, then delete its key; a failed deletion is retried by reconciliation."""
        self.inference.stop(session=session)
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
        failure = self.inference.failure(turn)
        if failure and status == "done":
            result = self.db.execute("SELECT result FROM turns WHERE id=?", (turn,)).fetchone()[0]
            if result and json.loads(result).get("outcome") == "ok":
                failure = None
        ended_binding = json.loads(runner["binding"] or "{}") if runner else {}
        funding_ended = ended_binding.get("funding") == "contributed" and not self.allowed(ended_binding)
        return {
            **({"funding_ended": ended_binding} if funding_ended else {}),
            **({"provider_failure": failure} if failure else {}),
            "turn": turn,
            "status": status,
            "created": created,
            "started": started,
            "finished": finished,
            **({"runner": {"session": session, "run_id": runner["run_id"]}} if runner else {}),
        }

    def admitted(self, turn, digest, now):
        """Return the exact existing-turn result before metadata, capabilities or current grant checks."""
        found = self.db.execute("SELECT digest,status,staff,result FROM turns WHERE id=?", (turn,)).fetchone()
        if found is None:
            return None
        if found[0] is None:
            return {**self.observation(turn), "dispatch": None}
        if found[0] != digest:
            raise Refused(409, "the turn ID is in use")
        staff = found[2] and self.row(found[2])
        pending = staff and staff["run_id"] is None and self.live(staff, now)
        reply = {**self.observation(turn), "dispatch": found[2] if pending else None}
        if (
            found[1] == "done"
            and json.loads(found[3]).get("outcome") != "ok"
            and self.inference.pending(turn=turn)
            and not self.inference.failure(turn)
        ):
            raise Refused(503, "native request observations pending")
        return {**reply, "result": json.loads(found[3])} if found[1] == "done" else reply

    def validate_admission(self, turn, binding, request, models):
        """Match the versioned controller receipt to the exact turn, payer and selected model."""
        if "version" not in binding:
            if "admission" in request or "attempt" in request:
                raise ValueError("admission requires a versioned binding")
            return
        value = request.get("admission")
        fields = {
            "version",
            "funding",
            "credential",
            "sponsorship_provider",
            "sponsor",
            "epoch",
            "grant_id",
            "route",
            "grant",
            "call",
            "owner_generation",
            "transcript_scope",
        }
        if (
            not isinstance(value, dict)
            or set(value) != fields
            or type(value["version"]) is not int
            or value["version"] != 1
            or type(value["call"]) is not int
            or value["call"] < 1
            or type(value["owner_generation"]) is not int
            or value["owner_generation"] != binding["generation"]
        ):
            raise ValueError("invalid frozen admission")
        attempt = request.get("attempt")
        try:
            if not isinstance(attempt, str) or str(uuid.UUID(attempt)) != turn:
                raise ValueError()
        except ValueError:
            raise ValueError("turn differs from frozen attempt") from None
        if any(
            value[k] != binding[k] for k in ("funding", "sponsorship_provider", "sponsor", "epoch", "grant_id", "route")
        ):
            raise ValueError("admission differs from session funding")
        credential = value["credential"]
        legacy = {"id", "provider", "kind", "adapter", "account"}
        fields = legacy | ({"route", "record_id", "version"} if binding["funding"] == "contributed" else set())
        if (
            not isinstance(credential, dict)
            or set(credential) != fields
            or credential["kind"] != binding["funding"]
            or credential["provider"] != request.get("provider")
            or not isinstance(credential["adapter"], str)
            or not credential["adapter"]
        ):
            raise ValueError("invalid admission credential")
        if value["transcript_scope"] != (request.get("session") or {}).get("scope"):
            raise ValueError("admission differs from retained transcript scope")
        if binding["funding"] == "org":
            if credential["id"] != binding["credential"] or value["grant"] is not None:
                raise ValueError("organization admission differs from credential")
        else:
            identity = [
                "contributed",
                binding["sponsor"],
                binding["route"],
                binding["credential"],
                binding["credential_version"],
            ]
            expected = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            if (
                credential["id"] != expected
                or credential["account"] != binding["sponsor"]
                or credential["route"] != binding["route"]
                or credential["record_id"] != binding["credential"]
                or credential["version"] != binding["credential_version"]
            ):
                raise ValueError("admission differs from credential version")
            if (
                not isinstance(value["grant"], dict)
                or any(value["grant"].get(k) != binding[k] for k in ("epoch", "grant_id"))
                or value["grant"].get("sponsor_id") != binding["sponsor"]
            ):
                raise ValueError("admission differs from provider grant")
        if models != [request.get("model")]:
            raise ValueError("admission models differ from selected route")

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
        self.validate_admission(turn, binding, request, models)
        self.inference.import_native()
        group = staffing(expected, binding, models, budget, lease)
        now = _now(now)
        digest = hashlib.sha256(
            json.dumps([expected, models, budget, lease, payload, pin, binding], sort_keys=True).encode()
        ).hexdigest()
        with self.locked():
            prior = self.admitted(turn, digest, now)
            if prior is not None:
                return prior
        self.metadata_for(binding, models)
        with self.locked():
            prior = self.admitted(turn, digest, now)
            if prior is not None:
                return prior
            if pin is not None:
                record = self.row(pin)
                if record["kind"] != "runner" or record["staffing"] != group:
                    raise Refused(409, "the pinned session serves another target")
            with self._transaction():
                if binding.get("version") == 2:
                    self.advance_generation(binding["repository"], binding["issue"], binding["generation"])
                    self.current_grant(binding, request.get("admission") or {})
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
        if not self.allowed(json.loads(bound)):
            self._lose_turn(turn, now)
            return None
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
            self.inference.import_native()
            with self.locked():
                current = _now(now)
                found = self.db.execute(
                    "SELECT status, session, result, expected, created, pin, staffing FROM turns WHERE id = ?", (turn,)
                ).fetchone()
                if not found:
                    raise Refused(404, "unknown turn")
                status, session, result, expected, created, pin, group = found
                failure = self.inference.failure(turn)
                if failure and not (status == "done" and json.loads(result).get("outcome") == "ok"):
                    return {**self.observation(turn), "provider_failure": failure}
                if group is None and status in ("queued", "running"):
                    self._lose_turn(turn, current)
                    status = "lost"
                if status == "queued" and pin and not self.live(self.row(pin), current):
                    # Only its session could serve it.
                    self._lose_turn(turn, current)
                    status = "lost"
                if status == "done":
                    if json.loads(result).get("outcome") != "ok" and self.inference.pending(turn=turn):
                        raise Refused(503, "native request observations pending")
                    return {**self.observation(turn), "result": json.loads(result)}
                if status == "running":
                    record = self.row(session)
                    if (
                        not self.live(record, current)
                        or record["binding"]
                        and not self.allowed(json.loads(record["binding"]))
                    ):
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
            self.inference.stop(turn=turn)
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
        event_error = None
        try:
            self.consume_events()
        except Refused as error:
            event_error = error
        with self.locked():
            self.retire_revoked(now)
            for (session,) in self.db.execute(
                "SELECT id FROM sessions WHERE kind = 'runner' AND ended IS NULL"
            ).fetchall():
                record = self.row(session)
                if not self.live(record, now):
                    self.inference.stop(session=session)
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
                    self.inference.stop(session=session)
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
            if event_error:
                raise event_error
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
            details = {"code": error.code, "transition": error.transition} if error.code else {}
            self.reply(error.status, {"error": str(error), **details})
        except ValueError as error:
            self.reply(400, {"error": str(error)})
        except Exception:
            self.reply(500, {"error": "internal error"})

    def runner(self, session, action, turn, bearer, body):
        if action == "engine":
            return self.server.exchange.engine(session, bearer)
        if action == "key":
            return self.server.exchange.key(session, bearer, body.get("engine"))
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
        # Deliberately reject empty/percent-encoded wait values; URL-decoding would widen admission.
        wait = float(dict(p.partition("=")[::2] for p in query.split("&") if p).get("wait", 0))
        if method == "POST" and path == "observations":
            return self.server.exchange.observations(body.get("repository"), body.get("issue"))
        if method == "GET" and path == "capabilities":
            return self.server.exchange.capabilities()
        if method == "POST" and path == "sponsorship":
            return self.server.exchange.sponsorship(body)
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
    vault_values = [
        env("EXCHANGE_VAULT_" + name) for name in ("METADATA_URL", "METADATA_TOKEN", "EVENTS_URL", "EVENTS_TOKEN")
    ]
    vault = Vault(*vault_values) if any(vault_values) else None
    exchange = Exchange(
        env("EXCHANGE_DB", "/data/exchange.db"),
        Gateway(
            env("EXCHANGE_ADMIN", "http://127.0.0.1:15000"),
            env("EXCHANGE_INFERENCE", "http://127.0.0.1:4000"),
            contributed=bool(env("EXCHANGE_PRIVATE_LISTEN")),
        ),
        Keys(),
        env("EXCHANGE_AUDIENCE", "concurro-gateway"),
        vault=vault,
    )
    if args.command == "serve":
        if env("EXCHANGE_PRIVATE_LISTEN"):
            host, port = env("EXCHANGE_PRIVATE_LISTEN").rsplit(":", 1)
            if host not in ("127.0.0.1", "::1"):
                raise ValueError("inference hooks require a loopback listener")
            exchange.inference.native_database = env("EXCHANGE_NATIVE_LOG")
            private = Bounded((host, int(port)), InferenceHandler)
            private.inference, private.max_body = exchange.inference, MAX_BODY
            private.body_budget = Budget(BODY_BUDGET)
            threading.Thread(target=private.serve_forever, daemon=True).start()
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
