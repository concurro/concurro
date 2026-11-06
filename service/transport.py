"""HTTPS to the GitHub API with one repository's token; reads raise, writes report for settlement."""

from dataclasses import dataclass, field
import http.client
import json
import re
import ssl
from urllib.parse import urlsplit

from github import NotFound, Transient

API = "api.github.com"
CODELOAD = "codeload.github.com"
TIMEOUT = 45
ARCHIVE_TIMEOUT = 120
MB = 1_000_000
JSON_LIMIT = 4 * MB
ARCHIVE_LIMIT = 100 * MB
# Endpoint-specific bounds; blobs cover GitHub's 100 MB endpoint plus base64/JSON overhead.
# https://docs.github.com/en/rest/git/blobs#get-a-blob
LIMITS = ((re.compile(r"git/blobs/[0-9a-f]{40}"), 144 * MB),
          (re.compile(r"issues/[1-9][0-9]*/artifacts(?:/[1-9][0-9]*)?(?:\?.*)?"), 16 * MB),
          (re.compile(r"git/trees/[^/?#]+\?recursive=1"), 16 * MB))
# Headers kept for settlement and redirects; nothing else leaves the transport.
KEPT = ("retry-after", "x-ratelimit-remaining", "x-ratelimit-reset", "location")
# A secondary rate limit without a requested delay: GitHub asks for at least a minute.
SECONDARY_SECONDS = 60
API_VERSION = "2022-11-28"
USER_AGENT = "concurro-service"
# A user or App bot login, as `users/<login>` (the bot suffix percent-encoded).
PROFILE = re.compile(r"users/[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:%5Bbot%5D)?")


@dataclass(frozen=True)
class Response:
    """What came back for one request; `body` is None when it could not be read completely."""

    sent: bool
    status: int | None = None
    headers: dict = field(default_factory=dict)
    body: bytes | None = None
    body_error: str | None = None

    def json(self):
        """The parsed body (None when empty); raises ValueError when unreadable or malformed."""
        if self.body is None:
            raise ValueError(self.body_error or "no body")
        return json.loads(self.body) if self.body.strip() else None


def connect(host, timeout):
    """A verified HTTPS connection; never a proxy."""
    return http.client.HTTPSConnection(host, timeout=timeout, context=ssl.create_default_context())


def exchange(connection, host, method, target, headers, body, limit, timeout):
    """One request: `sent=False` only when it failed before any request byte was written.

    Failures after the request starts are raised: the caller decides what an ambiguous
    outcome means.
    """
    conn = connection(host, timeout)
    try:
        try:
            conn.connect()
        except OSError:
            return Response(sent=False)
        conn.request(method, target, body=body, headers=headers)
        reply = conn.getresponse()
        kept = {name: value for name, value in ((k.lower(), v) for k, v in reply.getheaders()) if name in KEPT}
        expected = reply.getheader("content-length")
        chunks, size, error = [], 0, None
        try:
            while size <= limit:
                chunk = reply.read(min(65536, limit + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            if size > limit:
                error = "oversized"
            elif expected is not None and expected.isdecimal() and size != int(expected):
                error = "incomplete"
        except (OSError, http.client.HTTPException):
            error = "incomplete"
        return Response(True, reply.status, kept, None if error else b"".join(chunks), error)
    finally:
        conn.close()


def delay(headers, now):
    """The wait GitHub requested: `Retry-After`, else the reset of an exhausted quota, else a minute."""
    retry = headers.get("retry-after", "").strip()
    if retry:
        return max(1, int(retry)) if retry.isdecimal() else SECONDARY_SECONDS
    if headers.get("x-ratelimit-remaining", "").strip() == "0":
        reset = headers.get("x-ratelimit-reset", "").strip()
        return max(1, int(reset) - int(now)) if reset.isdecimal() else SECONDARY_SECONDS
    return SECONDARY_SECONDS


def limited(message):
    """Whether an error message names a rate limit."""
    return isinstance(message, str) and "rate limit" in message.lower()


def throttle(response, now):
    """The delay GitHub requested when a 403 or 429 shows throttling, else None.

    Evidence is `Retry-After`, an exhausted quota, or a message naming a rate limit;
    ordinary quota headers alone are not. The delay is never shortened.
    """
    if response.status not in (403, 429):
        return None
    headers = response.headers
    if headers.get("retry-after", "").strip() or headers.get("x-ratelimit-remaining", "").strip() == "0":
        return delay(headers, now)
    try:
        message = response.json().get("message", "")
    except (ValueError, AttributeError):
        return None
    return SECONDARY_SECONDS if limited(message) else None


def graphql_throttle(payload, headers, now):
    """The delay for a GraphQL HTTP 200 whose errors report rate limiting, else None."""
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if not isinstance(errors, list) or not any(
            isinstance(e, dict) and (e.get("type") == "RATE_LIMITED" or limited(e.get("message"))) for e in errors):
        return None
    return delay(headers, now)


class Transport:
    """Requests for one repository with one kind of token; any other repository is refused locally."""

    def __init__(self, repository, credential, clock, connection=connect):
        """`credential` has `get()` (raising when no token can be had) and `drop(token)` (after a 401)."""
        self.repository, self.credential, self.clock, self.connection = repository, credential, clock, connection

    def check(self, path, read=False):
        """Only this repository's endpoints and GraphQL; reads may also fetch one account's public profile."""
        if read and (PROFILE.fullmatch(path) or path == f"repos/{self.repository}"):
            return
        if path != "graphql" and not path.startswith(f"repos/{self.repository}/"):
            raise ValueError("path outside the transport's repository")

    def limit(self, path):
        """The response bound for one endpoint."""
        relative = path.removeprefix(f"repos/{self.repository}/")
        return next((size for pattern, size in LIMITS if pattern.fullmatch(relative)), JSON_LIMIT)

    def call(self, method, path, data, host=API, limit=None, timeout=TIMEOUT, authorize=True):
        """Authorize and send one request; a missing token is a request not sent."""
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION,
                   "User-Agent": USER_AGENT}
        token = None
        if authorize:
            try:
                token = self.credential.get()
            except Exception:
                return Response(sent=False)
            headers["Authorization"] = "Bearer " + token
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        target = path if path.startswith("/") else "/" + path
        response = exchange(self.connection, host, method, target, headers, body,
                            limit or self.limit(path), timeout)
        if token and response.status == 401:
            self.credential.drop(token)
        return response

    def failure(self, response):
        """The transient error for a response worth retrying, else None.

        An incomplete 2xx body is transient; an oversized one is not.
        """
        if not response.sent:
            return Transient("unavailable")
        status = response.status
        if 200 <= status < 300:
            return Transient("unavailable") if response.body_error == "incomplete" else None
        delay = throttle(response, self.clock())
        if delay is not None:
            return Transient("throttled", delay)
        if status in (401, 408, 429) or status >= 500:
            return Transient("unavailable")
        return None

    def request(self, method, path, data=None):
        """Read: parsed JSON, or `NotFound`, `Transient`, or a `RuntimeError` naming only the status."""
        self.check(path, read=method == "GET")
        try:
            response = self.call(method, path, data)
        except (OSError, http.client.HTTPException):
            raise Transient("unavailable") from None
        if response.status == 404:
            raise NotFound("GitHub request failed: " + method + " " + path)
        if response.sent and 200 <= response.status < 300 and response.body is not None:
            try:
                payload = response.json()
            except ValueError:
                raise Transient("unavailable") from None
            # GraphQL reports rate limiting with HTTP 200 and an error, not a status.
            wait = graphql_throttle(payload, response.headers, self.clock()) if path == "graphql" else None
            if wait is not None:
                raise Transient("throttled", wait)
            return payload
        error = self.failure(response)
        if error:
            raise error
        reason = "response exceeds limit" if response.body_error == "oversized" else f"HTTP {response.status}"
        raise RuntimeError(f"GitHub request failed: {method} {path} ({reason})")

    def send(self, method, path, body=None):
        """Write: the response for settlement; raises only when the outcome is ambiguous."""
        self.check(path)
        return self.call(method, path, body)

    def archive(self, path):
        """Follow one redirect to codeload without the API token; refuse anything else."""
        if not re.fullmatch(rf"repos/{re.escape(self.repository)}/tarball/[0-9a-f]{{40}}", path):
            raise ValueError("not a pinned source archive")
        try:
            response = self.call("GET", path, None)
            location = urlsplit(response.headers.get("location", ""))
            if response.status != 302 or location.scheme != "https" or location.hostname != CODELOAD \
                    or location.port is not None or location.username or location.password:
                raise (None if response.sent and response.status < 400 else self.failure(response)) \
                    or RuntimeError("source archive unavailable or too large")
            target = location.path + ("?" + location.query if location.query else "")
            source = self.call("GET", target, None, host=CODELOAD, limit=ARCHIVE_LIMIT,
                               timeout=ARCHIVE_TIMEOUT, authorize=False)
        except (OSError, http.client.HTTPException):
            raise Transient("unavailable") from None
        if source.sent and source.status == 200 and source.body is not None:
            return source.body
        raise (None if source.sent and 300 <= source.status < 400 else self.failure(source)) \
            or RuntimeError("source archive unavailable or too large")
