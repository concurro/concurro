# Personal credential vault

The owner API validates API keys and pasted CLI subscription tokens for personal sponsorship.
Configured contributed tasks resolve exact admitted versions; org-funded tasks retain org
credentials. Both subscription PUTs now accept an explicit selected model set; ChatGPT also
requires its account header. See [phase 5](../../docs/proposals/per-user-credentials/phase-5.md).
No CLI login, provider refresh or remote provider revoke runs in this service.

## Records and events

SQLite schema 3 retains lifecycle metadata and a monotonic, paginated nonsecret event log to the
existing owner/route/version rows. Migration preserves original ciphertext/AAD and unknown
legacy model/validation evidence. Each accepted replacement atomically retires the prior version,
destroys its usable material and emits retirement/validation events. Rejected validation leaves
that version unchanged. Public revoke events are idempotent. A private owner/route revision advances on every revoke,
including an empty slot, and accepted replacement. Candidate validation captures that revision
before probing and CAS checks it in the replacement transaction. A delayed candidate cannot
undo a concurrent replacement/revoke. Schema 2 migrates forward without changing records or
public events; no version-zero events are introduced. Destruction is logical; encrypted backups/WAL may
retain old bytes and must remain protected with their keys.

API-key model discovery follows provider cursor pages; incomplete/malformed or cyclic
pagination remains unknown coverage rather than a truncated model list. Anthropic's
[model-list contract](https://platform.claude.com/docs/en/api/models/list) supplies the cursor
and upstream page-size maximum; this does not impose a model-count cap.

Public owner responses include fingerprint, version, validated models, known/unknown expiry,
status reason and last resolution time. Metadata service responses exclude fingerprints,
account headers and secrets. Known expiry makes metadata unusable immediately; private
resolution commits destruction and one `unusable` event before denying. Unknown expiry stays
unknown. Replacement and revoke affect personal records only; current org sessions are untouched.

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
| `POST /resolve` | `{user_id: integer, route, record_id, version: integer}` identifies exactly one usable record; no latest-version selection or fallback. Returns token/account metadata with `Cache-Control: no-store`. Only the gateway principal may call it. |
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
