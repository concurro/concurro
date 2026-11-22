# Phase 5 S3 evidence

[S3](proposals/architecture/phase-5/s3-accounts.md) of the
[phase 5 plan](proposals/architecture/phase-5-plan.md): the accounts service's native
credential store, its metadata API and change facts, and the vault import. This is
local implementation evidence. Nothing was deployed, and no live credential was used.

## Delivered

| Item | Where |
| ---- | ---- |
| Schema and SQL | `crates/accounts-postgres`: migration `0002_credentials.sql`, `credentials` (store) and `vault_import` (reading a vault copy) |
| Native store | `crates/accounts/src/credentials/native.rs`: add, replace, edit, enable, disable, reorder and remove; pending activation and supersession; settlement, destroy and expiry |
| Vault-shaped answers | `credentials/view.rs`: the vault's `view`, `group`, `listing`, `record` and as-of `metadata`, ported, plus `relay_id` |
| Store selection | `credentials/mod.rs`: the browser routes decode one `Operation` and run it against the vault proxy (`vault.rs`, today's code) or the native store, chosen by `[credentials] store` |
| Jobs | `credentials/jobs.rs`: `destroy_credential_material` (Graphile Worker), settlement every 30 seconds, expiry hourly |
| Metadata API | `GET /v1/credentials/{user_id}?at=` on the internal listener, with its own bearer |
| Change facts | `credential.changed` v1 in `concurro-contracts`, with schemas, fixtures and the Python decoder. Appended to accounts' outbox in each change's transaction, forwarded to the `ACCOUNTS` stream |
| Import | `accounts capture-vault` and `accounts import-vault` (`crates/accounts/src/import.rs`) |
| Recorded vault history | `crates/accounts/tests/record_vault_history.py`, run against `gateway/vault/vault.py`, wrote `tests/vault_history/`: a vault database, its capture, and a scripted write sequence with the vault's answers |
| Docs | [Accounts service](../crates/accounts/README.md#native-credential-store), [contracts](proposals/contracts.md#credentials), [platform guide](platform.md#openbao), the S3 brief |

### Write path

1. A new secret is checked as the vault's `candidate` does, then reserved under the
   owner's advisory lock: the mutation journal is checked, a replace checks its entry's
   revision, and the version is inserted `pending` with a journal row whose response is
   null.
2. The material is created in OpenBao at `personal/<record_id>` with `cas=0`.
3. Activation, under the owner lock, in one transaction: an add inserts its entry; a
   replace requires its entry unchanged and retires the previous versions. Both mark the
   version `stored` and `activated`, write the list and policy revisions, append the
   fact and record the response. Otherwise the version is `superseded`, its destroy job
   queued and the journal row forgotten, so a retry is answered afresh (`409`).
4. A retry of a still-pending mutation settles that version first. Settlement activates
   a version OpenBao holds, and fails one it doesn't; a failed version's late create is
   superseded and destroyed.

Add entry IDs are derived from the owner and `mutation_id`, so a concurrent retry of
one add can't create two entries. The journal digest covers only nonsecret fields, so
unlike the vault, reusing a `mutation_id` with a different secret replays the first
write instead of answering `409`. Metadata and listing reads take one repeatable-read
snapshot.

## Tests

Based on `7f32afb` (S4a). `cargo nextest run` over `concurro-accounts`,
`concurro-accounts-postgres`, `concurro-contracts`, `concurro-vault-v1`,
`concurro-events-nats`, `concurro-exchange`, `concurro-exchange-client` and
`concurro-tasks`: 103 of 103 passed (the `events-nats` consumer tests need the contract
package's Python requirements). The store and import tests run against PostgreSQL 18.7
and OpenBao 2.7.3 started from `deploy/openbao/openbao.hcl`, so they use the deployed
policies. The contract package's Python tests passed.

| Test | Shows |
| ---- | ---- |
| `replay_matches_vault` | The recorded owner writes replayed through the native store answer exactly as the vault did: three adds (one with an account), a replace, a stale replace (`409`), an edit, a reorder, a remove and its repeat, a stale edit (`409`) and the listing, entry IDs aside. Metadata matches the vault's at every step time and now. Seven facts, one per committed change, with the replaced version and both removed versions in `revoked`; the reorder has no `entry_id`. The retired versions' material is destroyed |
| `overlapping_writes_supersede` | Two replaces reserved at one revision: the first activates, the second is superseded, and its retry answers `409`. A replace whose create lands after the entry's removal is superseded. Superseded versions never appear in the metadata API; their material is destroyed. Facts: added, replaced, removed |
| `crash_recovery` | An add whose create OpenBao refuses answers `503` and leaves nothing listed; its retry with the same `mutation_id` adds once, and a replay returns the same answer. A crash after the create settles to `stored`, once. A crash before the create settles to `failed`, and the retry succeeds. A refused destroy succeeds when retried |
| `expiry` | A version past its known expiry becomes `invalid` with `status_reason = expired`, its material is destroyed and one `unusable` fact appended, however often the job runs. An as-of read before the expiry still answers `valid` |
| `no_read_back` | The `accounts` client can't read material. No answer, journal response or digest contains the secret or its digest |
| `import_matches_vault` | The recorded vault is imported with an interruption: one path already holds the right material (compared, not written) and another a different value, which stops the import naming its `record_id`. The rerun resumes and passes: every recorded metadata read (26, covering legacy revision 0, retired, revoked and expired versions, a removed entry and relay custody) and every owner listing match the vault's; counts match; relay versions carry `relay_id`. A third run adds nothing. A format 2 envelope's account is copied |
| `import_refuses_inconsistent_copies` | A fingerprint mismatch stops the import, naming the `record_id` and no secret. A copy with a relay approval in progress isn't imported |
| `replay_ignores_a_changed_secret` | A retry under the same `mutation_id` with a different secret answers with the first write, adding nothing |
| `usage_advances_last_used` | Through the durable consumer path, an `inference.usage` fact sets `last_used`; its redelivery and an older fact change nothing |
| `relay_approval_and_device_replacement` | Against a fake owner API v1 verifier: pending lookup (and `404`), approval of two routes as relay-custody entries carrying the relay ID, replayed once, sent with the owner's numeric ID and login; metadata carries `relay_id`. Device replacement rebinds both entries, keeping their positions, with `replaced` facts revoking the previous relay versions. Status lists only the owner's current relay. Without a verifier, status is `unconfigured` |
| `relay_reservations_settle` | Reservations left by a verifier outage settle: an approval the verifier now grants commits, and its retry answers with it; one it refuses is released. A mutation ID a reservation holds is refused (`409`) for a different request, as the vault does |
| `native_store_activation` | Settlement and expiry run only with `store = "native"` |
| `reads_accounts_metadata_with_custody` (`concurro-vault-v1`) | The funding client reads `/v1/credentials`, keeps `custody` and `relay_id`, and builds grant credentials with `secret_store` or `relay` |
| `accounts_metadata_grants_are_admitted` (`concurro-exchange`) | Grants built from accounts metadata are admitted for both custodies; the unnormalized `vault` custody is refused with `invalid_grant` |
| `python_credential_parity`, browser and authority suites | Unchanged behavior with the vault proxy |
| Contract fixtures | `credential.changed` round-trips in Rust and Python; a mismatched owner subject and an unknown `change` are refused |

## Notes

- **Relay types** come from `concurro-relay-hosted-api` at relay commit `c1ea48b`, pinned
  by revision until the relay release is tagged.
- **The forwarder and the usage consumer** use the new shared
  `concurro_events_nats::run_producer` and `run_subscription`, the same loops the task
  application and ingress each still write out. Neither was run against NATS here.

## Review

An independent review found no important defects. Its minor findings were fixed: reads
now take one snapshot, the duplicated forwarder loop and JSON helper were shared, and
stray SQLite side files were removed.

Codex's review then asked for one correction batch, now done:

1. Native maintenance no longer runs while the vault serves the browser routes.
2. Relay approval, device replacement, status and reservation recovery use owner API v1.
3. Custody is normalized at the funding boundary (`vault` is `secret_store`), tested up to
   the exchange's grant admission.
4. `last_used` advances from `inference.usage`.

A narrow independent review of the batch found one minor defect, now fixed: a mutation
ID held by a relay reservation could be reused by a different request, leaving the
reservation's settlement retrying.
