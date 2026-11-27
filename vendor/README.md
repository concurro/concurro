# Dependency patches

`graphile_worker_database` is the published 0.3.0 crate (MIT), with one change in
`src/sqlx/timestamp.rs`: explicitly select `Decode<Postgres>` for `OffsetDateTime`.
The unqualified call is ambiguous when SQLx PostgreSQL and SQLite are both enabled.
Remove this patch when an upstream release includes that qualification.
