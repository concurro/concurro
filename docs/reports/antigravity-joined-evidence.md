# Antigravity joined inference and refresh evidence

Checks ran on 2026-11-27 against the integration work subsequently committed as Concurro
`b4cb82308417bf5bb44eee79a2e084efea19aeba` and relay
`dcb96c69ef4f1a7c5c355a40802ec7253084d849`. This report consolidates recorded results;
it is not a new test run. [Sanitized observations](antigravity-joined-observations.json)
retain measurements and SHA-256 identities of the original local artifacts.

## Joined provider test

The opt-in [`agy_joined_subscription`](../../crates/inference-gateway/tests/relay.rs)
test passed with real agy, PostgreSQL Exchange admission, the Rust gateway, hosted
verifier, Sponsor, native request proofs and the Google subscription provider.

| CLI invocation | Result | Wall time | Input tokens | Output tokens |
| ---- | ---- | ---- | ---- | ---- |
| Fresh conversation | Structured marker 741 | 31.798 s | 9,079 | 61 |
| Resumed conversation | Same conversation UUID; marker 742 | 4.498 s | 9,565 | 48 |

Both dispatched requests settled as `ok`; verifier and gateway usage matched at 18,753
total tokens. These are two CLI invocations under one fixture-admitted Exchange turn,
not two production task admissions. The test seeds the fixture session binding and
running state directly. It starts at `agy-runner` and serves gateway routes directly;
Python bridging, runner v2 authentication/claiming, Caddy routing and the GitHub Actions
launcher are outside this joined path.

## Near-expiry renewal

Isolated Linux agy 1.5.15 login copies used adjusted local expiry metadata and the real
provider refresh endpoint. `agy models` renewed the access token for remaining lifetimes
of 120, 61, 45, 15, 5 and -5 seconds. Each returned approximately 3,598 seconds of validity
in 1.8–2.7 seconds. The refresh token was unchanged. Controls at 3,600, 601 and 301 seconds
did not replace the access token. The normal login's access token was left unchanged.

These probes establish provider-tool refresh behavior, not Sponsor's trigger threshold:
Sponsor invokes agy before a new link when bearer expiry is within 60 seconds or unknown.
The implementation rereads the token and checks the bound account after renewal. The
file fallback was corrected to `.gemini/antigravity-cli/antigravity-oauth-token`.

Separate native-worker tests verified pre-expiry link closure; a real CLI fixture verified
fresh/resumed recovery from truncated SSE. No single live run crossed token expiry.
The joined run used the macOS Keychain login, but forced near-expiry renewal was tested
only with Linux file storage. Linux Secret Service renewal remains untested.

## Scope after dependency updates

The subsequent dependency update pins agy 1.5.18. Package integrity/version checks and
CI passed, but the live joined and refresh checks above were not repeated against that
version or the updated dependency/native-helper set. Earlier native tool/continuation
qualification used macOS agy 1.5.14; the Linux refresh probe used 1.5.15.

This evidence does not establish protocol-6 production deployment, an Actions-launched
provider turn, or live coverage of every org, personal, fallback and withdrawal path.
See [Phase 5 closeout](../proposals/architecture/phase-5-plan.md#implementation-status)
and [operation/reproduction](../antigravity.md#reproducible-checks).
