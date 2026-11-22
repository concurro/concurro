# Phase 5 S0 evidence

[S0](proposals/architecture/phase-5/s0-baseline.md) of the
[phase 5 plan](proposals/architecture/phase-5-plan.md). It records the contract decisions
and five spikes. The spikes ran on a workstation against throwaway containers and local
processes. Nothing was deployed, and no live credential or GitHub write was used. The
spike code is in [`spikes/phase-5-s0/`](../spikes/phase-5-s0/).

## Contracts

Every phase 5 contract in [contracts](proposals/contracts.md) is full, except two that
are moved to later work:

- **`funding.routes`.** The four routes stay defined in code.
- **`funding.charge`.** Calls stay the charged unit, and a turn's usage reaches the read
  model in `turn.finished`.

## Results

| Spike | Result | Changes the plan |
| ---- | ---- | ---- |
| 1. Relay protocol range | Infeasible without two native session implementations | Yes: Sponsor 0.2.0 ships before the S1 verifier |
| 2. OpenBao | Works as planned, with one correction | Yes: no mount-wide `cas_required`; self-init takes passwords from the environment; recovery keys work as break-glass |
| 3. Streaming passthrough with usage | Works | No |
| 4. `responses-bridge` in front of a Rust proxy | Works unchanged | No |
| 5. GitHub OIDC in Rust | Works | No |

### 1. Relay protocol range

**The question:** can one verifier build accept installed Sponsors (protocol 4) and
current ones (protocol 5)?

**What protocol 5 changed.** The bump (`concurro-relay` `e30d119`) didn't change the link
messages (`crates/protocol/src/link.rs`). It changed the native session the Sponsor and
the verifier run together:

- The Sponsor now approves each request before it reaches the provider. New peer messages
  carry this: `request_review`, `request_rejected` and `audit_usage`
  (`crates/native/src/session.rs`).
- The worker's review and verdict protocol is new (`crates/native/src/lib.rs`).

**Result:** accepting protocol 4 in `Acceptor` would also mean keeping the old native
session flow, two implementations of the MPC-side exchange, for one installed client.

**Decision: ship Sponsor 0.2.0 from relay `main` first.** It speaks protocol 5. The
owner's Sponsor devices upgrade before the S1 verifier deploys. Phase 4's live pairing
used Sponsor v0.1.1, so today's deployed verifier speaks protocol 4.

### 2. OpenBao

**Setup** (`spikes/phase-5-s0/openbao/`, with `check.sh` for the policies and
`recovery.sh` for break-glass):

- OpenBao 2.7.3 with PostgreSQL 17 storage and `max_connect_retries = 0`.
- The static seal, with a file key.
- A file audit device in the configuration's `audit` stanza.
- Self-init creating:
  - the userpass and AppRole methods;
  - a KV v2 mount `concurro`;
  - S2's `accounts`, `gateway`, `operator` and `admin` policies;
  - the `accounts` and `gateway` AppRoles;
  - the `admin` and `operator` users, whose passwords self-init reads from environment
    variables (`eval_source = "env"`).

**Passed:**

- **Self-init** ran at first boot and revoked its root token. The users signed in with
  the passwords it read from the environment.
- **Unattended unseal.** A restart unsealed by itself. With OpenBao started before
  PostgreSQL, it waited, then unsealed once PostgreSQL came up.
- **`accounts` policy:**
  - creates with `cas=0`;
  - is refused a second create and any overwrite, with or without `cas`;
  - is refused data reads, including its own new paths;
  - reads metadata, destroys versions and deletes metadata;
  - can't read ops paths.
- **`gateway` policy:** reads personal versions and its ops path; is refused writes and
  destroys.
- **`operator` policy:** writes and patches `ops/*`; is refused personal reads and
  writes.
- **`admin` policy:** issues AppRole secret IDs and reads policies; is refused personal
  reads.
- **`check.sh`:** 22 of 22 checks passed, and it exits non-zero if any fails.
- **Break-glass with a recovery key** (`recovery.sh`):
  - Self-init creates no recovery keys. The `admin` identity created one with
    `bao operator rotate-keys -target=recovery`.
  - With `disable_unauthed_generate_root_endpoints = false` set temporarily on the
    listener, the legacy unauthenticated endpoint (`sys/generate-root/attempt` and
    `update`) turned that key alone into a token with the `root` policy, and no other
    token was needed.
  - The token was revoked, the listener setting removed, and the endpoint answered
    `405` again.
  - The CLI's `generate-root` uses the newer authenticated `sys/generate-root-token`
    endpoint, so the script calls the legacy endpoint directly.
- **`vaultrs` 0.8.0** (`client/`) did every `concurro-secrets` operation S2 names:
  - AppRole sign-in;
  - create with `cas=0`, and a second create refused;
  - data read refused, metadata read;
  - the gateway's exact-version read;
  - destroy, after which the gateway's read fails;
  - metadata delete;
  - a refusal error that carries no secret value.

  11 of 11 checks passed.

**Correction to S2: no mount-wide `cas_required`.** With it on, the operator's routine
`bao kv patch` fails, because it must name the current version. Create-only is already
enforced by the `accounts` policy, since a write to an existing path is an `update`, and
accounts always sends `cas=0`. So the mount leaves `cas_required` off.

### 3. Streaming passthrough with usage

`spikes/phase-5-s0/passthrough/` is an `axum` and `reqwest` proxy with an SSE tap. The tap
is a Rust port of `gateway/provider_protocol.py`'s `Observation`. A fake provider streams
events 150 ms apart.

**Seven tests passed:**

- Anthropic Messages passes through byte for byte. Each event reached the client before
  the provider sent the next, so nothing beyond one event is buffered. Usage came out as
  120 input and 42 output tokens.
- OpenAI Responses usage was extracted: 77 input and 9 output tokens.
- An Anthropic `rate_limit_error` mapped to `unavailable`, with no usage.
- An event over 8 MiB made usage ambiguous, in each of three arrangements:
  - split across chunks;
  - complete in one chunk;
  - split mid-event, fed straight to the tap.

  Each completed event's size is checked before it is parsed, as well as the
  incomplete buffer's.
- A client disconnect after the first event stopped the upstream request in under a
  second, and was recorded as unknown usage.

All nine routes use these two stream shapes (Messages and Responses), so this covers
their passthrough. Per-route header injection is S5's work.

### 4. `responses-bridge` against the proxy

`responses-bridge` was built from `concurro-relay` `main` (`483df38`) and run with
`--gateway` pointing at the spike proxy (`passthrough/tests/bridge.rs`).

**Three tests passed:**

- A Codex WebSocket `response.create` reached the proxy as a streaming
  `POST /v1/responses` carrying `Bearer <session key>` unchanged, and its three events
  came back over the WebSocket.
- A second `response.create` sent while the first was in flight ran after it.
- `response.cancel` during a turn stopped the upstream stream in under a second.

The bridge needs no change to run in front of the Rust gateway.

### 5. GitHub OIDC verification

`spikes/phase-5-s0/oidc/` implements the Python exchange's claim rules
(`exchange.py:147, 906`) with `jsonwebtoken` 12.0.0 (`rust_crypto`, as the workspace pins
it) and a JWKS key. Time claims are checked against an injected clock.

**Five tests passed:**

- A valid token binds its run.
- **Refused as unauthenticated:**
  - a token signed by another key, or with an unknown `kid`;
  - a wrong issuer or audience;
  - `exp`, `nbf` or `iat` outside 60 seconds of skew, or `iat` older than 600 seconds;
  - an empty `jti`.

  An audience list containing ours passes.
- **A mismatched `repository_id` or `repository_owner_id`** is refused without recording.
- **Recorded on the session:**
  - a mismatched `job_workflow_ref` or `actor_id`;
  - a wrong `event_name` or `ref`;
  - a self-hosted runner;
  - run attempt 2;
  - another run once the session is bound.
- **A reused `jti` is refused.** A fresh token from the same run is accepted, which is
  runner v2's repeat claim.

The token was synthetic, using today's claim names. A real Actions token is first seen by
S4's optional probe or S7's qualification.

## Plan changes made

- [S1](proposals/architecture/phase-5/s1-relay.md): Sponsor 0.2.0 first, with no protocol
  range.
- [S2](proposals/architecture/phase-5/s2-openbao.md): no mount-wide `cas_required`;
  self-init reads initial passwords from the environment; break-glass is a recovery key
  with the legacy generate-root endpoint enabled temporarily, or the second offline
  `admin` identity.
- [The overview](proposals/architecture/phase-5-plan.md): decision 13, the deployability
  table and the untested assumptions.
