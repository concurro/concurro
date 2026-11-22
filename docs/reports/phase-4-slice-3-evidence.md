# Phase 4 Slice 3 evidence

Implemented on `feat/phase-4-slice-3` from signed Slice 2 commit `8617054`.
No deployment, live GitHub write or App permission change was made during
implementation.

## Implemented

- Accounts serves the current credential list and entry mutations, relay code
  preview and approval, device replacement, and relay status/setup. Every vault
  call carries the signed-in user's bearer. Body fields, role filters, revisions
  and mutation IDs follow the existing Python adapter and frontend contract.
- Writes require the session cookie and `X-Concurro-Request: 1` before body
  parsing. Malformed bodies return fixed `422 {"detail":"malformed request"}`.
  Vault `401` ends the local session; vault `409`, `410` and `422` retain its
  status and message. Transport outage returns `503`, while relay status reports
  `service: unavailable` with configured setup.
- Ordinary vault calls use a 30-second deadline. Relay approval and device
  replacement use 60 seconds. The accounts process does not store secrets or
  log credential request bodies.

## Python oracle and parity

Before writing Rust routes, the six existing
`backoffice.tests.test_portal_lifecycle` tests passed in a disposable environment.
The synthetic fixture [`credentials_python.json`](../../crates/accounts/tests/credentials_python.json)
was then captured through the real `backoffice.credentials.Vault` adapter and
its mapping functions with an `httpx.MockTransport`. Its inputs mirror the
existing portal lifecycle tests and vault fixtures. It records 16 vault
requests/responses and mapped browser results, including list, entry and relay
operations, relay unavailable status, and `401`/`409`/`410`/`422` errors. The
normal one-route relay approval uses the vault's bare-entry response; a separate
two-route case uses its `entries` map. All tokens and secrets are synthetic.
The reproducible migration capture script is
[`capture_credentials_python.py`](../../crates/accounts/tests/capture_credentials_python.py).
From the pre-retirement Python checkout, run it with the pinned Backoffice test
dependencies and `PYTHONPATH=backoffice/tests:gateway/tests:.`, redirecting
stdout to `crates/accounts/tests/credentials_python.json`. After Slice 4
retires Python, use that checkout to regenerate the baseline fixture.

The Rust integration test sends the corresponding browser requests to the real
accounts router and a fake vault, then compares exact forwarded JSON, method,
path, query, owner bearer, status and mapped response against every fixture.
Additional checks cover session and CSRF on every write, no secret echo on
malformed bodies, stale deletion, role rejection, unknown relay codes, vault
transport outage and local session invalidation after vault `401`.

## Verification

| Check | Result |
| ---- | ---- |
| Existing Python portal lifecycle suite | 6/6 passed; `/tmp/concurro-phase4-slice3-python-oracle.log` |
| `cargo nextest run -p concurro-accounts --all-targets --no-fail-fast` | Corrected tree: 8/8 passed, including prior command and authority/PostgreSQL integrations; `/tmp/concurro-phase4-slice3-corrected-nextest.log` |
| `cargo check -p concurro-accounts --all-targets` | Passed |
| Focused credential/settings tests after correction | 6/6 passed |
| `cargo fmt --check` and `git diff --check` | Passed |
| `cargo clippy -p concurro-accounts --all-targets` | Passed, zero accounts warnings; `/tmp/concurro-phase4-slice3-corrected-clippy.log`. Existing dependency warnings remain. |

An initial focused test rerun was denied loopback binding by the command sandbox;
the same 5/5 focused tests passed with loopback access. `cargo clippy ... -- -D
warnings` also stopped in unchanged `concurro-jsonb` and `concurro-github-v1`
warnings; the normal clippy run completed without accounts warnings.
The relay address table was also run against Python's `relay_address`: six
accepted and ten refused values matched Rust, including explicit `:80`,
bracketed IPv6 and shell punctuation refusals.

Lodestone semantic searches located the existing browser CSRF/session checks,
vault metadata HTTP request pattern, and accounts command error mapping. The
Rust routes reuse the browser header and cookie constants and session store.
A single Lodestone similarity call covered the original 40 added Rust
functions/types; a narrow follow-up call covered the two relay address
definitions. All 42 source definitions and locations are in
`/tmp/concurro-phase4-slice3-definitions.json` for the consolidated review.
`rust-mcp` confirmed the reqwest 0.13.5 request
timeout inspection API used by the deadline test.

## Review baseline and limits

After the initial 15-case integration milestone sent for consolidated review,
the production code added an inspectable request timeout helper and corrected
the current one-route relay approval mapping. The fixture initially paired one
route with a two-route vault shape; its old capture is preserved at
`/tmp/concurro-phase4-slice3-original-python.json`. The correction tests both
real vault response branches. Relay address validation now matches the Python
DNS/IPv4/bracketed-IPv6 grammar, including `:80`, and IPv6 setup commands use
the same shell quoting as `shlex.join`. The adapter is exported
as one type while its module and handlers remain private. Tests added deadline,
stale deletion, role refusal, unknown code and transport-outage assertions.
The documentation and this evidence report were completed afterward.

The real vault remains untested locally because its GitHub user endpoint is
fixed to `https://api.github.com/user`; the approved plan assigns that check to
P2 on a test deployment. Slice 4 browser cutover and Python retirement remain
out of scope.

The deadline unit test inspects reqwest's configured request timeout for each
operation class. It does not wait 60 elapsed seconds through the browser router;
the route-to-class selection is covered by code inspection and the relay wire
integration cases.
