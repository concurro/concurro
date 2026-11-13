"""Nonsecret admission identity and compatibility at the engine boundaries."""

import unittest
from copy import deepcopy

from support import CONFIG, setUpModule as setUpModule, tearDownModule as tearDownModule

import admissions
import attempt_result
import sessions
from credential_routes import ROUTES
from credentials import Credential
from policy import Blocked, digest


def selection(epoch=1, version=1, provider="openai"):
    """Use one exact record/version and provider consent without a vault or secret."""
    route, actor, record = provider + "-api-key", "101", "a" * 64
    credential = Credential(
        digest(["contributed", actor, route, record, version]),
        "codex" if provider == "openai" else "claude",
        "contributed",
        "gateway-key",
        actor,
        route,
        record,
        version,
    )
    return admissions.selection(
        credential,
        provider,
        {
            "status": "active",
            "sponsor_id": actor,
            "epoch": epoch,
            "grant_id": digest([actor, provider, epoch]),
            "provenance": "explicit_comment",
        },
    )


class AdmissionTests(unittest.TestCase):
    """Preserve old organization identities while isolating personal versions and transcripts."""

    def test_org_wire_and_legacy_receipt_keys_remain_exact(self):
        """No absent personal fields or receipt digest are added to legacy serialized identities."""
        credential = Credential("gateway:codex", "codex", "org", "gateway-key")
        record = credential.record()
        self.assertEqual(set(record), {"id", "provider", "kind", "adapter", "account"})
        self.assertEqual(Credential.parse(record), credential)
        with self.assertRaises(Blocked):
            Credential.parse({**record, "version": None})
        state = {"issue": 7, "calls": 1}
        operation = {
            "id": "operation",
            "attempt": "a" * 32,
            "provider": "codex",
            "stage": "intake",
            "request": "request",
        }
        legacy = attempt_result.binding(state, operation, "owner/repo")
        self.assertNotIn("admission", legacy)
        operation["admission"] = admissions.freeze(admissions.selection(credential), 1, None)
        current = attempt_result.binding(state, operation, "owner/repo")
        self.assertEqual({k: v for k, v in current.items() if k != "admission"}, legacy)
        self.assertEqual(current["admission"], digest(operation["admission"]))

    def test_route_version_and_uuid_identity_are_strict(self):
        """Route ownership comes from the shared catalog and transport normalization preserves the UUID."""
        first, replacement = selection(), selection(version=2)
        self.assertNotEqual(first["credential"]["id"], replacement["credential"]["id"])
        self.assertEqual(admissions.turn_id("1234567890abcdef1234567890abcdef"), "12345678-90ab-cdef-1234-567890abcdef")
        for field, value in (("sponsor", "202"), ("epoch", True), ("sponsorship_provider", "anthropic")):
            broken = deepcopy(first)
            broken[field] = value
            with self.assertRaises(Blocked):
                admissions.validate(broken)
        self.assertEqual(ROUTES["chatgpt-subscription"]["sponsorship_provider"], "openai")
        self.assertEqual(ROUTES["claude-subscription"]["sponsorship_provider"], "anthropic")

    def test_transcript_scope_survives_provider_transfer_and_credential_replacement(self):
        """Funding identity freezes independently of the task-scoped retained transcript."""
        state = {"issue": 7, "base": "a" * 40, "requirements": {}, "bundle_id": "bundle", "config_digest": "config"}
        context = {"base": state["base"]}
        first = sessions.prepare(state, CONFIG, "claude", "plan", context)
        original = admissions.freeze(selection(provider="anthropic"), 1, 1, first)
        current = sessions.prepare(state, CONFIG, "claude", "plan", context)
        replacement = admissions.freeze(selection(epoch=2, version=2, provider="anthropic"), 2, 1, current)
        self.assertEqual(original["transcript_scope"], replacement["transcript_scope"])
        self.assertNotEqual(original["credential"], replacement["credential"])
        self.assertNotEqual(original["grant_id"], replacement["grant_id"])

    def test_exchange_image_packages_shared_catalog_under_isolated_python(self):
        """The real image entrypoint loads the same catalog without repository import paths."""
        import shutil
        import subprocess
        import sys
        import tempfile
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        dockerfile = (root / "gateway/exchange/Dockerfile").read_text()
        self.assertIn("COPY engine/credential_routes.py /app/credential_routes.py", dockerfile)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            shutil.copy2(root / "gateway/exchange/exchange.py", target / "exchange.py")
            shutil.copy2(root / "gateway/exchange/inference.py", target / "inference.py")
            shutil.copy2(root / "gateway/provider_protocol.py", target / "provider_protocol.py")
            shutil.copy2(root / "engine/credential_routes.py", target / "credential_routes.py")
            result = subprocess.run(
                [sys.executable, "-I", str(target / "exchange.py"), "--help"], capture_output=True, timeout=10
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode())
