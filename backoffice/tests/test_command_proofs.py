"""The retained browser session signs one exact Rust task request."""

import json
import unittest

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from backoffice.commands import ServiceCommands
from backoffice.proofs import CommandSigner


class CommandProofTests(unittest.TestCase):
    """Exercise the real signer and forwarding request without a user bearer."""

    def test_signed_retry_binds_feedback_and_source_time(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        signer = CommandSigner(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        observed = []

        def answer(request):
            observed.append(request)
            return httpx.Response(202, json={"request_id": "recorded", "status": "pending"})

        client = httpx.Client(transport=httpx.MockTransport(answer))
        commands = ServiceCommands(client=client, task_api_url="http://task-internal:9000")
        action = "00ced378-49a4-4c99-a2d2-7bd7eac05801"
        source = "2026-11-20T10:11:12Z"
        result = commands.send_signed(
            42,
            7,
            "retry",
            "2",
            {"id": 9, "login": "writer"},
            action,
            source,
            signer,
            "correct the plan",
        )
        self.assertEqual(result["status"], "pending")
        request = observed[0]
        self.assertEqual(request.headers["idempotency-key"], action)
        self.assertNotIn("authorization", request.headers)
        body = json.loads(request.content)
        self.assertEqual(body["kind"], "extend")
        self.assertEqual(body["argument"], {"plan_revisions": 2})
        self.assertEqual(body["detail"], {"feedback": "correct the plan"})
        self.assertEqual(body["source_at"], source)
        self.assertEqual(len(body["proof"].split(".")), 3)
