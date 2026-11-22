# Phase 5 S0 spikes

Throwaway experiments for [S0](../../docs/proposals/architecture/phase-5/s0-baseline.md).
Each directory is its own Cargo workspace, outside the main crates. The results are in
[the S0 evidence](../../docs/rust-phase-5-s0-evidence.md).

| Directory | Question | Run |
| ---- | ---- | ---- |
| `openbao/` | OpenBao 2.7.3 on PostgreSQL with the static seal, self-init and S2's policies | `openssl rand -out openbao/seal.key 32 && chmod 644 openbao/seal.key`, `docker compose -f openbao/compose.yaml up -d`, then `openbao/check.sh`, `openbao/recovery.sh` (break-glass, on a fresh stack) and `openbao/client` (see the evidence for its arguments) |
| `passthrough/` | Streaming passthrough with a usage tap, and `responses-bridge` in front of it | `cargo test` in `passthrough/`; set `BRIDGE_BIN` to a `responses-bridge` built from `concurro-relay` for the bridge tests |
| `oidc/` | GitHub Actions OIDC verification with `jsonwebtoken` | `cargo test` in `oidc/` |

Delete this directory once the slices that use these results are implemented.
