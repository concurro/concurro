# Personal credential vault

The owner API stores API keys and pasted CLI subscription tokens for personal sponsorship.
Saving does not contact the provider, discover models or run inference. Token and model-access
problems are reported during execution. ChatGPT tokens require their supplied account ID;
other routes require only the secret. No CLI login, provider refresh or remote provider revoke
runs in this service. See [phase 5](../../docs/proposals/per-user-credentials/phase-5.md).

## Entries, records and events

SQLite schema 6 keeps each owner's credentials as **entries**: a stable `entry_id`, a fixed
route, a name, an enabled flag and optional role/profile filters. Each provider has one ordered
list of its live entries; position is selection priority. An entry owns its **records**, the
immutable encrypted versions keyed `(user_id, route, version)`. Versions are allocated per
owner/route across all of that route's entries, so `record_id` and the encryption AAD stay
unique; one entry's versions need not be consecutive.

Claude and ChatGPT subscription entries can hold relay custody. Their immutable versions have
`custody: relay` and an approved relay ID, without ciphertext or account/token
material. It participates in the same ordered list, filters, retirement events
and exact admission binding as a vault-backed entry. A relay record is usable
only while its version is current and unretired; the Rust verifier separately
requires a live authenticated link. Existing databases gain relay metadata and
route reservations in forward transactions without changing ciphertext or version identities.

Replacing an entry's secret atomically retires its prior version, destroys that material and
emits `retired`/`stored` events; other entries of the same route are untouched. Saving a
nonworking replacement still retires the prior version. Removing an entry tombstones it,
revokes its latest record and destroys its material; a removed entry cannot be written again,
and a later save creates a new entry. Destruction is logical; encrypted backups/WAL may retain
old bytes and must remain protected with their keys.

Every entry write advances the entry's revision and its provider list's revision, records the
list's policy (order, names, enabled flags and filters) at that revision and appends a `policy`
event. Policy events carry `entry_id`, `provider`, the list `revision` and a reason (`added`,
`edited`, `enabled`, `disabled`, `replaced`, `reordered`, `removed`); their `record_id` is empty
and `version` is 0. They never revoke anything: Exchange advances past them. Record events
(`stored`, `validated`, `snapshot`, `retired`, `revoked`, `unusable`, `rewrapped`) also name
their `entry_id` and `provider`.

Public owner responses include fingerprint, version, update time, known/unknown expiry,
status reason and last resolution time. Known expiry makes metadata unusable immediately; private
resolution commits destruction and one `unusable` event before denying. Unknown expiry stays
unknown. Writes affect personal records only; current org sessions are untouched.

New records have `status_reason: stored`, with no validation timestamp or model coverage.
The legacy `valid` status means stored and usable, not provider-verified. Historical validation
fields remain readable. Consumers accept both `stored` and historical `validated` events;
either records an available replacement for recovery.
A saved version, like any list change, wakes that owner's eligible paused tasks to select
again ([fallback and recovery](../../docs/design/service.md#credential-fallback-and-recovery)).
Even a mistyped replacement consumes a model-call attempt if execution starts and fails;
provider billing depends on the response. Failed versions are not retried automatically again.
See [save-only rollout](../README.md#save-only-credential-rollout) for component ordering.

## Owner API

Owner requests carry the owner's GitHub user token. Role filters are role keys; profile filters
are `{target, profile}` with the target's canonical upstream repository. Empty filters match
all of the provider's work.

| Request | Body | Result |
| ---- | ---- | ---- |
| `GET /credentials` | | `{providers: [{provider, revision, entries}], types}` |
| `POST /credentials` | `{route, name, secret, account_id?, roles?, profiles?, mutation_id}` | The new entry, appended and enabled |
| `POST /credentials/relay` | `{code, name, routes?, roles?, profiles?, mutation_id}` | Approves the Sponsor's exact route set and appends one relay custody entry per route |
| `PATCH /credentials/{entry_id}` | `{revision, mutation_id, name?, enabled?, roles?, profiles?}` | The entry; material and version unchanged |
| `PUT /credentials/{entry_id}/secret` | `{revision, secret, account_id?, mutation_id}` | The entry with its new record |
| `PUT /credentials/{entry_id}/relay` | `{revision, code, mutation_id}` | Replaces that entry's record with a newly approved relay |
| `PUT /credentials/order/{provider}` | `{revision, entries, mutation_id}` | The reordered list |
| `DELETE /credentials/{entry_id}?revision=N` | | The tombstoned entry; repeating it returns the same |

An entry carries `entry_id`, `provider`, `route`, `kind`, `name`, `enabled`, `roles`,
`profiles`, `revision`, `position`, timestamps and `record`, its current version's nonsecret
metadata. `revision` in a write is the entry's last-read revision, or the list's for ordering;
a mismatch answers 409, and a write to a removed entry 410. Removal requires it too, so a
removal cannot destroy a version saved after the owner last read the entry. A write to one entry never
conflicts with a write to another. `entries` must be exactly the list's current entry IDs.

`mutation_id` (16–64 URL-safe characters, scoped to the owner) makes a write replay-safe:
repeating it with the same request returns the first result without a second entry or
version, and reusing it for a different request answers 409. The journal stores a request
digest and the nonsecret result only.

Relay approval requires `VAULT_VERIFIER_URL` and
`VAULT_VERIFIER_TOKEN_FILE`. The owner API resolves the caller's GitHub
identity first, durably reserves exact entry/versions, and calls the Rust
verifier's private `POST /approve` with the complete binding set and displayed code.
The same code can approve only that set. If the private call fails, the
Vault write rolls back; no incomplete relay record enters the list. A mixed
approval commits both route entries in one transaction. The
verifier persists pending and approved registrations across restarts.

## Metadata

`GET /metadata/{user_id}[?at=...]` returns, with the service bearer only, each entry's current
record as of now or `at`, joined with the policy in force then: `entry_id`, `provider`, `name`,
`enabled`, `roles`, `profiles`, `position` and `removed`, plus `lists` with each provider's
revision and ordered entry IDs. Responses exclude fingerprints, account headers and secrets.
List revision 0 is the state adopted from schema 3: a boundary before the migration reads
every adopted entry as enabled and unfiltered, with record status from the retained
per-version evidence. Unknown legacy evidence stays unknown.

## Migrating from schema 3

Opening a schema 2 or 3 database adopts it in one transaction: each existing owner/route
becomes one entry with a deterministic ID that owns all of that route's versions. Ciphertext,
AAD, record identities, token revisions, timestamps, retirement evidence and event
sequences are unchanged, no event is appended and no token needs re-entry. Routes whose latest
record was revoked become tombstones; expired material stays expired. The former per-route
write fence becomes the entry's starting revision. A failed migration rolls back completely;
reopening a migrated database changes nothing.

Each provider list starts with subscriptions before API keys unless the optional manifest
named by `VAULT_ORDER_MANIFEST_FILE` says otherwise:

```json
{"format": 1, "owners": {"<user id>": {"<provider>": {"first": "api_key", "evidence": "agreement"}}}}
```

The service CLI of this release produces it before the vault migrates. It reads the registry
database directly and read-only, without the daemon, its lock or a registry migration, so it
runs from the new image against the deployed state directory while the earlier daemon is
still installed:

```bash
python3 -m service --settings <settings file> credential-order-manifest > manifest.json
```

For every active personal grant on an unfinished task or issue it records the credential types
that task's effective configuration selects for the provider, with the repository, issue and pinned
configuration commit as sources. Selections that all name one type give `first` with
`agreement`; differing types give `conflict`, and any grant without route evidence makes the
provider `unavailable`. Only `agreement` changes the default order.
Org-funded providers, withdrawn grants and finished tasks are not preferences.

`first` is `api_key` or `subscription`; any other value keeps the default. The file is opened
only while adopting schema 3 records: later starts neither read it nor need it to exist. The vault
prints one `adopted_lists` JSON line naming each owner/provider order, whether it came from
the manifest or the default, and the manifest's evidence.

## Service metadata events

`GET /metadata/events?after=N` exposes the existing paginated nonsecret lifecycle log on
the metadata listener using only `VAULT_SERVICE_TOKEN_FILE`. Owner/GitHub and gateway
bearers are refused without identity lookup. The service stores its own cursor and latest
saved-version evidence; it never receives the resolver token. Private `/events` remains available
to Exchange. See [phase 6](../../docs/proposals/per-user-credentials/phase-6.md).

## Optional private listener

Existing configuration starts only the public owner/metadata listener on 8090. To expose the
separate private API, provision an owner-only `VAULT_RESOLVER_TOKEN_FILE` distinct from
`VAULT_SERVICE_TOKEN_FILE`, and set `VAULT_RESOLVER_LISTEN` (default `127.0.0.1:8091`).
`compose.private.yaml` adds an internal network shared only by vault and gateway/Exchange;
the resolver binds the vault's private network alias. Do not bind it to the edge interface or
publish a port. There is no Caddy route. The listener dispatch uses the accepted socket port,
never client Host or forwarded headers. Private bearers are refused on public owner routes
before GitHub identity lookup.

| Private operation | Contract |
| ---- | ---- |
| `POST /resolve` | `{user_id: integer, route, record_id, version: integer}` identifies exactly one usable record; no latest-version selection or fallback. Returns token/account metadata for vault custody or only relay/entry IDs for relay custody, with `Cache-Control: no-store`. Only the gateway principal may call it. |
| `GET /events?after=N` | At most 100 immutable nonsecret events, `next` and `more`; consumers persist acknowledgments after durable processing. Replaying a cursor is safe. |

This listener does not establish an admitted session's authority. The configured Exchange
binds exact selection to authenticated sessions and consumes retirement events. Its private
inference hook resolves token/account metadata for each request. ChatGPT's sealed account map
uses `chatgpt_account_id`; the hook extracts only that header. Missing/malformed account
material fails closed without operator defaults. The metadata principal cannot resolve.

## Wrapping-key rotation and recovery

Keep keys outside the checkout, owner-only regular files, 64 hex characters (32 bytes).
Existing `VAULT_MASTER_KEY_FILE` remains the `legacy` key. Optional `VAULT_KEYRING_FILE` is an
owner-only JSON file with `active` and `keys` mapping IDs to separately mounted key paths:

```json
{"active":"new","keys":{"legacy":"/run/secrets/vault-master-key","new":"/run/secrets/vault-next-key"}}
```

Stop the vault, back up SQLite consistently with its WAL and escrow keys separately. With all
old keys retained in the keyring, run the vault image's entrypoint with `--rotate`, then restart
normally with that keyring. Rotation atomically rewraps data keys under the active key, keeps
provider ciphertext/version/AAD unchanged and emits `rewrapped` events. A missing old key or
failure rolls the transaction back; repeating completed rotation is a no-op. Retain old keys for
backups; removing them can make those backups unreadable.

Recovery is roll-forward only. Corrective releases retain support for the current schema
and named-key envelopes; deployment rollback and downgrade conversion are not required.
Keep the database and keyring together through recovery, preserving newer writes and
retirement evidence. Transaction rollback on a failed rotation remains ordinary atomicity,
not a deployment recovery strategy.
