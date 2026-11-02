"""App identity: tokens narrowed to one repository and use, verified, cached once, and revoked."""

import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest

from fixtures import Clock, Reply
from service import identity
from service.identity import CUTOVER, READ, WRITE, Identity, IdentityError

REPOSITORY = "owner/target"
OTHER = "owner/second"


class App:
    """An in-memory GitHub App API with switchable misbehaviour."""

    def __init__(self, clock):
        self.clock = clock
        self.selection, self.owner = "selected", None
        self.extra_permission = self.extra_repository = False
        self.minted, self.revoked, self.requests = [], [], []
        self.guard = threading.Lock()
        self.delay = 0

    def connection(self, host, timeout):
        return Connection(self)

    def handle(self, method, target, headers, body):
        token = headers["Authorization"].removeprefix("Bearer ")
        data = json.loads(body) if body else None
        self.requests.append((method, target, data))
        if (method, target) == ("GET", "/app"):
            return 200, {"slug": "concurro-controller"}
        if method == "GET" and target.endswith("/installation"):
            repository = target.removeprefix("/repos/").removesuffix("/installation")
            return 200, {"id": 5, "repository_selection": self.selection,
                         "account": {"login": self.owner or repository.split("/")[0]}}
        if (method, target) == ("POST", "/app/installations/5/access_tokens"):
            time.sleep(self.delay)
            with self.guard:
                minted = f"token-{len(self.minted)}"
                self.minted.append((minted, data["repositories"], data["permissions"]))
            permissions = {**data["permissions"], "metadata": "read"}
            if self.extra_permission:
                permissions["actions"] = "write"
            expires = datetime.fromtimestamp(self.clock() + 3600, timezone.utc).isoformat().replace("+00:00", "Z")
            return 201, {"token": minted, "permissions": permissions, "expires_at": expires}
        if (method, target) == ("GET", "/installation/repositories?per_page=100"):
            [(_, names, _)] = [m for m in self.minted if m[0] == token]
            listed = [{"full_name": "owner/" + names[0]}] + ([{"full_name": OTHER}] if self.extra_repository else [])
            return 200, {"total_count": len(listed), "repositories": listed}
        if (method, target) == ("DELETE", "/installation/token"):
            self.revoked.append(token)
            return 204, None
        return 404, {"message": "Not Found"}


class Connection:
    """The parts of `http.client.HTTPSConnection` the transport uses."""

    def __init__(self, app):
        self.app = app

    def connect(self):
        pass

    def request(self, method, target, body=None, headers=None):
        self.reply = self.app.handle(method, target, headers, body)

    def getresponse(self):
        return Reply(*self.reply)

    def close(self):
        pass


class Settings:
    """The two settings fields the identity service reads."""

    def __init__(self, key):
        self.app_id, self.private_key_path = 123, key


class IdentityTests(unittest.TestCase):
    """Least privilege per repository and use, exactly as requested, or no token at all."""

    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory()
        Path(cls.keys.name).chmod(0o700)
        cls.key = Path(cls.keys.name) / "app.pem"
        subprocess.run(["openssl", "genrsa", "-out", str(cls.key), "2048"], check=True, capture_output=True)
        cls.key.chmod(0o600)

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    def setUp(self):
        self.clock = Clock()
        self.app = App(self.clock)
        self.identity = Identity(Settings(self.key), self.app.connection, self.clock, lambda key, message: b"signature")

    def test_app_identity(self):
        """The bot login is the App slug with `[bot]`."""
        self.assertEqual(self.identity.verify(), "concurro-controller[bot]")

    def test_jwt_is_signed_with_the_app_key(self):
        """A real RS256 signature over the header and claims, by the configured App."""
        jwt = Identity(Settings(self.key), self.app.connection, self.clock).app_jwt()
        header, claims, signature = jwt.split(".")
        decode = lambda part: base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
        self.assertEqual(json.loads(decode(claims)), {"iat": 1_000_000 - 60, "exp": 1_000_000 + 540, "iss": "123"})
        with tempfile.TemporaryDirectory() as scratch:
            public, sig = Path(scratch) / "public.pem", Path(scratch) / "signature"
            subprocess.run(["openssl", "rsa", "-in", str(self.key), "-pubout", "-out", str(public)],
                           check=True, capture_output=True)
            sig.write_bytes(decode(signature))
            verified = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(public), "-signature", str(sig)],
                                      input=f"{header}.{claims}".encode(), capture_output=True)
        self.assertEqual(verified.returncode, 0)

    def test_tokens_are_narrowed_to_one_repository_and_use(self):
        """Read and write tokens request exactly their permissions for that repository's name."""
        self.assertEqual(self.identity.token(REPOSITORY, "read"), "token-0")
        self.assertEqual(self.identity.token(REPOSITORY, "write"), "token-1")
        self.assertEqual(self.identity.token(OTHER, "read"), "token-2")
        self.assertEqual([(names, permissions) for _, names, permissions in self.app.minted],
                         [(["target"], READ), (["target"], WRITE), (["second"], READ)])
        self.assertNotIn("workflows", {**READ, **WRITE, **CUTOVER})

    def test_installation_must_be_selected_and_owned(self):
        """All-repositories installations and foreign owners never mint."""
        for change in ({"selection": "all"}, {"owner": "someone-else"}):
            with self.subTest(change=change):
                app = App(self.clock)
                vars(app).update(change)
                ident = Identity(Settings(self.key), app.connection, self.clock, lambda key, message: b"s")
                with self.assertRaises(IdentityError):
                    ident.token(REPOSITORY, "read")
                self.assertEqual(app.minted, [])

    def test_a_token_differing_from_the_request_is_revoked(self):
        """Extra permissions or another repository: minting fails and that token is revoked."""
        for change in ("extra_permission", "extra_repository"):
            with self.subTest(change=change):
                self.setUp()
                setattr(self.app, change, True)
                with self.assertRaises(IdentityError):
                    self.identity.token(REPOSITORY, "write")
                self.assertEqual(self.app.revoked, ["token-0"])
                self.assertEqual(self.identity.tokens, {})

    def test_concurrent_callers_mint_once(self):
        """One lock per repository and kind."""
        self.app.delay = 0.05
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.identity.token(REPOSITORY, "write")))
                   for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual((results, len(self.app.minted)), (["token-0"] * 8, 1))

    def test_cache_expiry_and_drop(self):
        """Reused until ten minutes before expiry; a refused token is forgotten."""
        credential = self.identity.credential(REPOSITORY, "read")
        self.assertEqual([credential.get(), credential.get()], ["token-0", "token-0"])
        self.clock.now += 3600 - identity.TOKEN_MARGIN - 1
        self.assertEqual(credential.get(), "token-0")
        self.clock.now += 2
        self.assertEqual(credential.get(), "token-1")
        credential.drop("token-0")
        self.assertEqual(credential.get(), "token-1")
        credential.drop("token-1")
        self.assertEqual(credential.get(), "token-2")
        self.assertEqual(self.app.revoked, [])

    def test_close_revokes_every_cached_token(self):
        """Shutdown revokes what the cache still holds."""
        self.identity.token(REPOSITORY, "read")
        self.identity.token(REPOSITORY, "write")
        self.identity.close()
        self.assertEqual(sorted(self.app.revoked), ["token-0", "token-1"])

    def test_cutover_token_is_command_scoped(self):
        """Minted with the cutover permissions, never cached, revoked when the command ends."""
        with self.identity.cutover(REPOSITORY) as token:
            self.assertEqual(self.app.minted[-1][2], CUTOVER)
            self.assertEqual(self.app.revoked, [])
        self.assertEqual(self.app.revoked, [token])
        for call in (lambda: self.identity.token(REPOSITORY, "cutover"),
                     lambda: self.identity.credential(REPOSITORY, "cutover")):
            with self.assertRaises(IdentityError):
                call()

    def test_errors_never_contain_tokens(self):
        """A rejected App call names only its status."""
        self.app.handle = lambda *_: (500, {"message": "token-0 leaked"})
        with self.assertRaises(IdentityError) as raised:
            self.identity.token(REPOSITORY, "read")
        self.assertEqual(str(raised.exception), "GitHub App request failed (HTTP 500)")


if __name__ == "__main__":
    unittest.main()
