"""Test-only bounded response-observer hop; no production proxy integration.

The resolver/native gateway inject credentials. This hop snapshots their trusted synthetic
request association, forwards to a loopback fake provider, and exports sanitized events.
"""

import http.client
import json
import time

ERRORS = {"invalid_api_key": "auth", "insufficient_quota": "quota_exhausted", "rate_limit_error": "rate_limited"}


def error_category(value):
    """Recognize only fixture structured codes, never HTTP status or provider message text."""
    error = value.get("error", {}) if isinstance(value, dict) else {}
    return ERRORS.get(error.get("code") or error.get("type")) if isinstance(error, dict) else None


def retry_after(value):
    """Keep a bounded numeric retry delay without exporting arbitrary header text."""
    return min(int(value), 300) if value and value.isdecimal() else None


class Observation:
    """Accumulate bounded structured metadata without retaining provider response text."""

    def __init__(self, snapshot, status, delay):
        """Capture attribution once before response streaming can race another turn."""
        self.record = {key: snapshot[key] for key in ("request", "session", "turn", "account", "route")}
        self.record.update(status=status, category=None, completed=False, usage=None, retry_after=None, cancelled=False)
        self.delay = retry_after(delay)
        self.input_tokens = None
        self.ambiguous = False

    def accept(self, value):
        """Inspect one JSON result/event and return whether its error text must be suppressed."""
        if not isinstance(value, dict):
            return False
        kind = value.get("type")
        error = kind in ("error", "response.failed") or isinstance(value.get("error"), dict)
        if error:
            self.record["category"] = error_category(value) or "unknown"
            self.record["retry_after"] = self.delay if self.record["category"] == "rate_limited" else None
            return True
        if kind == "message" and value.get("stop_reason"):
            self.record["completed"] = True
            usage = value.get("usage", {})
            if all(isinstance(usage.get(k), int) for k in ("input_tokens", "output_tokens")):
                self.record["usage"] = {key: usage[key] for key in ("input_tokens", "output_tokens")}
        if kind == "message_start":
            self.input_tokens = value.get("message", {}).get("usage", {}).get("input_tokens")
        if kind == "message_delta":
            output = value.get("usage", {}).get("output_tokens")
            if isinstance(self.input_tokens, int) and isinstance(output, int):
                self.record["usage"] = {"input_tokens": self.input_tokens, "output_tokens": output}
        if kind == "message_stop":
            self.record["completed"] = True
        if kind == "response.completed" or value.get("object") == "response" and value.get("status") == "completed":
            response = value.get("response", value)
            usage = response.get("usage")
            self.record["completed"] = True
            if isinstance(usage, dict) and all(
                isinstance(usage.get(k), int) for k in ("input_tokens", "output_tokens")
            ):
                self.record["usage"] = {key: usage[key] for key in ("input_tokens", "output_tokens")}
        return False

    def finish(self):
        """Missing terminal events remain unknown and never fabricate zero token usage."""
        if self.ambiguous or not self.record["completed"]:
            self.record["category"] = self.record["category"] or "unknown"
            self.record["usage"] = None
        return self.record


def observe(handler, body):
    """Forward one bounded local request and observe actual fake-provider response bytes."""
    state = handler.server.state
    identifier = handler.headers.get("x-phase0-request")
    with state.lock:
        snapshot = dict(state.pending.get(identifier, {}))
    if not snapshot:
        return handler.json_reply(403, {"error": {"type": "fixture_observer_denied"}})
    upstream = http.client.HTTPConnection("127.0.0.1", handler.server.server_port, timeout=5)
    headers = {
        name: value
        for name, value in handler.headers.items()
        if name.lower() not in ("host", "content-length", "connection", "transfer-encoding")
        and not name.lower().startswith("x-phase0-")
    }
    path = handler.path.removeprefix("/observe")
    observer = None
    started = time.monotonic()
    try:
        upstream.request("POST", path, json.dumps(body), headers)
        response = upstream.getresponse()
        observer = Observation(snapshot, response.status, response.getheader("Retry-After"))
        streaming = "text/event-stream" in response.getheader("Content-Type", "")
        if not streaming:
            raw = response.read(65537)
            try:
                value = json.loads(raw) if len(raw) <= 65536 else None
            except (ValueError, UnicodeDecodeError):
                value = None
            if value is None:
                observer.ambiguous = True
                return handler.json_reply(
                    502, {"error": {"type": "unknown", "message": "unsupported provider response"}}
                )
            if response.status >= 400 or observer.accept(value):
                observer.record["category"] = error_category(value) or "unknown"
                observer.record["retry_after"] = (
                    observer.delay if observer.record["category"] == "rate_limited" else None
                )
                return handler.json_reply(
                    response.status,
                    {"error": {"type": observer.record["category"], "message": "provider request failed"}},
                    {"Retry-After": str(observer.record["retry_after"])}
                    if observer.record["retry_after"] is not None
                    else {},
                )
            return handler.json_reply(response.status, value)
        handler.connection.settimeout(3)
        handler.send_response(response.status)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Connection", "close")
        handler.end_headers()
        event = bytearray()
        opaque = False
        while chunk := response.readline(65536):
            if not opaque and len(event) + len(chunk) <= 65536:
                event.extend(chunk)
            else:
                observer.ambiguous = True
                if event:
                    handler.wfile.write(event)
                    event.clear()
                handler.wfile.write(chunk)
                handler.wfile.flush()
                opaque = True
            if chunk in (b"\n", b"\r\n"):
                if event:
                    payload = b"\n".join(line[5:].strip() for line in event.splitlines() if line.startswith(b"data:"))
                    try:
                        value = json.loads(payload)
                    except (ValueError, UnicodeDecodeError):
                        value = {}
                        observer.ambiguous = True
                    if observer.accept(value):
                        event = bytearray(
                            (
                                "event: error\ndata: "
                                + json.dumps(
                                    {
                                        "type": "error",
                                        "error": {
                                            "type": observer.record["category"],
                                            "message": "provider request failed",
                                        },
                                    }
                                )
                                + "\n\n"
                            ).encode()
                        )
                    handler.wfile.write(event)
                    handler.wfile.flush()
                    event.clear()
                opaque = False
        if event:
            observer.ambiguous = True  # Do not forward an uninspected truncated event.
        handler.close_connection = True
    except (BrokenPipeError, ConnectionResetError, TimeoutError, http.client.HTTPException) as error:
        if observer:
            observer.record["cancelled"] = True
            observer.record["termination"] = type(error).__name__
            observer.record["elapsed_seconds"] = round(time.monotonic() - started, 3)
        handler.close_connection = True
    finally:
        upstream.close()
        if observer:
            with state.lock:
                state.observations.append(observer.finish())
