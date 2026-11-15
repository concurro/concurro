# Proposal: sponsor-held credentials through a relay

**Status: Concurro's side is not implemented.** The relay, a verifier library and a
development stand-in for Concurro exist in the `concurro-relay` repository and have
carried real Codex sessions. Nothing in Concurro's gateway, vault, service or backoffice
has changed. The feasibility spike which preceded them (`tlsn-spike/`, decision record in
its `DECISION.md`) is outside both repositories.

Let a sponsor fund a subscription route without giving Concurro the token. The sponsor
runs one program, the relay, which keeps the token on their machine and performs each
admitted provider request. Concurro sits on the network path and accepts a response only
with a proof that the provider received exactly the request Concurro asked for.

This adds a second way to hold a credential beside the [vault](per-user-credentials.md#5-vault).
Sponsorship, admission, coverage and pauses stay as
[per-user provider credentials](per-user-credentials.md) defines them.

## Today

- **Concurro holds every subscription token.** A sponsor pastes a CLI-issued token into the
  portal; the vault stores it encrypted, versioned per `(user id, route)`, and the gateway's
  private resolver hands the exact admitted version to the native adapter for each request
  ([vault](../../gateway/vault/README.md), [phase 4](per-user-credentials/phase-4.md)).
- **A vault compromise exposes every sponsor's tokens.** That risk is recorded in the
  per-user credentials proposal. Subscription tokens can do more than inference, so the
  exposure is wider than the routes Concurro uses.
- **Refresh is manual.** Concurro runs no provider login and no refresh service; an expired
  token waits for the sponsor to paste a new one.
- **Runners never see provider secrets,** and Codex runs with WebSockets disabled
  ([inference gateway](../design/inference-gateway.md)).
- **No relay concept exists.** Nothing registers a sponsor's machine, and every inference
  request goes from the gateway straight to the provider.

## What exists

The relay opens the TLS connection to the provider through Concurro's verifier, which
forwards ciphertext it cannot read (TLSNotary proxy mode). The relay then proves in zero
knowledge what it sent, revealing everything except the credential bytes.

**The spike** measured this with stand-in binaries on one machine:

| Route | Evidence | Relay upload |
| ---- | ---- | ---- |
| Claude subscription, HTTP with message threads | 22 exchanges of a real Claude Code session verified | 5.2 MB first exchange, 0.2 to 0.9 MB per continuation |
| ChatGPT subscription, HTTP with zstd bodies | Two exchanges of a real Codex session verified | 2.1 MB per exchange |
| ChatGPT subscription, WebSocket | 33 turns of two real Codex sessions verified on one link | 3.5 MB for the 68 KB first request, median 0.57 MB per continuation |

Verification typically added 0.1 to 0.6 seconds per exchange. It also holds the tests of
what the verifier rejects from a modified relay or a misbehaving provider, and the
records of five independent review rounds on the proof logic.

**`concurro-relay`** is the product code, built from the spike:

| Part | State |
| ---- | ---- |
| `concurro-relay` program | Registers, keeps links open, serves all three routes above, keeps its own provider credentials, has a full-screen view and plain commands |
| Verifier library | Accepts relays' links, orders exchanges and checks proofs. Registration sits behind an interface Concurro's records would implement |
| `dev-verifier` | Stands in for Concurro: approves every registration by itself and serves a real harness through a relay |
| Evidence | 44 tests against a mock provider. Real Codex sessions ran through it over HTTP and over WebSocket. The Claude route was tried by hand; no log was kept |

It has had no independent review, the adversarial tests have not been carried over, and
it speaks to nothing in Concurro. The harness versions used were newer than the pinned
runner clients (`engine/agent-clients.txt`).

## Goals

- **The token never reaches Concurro:** not in the vault, the gateway, logs or proofs. That
  covers access tokens, refresh tokens and session cookies.
- **Concurro still trusts the exchange:** a response is accepted only if the provider
  received the exact admitted request and the response is the provider's.
- **One program for the sponsor:** sign in once, leave it running. No inbound ports, no
  hand-edited policy, no token given to Concurro.
- **Nothing changes for runners or tasks:** a job still sees only its session key, and
  tools still execute in Concurro's runners.
- **Vault custody stays.** A sponsor chooses per route; neither is a fallback for the
  other.

## Non-goals

- Hiding task content from the sponsor. The relay sees every prompt and response it
  carries.
- API-key routes, at first. A key can be scoped and rotated at the provider, so the vault
  stays its custody.
- Deciding whether a provider's terms allow relayed subscription use. As for pasted
  tokens, that is the operator's and the sponsor's responsibility.
- Proving which model ran or that the provider reported usage correctly.

## Design

### 1. Custody

A credential record gains a custody: `vault` (today's behaviour) or `relay`.

- **A relay record holds no secret.** It stores the relay's public key, its policy version,
  the models validated through it and its status. It is keyed and versioned exactly as a
  vault record, so the sponsor tuple and the frozen admission record
  ([cross-cutting rules](README.md#cross-cutting-rules)) need no new shape.
- **A new version** is created when the sponsor registers a relay for the route again or
  rotates its key. Retirement, revocation and lifecycle events are the vault's existing
  ones.
- **One custody per `(user, route)` at a time.** Switching custody is a replacement: it
  retires the prior version and, for a vault version, destroys its material.

### 2. The relay program

One binary, `concurro-relay`. Without a command it opens a full-screen view which signs
in if needed and then serves; `login`, `run`, `connect`, `status` and `logout` do the
same from the command line.

- **Registration** needs no GitHub token on the sponsor's machine. The relay generates a
  key pair, sends Concurro the public key and the routes it will serve, and is given a
  short code. The sponsor approves that code in the portal, signed in as themselves, and
  the relay is then registered to their account. Concurro stores only the public key.
- **Serving:** the relay dials out to Concurro, proves possession of its key on every
  connection and holds its links open. It reconnects with backoff, and stops if Concurro
  refuses it.
- **Provider credentials are the relay's own.** It does not read the logins Codex and
  Claude Code keep, and signing in does not disturb them:
  - **ChatGPT:** the relay runs Codex's device sign-in with a private directory of its
    own as Codex's home, takes the login Codex is given and removes the directory.
  - **Claude:** the sponsor pastes the token `claude setup-token` prints.

  Credentials are kept in the operating system's keychain on macOS and in owner-only
  files elsewhere, are read for every request, and go nowhere but to the provider.
  `connect` signs in to a provider again without registering again.
- **Policy is built in.** Each route's allowlist of host, path, headers and body fields
  ships in the binary, with a separate allowlist for WebSocket connections. The relay
  refuses anything outside it and sends nothing for a refused request.
- **The sponsor can see what it carries.** The view lists each request the relay proved
  and can show what it and its response said. That text is held in memory only.

Not built: the relay's own key is in an owner-only file, not the keychain; `status` does
not show the links of a relay running in another process.

### 3. Links

A link is two authenticated connections from a relay to the verifier: one carries the
verifier's orders and the relay's answers, the other the proof session.

- **Outbound only, over TLS.** The verifier authenticates the relay by a signature from
  its registered key over a fresh challenge; the relay authenticates Concurro by its
  certificate. Today these are plain TLS connections with length-prefixed messages, which
  the existing HTTP edge cannot pass (see [open questions](#open-questions)).
- **A version check comes first.** Relay and verifier must run the same protocol and
  proof-stack version, so a mismatch is refused with a message telling the sponsor to
  update.
- **The verifier orders everything the relay sends.** An order is a single request, or a
  step on a WebSocket connection: open, send a request, answer a ping, report, close. The
  relay answers each by refusing it, with nothing sent, or by saying its proof begins, so
  the verifier never waits on a proof which is not coming.
- **One request or connection at a time per link.** Concurrent turns for one sponsor need
  several links; each costs a few seconds to establish and holds memory on both sides
  (see [risks](#risks)).
- **Any failed proof drops the link.** Its proofs share state which a failed one must not
  be allowed to probe twice. The relay opens a new one.

### 4. Verifier

A new service in the gateway's trust zone, beside the Exchange and the vault, built on
the verifier library.

- **Selection is the Exchange's, as today.** For a relay-custody admission the private
  resolve hook returns no token. It returns a one-use binding of session, turn, sponsor
  and exact record version, and the native adapter forwards the request to the verifier
  in place of the provider. Whether the pinned agentgateway can do that is a phase 0
  question; the fallback is [injection option 3](per-user-credentials.md#6-injection-decided-by-the-phase-0-spike).
- **The verifier picks the link** registered for that exact version, has the relay perform
  the request, and returns the response only once the proof is accepted: the expected
  provider, the admitted request byte for byte outside the credential, exactly the
  credential hidden, the response fully revealed.
- **No streaming to the harness.** A response is released whole after verification. Both
  harnesses accepted this in the spike.
- **It is the trusted observer for these routes.** It sees the plaintext response, so it
  feeds the same sanitized outbox the loopback observer feeds today.
- **Native key, model and token-budget enforcement stay in front,** unchanged.

### 5. Routes

| Route | Transport | Notes |
| ---- | ---- | ---- |
| Claude subscription | HTTP, message threads | Needs the harness's thread switch, which is undocumented, and a beta API feature. Without threads every exchange proves the whole conversation: 137.7 MB of upload for the 19-exchange session against 34.2 MB with threads, before persistent links |
| ChatGPT subscription | HTTP, zstd bodies | Fits the current request path |
| ChatGPT subscription | WebSocket | About a quarter of the upload per turn. Needs Codex WebSockets enabled for relay custody and a WebSocket path through the gateway |

The relay serves all three. A thread continuation is accepted only if it builds on a
response the verifier itself verified. On a WebSocket connection the relay allows a
continuation only of a response it received on that connection; keeping responses apart
between connections otherwise rests on the provider.

### 6. Coverage, admission and failure

- **Registration validates through the relay.** The model probes which validate a pasted
  token ([phase 5](per-user-credentials/phase-5.md)) run as proven exchanges on the relay's
  link. Coverage is recorded as for a vault version.
- **A new attempt needs a live link** for its exact record version, checked where the
  vault's usability is checked today. Without one the task waits in
  `awaiting_credentials`, and the notice says the relay is offline.
- **A link lost mid-turn** fails that request. The verifier records a trusted
  `relay_unavailable` observation; the attempt stays charged.
- **A provider refusal of the token** reaches the verifier in the proven response and is
  classified by the existing route-specific rules ([phase 4](per-user-credentials/phase-4.md)).
  Nothing new is inferred from a status alone.
- **A rejected proof** is recorded as `relay_rejected`, retires the link and pauses that
  sponsor's work on the route for a writer to resume. It never falls back to another
  payer.
- **Recovery** follows [phase 6](per-user-credentials/phase-6.md): work resumes when a
  usable version has a live link again.

### 7. Portal

- A route's credential card offers "keep it on my machine" beside "paste a token", with
  the install steps.
- A page where the signed-in sponsor enters the code their relay shows, sees the relay's
  name and routes, and approves or refuses it.
- A relay record shows custody, relay name, online or last seen, policy version and
  validated models. Revoke works as for a vault version.

## Phasing

0. **Evidence and decisions,** before Concurro's side is built:
   - Can the pinned agentgateway hand an admitted request to a loopback verifier, and pass
     a WebSocket upgrade through its key and budget policy?
   - Rerun the routes with the pinned runner clients, including the Claude thread switch.
   - Decide the [open design finding](#risks) on revealed server keys.
   - Commission a cryptographic review of the patched proof stack.
   - Decide how a relay's connections reach the verifier past the edge.
1. **Relay and verifier as products.** Built in `concurro-relay` against the development
   stand-in. Remaining before it can face staging: the adversarial tests from the spike,
   an independent review, and the transport decided in phase 0.
2. **Custody in the vault and portal:** relay records behind the verifier's registration
   interface, the approval page, validation through the relay, status, revoke.
3. **Admission and enforcement:** resolver binding, verifier in the request path, the two
   observations, pauses and recovery. Contributed funding only. HTTP routes first; the
   WebSocket route if phase 0 allows it.
4. **Packaging:** signed macOS and Linux binaries, the relay's key and credentials tied
   to the signed binary in the keychain, running as a background service, updates.

## Invariants

- A relay-custody token never leaves the sponsor's machine. Concurro stores, logs and
  receives nothing from which it can be derived.
- Concurro releases a response to a runner only after verifying the proof for the exact
  admitted request.
- The relay sends the provider nothing Concurro did not order and its own policy does not
  allow.
- Custody is part of the frozen admission record. An admitted attempt is never moved
  between vault and relay custody, or to another payer.
- Vault-custody behaviour and organization funding are unchanged.

## Risks

| Risk | Detail |
| ---- | ---- |
| Open design finding | On a connection kept open across turns, the proof reveals the provider's traffic key at the first turn. Nothing the relay reads afterwards is authentic to the relay, so the sponsor is protected by the relay's policy and by the provider keeping responses local to a connection. Three real runs on one account support that isolation; they do not prove it. This applies to the WebSocket route and to any kept-alive HTTP connection |
| Proof stack | TLSNotary is pre-release and requires TLS 1.2. The relay carries eighteen local patches, none upstream. Its sharing of proof state across a link's exchanges has had no cryptographic audit |
| Sponsor sees task content | Prompts and responses of the tasks a sponsor funds reach their machine. Today they stay within runners, the gateway and the provider |
| Memory | Each link retained 130 to 250 MB on each side in the spike, and proving a 200 KB request peaked near 1 GB per side. The gateway host must budget this per live link |
| Sponsor bandwidth | Upload is about 50 bytes per request byte. A slow uplink makes large first requests slow |
| Availability | A sponsor's laptop that sleeps pauses their tasks. Vault custody has no such dependency |
| Harness switches | Claude threads depend on an undocumented variable and a beta feature; Codex WebSockets are disabled today |
| Credentials at rest | The relay keeps provider credentials on the sponsor's machine. On macOS any program running as the sponsor can read them through the system's keychain tool without a prompt, as it can Claude Code's own login, until a signed relay binary owns them |
| Sign-in lifetime | A ChatGPT sign-in was valid for nine days. The relay keeps the access token only, refuses requests once it expires and asks for a new sign-in. The Claude token is long-lived |
| Known defects | An intermittent disconnect under four or more concurrent proofs, with no known cause; no cancellation of a turn in flight; a harness message other than a request is not read while a response is in flight |

## Open questions

1. Is the revealed-server-key design acceptable for the WebSocket route, or does relay
   custody ship on one-connection-per-exchange HTTP only?
2. Should the relay also keep Codex's refresh token and renew the ChatGPT sign-in itself?
   That spares the sponsor a sign-in every nine days, at the cost of a longer-lived
   secret on their machine and the relay acting as its own client of OpenAI's sign-in.
3. How do a relay's connections reach the verifier: a listener of its own beside the
   edge, or tunnelled through the edge as WebSocket connections?
4. May a relay serve several routes and several links by default, and who sets the limit:
   the sponsor, the operator or both?
5. Should a sponsor be able to restrict a relay to named repositories, beyond what
   sponsorship consent already scopes?
6. Does the verifier keep proofs or transcripts as evidence, and for how long?
