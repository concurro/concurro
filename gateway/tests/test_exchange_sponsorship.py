"""Provider fence receipts and cutoffs are durable while organization work is unchanged."""

import hashlib
import json
import unittest

from exchange_fixtures import AUDIENCE, BINDING, ExchangeFixture

from exchange import Exchange, Refused


def transition(provider="openai", expected=-1, epoch=1, actor="101", reason="initialize", cutoff=None, generation=1):
    """Build a deterministic caller/provider-specific operator transition."""
    body = {
        "repository": BINDING["repository"],
        "issue": 7,
        "provider": provider,
        "generation": generation,
        "expected_epoch": expected,
        "slot": {
            "epoch": epoch,
            "sponsor_id": actor,
            "status": "active" if actor else "withdrawn",
            "grant_id": "a" * 64,
        },
        "reason": reason,
        "actor": actor,
        "cutoff": cutoff,
    }
    body["transition"] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return body


class SponsorshipTests(ExchangeFixture, unittest.TestCase):
    """Use the real lifecycle lock and SQLite transaction across restart/replay/conflicts."""

    def test_replay_conflict_provider_isolation_and_no_aba(self):
        """Replay is exact, independent providers retain epochs, and withdrawal never resets identity."""
        first = transition()
        reply = self.exchange.sponsorship(first)
        self.assertEqual(self.exchange.sponsorship(first), reply)
        other = transition("anthropic", actor="202")
        self.exchange.sponsorship(other)
        move = transition(expected=1, epoch=2, actor="303", reason="sponsor")
        self.exchange.sponsorship(move)
        rows = self.exchange.db.execute("SELECT provider,epoch FROM sponsorship ORDER BY provider").fetchall()
        self.assertEqual(rows, [("anthropic", 1), ("openai", 2)])
        with self.assertRaises(Refused):
            self.exchange.sponsorship({**first, "slot": move["slot"]})
        with self.assertRaises(Refused):
            self.exchange.sponsorship(transition(expected=1, epoch=2, actor="404", reason="sponsor"))
        withdraw = transition(expected=2, epoch=3, actor=None, reason="withdraw", cutoff=2)
        withdraw["actor"] = "303"
        self.exchange.sponsorship(withdraw)
        self.exchange.sponsorship(transition(expected=3, epoch=4, actor="303", reason="sponsor"))
        self.assertEqual(
            self.exchange.db.execute("SELECT epoch FROM sponsorship WHERE provider='openai'").fetchone()[0], 4
        )

    def test_permission_batch_is_atomic_and_old_owner_withdraw_preserves_current_slot(self):
        """A failed member rolls back the whole task/user permission-loss publication."""
        self.exchange.sponsorship(transition())
        self.exchange.sponsorship(transition("anthropic"))
        one = transition(expected=1, epoch=2, actor=None, reason="permission_loss", cutoff=1)
        one["actor"] = "101"
        two = transition("anthropic", expected=99, epoch=100, actor=None, reason="permission_loss", cutoff=1)
        two["actor"] = "101"
        with self.assertRaises(Refused):
            self.exchange.sponsorship({"transitions": [one, two]})
        self.assertEqual(self.exchange.db.execute("SELECT count(*) FROM sponsorship_revocations").fetchone()[0], 0)
        previous = self.exchange.db.execute("SELECT slot FROM sponsorship WHERE provider='openai'").fetchone()[0]
        old = transition(expected=1, epoch=1, reason="withdraw", cutoff=1)
        old["actor"] = "999"
        self.exchange.sponsorship(old)
        self.assertEqual(
            self.exchange.db.execute("SELECT slot FROM sponsorship WHERE provider='openai'").fetchone()[0], previous
        )

    def test_org_key_and_turn_survive_prospective_withdrawal_and_restart(self):
        """Shadow consent changes cannot retire a session whose actual payer is the org."""
        self.mint()
        self.exchange.sponsorship(transition())
        value = transition(expected=1, epoch=2, actor=None, reason="withdraw", cutoff=1)
        value["actor"] = "101"
        self.exchange.sponsorship(value)
        self.assertIsNone(self.exchange.row(self.session)["ended"])
        self.assertTrue(self.gateway.list())
        reopened = Exchange(self.exchange.lockfile.removesuffix(".lock"), self.gateway, self.exchange.keys, AUDIENCE)
        try:
            self.assertEqual(reopened.sponsorship(value), self.exchange.sponsorship(value))
        finally:
            reopened.db.close()
