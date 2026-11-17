# Prompt optimizations — unreleased

Removes redundant prompt fields and the duplicate response schema, uses compact UTF-8 JSON,
and omits `CONTRIBUTING.md` from the stacks-core policy lists. Native schema delivery,
validation, session checks, size accounting and the backoffice schema panel are preserved.

## Deployment

Finish active turns and consume their responses before deploying. Removing duplicate context
fields changes retained request digests without changing the bundle identity: a pre-upgrade
response awaiting exact-turn replay cannot be replayed against the new prompt. Ordinary next
turns remain compatible and remove obsolete context keys through the existing session-update
mechanism.

Deploy the target policy-list changes with the engine update to obtain the document-size
savings. No database migration is required. Existing diagnostics rows are not rewritten.

See [Operating Concurro](../concurro.md) for the resulting prompt and accounting behavior.
