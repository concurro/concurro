# S0: contracts and spikes

Part of the [phase 5 plan](../phase-5-plan.md). S0 changes no running service.

## Phase 4 baseline

Phase 4's live checks passed on the current stack with the operator's own account, and
are recorded in the private deployment records
([phase 4 plan](../phase-4-plan.md#slices), P1–P3):

- **Credentials:** add, reorder and delete through accounts and the vault.
- **A browser command:** its request receipt carries the signed-in user as the durable
  actor. Browser commands enter the task request API directly; GitHub replies belong to
  issue-comment commands.
- **Relay:** fresh Sponsor pairing through accounts, the vault and the verifier.

These are the paths S7 replaces, so S7's qualification repeats them on the new stack.

## Contract decisions

S0 makes every phase 5 contract full ([contracts](../../contracts.md)) before the slices
that depend on it are implemented:

- Exchange operator API v1, internal admission API, runner API v2 and turn payload v2.
- `exchange.turn.*` and `inference.usage`.
- The credential metadata API and `credential.<user_id>.changed`.
- Verifier owner API v1, the session claim and its release.

S0 also records the deployed verifier image's version and relay protocol in the
private deployment records.

## Spikes

Each spike is small, kept outside the main crates, and answers one question with a
recorded result:

1. **Relay protocol range.** Can the verifier's `Acceptor` accept protocols 4 and 5,
   with `Order` and `Notice` unchanged for version 4 clients? If not, S1 ships Sponsor
   0.2.0 first and asks installed Sponsors to upgrade before the verifier deploys.
2. **OpenBao.** OpenBao 2.7.3 on a throwaway PostgreSQL database, with:
   - the static seal and declarative self-init;
   - a KV v2 mount with `cas_required`;
   - an AppRole whose policy has `create` on `data/personal/*` and `read` on
     `metadata/personal/*`.

   Check, through the `concurro-secrets` operations S2 specifies, on `vaultrs` 0.8:
   - a `cas=0` write succeeds;
   - a second write to the same path is refused;
   - a data read is refused;
   - a metadata read and a `destroy` work;
   - a restart unseals without anyone stepping in;
   - how self-init with the static seal produces recovery keys, and that a root token
     can be generated from them.
3. **Streaming passthrough.** A minimal `axum` and `hyper` proxy forwards recorded SSE
   streams for Messages and Responses without buffering beyond one event. It extracts
   usage as `gateway/provider_protocol.py` does, and stops the upstream request when the
   client disconnects.
4. **`responses-bridge` against the spike proxy.** Codex's WebSocket turns, with
   cancel and a queued second turn, reach the proxy as streaming `POST /v1/responses`
   with the session key intact.
5. **GitHub OIDC verification in Rust.** `jsonwebtoken` with a cached JWKS verifies a
   recorded Actions token and applies today's claim rules (`exchange.py:147, 906`).

## Acceptance

- Every phase 5 contract in `contracts.md` is marked full.
- Each spike has a short recorded result: what was run and what it showed. A spike that
  fails changes the dependent brief before that slice begins.
