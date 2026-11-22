"""Validate the HTTP fixtures with generated exchange API models."""

import importlib
import json
import unittest
from pathlib import Path

from pydantic import ValidationError


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "exchange.http.v1"
CASES = (
    ("operator_submit.json", "exchange_operator_submit_v2", "TurnSubmissionV2"),
    ("operator_observation.json", "exchange_operator_observation_v1", "TurnObservation"),
    ("operator_retirement.json", "exchange_operator_retirement_v1", "RetiredSessions"),
    ("admission_request.json", "exchange_admission_request_v1", "AdmissionRequest"),
    ("admission_reply.json", "exchange_admission_admitted_v1", "Admission"),
    ("finish_request.json", "exchange_admission_finish_v1", "FinishRequest"),
    ("probe_reply.json", "exchange_admission_probe_v1", "ProbeResponse"),
    ("sessions_request.json", "exchange_admission_sessions_request_v1", "SessionStatusRequest"),
    ("sessions_reply.json", "exchange_admission_sessions_response_v1", "SessionStatusResponse"),
    ("runner_payload.json", "exchange_runner_payload_v2", "TurnPayloadV2"),
    ("runner_result.json", "exchange_runner_result_v2", "TurnResultV2"),
    ("runner_claim_reply.json", "exchange_runner_claim_reply_v2", "RunnerClaimReply"),
    ("runner_key_request.json", "exchange_runner_key_request_v2", "RunnerKeyRequest"),
    ("runner_key_reply.json", "exchange_runner_key_reply_v2", "RunnerKeyReply"),
    ("runner_next_turn.json", "exchange_runner_next_turn_v2", "RunnerNextTurn"),
    ("relay_session_claim.json", "exchange_relay_session_claim_v1", "ExchangeSessionClaim"),
)


class ExchangeApiFixtures(unittest.TestCase):
    """Keep Python consumers aligned with Rust's registered HTTP wire shapes."""

    def test_api_fixtures(self):
        """Every representative message validates and preserves its JSON values."""
        for name, module, model_name in CASES:
            with self.subTest(name=name):
                model = getattr(importlib.import_module(f"concurro_contracts.{module}"), model_name)
                payload = json.loads((FIXTURES / name).read_text())
                parsed = model.model_validate_json((FIXTURES / name).read_bytes(), strict=True)
                dumped = parsed.model_dump(mode="json", by_alias=True, exclude_unset=True)
                self.assertEqual(dumped, payload)

    def test_unknown_payer_field_is_refused(self):
        """The generated payer projection matches operator admission's closed fields."""
        module = importlib.import_module("concurro_contracts.exchange_operator_submit_v2")
        body = json.loads((FIXTURES / "operator_submit.json").read_text())
        body["grant"]["purpose"] = "task_contributed"
        body["grant"]["payer"] = {"id": 42, "login": "payer", "extra": True}
        with self.assertRaises(ValidationError):
            module.TurnSubmissionV2.model_validate_json(json.dumps(body), strict=True)


if __name__ == "__main__":
    unittest.main()
