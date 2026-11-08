"""Credential identities are chosen per call and recorded without material."""

import unittest

from support import (
    setUpModule as setUpModule,  # unittest module fixtures
    tearDownModule as tearDownModule,
)

from credentials import Credential
from pipeline_fixtures import ManualPipelineFixture
from policy import Blocked


class CredentialIdentityTests(unittest.TestCase):
    """Identities name a route, never a secret."""

    def test_transport_identity_is_strict(self):
        """Only the exact identity fields and known kinds are accepted from transport."""
        record = Credential("gateway:codex", "codex", "org", "gateway-key").record()
        self.assertEqual(Credential.parse(record).record(), record)
        for bad in (
            {**record, "secret": "x"},
            {**record, "kind": "shared"},
            {**record, "id": ""},
            {k: v for k, v in record.items() if k != "account"},
            "gateway:codex",
        ):
            with self.subTest(bad=bad), self.assertRaises(Blocked):
                Credential.parse(bad)


class ControllerCredentialTests(ManualPipelineFixture, unittest.TestCase):
    """The controller selects per call and records identities, not secrets, in audit state."""

    def test_lifecycle_records_credential_usage(self):
        """Every debited call is attributed to the selected credential identity."""
        self.ready()
        state = self.tick()
        self.assertEqual(set(state["credentials"]), {"fixture:claude", "fixture:codex"})
        self.assertEqual(sum(v["calls"] for v in state["credentials"].values()), state["calls"])
        self.assertEqual(
            state["credentials"]["fixture:codex"],
            {
                "provider": "codex",
                "kind": "org",
                "adapter": "direct-api-key",
                "account": None,
                "calls": state["credentials"]["fixture:codex"]["calls"],
            },
        )
        self.assertTrue(self.agents.calls)
        self.assertTrue(all(c["controller_credential"]["provider"] == p for p, _, c in self.agents.calls))


if __name__ == "__main__":
    unittest.main()
