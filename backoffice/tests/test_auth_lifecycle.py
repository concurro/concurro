"""GitHub refresh/logout protocol, crash recovery and browser fences use synthetic tokens only."""

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from fastapi.testclient import TestClient
from test_backoffice import USER, settings

from backoffice.app import CSRF_HEADER, SESSION_COOKIE, create_app
from backoffice.auth import AuthError, BearerAuth, GitHubOAuth, Sessions, TokenGrant
from backoffice.credentials import Vault
from backoffice.revocations import RevocationError, Revocations
from backoffice.settings import SettingsError, load


class AuthLifecycleTests(unittest.TestCase):
    """Exercise real session coordination rather than duplicating its implementation in fixtures."""

    def test_singleflight_refresh_and_refused_old_revision(self):
        """Concurrent request streams consume one refresh pair and never invalidate its replacement."""
        oauth, clock = Mock(), [1000]
        oauth.refresh.return_value = TokenGrant("new-access", 3600, "new-refresh", 7200)
        sessions = Sessions(clock=lambda: clock[0], oauth=oauth)
        sid = sessions.create("old-access", USER, 60, grant=TokenGrant("old-access", 60, "old-refresh", 7200))
        credential = sessions.sessions[sid].credential
        with ThreadPoolExecutor(8) as pool:
            tokens = list(pool.map(lambda _: credential.get(), range(16)))
        self.assertEqual(tokens, ["new-access"] * 16)
        oauth.refresh.assert_called_once_with("old-refresh")
        credential.drop("old-access")
        self.assertEqual(credential.get(), "new-access")
        self.assertNotIn("new-access", repr(sessions.sessions[sid]))

    def test_logout_fences_late_refresh_and_durable_retry(self):
        """Logout journals first, closes local authorization immediately, and cleans up a late pair."""
        entered, release = threading.Event(), threading.Event()
        oauth = Mock()

        def refresh(token):
            """Hold the synthetic refresh until the logout request has installed its fence."""
            entered.set()
            self.assertTrue(release.wait(3))
            return TokenGrant("late-access", 3600, "late-refresh", 7200)

        oauth.refresh.side_effect = refresh
        oauth.revoke.return_value = False
        sessions = Sessions(oauth=oauth)
        sid = sessions.create("old-access", USER, 60, grant=TokenGrant("old-access", 60, "old-refresh", 7200))
        with tempfile.TemporaryDirectory() as root:
            queue = Revocations(Path(root), b"k" * 32)
            client = TestClient(
                create_app(
                    settings(public_url="http://testserver"),
                    oauth=oauth,
                    sessions=sessions,
                    vault=Vault(None),
                    revocations=queue,
                )
            )
            client.cookies.set(SESSION_COOKIE, sid)
            with ThreadPoolExecutor(1) as pool:
                pending = pool.submit(sessions.get, sid)
                self.assertTrue(entered.wait(3))
                self.assertEqual(client.post("/auth/logout").status_code, 403)
                response = client.post("/auth/logout", headers={CSRF_HEADER: "1"})
                self.assertEqual(response.json(), {"signedOut": True, "remote": "pending"})
                self.assertIsNone(sessions.get(sid))
                self.assertEqual(client.get("/api/me").status_code, 401)
                release.set()
                self.assertIsNone(pending.result(3))
            self.assertEqual(client.get("/auth/logout-status").json(), {"remote": "pending"})
            queue.db.execute("UPDATE logout_jobs SET due=0")
            oauth.revoke.return_value = True
            queue.drain(oauth)
            self.assertEqual(client.get("/auth/logout-status").json(), {"remote": "revoked"})
            self.assertEqual({c.args[0] for c in oauth.revoke.call_args_list}, {"old-access", "late-access"})
            self.assertTrue(all(r["sealed"] is None for r in queue.db.execute("SELECT * FROM logout_jobs")))
            queue.close()

    def test_logout_without_queue_never_claims_late_refresh_cleanup_complete(self):
        """Best-effort old-token revoke cannot prove the fate of an in-flight replacement pair."""
        entered, release = threading.Event(), threading.Event()
        oauth, late_attempted = Mock(), threading.Event()

        def revoke(token):
            """Finish old-token cleanup only after the late pair's cleanup has failed."""
            if token == "old-access":
                release.set()
                self.assertTrue(late_attempted.wait(3))
                return True
            late_attempted.set()
            return False

        oauth.revoke.side_effect = revoke

        def refresh(token):
            """Mint a synthetic late pair while logout waits on old-token cleanup."""
            entered.set()
            self.assertTrue(release.wait(3))
            return TokenGrant("late-access", 3600, "late-refresh", 7200)

        oauth.refresh.side_effect = refresh
        sessions = Sessions(oauth=oauth)
        sid = sessions.create("old-access", USER, 60, grant=TokenGrant("old-access", 60, "old-refresh", 7200))
        client = TestClient(
            create_app(settings(public_url="http://testserver"), oauth=oauth, sessions=sessions, vault=Vault(None))
        )
        client.cookies.set(SESSION_COOKIE, sid)
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(sessions.get, sid)
            self.assertTrue(entered.wait(3))
            response = client.post("/auth/logout", headers={CSRF_HEADER: "1"})
            self.assertEqual(response.json(), {"signedOut": True, "remote": "unknown"})
            self.assertIsNone(sessions.get(sid))
            release.set()
            self.assertIsNone(pending.result(3))
        self.assertEqual({call.args[0] for call in oauth.revoke.call_args_list}, {"old-access", "late-access"})
        self.assertEqual(client.get("/auth/logout-status").json(), {"remote": "none"})

    def test_logout_storage_failure_keeps_local_session_closed(self):
        """A cleanup outage cannot prevent local logout or report a successful remote revoke."""
        oauth, queue = Mock(), Mock()
        queue.drain.side_effect = OSError("synthetic storage failure")
        sessions = Sessions()
        sid = sessions.create("synthetic-access", USER, 3600)
        client = TestClient(
            create_app(
                settings(public_url="http://testserver"),
                oauth=oauth,
                sessions=sessions,
                vault=Vault(None),
                revocations=queue,
            )
        )
        client.cookies.set(SESSION_COOKIE, sid)
        reply = client.post("/auth/logout", headers={CSRF_HEADER: "1"})
        self.assertEqual(reply.json(), {"signedOut": True, "remote": "unavailable"})
        self.assertIsNone(sessions.get(sid))
        self.assertEqual(client.get("/api/me").status_code, 401)
        queue.prepare.assert_called_once()

    def test_ambiguous_refresh_is_not_replayed(self):
        """An uncertain one-use refresh signs out instead of guessing which pair survived."""
        oauth = Mock()
        oauth.refresh.side_effect = AuthError("sign in again", uncertain=True)
        sessions = Sessions(oauth=oauth)
        sid = sessions.create("access", USER, 60, grant=TokenGrant("access", 60, "refresh", 7200))
        with self.assertRaises(AuthError):
            sessions.get(sid)
        self.assertIsNone(sessions.get(sid))
        oauth.refresh.assert_called_once()

    def test_queue_restart_backoff_key_check_and_ciphertext_binding(self):
        """Encrypted work survives restart; swapping ciphertext or the state key cannot disclose tokens."""
        with tempfile.TemporaryDirectory() as root:
            now, key = [100], b"k" * 32
            queue = Revocations(Path(root), key, clock=lambda: now[0])
            queue.prepare("receipt", "synthetic-marker-access", True)
            oauth = Mock()
            oauth.revoke.return_value = False
            queue.drain(oauth)
            queue.drain(oauth)
            oauth.revoke.assert_called_once()
            queue.close()
            self.assertNotIn(b"synthetic-marker-access", (Path(root) / "logout.db").read_bytes())
            with self.assertRaises(RevocationError):
                Revocations(Path(root), b"x" * 32)
            queue = Revocations(Path(root), key, clock=lambda: now[0])
            self.assertEqual(queue.status("receipt"), {"remote": "pending"})
            now[0] += 100
            oauth.revoke.return_value = True
            queue.drain(oauth)
            self.assertEqual(queue.status("receipt"), {"remote": "unknown"})
            queue.prepare("other", "other-marker", False)
            queue.db.execute("UPDATE logout_jobs SET id='different-id' WHERE group_id='other'")
            oauth.reset_mock()
            queue.drain(oauth)
            oauth.revoke.assert_not_called()
            self.assertEqual(queue.status("other"), {"remote": "unknown"})
            broken = queue.db.execute("SELECT state,sealed FROM logout_jobs WHERE group_id='other'").fetchone()
            self.assertEqual((broken["state"], broken["sealed"]), ("unknown", None))
            now[0] += 100
            queue.drain(oauth)
            oauth.revoke.assert_not_called()
            queue.close()

    def test_expired_retry_lease_does_not_allow_stale_completion(self):
        """A second worker's retry decision survives a late success from an expired claim."""
        with tempfile.TemporaryDirectory() as root:
            now = [100]
            queue = Revocations(Path(root), b"k" * 32, clock=lambda: now[0])
            queue.prepare("receipt", "synthetic-access", False)
            entered, release = threading.Event(), threading.Event()
            slow = Mock()

            def revoke(token):
                """Hold one network outcome beyond its lease while another worker claims the row."""
                entered.set()
                self.assertTrue(release.wait(3))
                return True

            slow.revoke.side_effect = revoke
            with ThreadPoolExecutor(1) as pool:
                pending = pool.submit(queue.drain, slow)
                self.assertTrue(entered.wait(3))
                now[0] += 61
                other = Mock()
                other.revoke.return_value = False
                queue.drain(other)
                release.set()
                pending.result(3)
            self.assertEqual(queue.status("receipt"), {"remote": "pending"})
            row = queue.db.execute("SELECT * FROM logout_jobs").fetchone()
            self.assertEqual((row["state"], row["attempts"]), ("pending", 1))
            self.assertIsNotNone(row["sealed"])
            queue.close()

    def test_github_wire_protocol_and_redacted_failures(self):
        """Refresh fields are actual response evidence; revoke targets one token, never the grant."""
        requests = []

        def handle(request):
            """Implement only GitHub's documented token endpoints using synthetic values."""
            requests.append(request)
            if request.url.host == "github.com":
                return httpx.Response(
                    200,
                    json={
                        "access_token": "issued",
                        "expires_in": 100,
                        "refresh_token": "rotated",
                        "refresh_token_expires_in": 1000,
                    },
                )
            return httpx.Response(422 if request.method == "DELETE" else 404)

        oauth = GitHubOAuth("app-id", "client-secret", httpx.Client(transport=httpx.MockTransport(handle)))
        self.assertEqual(oauth.refresh("refresh-marker").refresh_token, "rotated")
        self.assertIn(b"grant_type=refresh_token", requests[0].content)
        self.assertTrue(oauth.revoke("access-marker"))
        self.assertEqual([r.url.path for r in requests[1:]], ["/applications/app-id/token"] * 2)
        self.assertEqual(json.loads(requests[1].content), {"access_token": "access-marker"})
        bad = GitHubOAuth(
            "app",
            "secret",
            httpx.Client(
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(200, json={"access_token": "never-echo", "refresh_token": "no-expiry"})
                )
            ),
        )
        with self.assertRaises(AuthError) as error:
            bad.refresh("refresh-marker")
        self.assertNotIn("never-echo", str(error.exception))

    def test_late_refresh_journal_failure_attempts_revoke_and_reports_unknown(self):
        """Failed late-token retention cannot leave a misleading permanent pending receipt."""
        entered, release = threading.Event(), threading.Event()
        oauth = Mock()

        def refresh(token):
            """Complete one synthetic refresh after local logout has installed the callback."""
            entered.set()
            self.assertTrue(release.wait(3))
            return TokenGrant("late-access", 3600, "late-refresh", 7200)

        oauth.refresh.side_effect = refresh
        oauth.revoke.return_value = False
        sessions = Sessions(oauth=oauth)
        sid = sessions.create("old-access", USER, 60, grant=TokenGrant("old-access", 60, "old-refresh", 7200))
        with tempfile.TemporaryDirectory() as root:
            queue = Revocations(Path(root), b"k" * 32)
            client = TestClient(
                create_app(
                    settings(public_url="http://testserver"),
                    oauth=oauth,
                    sessions=sessions,
                    vault=Vault(None),
                    revocations=queue,
                )
            )
            client.cookies.set(SESSION_COOKIE, sid)
            with (
                patch.object(queue, "refreshed", side_effect=RevocationError("synthetic journal failure")),
                ThreadPoolExecutor(1) as pool,
            ):
                pending = pool.submit(sessions.get, sid)
                self.assertTrue(entered.wait(3))
                self.assertEqual(client.post("/auth/logout", headers={CSRF_HEADER: "1"}).json()["remote"], "pending")
                release.set()
                self.assertIsNone(pending.result(3))
            self.assertIn("late-access", [call.args[0] for call in oauth.revoke.call_args_list])
            self.assertEqual(client.get("/auth/logout-status").json(), {"remote": "unknown"})
            queue.db.execute("UPDATE logout_jobs SET due=0")
            oauth.revoke.return_value = True
            queue.drain(oauth)
            queue.close()
            reopened = Revocations(Path(root), b"k" * 32)
            self.assertEqual(reopened.db.execute("SELECT waiting,unknown FROM logout_groups").fetchone()["unknown"], 1)
            reopened.close()

    def test_cleanup_status_storage_faults_are_unavailable_on_both_read_paths(self):
        """Status failure is distinct from no receipt even after the local session is already absent."""
        queue = Mock()
        queue.status.side_effect = OSError("synthetic storage failure")
        client = TestClient(
            create_app(settings(public_url="http://testserver"), oauth=Mock(), vault=Vault(None), revocations=queue)
        )
        self.assertEqual(client.get("/auth/logout-status").json(), {"remote": "unavailable"})
        self.assertEqual(
            client.post("/auth/logout", headers={CSRF_HEADER: "1"}).json(), {"signedOut": True, "remote": "unavailable"}
        )

    def test_bearer_401_refresh_replays_one_httpx_request_with_same_body(self):
        """The real httpx auth flow retries only a definite refusal and forwards the replacement revision."""
        oauth = Mock()
        oauth.refresh.return_value = TokenGrant("new-access", 3600, "new-refresh", 7200)
        sessions = Sessions(oauth=oauth)
        sid = sessions.create("old-access", USER, 3600, grant=TokenGrant("old-access", 3600, "old-refresh", 7200))
        seen = []

        def handle(request):
            """Refuse before admission, then record the actual replayed HTTP request."""
            seen.append((request.headers["authorization"], request.content))
            return httpx.Response(401 if len(seen) == 1 else 200, json={})

        client = httpx.Client(transport=httpx.MockTransport(handle))
        response = client.put(
            "http://vault.invalid/credentials/openai-api-key",
            auth=BearerAuth(sessions.sessions[sid].credential),
            json={"secret": "synthetic-candidate"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row[0] for row in seen], ["Bearer old-access", "Bearer new-access"])
        self.assertEqual(seen[0][1], seen[1][1])
        oauth.refresh.assert_called_once_with("old-refresh")

    def test_lifespan_retries_existing_durable_jobs(self):
        """The actual application lifespan drains queued work and stops its worker on exit."""
        with tempfile.TemporaryDirectory() as root:
            queue = Revocations(Path(root), b"k" * 32)
            queue.prepare("receipt", "synthetic-access", False)
            done = threading.Event()
            oauth = Mock()

            def revoke(token):
                """Signal the actual retry worker's remote attempt without network access."""
                done.set()
                return True

            oauth.revoke.side_effect = revoke
            with TestClient(
                create_app(settings(public_url="http://testserver"), oauth=oauth, vault=Vault(None), revocations=queue)
            ):
                self.assertTrue(done.wait(3))
            self.assertEqual(queue.status("receipt"), {"remote": "revoked"})
            queue.close()

    def test_github_revoke_success_and_transport_failure(self):
        """Single-token cleanup distinguishes a confirmed 204 from no remote response."""
        good = GitHubOAuth(
            "app", "synthetic-secret", httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(204)))
        )
        self.assertTrue(good.revoke("synthetic-access"))

        def failed(request):
            """Fail before a response without leaking token/client-secret request fields."""
            raise httpx.ConnectError("synthetic failure", request=request)

        bad = GitHubOAuth("app", "synthetic-secret", httpx.Client(transport=httpx.MockTransport(failed)))
        self.assertFalse(bad.revoke("synthetic-access"))

    def test_optional_auth_settings_startup_and_pair_validation(self):
        """Configured state/key wiring is real; legacy omission stays optional and incomplete pairs fail safely."""
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root).resolve()
            key, secret = directory / "auth-key", directory / "client-secret"
            key.write_text("ab" * 32)
            key.chmod(0o600)
            secret.write_text("synthetic-client-secret")
            reader_key = directory / "reader.pem"
            reader_key.write_text("synthetic-reader-private-key")
            reader_key.chmod(0o600)
            config = directory / "settings.json"
            data = {
                "public_url": "http://testserver",
                "client_id": "app",
                "client_secret_path": str(secret),
                "reader": {"app_id": 1, "private_key_path": str(reader_key)},
                "targets": [{"upstream": "up/project", "fork": "owner/project", "issues": "fork"}],
                "auth_state_dir": str(directory / "state"),
                "auth_key_path": str(key),
            }
            config.write_text(json.dumps(data))
            configured = load(config)
            self.assertEqual(configured.auth_state_dir, directory / "state")
            with TestClient(create_app(configured, oauth=Mock(), vault=Vault(None))) as client:
                self.assertEqual(client.get("/api/health").json(), {"status": "ok"})
            self.assertTrue((directory / "state/logout.db").exists())
            key.chmod(0o644)
            with self.assertRaises(SettingsError) as error:
                load(config)
            self.assertNotIn("synthetic-client-secret", str(error.exception))
            key.chmod(0o600)
            del data["auth_key_path"]
            config.write_text(json.dumps(data))
            with self.assertRaises(SettingsError):
                load(config)
            del data["auth_state_dir"]
            config.write_text(json.dumps(data))
            self.assertIsNone(load(config).auth_state_dir)
