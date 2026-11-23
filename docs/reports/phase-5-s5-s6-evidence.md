# Phase 5 S5/S6 completion evidence

Local candidate on `fee3ec52f21e9946501be7646b62fd8695349300`; all changes remain
unstaged and uncommitted. S7 owns live provisioning, routing, cutover, and retirement.

| Capability | Evidence | Status |
| ---- | ---- | ---- |
| Org rotation and provider refusal | Same-path OpenBao token/account snapshot; cached values refresh within the configured 60 seconds. A provider 401 forces one fresh read and one retry of the same admitted request only if token or account changed. Org gateway and OpenBao tests. | Added |
| Native routes and accounting | Real exchange admission, Anthropic and Responses routes, caller-header stripping, server credentials, preserved response metadata, structured usage and failure handling. Personal route and relay tests cover four custody routes. | Existing S4c/S4d paths, expanded S5 tests |
| Codex WebSocket | Pinned S1 `responses-bridge` binary reaches the Rust gateway and fake provider; `response.cancel` closes the upstream stream. Test is opt-in because the bridge is built in the separate relay repository. | Added local integration |
| Gateway lifetime | Revocation, deadline, exchange outage, stalled headers, downstream backpressure and process restart close in-flight streams. New requests fail during outage and admit after restart. Exchange's `not_reported` settlement deadline is covered by the existing admission tests. | Expanded S5 tests |
| Task mode, credentials, fallback, usage | Rust/Python mode selection, exact metadata version and custody, provider-failure fallback, contributed and standalone usage, and reconciliation were accepted in S4b–S4d and remain covered by the task suite. Python mode keeps its slot HTTP adapter; Rust mode uses local slot coordination. | Already covered |
| Withdrawal reporting | Rust-mode applied and blocked facts commit with the cutoff for issue and standalone PR browser/comment commands. Repeated withdrawals retain earlier pending revoke obligations. A restart-safe scan emits stopped only after every affected transition acknowledges and 40 seconds have elapsed. Comment step replies use the existing uncertain-create path, scoped to the correct repository. Python mode retains its single applied outcome. | Added |
| Retained request facts | Backoffice keeps its existing v2 durable consumer and adds v1 and v3 consumers; the shared typed delivery path applies old facts followed by v3 later steps. | Added |
| Inactive release packaging | Exchange and gateway Dockerfiles, commit-tagged CI image matrix entries, generic inactive Compose example with separate service-only settings and secret mounts. | Added; no live deployment |

## Checks

- `cargo fmt --all --check`, `git diff --check`, changed-crate `cargo check --offline --tests`, contract compatibility against `HEAD`, and advisory changed-component `cargo clippy --all-targets`: pass. Strict `-D warnings` Clippy stops in unchanged `concurro-jsonb` and `concurro-github-v1` warnings; no unrelated lint refactor was made. Advisory log: `/private/tmp/concurro-s5s6-corrected-clippy-advisory.log`.
- The pre-review changed-component serial suite passed **116/116**, one ignored bridge test; `/private/tmp/concurro-s5s6-nextest-serial.log`. The corrected serial suite including backoffice API ran **128 tests: 127 passed, one failed**; `/private/tmp/concurro-s5s6-corrected-nextest.log`. Its lone failure exposed an accidental Rust-mode condition on the existing label unenroll cutoff. That condition was removed; focused label and withdrawal regressions passed **4/4**, run `f0c33522-85a3-43c2-9374-aad4a2f02e21`, `/private/tmp/concurro-s5s6-correction-regressions.log`. A final borrow cleanup in that branch passed **3/3** affected tests, run `3774b0e1-4b4a-4e49-b43e-78233065748c`, `/private/tmp/concurro-s5s6-final-targeted.log`. The full serial suite was not repeated for these narrow fixes. The first sandboxed attempt lacked Docker access; the first approved parallel attempt exhausted local ephemeral ports; both logs remain under `/private/tmp/concurro-s5s6-nextest*.log`.
- Pinned S1 bridge binary test: **1/1 passed** with `CONCURRO_RESPONSES_BRIDGE_BIN` and `--run-ignored all`; `/private/tmp/concurro-s5s6-bridge-diagnostic.log`.
- Gateway process restart: **1/1 passed**; `/private/tmp/concurro-s5s6-restart.log`.
- Withdrawal PostgreSQL restart/bound: **1/1 passed** before consolidated corrections; `/private/tmp/concurro-s5s6-final-narrow.log`. Corrected focused regressions cover W1/W2 pending revocation, Python-mode omission of later steps, and standalone browser/comment routes: **3/3 passed**; nextest run `48e6a354-7339-49f8-8cd7-5e80b68235d4`. The standalone route was strengthened afterward with a real pending revoke and publication-scope assertion: **1/1 passed**, run `bcf727a2-a228-4b21-a4d7-25c92555436d`, log `/private/tmp/concurro-s5s6-standalone-scope.log`. All four affected tests passed again on the final cutoff fix.
- Backoffice request projection reorder/dedup: **1/1 passed**; `/private/tmp/concurro-s5s6-read-model.log`.
- Retained v2 then v3 runtime delivery through the shared consumer and read model: **1/1 passed**, nextest run `d873d795-7fe5-4d87-8aec-ba39d0c6a37e`.
- Coordinator's independent final-tree acceptance reran retained v2→v3 delivery, W1/W2 withdrawal, and standalone withdrawal/publication: **3/3 passed**, nextest run `9658b851-c3de-4e9b-887a-6c22aaf020af`, `/private/tmp/concurro-s5s6-root-acceptance.log`. Source review closed all four consolidated corrections; the independent 24-definition Lodestone batch found no material reuse issue.
- Inactive Compose config with distinct `CONCURRO_EXCHANGE_ETC` and `CONCURRO_GATEWAY_ETC`: pass. Both Docker images built locally; logs `/private/tmp/concurro-s5s6-exchange-image.log` and `/private/tmp/concurro-s5s6-gateway-image.log`.
- Generated schemas/Python models and fixtures: 29 Python tests passed; `contracts/check_compat.py HEAD` passed. The new outcomes are `request.tasks` v3; v1/v2 remain retained.
- CI monitoring: exact pushed `fee3ec52f21e9946501be7646b62fd8695349300`, run `45119956961`. Rust, contracts, checks, service, gateway and deploy succeeded; engine failed because a test stub did not accept the runner's existing `file_artifacts` keyword; image jobs were skipped. The uncommitted `engine/tests/test_run_display.py` stub correction passes its focused test and Ruff check/format. No CI correction was pushed.

## Limits

- Real provider credentials, paid inference, live Sponsor, host networking and S7
  cutover were not exercised.
- The WebSocket bridge test uses the separately pinned S1 source and a local fake
  provider; CI requires its external binary to opt in.
- A process kill loses in-memory gateway finish retries. The exchange's durable
  settlement deadline records `not_reported`; the gateway restart test proves
  subsequent admission independently.

All 24 new definition snippets, including `authorized_request`, are in
`/private/tmp/concurro-s5s6-definitions.json`.
