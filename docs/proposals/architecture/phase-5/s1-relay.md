# S1: relay verifier release

Part of the [phase 5 plan](../phase-5-plan.md). The work is in `concurro-relay`, and it
can deploy before the cutover, but Sponsor functionality requires the new Phase 5
callers. The relay is unused during this replacement; Python API compatibility is
not required.

## Today

The hosted verifier (`crates/hosted`, binary `concurro-relay-hosted`) trusts one shared
bearer on its private listener:

- **One bearer, three callers.** The vault uses it for `/approve`, `/pending` and
  `/status`; the exchange's forwarder uses it for `/dispatch/*` and `/release`.
- **Dispatch trusts its caller.** It accepts the session, turn and deadline in
  `x-concurro-binding` and checks only that they match the registry's binding exactly.
- **Release is in memory.** `/release` cancels one turn, and a restart forgets it.
- **Exact protocol match.** Installed Sponsors (v0.1.x) speak relay protocol 4, while
  `main` accepts only protocol 5.

## Scope

1. **Sponsor 0.2.0 and a protocol-5 verifier.** S0 found that protocol 5 changed the native session the
   Sponsor and the verifier run together, not just the link messages
   ([evidence](../../../rust-phase-5-s0-evidence.md#1-relay-protocol-range)). Accepting
   protocol 4 would mean keeping two native session implementations.
   - So Sponsor 0.2.0 is released from relay `main`, speaking protocol 5.
   - Sponsor and verifier are upgraded together before relay qualification.
   - The verifier accepts only protocol 5; no dual-protocol transition is needed.
2. **Three caller credentials,** each a separate token file and each accepted only on
   its own routes, as `Authorization: Bearer`:

   | Credential | Routes | Holder |
   | ---- | ---- | ---- |
   | `owner` | `/owner/v1/*` | Accounts |
   | `inference` | `/dispatch/v2/*` | Gateway |
   | `release` | `/release/v1/*` | Exchange |

   Remove the old private routes and `x-concurro-verifier-auth`. The Python vault
   and exchange cannot call the replacement verifier; no compatibility bridge is
   introduced. Sponsor registration and link transport remain available.
3. **Owner API v1** ([contract](../../contracts.md#verifier-owner-api)):
   `GET /owner/v1/registrations/{code}`, `POST /owner/v1/approvals` and
   `GET /owner/v1/relays?user_id=`.
   - The registry gains an index from owner to relays, for the last route.
   - Times are RFC 3339, and errors use one shape.
4. **Signed session claims** on `/dispatch/v2/messages` and `/dispatch/v2/responses`:
   - The claim is an EdDSA (Ed25519) compact JWS in `x-concurro-claim`
     ([contract](../../contracts.md#session-claims)).
   - The verifier checks the signature against the configured exchange public keys by
     `kid`. It then checks expiry and scope: relay, record, version and route must match
     the registry's binding, and the requested model must be in the claim.
   - It checks the claim isn't revoked and has allowance left.
   - It leases under the claim's session (`sub`). A session serves one turn under the
     task application, so this keeps today's one lease, link and retained native session
     per turn (`crates/hosted/src/main.rs:382-404`). Later requests reuse the lease, and
     only release or expiry frees it.
   - `x-concurro-request` is used only for allowance and accounting. The verifier no
     longer trusts a caller-supplied session, turn or deadline.
5. **Allowance.** Each claim carries `calls` and `tokens`.
   - **Calls** are counted when a dispatch is admitted.
   - **Tokens** are counted from the usage the verifier already parses: Codex's
     `Turn.usage`, and for Claude a tap on the SSE stream through `summarize_sse`.
   - A dispatch is refused once either recorded allowance is spent. A stream already
     running isn't cut, so known usage may overshoot by that one request.
   - Cancellation stops provider work promptly. Observed partial usage is charged as
     a lower bound, with incomplete accounting identified in the journal. Unavailable
     usage is not invented; call allowance and expiry still bound subsequent requests.
     The one-request token overshoot bound assumes usage was observed.
6. **Persisted revocation and allowance.** `POST /release/v1/claims/{claim_id}`
   revokes a whole claim and ends its leases.
   - It answers only after the revocation is durable.
   - Revoked claims and spent allowance live in an append-only journal beside the
     registry, written with the registry's atomic-file and journal pattern. They
     survive restarts and are pruned after the claim's expiry.
7. **Claims are always required.** There is no unsigned dispatch or compatibility flag.
8. **A `hosted-api` library crate** carries the owner, dispatch and release types and
   the claim codec. Concurro depends on it through a git tag. The hosted binary uses it
   too, so the types are defined once.

## Non-goals

- TLS on the private listener. It stays on its internal network; TLS is later
  hardening.
- Changes to the Sponsor client beyond releasing current `main` as 0.2.0.
- Python vault or exchange bridges to the replacement verifier.

## Release

- Release Sponsor 0.2.0 from relay `main` through the existing `release-plz` and `dist`
  process. The owner upgrades their Sponsor devices to it.
- Tag the relay repository, and build the verifier image from that tag.
- Document the new flags in its README: the three token files, the exchange public key
  file with its key ID, and the revocation journal path.
- `concurro-deploy` installs three token files in place of the shared one. Only the
  native accounts service, Rust gateway and Rust exchange receive their respective
  credentials. Until those callers are connected, pairing through the backoffice and
  relay inference are unavailable. S7 qualifies them together.

## Acceptance

- A Sponsor 0.2.0 client registers, links and serves a dispatch against the S1 verifier.
  A protocol 4 client is refused with the existing "update concurro-relay" message.
- Each credential is refused on the other credentials' routes.
- The removed private routes are unavailable, and the old authentication header is
  refused on the new APIs.
- A claim signed with an unknown `kid`, expired, scoped to another relay, record,
  version or route, or naming a model outside its list is refused, and no lease is
  opened.
- After `release/v1/claims/{id}` and a verifier restart, the claim stays refused and
  its spent allowance is unchanged.
- Allowance is enforced: one call more than `calls` is refused. A token count past
  `tokens`, from a recorded Claude stream and a recorded Codex turn, refuses the next
  call.
- Dispatch without a signed claim is refused.
- Live, with the Phase 5 callers connected: Sponsor pairing passes through the native
  accounts service, and the Rust gateway and exchange complete a relay turn.
