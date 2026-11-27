# Antigravity operation

Google profiles run the pinned Antigravity CLI through `crates/agy`'s Rust adapter.
The retained Python runner supplies the invocation and continues to own stage-schema
validation, completion journals, artifacts and uncertain-result handling.

## Sponsor login and account attribution

Run `agy` to sign in, then `concurro-relay connect antigravity_subscription`.
Sponsor asks `agy models` to renew the provider login and resolves its subscription
project through `loadCodeAssist`. It records the account email and project. Subsequent
links read agy's current access token from Keychain (`gemini` / `antigravity`), Linux
Secret Service, or agy's `.gemini/antigravity-cli/antigravity-oauth-token` fallback. They do not
read Gemini CLI's `oauth_creds.json` or retain Google's refresh token.

Before a new link, a bearer within 60 seconds of expiry, or with unknown expiry, is
renewed by `agy models`.
Each Google link closes 60 seconds before its bearer expires; a subsequent link reads
the renewed login. An interrupted request follows the existing uncertain-result retry path.
Sponsor owns no OAuth refresh implementation. An account switch requires reconnecting
that route. Sponsor replaces the request's outer project with its recorded subscription
project; the caller cannot choose which account or project pays. Account entitlement and
quota remain associated with that Google login and project.

## Request and validator path

The runner uses its derived gateway session key at `/inference/v1beta/models/...`.
The gateway checks the canonical model against the admitted grant. Subscription requests
are wrapped for `/dispatch/v2/antigravity`; the hosted verifier validates the same signed
claim before mapping the explicit effort to Google's subscription wire model. The
subscription provider endpoint is `daily-cloudcode-pa.googleapis.com`.

The shared `TlsKeyUpdate` transport uses HTTP/2, Sponsor credential injection and native
request proofs. Semantic review uses decoded JSON; proofs bind the exact compressed bytes sent over TLS. Review and completion audit
digests both identify the decoded request. Both Claude and Antigravity requests use gzip. Google
SSE envelopes are unwrapped for agy, preserving tool calls and thought signatures. Usage
is finalized at stream EOF, including late usage and thought tokens.

## Conversations and tools

Each turn gets a private HOME and project root, with the task checkout explicitly named
in the prompt. File tools use checkout paths; shell commands start with `cd` to that
checkout. Approved MCP definitions are recreated for the turn. Repository customization
files are outside agy's discovery root. Existing Cargo and Rustup locations are preserved. Target setup must expose any other
HOME-relative toolchains through explicit paths or environment configuration.

Only the selected role's conversation UUID is restored. SQLite backup includes committed
WAL contents; a successful turn saves an atomic snapshot for the next invocation.
Implementation and review turns omit the CLI timeout and use the outer runner lifetime;
bounded stages retain their execution deadline. A timeout, interruption or unsuccessful result cannot commit a successful journal entry.
Google requests still carry full conversation history, including tool results and signed
thoughts. A saved conversation preserves context but is not a provider-side previous-response
ID. Gzip reduces transfer size; it does not remove this full-history requirement.

Agy may attempt auxiliary model requests for its own bookkeeping. The gateway retains
the grant's model allowlist; ungranted auxiliary requests are rejected. The offline native
qualification checks that the main tool-driven turn still completes.

## Installation and release

`engine/agent-native-clients.json` pins the Linux x64 CLI archive and SHA-512 digest.
The launcher installs it and builds `agy-runner` with Concurro's pinned Rust toolchain
only for an `agy` binding. Existing Claude and Codex installation remains unchanged.

Release protocol 6 Sponsor and hosted verifier together with the service, launcher and
Backoffice. Upgrade Sponsor devices before enabling the new verifier; protocol 5 clients
receive the existing upgrade refusal. Publish the pinned relay commit before publishing
Concurro so clean builds can resolve its hosted-api dependency. Service startup applies
Accounts migration `0003` and Tasks migration `0023`
forward. Existing Anthropic/OpenAI rows remain valid. New Google credentials and funding
appear in the same credential and task views. No GitHub App permission expansion is needed.

## Deployment configuration

Add `gemini-*` (or the specific canonical model IDs) to the service's `gateway.models`
allowlist. A named profile uses `provider: google`, for example `model: gemini-4-neon`
and `effort: high`; assign that profile to the required agent roles.

Subscription funding uses the gateway's existing `relay` configuration and the connected
`antigravity-subscription` route. If personal API keys are also offered, add a
`personal_routes` entry for `gemini-api-key` with
`upstream: https://generativelanguage.googleapis.com` and `anthropic_beta: null`;
the gateway selects `x-goog-api-key` authentication from the credential route. Org API-key
routes use that `upstream`, `auth: x-goog-api-key`, and an operator-owned `token` secret
reference.

The hosted verifier defaults to
`--antigravity-provider-host=daily-cloudcode-pa.googleapis.com` and
`--antigravity-provider-port=443`; retain the production provider CA roots. Sponsor keeps
the TLS identity bound to that provider. No extra endpoint is needed in the relay-only
credential form.

## Reproducible checks

The [2026-11-27 evidence report](reports/antigravity-joined-evidence.md) records the
completed local joined provider and refresh checks, their versions and their limits.

```sh
cargo nextest run --workspace --locked
cargo nextest run -p concurro-agy --test native --run-ignored ignored-only --no-capture
```

The second command requires `agy` on PATH and uses only a loopback Gemini fixture. It
checks native file read/write, shell, configured MCP, a structured result and a fresh-process
continuation using the saved database. Relay's native helper tests additionally require
its accepted helper bundle; see that repository's `native/README.md`.

The opt-in `agy_joined_subscription` gateway test consumes subscription quota and requires
`CONCURRO_AGY_JOINED_CONFIG` naming a JSON file with `verifier_url`, `relay_id` and
`output_dir`. It uses a local hosted verifier paired to a signed-in Sponsor, the fixture
Exchange signing key and owner binding, and `target/debug/agy-runner`. It checks fresh and
resumed native CLI turns through real PostgreSQL admission, gateway dispatch, native
proofs and provider settlement. Build `agy-runner` first, or set `CONCURRO_AGY_RUNNER`
to its executable when using a custom target directory. Normal test runs skip it.
