"""Validate the same committed event fixtures with generated Pydantic models."""

import json
import unittest
from pathlib import Path

from pydantic import ValidationError

from concurro_contracts import validation
from concurro_contracts.strict import StrictTimestamp


CONTRACTS = Path(__file__).resolve().parents[2]
REGISTRY = json.loads((CONTRACTS / "registry.json").read_text())


class FixtureTests(unittest.TestCase):
    """Check strict Python deserialization for every representative event."""

    def test_valid(self):
        """Accept known actions, optional fields, unknown fields, and open enum values."""
        for registration in REGISTRY:
            root = CONTRACTS / "fixtures" / registration["stem"]
            decode = getattr(validation, registration["python_decoder"])
            for path in sorted((root / "valid").glob("*.json")):
                with self.subTest(path=path.name):
                    event = decode(path.read_bytes())
                    actual = event.model_dump(mode="json", by_alias=True)
                    if registration["stem"] == "exchange.turn.finished.v1":
                        observation = actual["data"].get("observation")
                        if observation is None:
                            actual["data"].pop("observation", None)
                        else:
                            actual["data"]["observation"] = {
                                key: value for key, value in observation.items() if value is not None
                            }
                    if registration["stem"] == "turn.finished.v1" and actual["data"].get("usage") is None:
                        actual["data"].pop("usage", None)
                    for field in ("entity", "revision", "causation"):
                        if actual.get(field) is None:
                            actual.pop(field, None)
                    expected = root / "expected" / path.name
                    expected_json = json.loads((expected if expected.exists() else path).read_text())
                    self.assertEqual(actual, expected_json)

    def test_invalid(self):
        """Reject coerced integers and unknown closed enum members."""
        for registration in REGISTRY:
            root = CONTRACTS / "fixtures" / registration["stem"]
            decode = getattr(validation, registration["python_decoder"])
            for path in sorted((root / "invalid").glob("*.json")):
                with self.subTest(path=path.name), self.assertRaises((ValidationError, ValueError)):
                    decode(path.read_bytes())

    def test_version_and_timestamp_rules(self):
        """Separate unsupported versions from malformed envelopes and pin date grammar."""
        root = CONTRACTS / "fixtures/github.issues.v1/invalid"
        with self.assertRaises(validation.UnsupportedVersion):
            validation.decode_github_issues((root / "future_version.json").read_bytes())
        with self.assertRaises(ValueError) as malformed:
            validation.decode_github_issues(b"[]")
        self.assertNotIsInstance(malformed.exception, validation.UnsupportedVersion)
        schema = json.loads((CONTRACTS / "schemas/github.issues.v1.de.json").read_text())
        self.assertEqual(StrictTimestamp.PATTERN.pattern, schema["$defs"]["Timestamp"]["pattern"])

    def test_shared_timestamp_calendar(self):
        """The Python decoder accepts the same timestamp domain as Rust."""
        corpus = json.loads((CONTRACTS / "fixtures/timestamps.json").read_text())
        for value in corpus["valid"]:
            with self.subTest(value=value):
                StrictTimestamp.model_validate(value)
        for value in corpus["invalid"]:
            with self.subTest(value=value), self.assertRaises((ValidationError, ValueError)):
                StrictTimestamp.model_validate(value)


if __name__ == "__main__":
    unittest.main()
