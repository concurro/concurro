# Sponsor-held credentials

**Status (2026-11-27): implemented on main for Claude, ChatGPT and Antigravity,
using relay protocol 6.** Sponsor v0.1.2 / protocol 5 is the earlier deployed baseline,
not a compatible client for the current verifier. Upgrade Sponsor and verifier together;
see [Antigravity release requirements](../antigravity.md#installation-and-release).

Local fresh and resumed agy turns passed through PostgreSQL admission, the Rust gateway,
hosted verifier, Sponsor and the real Google provider. Separate near-expiry login refresh
checks passed. The [evidence report](../reports/antigravity-joined-evidence.md) records
versions, timings and limits. These results do not establish deployed GitHub Actions
qualification; [Phase 5 closeout](architecture/phase-5-plan.md#implementation-status)
remains partial.

## Authority and custody

Accounts owns ordered/filterable entries, immutable versions and lifecycle facts. A
relay entry carries the same owner, route, entry, record and version identity as a pasted
credential. Accounts stores its relay binding; provider material stays on the Sponsor
instead of entering OpenBao. Both custodies can share a provider's ordered list. The task application's funding module selects the
payer and exact credential; fallback remains within that payer's eligible list.

Accounts approves exact owner/route/entry/record/version bindings through the verifier's
owner API. The verifier persists registration and approval state. Sponsor proves possession
of its registered identity on both live links. Retirement and withdrawal revoke the
corresponding execution authority through the existing accounts/task/exchange paths.

## Owner portal

The credentials page groups a device's route entries. Pairing binds the routes registered
by that device; replacement preserves entry policy and order. The owner sees connection
and lease status. Usage keeps Concurro's charged attempts separate from provider token counts.

Antigravity subscription setup uses `agy` login and
`concurro-relay connect antigravity_subscription`. Sponsor records the login's email and
subscription project, rereads agy's bearer, and asks `agy models` to renew it before a new
link when expiry is within 60 seconds or unknown. Sponsor does not implement Google OAuth
refresh or retain the refresh grant. Account changes require reconnecting; Sponsor injects
its bound project, so the caller cannot choose the subscription account being charged.
See [credential locations and link expiry](../antigravity.md#sponsor-login-and-account-attribution).

## Inference path

1. The task application freezes payer, credential version, model and allowance in an
   admitted attempt. Exchange creates its runner session and signs relay authority.
2. The Rust inference gateway authenticates the runner's session key through Exchange
   admission. It resolves the frozen route and sends the request with its signed claim
   to the hosted verifier. Provider credentials never enter the runner or gateway.
3. The verifier checks the claim and exact approved relay binding, then leases a live
   Sponsor link to the admitted session/turn. It enforces request identity, revocation,
   allowance and lifetime. Dispatch failure does not trigger an automatic payer switch.
4. Claude and Antigravity share the HTTP/2 `TlsKeyUpdate` transport. Sponsor injects its
   credential; semantic review uses decoded JSON while native proofs bind the exact gzip
   bytes sent over TLS. Claude uses compact Message Threads; agy retains its conversation
   locally but sends full history. Codex uses WebSockets and `previous_response_id`.
5. Authenticated provider responses return through the verifier and gateway. Google SSE
   envelopes are unwrapped for agy, preserving tools and thought signatures; final usage
   includes late events. Gateway reports settlement to Exchange, which releases the lease
   at turn completion or revocation.

Sponsor can see the task content it proves. The proof authenticates the provider exchange;
it does not independently establish the provider's model choice or reported usage.

## Deployment boundary

Sponsor links use a dedicated raw TLS listener, separate from the HTTP edge. The private
HTTP listener uses separate Accounts owner, Gateway inference and Exchange release bearer
credentials, plus Exchange signing keys for claims. See the Phase 5
[relay](architecture/phase-5/s1-relay.md), [accounts](architecture/phase-5/s3-accounts.md)
and [gateway](architecture/phase-5/s5-gateway.md) interfaces. Current protocol-6 release
coordination supersedes their historical protocol-5 cutover sequence.

## Historical qualification

The [sustained synthetic CLI report](sponsor-relay-sustained.md) covers 25 tool turns per
CLI. Five-file live reports cover [Claude Message Threads](sponsor-relay-message-threads.md)
and [Codex continuations](sponsor-relay-codex-compact.md). Those results retain their
original source and deployment baselines; they do not qualify a later service release.
The original Vault/Agentgateway relay design remains in Git history.
