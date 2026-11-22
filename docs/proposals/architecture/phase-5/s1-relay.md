# S1: relay verifier release

Part of the [phase 5 plan](../phase-5-plan.md). The work is in `concurro-relay`, and it
deploys and serves production on its own, before the cutover.

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

1. **Protocol range.** The acceptor accepts protocols 4 and 5 (per S0), so installed
   Sponsors keep working. If S0 shows it can't, Sponsor 0.2.0 ships first and the
   verifier deploys after the owner's Sponsors upgrade.
2. **Three caller credentials,** each a separate token file and each accepted only on
   its own routes, as `Authorization: Bearer`:

   | Credential | Routes | Holder from S7 | Holder until S7 |
   | ---- | ---- | ---- | ---- |
   | `owner` | `/owner/v1/*`, and the old `/approve`, `/pending`, `/status` | Accounts | Vault |
   | `inference` | `/dispatch/v2/*`, and the old `/dispatch/v1/*` | Gateway | Python exchange |
   | `release` | `/release/v1/*`, and the old `/release` | Exchange | — (the Python exchange uses `inference`) |

   The old routes keep accepting `x-concurro-verifier-auth`. The old `/release` also
   accepts the `inference` credential until S7, because the Python exchange reads one
   token file for both dispatch and release (`exchange.py:1777-1779`). So the vault reads
   the owner token, the Python exchange reads the inference token, and neither needs a
   code change.
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
   - A dispatch is refused once either is spent. A stream already running isn't cut,
     so it may overshoot by that one request, within the owner's tolerance.
6. **Persisted revocation and allowance.** `POST /release/v1/claims/{claim_id}`
   revokes a whole claim and ends its leases.
   - It answers only after the revocation is durable.
   - Revoked claims and spent allowance live in an append-only journal beside the
     registry, written with the registry's atomic-file and journal pattern. They
     survive restarts and are pruned after the claim's expiry.
   - The old per-turn `/release` stays for the Python exchange until S7.
7. **`--require-claims`.** When set, `/dispatch/v1/*` is refused. S7 sets it.
8. **A `hosted-api` library crate** carries the owner, dispatch and release types and
   the claim codec. Concurro depends on it through a git tag. The hosted binary uses it
   too, so the types are defined once.

## Non-goals

- TLS on the private listener. It stays on its internal network; TLS is later
  hardening.
- Changes to the Sponsor client, beyond accepting protocol 4.
- Removing the old routes. The relay release after S7 removes them.

## Release

- Tag the relay repository, and build the verifier image from that tag.
- Document the new flags in its README: the three token files, the exchange public key
  file with its key ID, `--require-claims`, and the revocation journal path.
- `concurro-deploy` installs three token files in place of the shared one. The vault
  then reads the owner token, and the Python exchange reads the inference token, which
  the old `/release` also accepts until S7. The release token waits for the Rust
  exchange.

## Acceptance

- A protocol 4 client and a protocol 5 client both register, link and serve a dispatch
  against the same verifier build.
- Each credential is refused on the other credentials' routes.
- The old routes keep working with their matching credential, which is the vault's and
  the Python exchange's path until S7.
- A claim signed with an unknown `kid`, expired, scoped to another relay, record,
  version or route, or naming a model outside its list is refused, and no lease is
  opened.
- After `release/v1/claims/{id}` and a verifier restart, the claim stays refused and
  its spent allowance is unchanged.
- Allowance is enforced: one call more than `calls` is refused. A token count past
  `tokens`, from a recorded Claude stream and a recorded Codex turn, refuses the next
  call.
- With `--require-claims`, `/dispatch/v1/*` is refused.
- Live, after deployment: the Phase 4 Sponsor pairing check passes again, using the
  vault's new owner credential.
