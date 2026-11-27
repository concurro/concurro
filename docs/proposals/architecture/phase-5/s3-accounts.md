# S3: accounts credential store

**Implementation status (2026-11-24): complete.** This brief retains the original
work order and acceptance design. See the [Phase 5 status](../phase-5-plan.md#implementation-status)
for deployment, evidence and remaining live qualification/retirement. A listed acceptance
case is not, by itself, evidence that it ran live.

The subsequent [Google integration](../../../antigravity.md) adds `gemini-api-key`
and Sponsor-held `antigravity-subscription` credentials. Subscription login material
stays with agy on the Sponsor machine; accounts stores the exact relay binding.

Part of the [phase 5 plan](../phase-5-plan.md). Accounts gains a native credential
store. It is deployed inactive (`credential_store = "vault"`) and becomes authoritative
at S7 (`"native"`). The browser routes and their response shapes don't change.

## Ownership

**Accounts owns** credential entries, provider lists and their order, filters, policy
history, versions and their metadata, relay records and intents, and the change feed.

**The task application's funding module keeps** consent, selection, exclusions, grants,
budgets and accounting. It reads metadata through the API below and consumes the change
facts ([S6](s6-tasks.md)).

## Crates

- **`crates/accounts`:** a `credentials` module split into a vault proxy (today's code)
  and a native store, behind one trait that the browser routes call. Also a
  `credential_jobs` module (settlement, destroy, expiry) and the `import-vault`
  command.
- **`crates/accounts-postgres`:** migrations and all SQL.
- **Shared crates:**
  - `concurro-secrets` for OpenBao.
  - `concurro-events-postgres` and `concurro-events-nats` for the outbox, forwarder and
    the `inference.usage` consumer.
  - `graphile_worker`, which accounts already runs, for jobs.
- **`hosted-api`** from the relay release, for owner API v1.

## Schema

These tables mirror the vault's model (`gateway/vault/vault.py:246-302`) with the same
identities:

| Table | Key | Holds |
| ---- | ---- | ---- |
| `credential_entries` | `entry_id` | `user_id`, `provider`, `route`, `name`, `enabled`, `roles`, `profiles`, `revision`, `created`, `updated`, `removed` |
| `credential_lists` | `(user_id, provider)` | `revision` and the ordered live entry IDs |
| `credential_policy` | `(user_id, provider, revision)` | `at` (null means always in force, as the vault's legacy revision 0) and the list's policy document |
| `credential_versions` | `record_id` | `user_id`, `route`, `version`, `entry_id`, `status`, `status_reason`, `kind`, `custody`, `relay_id`, `fingerprint`, `account_label`, `verified_account`, `expires_at`, `expiry_known`, `created`, `updated`, `retired`, `last_used`, `activated`, and `material`: `pending`, `importing`, `stored`, `failed`, `superseded`, `destroying`, `destroyed` or `none` (relay); `mutation_id`, and the expected entry revision (replace) or the new entry's policy (add) while pending |
| `credential_mutations` | `(user_id, mutation_id)` | A digest of the request's nonsecret fields and the response, which is null while the mutation's version is pending. Nothing derived from secret material is stored |
| `relay_intents` | `(user_id, route, version)` | Reserved bindings before an owner approval |
| `vault_imports` | SHA-256 of the vault copy | Import progress ([below](#import)); each version's material progress is its `material` (`importing`, then `stored`) |

Plus the shared outbox and consumer tables.

Version numbers increase per user and route across entries and pending relay
reservations, as in the vault (`vault.py:570-578`). `record_id` stays
`sha256("{user}/{route}/{version}")`.

## Writes

Each browser write keeps today's revision checks (`409` stale, `410` removed) and
`mutation_id` idempotency. In one transaction it changes the entry, list and policy
history, and appends `credential.<user_id>.changed`. Material crosses to OpenBao
around that transaction.

- **Add or replace a secret:**
  1. Insert the new version as `material = pending`, recording its `mutation_id` and
     what it expects of its entry. The entry, its list and the policy history don't
     change yet.
     - **For a replace,** that is the entry's current revision.
     - **For an add,** a new `entry_id` that doesn't exist yet. Nothing about the entry
       is written until activation, so a failed add leaves nothing visible.
  2. `create` it in OpenBao at `personal/<record_id>` with `cas=0`.
  3. **Activate,** in one transaction:
     - **An add** inserts the entry, appends it to its provider list, and writes the new
       list and policy revisions. If the `entry_id` already exists, it's a replay of the
       same mutation, and answers with its first result.
     - **A replace** requires the entry to still exist at the expected revision, with no
       other version activated for it meanwhile. It bumps the entry, list and policy
       revisions, then retires the previous version (`status_reason = replaced`) and
       queues its destroy job.
     - **Both** mark the version `stored` and `activated`, and append the change.

     A replace whose check fails marks the version `superseded`, queues its destroy
     job, and answers the mutation with `409`, as a stale revision is answered today.
  4. Answer the browser.
- **Pending settlement.** A job settles any version left `pending` for two minutes:
  - The OpenBao metadata exists: activate it under the same rule, or supersede it.
  - It doesn't: mark it `failed`. A retry with the same `mutation_id` and the secret
    runs the write again.
- **Only activated versions are visible.**
  - A version is `activated` once it reaches `stored`, or is committed as a relay
    version. Every imported vault version counts as activated, since the vault listed
    them.
  - Versions that never activated (`pending`, `failed`, `superseded`) are never listed,
    selected or named in a change fact.
  - Activated versions keep their metadata whatever later happens to their material.
    A replaced, removed or expired version whose material is `destroying` or
    `destroyed` still answers as-of reads with its historical status, and is named in
    the `revoked` list of the fact that retired it.
  - Current selection is unchanged: it still needs `status = valid`, which a retired
    version never has.
- **Edit, enable, disable or reorder:** metadata only, with a new policy revision.
- **Remove:** retire every version of the entry (`status = revoked`), tombstone the
  entry, queue the destroy jobs and append the change.
- **Destroy job:** `destroy` the path, then delete its metadata, retried until OpenBao
  confirms. An absent path counts as done. The version becomes `destroyed`.
- **Fingerprint and account,** computed at write from the plaintext as the vault does
  (`vault.py:80`), and stored as metadata. The plaintext exists only in the request.
- **Relay approval and device replacement:**
  1. Reserve versions in `relay_intents`.
  2. Call the verifier's `POST /owner/v1/approvals`, which is idempotent for identical
     bindings.
  3. Commit the entries and the relay-custody versions (`material = none`), and delete
     the intents.

  A job settles leftover intents by repeating the identical approval, then committing
  or releasing them.
- **Relay status:** `GET /owner/v1/relays?user_id=`, shaped as today.

## Metadata API

`GET /v1/credentials/{user_id}?at=` on accounts' internal listener, authorized by the
task application's own bearer ([contract](../../contracts.md#credentials)).

- **The response is the vault's `/metadata/{user_id}?at=` (`vault.py:1086-1165`), plus
  `relay_id` on relay-custody versions.** The vault omits `relay_id` today
  (`vault.py:1136`), because its resolver supplied it later, and the resolver retires.
  - The task application's client (`crates/vault-v1/src/lib.rs:69`) gains `custody` and
    `relay_id`.
  - Selection freezes both into the grant's credential, so the exchange can sign a relay
    claim without asking anyone. Custody is normalized there: the metadata's `vault`
    is the grant's `secret_store`.
  - Otherwise `Metadata::select` is unchanged.
- **The response's contents:**
  - `credentials` holds each entry's latest version created at or before `at`, with the
    policy fields from the snapshot in force at `at`.
  - `lists` holds `{provider: {revision, entries}}`.
  - `history_complete` is false only if a version's status at `at` can't be known.
- **Status rules are ported exactly:**
  - With `at`, a retired version is `valid` before it was retired and `revoked` after.
  - A known expiry at or before the cutoff is `invalid`.
  - Removed entries are listed with `enabled = false` and `removed = true`.

## Change facts

`credential.<user_id>.changed`, appended in the transaction of each committed change and
forwarded to accounts' own stream ([contract](../../contracts.md#credentials)):

| Operation | `change` | `revoked` |
| ---- | ---- | ---- |
| Add, approve a relay | `added` | — |
| Replace a secret, replace a device | `replaced` | The previous versions |
| Rename, filters, enable, disable | `edited` | — |
| Reorder | `reordered` | — |
| Remove | `removed` | Every version of the entry |
| Expiry | `unusable` | The expired version |

## Expiry and last use

- **Expiry job.** Hourly, every `valid` version with a known `expires_at` that has
  passed becomes `invalid`, with `status_reason = expired`. Its material is destroyed
  and an `unusable` change is appended. This replaces the vault's expiry at resolve
  time (`vault.py:1209-1225`).
- **`last_used`** comes from `inference.usage`. A durable consumer sets it to the
  latest request time for the fact's `record_id`. This replaces the vault's write at
  resolve time.

## Import

`accounts import-vault --vault-db <copy> --master-key <file> [--keyring <file>] --capture
<file> --migrator-role-id <id> --migrator-secret-id-file <file>` runs in S7's window,
against a copy of the stopped vault's database. The key arguments are the vault's
`VAULT_MASTER_KEY_FILE` and `VAULT_KEYRING_FILE`.

1. **Preconditions.** It refuses to start if the copy has a relay intent, or if
   accounts' native tables already hold data from another import.
2. **Metadata, in one transaction:**
   - all entries, lists, policy revisions (`at = ""` becomes null) and versions, retired
     and revoked ones included;
   - `material` set to `importing` where ciphertext exists, `destroyed` where it was
     nulled, and `none` for relay custody.

   The mutation journal isn't imported: its digests include plaintext, and writes are
   paused. Neither are vault events: the change feed starts empty, and no consumer
   needs vault history.
3. **Material, resumable.** For each `importing` version:
   - decrypt it with the keyring, handling envelope formats 1 and 2, and check its
     fingerprint against the stored one;
   - `create` it in OpenBao with the `migrator` policy and `cas=0`;
   - read it back, compare, and mark it `stored`.

   An existing path is read back and compared instead of written. A mismatch stops the
   import and names the `record_id`.
4. **Verification.**
   - **The capture file is recorded after writes pause and before the vault stops,** by
     `accounts capture-vault`: the live vault's `/metadata/{user_id}?at=` for every user
     in the database copy, at every policy revision time, version creation and
     retirement, and at the current time.
   - Accounts' metadata API must answer each of those reads identically, with
     `user_id` a string as the vault returns it, and `relay_id` left out. Current reads
     are answered as of the capture's time.
   - Counts per table must match.
   - The report holds IDs, counts and pass or fail, and never secret material or
     digests of it.

Once the report is clean, the `migrator` AppRole is deleted and its tokens revoked
([platform guide](../../../platform.md#openbao)). Deleting the role alone would leave
issued tokens working.

## Acceptance

- **Writes,** against PostgreSQL and a real OpenBao in tests:
  - add, replace, edit, reorder and remove produce today's browser responses
    (phase 4 fixtures);
  - the policy history and version rows are right;
  - one change fact is appended per operation, with the right `revoked` list.
- **Overlapping writes:**
  - two replaces of one entry, and a replace racing a remove: one activates, the other
    is superseded and its material destroyed;
  - a create delayed until after the entry was removed is superseded, never listed;
  - an add whose OpenBao write fails leaves no entry, and its retry with the same
    `mutation_id` adds it once;
  - pending, failed and superseded versions never appear in the metadata API;
  - a replaced version whose material is destroyed still answers an as-of read before
    the replacement with its original status.
- **Relay identity:** a relay version's `relay_id` is in the metadata API, and the task
  application's selection carries it into the grant.
- **Crash recovery:**
  - a crash after the OpenBao create but before `stored` settles to `stored`, with one
    fact;
  - a crash before the create settles to `failed`, and the retry succeeds;
  - a destroy job retried after OpenBao comes back destroys and deletes the metadata.
- **No read-back:** with only the `accounts` policy, the service can't read
  `data/personal/*`. No log line or response contains a submitted secret.
- **As-of parity:** a recorded vault database, imported, answers recorded vault metadata
  reads identically at every policy revision. The cases include legacy revision 0,
  retired, revoked and expired versions, removed entries, and relay custody.
- **Import:** it resumes after an interruption mid-material, a fingerprint mismatch
  stops it, and running it twice adds nothing.
- **Relay:** approval and device replacement through a fake verifier speaking owner
  API v1; a leftover intent settles after a crash.
- **Expiry and last use:** a version past its expiry becomes `invalid`, its material is
  destroyed and an `unusable` fact appended. An `inference.usage` fact updates
  `last_used` once, however often it's redelivered.
