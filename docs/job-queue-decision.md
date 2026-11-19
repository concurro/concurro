# Job queue spike

The phase 1 spike keeps `graphile_worker` 0.14.2 as the job queue candidate. The
Docker-backed tests in `crates/events-postgres/tests/job_queue_spike.rs` verify:

| Requirement | Evidence |
| ---- | ---- |
| Enqueue in a rolled-back SQLx transaction | No job remains after `with_executor` and rollback |
| Replace a pending job by key | The second enqueue keeps the same job ID and one row |
| Stable effect across a retry | A handler commits its stable ID, fails, runs again, and leaves one effect |
| Retry limit and last error | A permanent failure stops at two attempts with its error stored |
| Retry backoff | The first failed attempt schedules `run_at` in the future |
| Process kill recovery | A separate worker is killed mid-job; a new worker runs the recovered job and leaves one effect |
| Repeatable service-local migration | Two migrations succeed as a service role in its own database |
| Workspace dependencies | Builds with pinned SQLx 0.9.0, Tokio 1.54.1, and Rust 1.100.0 |

The retry test simulates failure after committing the effect by returning an
error. Queue tests run against a database owned by the service role.
`graphile_worker` requires `chrono` 0.4.45 or later, so the workspace pin moved
to 0.4.45.
