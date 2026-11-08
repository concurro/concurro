"""Retained sessions are scoped, explicit, recoverable, and never approval authority."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from support import setUpModule as setUpModule, tearDownModule as tearDownModule  # unittest module fixtures
from agent_entry import command_for
from session_entry import SessionTurn
from sessions import prepare
from fakes import CONFIG


class SessionTests(unittest.TestCase):
    """Verify provider-independent lifecycle and provider-specific resume arguments."""

    def state(self):
        """Return enough authoritative identity to bind one cooperative loop."""
        return {"issue": 7, "base": "a" * 40, "requirements": {"body": "Improve guide"},
                "bundle_id": "bundle", "agent_image": "sha256:" + "a" * 64,
                "config_digest": "config", "plan_id": "plan", "design_reopens": 0}

    def test_scope_isolates_roles_issues_models_bases_and_reopened_designs(self):
        """A matching role resumes; changed authority or a fresh review cannot reuse it."""
        state = self.state()
        first = prepare(state, CONFIG, "claude", "plan", {})
        state["sessions"][first["role"]]["turn"] = 2
        self.assertEqual(prepare(state, CONFIG, "claude", "plan", {})["turn"], 2)
        for key, value in (("issue", 8), ("base", "b" * 40), ("requirements", {}),
                           ("bundle_id", "other"),
                           ("config_digest", "other"), ("design_reopens", 1)):
            changed = {**deepcopy(state), key: value}
            fresh = prepare(changed, CONFIG, "claude", "plan", {})
            self.assertNotEqual(fresh["scope"], first["scope"])
            self.assertEqual(fresh["turn"], 0)
        reviewer = {"reviewer_identity": {"model": CONFIG["models"]["claude"]}}
        self.assertNotEqual(prepare(state, CONFIG, "claude", "plan_qc", reviewer)["scope"], first["scope"])
        model = deepcopy(CONFIG)
        model["models"]["claude"] = "different-model"
        self.assertNotEqual(prepare(state, model, "claude", "plan", {})["scope"], first["scope"])
        for stage in ("intake", "local_review", "review"):
            self.assertIsNone(prepare(state, CONFIG, "claude", stage, {}))
        self.assertIsNone(prepare(state, CONFIG, "claude", "plan_qc", {"review_phase": "fresh"}))

    def test_provider_resume_uses_exact_identity(self):
        """All configured models resume only their own explicit session; no latest-session lookup."""
        identifier = "12345678-1234-1234-1234-123456789abc"
        for provider in ("claude", "codex"):
            request = {"provider": provider, "model": "chosen-model", "schema": {},
                       "session": {"scope": "a" * 64, "turn": 1}, "gateway": {"url": "https://gateway.invalid"},
                       "session_id": identifier, "resume_id": identifier}
            command = command_for(request, "schema", "output")
            self.assertNotIn("--last", command)
            self.assertNotIn("--continue", command)
            self.assertNotIn("--ephemeral", command)
            self.assertNotIn("--no-session-persistence", command)
            self.assertEqual(command[command.index("--model") + 1], "chosen-model")
            if provider == "codex":
                self.assertEqual(command[:3], ["codex", "exec", "resume"])
                self.assertIn(identifier, command)
            else:
                self.assertEqual(command[command.index("--resume") + 1], identifier)

    def prompt(self, **context):
        """Represent the stable instruction prefix and replaceable untrusted input."""
        return json.dumps({"instructions": "same", "response_schema": {},
                           "repository_snapshot": {"commit": "a" * 40}, "untrusted_context": context})

    def test_completed_turn_replays_and_followup_contains_only_context_delta(self):
        """A lost successful response replays locally; a revision appends changed keys."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = {"scope": "a" * 64, "turn": 0}
            first_prompt = self.prompt(plan="v1", stable="unchanged", obsolete=True)
            turn = SessionTurn(session, first_prompt, root)
            self.assertEqual(json.loads(turn.start()), json.loads(first_prompt))
            turn.finish({"decision": "revise"}, turn.identifier)
            turn.close()
            replay = SessionTurn(session, first_prompt, root)
            self.assertEqual(replay.cached, {"decision": "revise"})
            replay.close()
            revised = SessionTurn({**session, "turn": 1}, self.prompt(plan="v2", stable="unchanged"), root)
            delta = json.loads(revised.start())
            self.assertEqual(delta["context_update"], {"plan": "v2"})
            self.assertEqual(delta["removed_context_keys"], ["obsolete"])
            self.assertNotIn("instructions", delta)
            revised.finish({"decision": "approve"}, revised.identifier)
            revised.close()

    def test_ambiguous_turn_and_missing_transcript_fail_closed(self):
        """A partial provider turn is never silently replayed as a fresh request."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = {"scope": "a" * 64, "turn": 0}
            prompt = self.prompt(plan="v1")
            turn = SessionTurn(session, prompt, root)
            turn.start()
            turn.close()
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                SessionTurn(session, prompt, root)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "missing"):
                SessionTurn({"scope": "b" * 64, "turn": 1}, self.prompt(), Path(directory))


if __name__ == "__main__":
    unittest.main()
