"""Offline regression tests for the isolated observer and the metadata-buffering workaround."""

import json
import tempfile
import time
import unittest
from pathlib import Path

from spike_fixtures import FakeServices, configuration, fake_server, request, scan_paths
from spike_observer import Observation, error_category, retry_after

SNAPSHOT = {"request": "request-1", "session": "session-1", "turn": "turn-1", "account": "alice", "route": "chatgpt"}


class ObserverTests(unittest.TestCase):
    """Keep fixture evidence conservative independently of the Docker experiment runner."""

    def test_categories_use_structured_codes_never_status_or_text(self):
        """Ignore untrusted error text when choosing a fixture category."""
        for code, category in (
            ("invalid_api_key", "auth"),
            ("insufficient_quota", "quota_exhausted"),
            ("rate_limit_error", "rate_limited"),
        ):
            with self.subTest(code=code):
                self.assertEqual(error_category({"error": {"code": code, "message": "ignored"}}), category)
        self.assertIsNone(error_category({"error": {"message": "invalid_api_key insufficient_quota"}}))
        self.assertIsNone(error_category({"error": "rate_limit_error"}))

    def test_incomplete_stream_has_unknown_usage(self):
        """Discard partial usage without a terminal completion event."""
        observer = Observation(SNAPSHOT, 200, None)
        observer.accept({"type": "message_start", "message": {"usage": {"input_tokens": 2}}})
        observer.accept({"type": "message_delta", "usage": {"output_tokens": 3}})
        result = observer.finish()
        self.assertEqual(result["category"], "unknown")
        self.assertIsNone(result["usage"])
        self.assertEqual(result["turn"], "turn-1")

    def test_observation_does_not_keep_provider_text_and_freezes_identity(self):
        """Keep attribution immutable and provider text out of exported metadata."""
        snapshot = dict(SNAPSHOT)
        observer = Observation(snapshot, 429, "99999")
        snapshot["turn"] = "later"
        observer.accept({"type": "error", "error": {"type": "rate_limit_error", "message": "secret fixture text"}})
        result = observer.finish()
        self.assertEqual(result["retry_after"], 300)
        self.assertEqual(result["turn"], "turn-1")
        self.assertNotIn("secret fixture text", str(result))
        self.assertIsNone(retry_after("Wed, 21 Oct"))

    def test_secret_scanner_negative_control(self):
        """The exact scanner used by integration detects a deliberately seeded fake secret."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway.stderr.log"
            path.write_bytes(b"prefix FAKE_NEGATIVE_CONTROL_TOKEN suffix")
            self.assertEqual(scan_paths([path], [b"FAKE_NEGATIVE_CONTROL_TOKEN"]), [path])
            path.write_bytes(b"redacted")
            self.assertEqual(scan_paths([path], [b"FAKE_NEGATIVE_CONTROL_TOKEN"]), [])

    def test_native_metadata_uses_headers_to_avoid_main_response_buffering(self):
        """Keep resolver metadata off the buffering response-body access path."""
        config = configuration(1234)
        metadata = config["llm"]["policies"]["extAuthz"]["protocol"]["http"]["metadata"]
        self.assertTrue(metadata)
        self.assertTrue(
            all(
                "response.headers" in expression and "response.body" not in expression
                for expression in metadata.values()
            )
        )

    def test_unsupported_response_bytes_remain_unknown_and_trailing_errors_are_dropped(self):
        """Reject unsupported JSON, preserve opaque SSE and drop an uninspected trailing event."""
        for mode in ("json_invalid", "json_large", "sse_invalid", "sse_large", "stream_error_unterminated"):
            with self.subTest(mode=mode):
                state = FakeServices()
                state.records["session"] = {
                    "token": "fake-local-token",
                    "account": "alice",
                    "route": "chatgpt",
                    "turn": "turn",
                    "mode": mode,
                }
                state.credentials["fake-local-token"] = {"account": "alice", "route": "chatgpt"}
                _, resolved = state.resolve("session")
                with fake_server(state) as port:
                    status, _, body = request(
                        f"http://127.0.0.1:{port}/observe/codex/responses",
                        "POST",
                        {"model": "fixture", "stream": mode.startswith("sse_") or mode.startswith("stream_")},
                        [
                            ("Authorization", "Bearer fake-local-token"),
                            ("chatgpt-account-id", "alice"),
                            ("x-phase0-request", resolved["request"]),
                        ],
                    )
                    deadline = time.monotonic() + 2
                    while not state.observations and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertEqual(state.observations[-1]["category"], "unknown")
                    self.assertIsNone(state.observations[-1]["usage"])
                    if mode.startswith("json_"):
                        self.assertEqual(status, 502)
                        self.assertEqual(json.loads(body)["error"]["type"], "unknown")
                    if mode.startswith("sse_"):
                        self.assertIn("not JSON" if mode == "sse_invalid" else "x" * 65537, body)
                    self.assertNotIn("FAKE_PROVIDER_DETAIL_SENTINEL", body)
                    self.assertNotIn("fake-local-token", body)
