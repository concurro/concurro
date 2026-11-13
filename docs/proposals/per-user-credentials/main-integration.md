# Sponsorship and current main integration

Pending merges combine backend `57a6442aef58c4006de34f7f0428747c0b7f379b` with
`f7a3d70b24536f6ec32524f6dba4328f870e5bb7`, and portal
`8aa752710f697ab145d4e7b2e521f03f79adf32b` with
`e3e42447742c32315c4379524ee0dfda5dd578b8`. This note supersedes App/visibility/release
instructions in the [previous final review](final-review.md) and
[readiness guide](deployment-readiness.md); their old test counts are historical.

## Integrated contracts

- Scope-free Sign-in identity owns browser refresh/logout and personal vault requests.
  Repository/config/checkpoint/run reads use the separately installed Reader App. Its
  installation tokens never identify a personal vault owner or delegated command actor.
- New sponsor/start/control commands and source-comment confirmation use configured
  upstream writer authority, independent of fork permissions. Precharge rechecks use the
  same upstream and stable actor; permission loss fences only that actor's task grants.
  Frozen admissions, previously charged identities and unrelated provider work are retained.
- Main's upstream-writer gate protects full target/task/checkpoint/diagnostic reads.
  A narrow exception permits visibility-checked own-consent reads/discovery and self-withdrawal
  after writer loss, including former owners and callers without a prior grant. Shared service
  and BFF redaction excludes other slots/history, task state, coverage and accounting. Existing
  transition history supplies own receipts/cutoffs; no parallel ledger is introduced.
- Nonwriter own-consent projections read Registry evidence directly, with task slots taking
  precedence over source intents. They omit foreign actor/cutoff history and perform no vault
  coverage or Exchange observation calls. Numeric stable actor lookups use the production
  transport; unassociated human control commands wake reconciliation before upstream checks.
- Persistent issue summaries, retry cooldowns, command convergence and selective phase reads
  remain display caches. Summary responses attach current service consent separately; cached
  state never grants sponsorship or authorizes admission. Explicit task sponsorship refresh
  invokes its existing summary resource even inside the display freshness interval. Reader hydration accepts wire formats
  7/8/9 and preserves immutable admission data. Normalized in-memory state remains version 2.
- Runner assignment/run attribution accompanies the original turn. It does not replace payer,
  credential/version, receipt, generation or charged-attempt bindings. Provider failure draining
  and durable unknown-gap settlement retain their earlier contracts.
- `update-concurro.sh SERVICE_FULL_SHA WEB_FULL_SHA` is the combined host release path.
  Individual workstation releases remain supported. Every path retains the optional traversable
  `/etc/concurro/compose.service.host.yaml`; private resolver networking/state mounts remain
  explicit host configuration. Preserve Registry8/source policies, vault3 and checkpoint9 state.
  Optional logout state binds at container `/var/lib/concurro/backoffice-auth`, separately
  from the named read-cache volume; existing host auth files remain in place. Update the
  configured container path when adopting this overlay. Recovery is roll-forward only. See [App migration](../../../backoffice/APP-MIGRATION.md).

## Evidence and remaining work

Merge validation uses both parents' actual persistence writers to produce portable, hash-bound
Registry5/8, source-policy, checkpoint7/8/9 and persistent-cache fixtures. Their provenance and
regeneration tool live in `backoffice/tests/fixtures/main-integration`. Cross-repo regressions
exercise user/Reader token isolation, own history after takeover, no-prior-grant withdrawal,
source ingestion and precharge upstream checks. Final merged test/review identities will be
recorded separately; preparatory failures remain in `/private/tmp/concurro-main-merge-*`.

Before deployment, accept/stage and sign both pending merges, pass exact-release CI/image
publication, and verify external Sign-in/Reader App configuration, grants, callbacks, private
principal/network/state wiring and active-task bundle handling. No private host settings were
inspected by this integration. No deployment, live task retry or provider call is authorized here.
Real subscription backend/model/account-header/expiry/portability checks remain user-deferred;
optional provider refresh stays separate. Fake providers and intercepted UI requests prove local
mechanics, not live account acceptance.
