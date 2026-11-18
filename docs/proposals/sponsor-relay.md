# Sponsor-held Claude credentials

**Status:** local synthetic and bounded live-provider integration passed and has not been
deployed. The frozen Sponsor relay at `5d57cfab313d69c16a3b3e39fda5eb721e83c070`
speaks protocol v4. Earlier TLSNotary spike measurements and the superseded
design remain in Git history; the current native transport uses TLS 1.3 MPC
and proof verification.

The sponsor runs `concurro-relay` on their machine. Its provider credential
stays there. Concurro's Rust hosted verifier authenticates its two outbound
TLS links, starts the existing native Concurro role, verifies each provider
response, and leases that proved session to one admitted task turn. The local
integration covers Claude HTTP and ChatGPT/Codex WebSocket routes, including
the portal's exact-route approval. The [sustained synthetic CLI report](sponsor-relay-sustained.md)
records 25 tool turns per CLI. The five-file live reports cover
[Claude compact Message Threads](sponsor-relay-message-threads.md) and
[Codex compact continuations](sponsor-relay-codex-compact.md), including their
validation limits. Rollout remains later work.

## Authority and custody

The [Vault](../../gateway/vault/README.md) owns current ordered/filterable
entries, immutable versions and retirement events. A relay entry uses the
same owner, route, entry, record and version identity as a pasted credential,
but stores a relay ID instead of encrypted provider material. Entries of both
custodies can coexist in one provider's ordered list. Selection and sponsorship
remain the existing service decisions; the relay adds no priority or fallback
system.

The Rust verifier persists pending registration codes and approved public keys.
Its private approval endpoint accepts an exact Vault owner/route/entry/record/
version binding only from the owner-authenticated Vault API. A Sponsor must
prove possession of that key on both live links. A withdrawn relay loses its
active link leases. Vault retirement makes the old exact record unusable.

## Owner portal

The credentials page groups each device's route entries. Approval looks up the code
and binds exactly the routes it registered; a reinstalled device is replaced with one
new code, keeping entry policy and order. Live status comes from the verifier's
authenticated links and task leases, reported only to their owner: connected with
free links, offline since a time, or unknown before it reconnects after a verifier
restart. Device usage reuses task funding evidence and keeps Concurro's charged
calls apart from provider-reported tokens. No Sponsor release is published; the
setup steps show only the configured relay address and download location.

## Inference path

1. The existing controller freezes the selected sponsor and credential version
   in its admission. Exchange validates the controller receipt and creates a
   scoped session key.
2. Agentgateway authenticates that key and enforces the model and token budget.
   Its `/start` hook freezes the sole running turn; `/resolve` obtains the
   exact Vault record. Relay custody supplies only a local placeholder to the
   gateway, never a provider credential.
3. Exchange consumes the one-use request nonce, rechecks the exact relay
   record and current turn authority, then forwards the request over its
   private authenticated Rust service connection. It keeps cancellation,
   deadlines and sanitized observations.
4. Rust matches the exact approved relay ID and record to a live link. The
   first request starts a native session; later requests from that same
   session and turn reuse it. A disconnected, non-generating Codex warmup
   finishes its admitted setup so the following generation can use that link.
   Exchange deadlines, cancellation and revocation still release it. Another
   task cannot borrow its link.
   Missing, stale and withdrawn links fail the request; there is no payer
   fallback or automatic second inference.
5. The native Concurro worker releases only authenticated provider response
   fragments to the CLI. Ending the Exchange turn releases the native lease.

The sponsor can see the task content it proves. The proof authenticates the
provider exchange; it does not independently establish the provider's model
choice or reported usage.

## Deployment boundary

The hosted Rust listener for Sponsor links is raw TLS, separate from the
HTTP edge. Its private control/dispatch listener is reachable only on the
credential network and requires a service bearer. The current qualified
native package includes a locally tested Linux amd64 Concurro role bundle.
Deployment and live-provider calls require separate authorization.
