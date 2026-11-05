# Implementation

Use the repository's Rust toolchain and contribution instructions. Run relevant tests
and the `cargo fmt-stacks`, `cargo clippy-stacks` and `cargo clippy-stackslib` aliases
where applicable. Choose a narrower test command for a focused change when useful.
Report exactly what ran and any failure or check you could not complete. These results
are agent-reported feedback; upstream CI remains authoritative for PR readiness.

Generate lockfiles with Cargo rather than editing dependency checksums by hand.
