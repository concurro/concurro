"""Record a vault history for the native credential store's parity tests.

Run from the repository root with the vault's pinned requirements installed:

    python crates/accounts/tests/record_vault_history.py crates/accounts/tests/vault_history

It writes, with synthetic secrets and a fixed clock:

- `vault.db` and `master.key`: a vault that adopted schema 3 records, then took owner
  writes, relay approvals and an expiry, for `accounts import-vault`;
- `capture.json`: the vault's `/metadata/{user_id}?at=` answers at every recorded instant
  and now, as the import's capture file, plus each owner's listing;
- `writes.json`: a scripted sequence of owner writes on a fresh vault, each with its
  clock, request and answer, which the native store replays.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "gateway" / "vault"))
sys.path.insert(0, str(ROOT / "gateway" / "tests"))

import vault  # noqa: E402
from vault import Envelope, Store, User  # noqa: E402
from vault_fixtures import legacy_vault  # noqa: E402

MASTER = bytes(range(32))
START = 1_800_000_000.0


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now

    def tick(self, seconds=60):
        self.now += seconds


def instant(seconds):
    return vault.seconds(seconds)


def history(directory: Path, clock: Clock) -> None:
    """A vault with legacy adoption, owner writes, relay custody and an expiry."""
    path = directory / "vault.db"
    path.unlink(missing_ok=True)
    envelope = Envelope(MASTER)
    alice, bob, carol = User(101, "alice"), User(202, "bob"), User(303, "carol")
    legacy_vault(
        path,
        envelope,
        [
            (101, "anthropic-api-key", 1, None, "valid", instant(START - 7200)),
            (101, "anthropic-api-key", 2, "sk-ant-legacy-secret-0002", "valid", instant(START - 3600)),
            (101, "openai-api-key", 1, "sk-proj-legacy-secret-0001", "revoked", instant(START - 3000)),
            (202, "claude-subscription", 1, "sk-ant-oat-legacy-0001", "valid", instant(START - 1800)),
        ],
    )
    store = Store(path, envelope, clock=clock)
    clock.tick()
    adopted = {e["route"]: e for g in store.listing(101)["providers"] for e in g["entries"]}
    replaced = store.replace(
        alice, adopted["anthropic-api-key"]["entry_id"], "sk-ant-replaced-0003",
        revision=adopted["anthropic-api-key"]["revision"], mutation="history-replace-0001",
    )
    clock.tick()
    chatgpt = store.add(
        alice, "chatgpt-subscription", "chatgpt-access-token-0001", name="Work ChatGPT",
        roles=["implementer"], profiles=[{"target": "acme/demo", "profile": "gpt-high"}],
        mutation="history-add-chatgpt-01", account={"chatgpt_account_id": "acct-0001"},
        account_label="Supplied ChatGPT account " + vault.digest("acct-0001")[:8],
    )
    clock.tick()
    expiring = store.add(
        alice, "claude-subscription", "sk-ant-oat-expiring-0001", name="Expiring",
        mutation="history-add-expiring", expires_at=instant(clock.now + 600), expiry_known=True,
    )
    clock.tick()
    store.add(
        alice, "openai-api-key", "sk-proj-expired-unread-0001", name="Expires unread",
        mutation="history-add-unread-01", expires_at=instant(clock.now + 300), expiry_known=True,
    )
    clock.tick()
    store.edit(101, chatgpt["entry_id"], revision=chatgpt["revision"], mutation="history-edit-00001",
               name="Team ChatGPT", enabled=False)
    clock.tick()
    group = store.group(101, "anthropic")
    store.reorder(101, "anthropic", [e["entry_id"] for e in reversed(group["entries"])],
                  revision=group["revision"], mutation="history-reorder-0001")
    clock.tick(900)
    # The resolver expires material at read time; the native store's expiry job does now.
    try:
        store.resolve(101, "claude-subscription", expiring["record"]["record_id"], expiring["record"]["version"])
    except vault.VaultError as error:
        assert error.status == 410
    else:
        raise AssertionError("the expired version resolved")
    clock.tick()
    current = next(e for g in store.listing(101)["providers"] for e in g["entries"]
                   if e["entry_id"] == replaced["entry_id"])
    store.remove(101, current["entry_id"], current["revision"])
    clock.tick()
    store.add(bob, "openai-api-key", "sk-proj-bob-0001", name="Bob", mutation="history-add-bob-0001")
    clock.tick()
    approve = lambda code, binding: "relay-0001"  # noqa: E731
    approved = store.add_relay(carol, "BCDF-GHJK", approve, name="Desk", roles=[], profiles=[],
                               mutation="history-relay-00001", routes=["claude-subscription", "chatgpt-subscription"])
    clock.tick()
    claude = approved["entries"]["claude-subscription"]
    store.replace_relay(carol, claude["entry_id"], "LMNP-QRST", lambda code, binding: "relay-0002",
                        revision=claude["revision"], mutation="history-relay-00002")
    clock.tick()
    reads, listings = [], []
    for user_id in (101, 202, 303):
        moments = {row[0] for row in store.db.execute(
            "SELECT at FROM credential_policy WHERE user_id=? AND at<>''", (user_id,))}
        moments |= {row[0] for row in store.db.execute(
            "SELECT created FROM credentials WHERE user_id=?", (user_id,))}
        moments |= {row[0] for row in store.db.execute(
            "SELECT retired FROM credential_metadata WHERE user_id=? AND retired IS NOT NULL", (user_id,))}
        for at in sorted(moments) + [instant(START - 10_000)]:
            reads.append({"user_id": user_id, "at": at, "response": store.metadata(user_id, at)})
        reads.append({"user_id": user_id, "at": None, "response": store.metadata(user_id)})
        listings.append({"user_id": user_id, "listing": store.listing(user_id)})
    store.close()
    (directory / "master.key").write_text(MASTER.hex() + "\n")
    capture = {"now": instant(clock.now), "reads": reads, "listings": listings}
    (directory / "capture.json").write_text(json.dumps(capture, indent=1, sort_keys=True) + "\n")


def writes(directory: Path, clock: Clock) -> None:
    """Owner writes on a fresh vault, each answer recorded for the native store's replay."""
    path = directory / "writes.db"
    path.unlink(missing_ok=True)
    store = Store(path, Envelope(MASTER), clock=clock)
    alice = User(101, "alice")
    steps = []

    def step(operation, call, **request):
        clock.tick()
        try:
            response, status = call(), 200
        except vault.VaultError as error:
            response, status = {"error": error.reason}, error.status
        steps.append({"at": instant(clock.now), "operation": operation, "request": request,
                      "status": status, "response": response})
        return response

    first = step("add", lambda: store.add(alice, "anthropic-api-key", "sk-ant-api03-firstAAAA", name="First",
                 roles=["triage"], mutation="writes-add-first-01"),
                 route="anthropic-api-key", name="First", roles=["triage"], profiles=[],
                 secret="sk-ant-api03-firstAAAA", mutation_id="writes-add-first-01")
    second = step("add", lambda: store.add(alice, "claude-subscription", "sk-ant-oat01-secondBBBB",
                  name="Second", mutation="writes-add-second-1"),
                  route="claude-subscription", name="Second", roles=[], profiles=[],
                  secret="sk-ant-oat01-secondBBBB", mutation_id="writes-add-second-1")
    step("add", lambda: store.add(alice, "chatgpt-subscription", "chatgpt-token-CCCC", name="Third",
         profiles=[{"target": "acme/demo", "profile": "gpt-high"}], mutation="writes-add-third-01",
         account={"chatgpt_account_id": "acct-1"},
         account_label="Supplied ChatGPT account " + vault.digest("acct-1")[:8]),
         route="chatgpt-subscription", name="Third", roles=[],
         profiles=[{"target": "acme/demo", "profile": "gpt-high"}], secret="chatgpt-token-CCCC",
         account_id="acct-1", mutation_id="writes-add-third-01")
    replaced = step("replace", lambda: store.replace(alice, first["entry_id"], "sk-ant-api03-replacedDDDD",
                    revision=first["revision"], mutation="writes-replace-0001"),
                    entry=0, revision=first["revision"], secret="sk-ant-api03-replacedDDDD",
                    mutation_id="writes-replace-0001")
    step("replace", lambda: store.replace(alice, first["entry_id"], "sk-ant-api03-staleEEEE",
         revision=first["revision"], mutation="writes-replace-0002"),
         entry=0, revision=first["revision"], secret="sk-ant-api03-staleEEEE",
         mutation_id="writes-replace-0002")
    edited = step("edit", lambda: store.edit(101, second["entry_id"], revision=second["revision"],
                  mutation="writes-edit-000001", name="Renamed", enabled=False, roles=["planner"]),
                  entry=1, revision=second["revision"], name="Renamed", enabled=False, roles=["planner"],
                  mutation_id="writes-edit-000001")
    group = store.group(101, "anthropic")
    order = [e["entry_id"] for e in reversed(group["entries"])]
    step("reorder", lambda: store.reorder(101, "anthropic", order, revision=group["revision"],
         mutation="writes-reorder-0001"),
         provider="anthropic", entries=[1, 0], revision=group["revision"], mutation_id="writes-reorder-0001")
    step("remove", lambda: store.remove(101, replaced["entry_id"], replaced["revision"]),
         entry=0, revision=replaced["revision"])
    step("remove", lambda: store.remove(101, replaced["entry_id"], replaced["revision"]),
         entry=0, revision=replaced["revision"])
    step("edit", lambda: store.edit(101, edited["entry_id"], revision=edited["revision"] - 1,
         mutation="writes-edit-000002", name="Stale"),
         entry=1, revision=edited["revision"] - 1, name="Stale", mutation_id="writes-edit-000002")
    step("listing", lambda: store.listing(101))
    reads = [{"at": at, "response": store.metadata(101, at)} for at in
             sorted({s["at"] for s in steps}) + [None]]
    store.close()
    path.unlink()
    (directory / "writes.json").write_text(
        json.dumps({"steps": steps, "reads": reads}, indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    history(out, Clock())
    writes(out, Clock())
