# Phase 2 C1 local exit evidence

The exit table is `docs/proposals/architecture/phase-2-plan.md:504`. "Composed" means producer/storage selection and real NATS/service effect were asserted in separate tests. The full-stack Python case uses signed ingress HTTP, PostgreSQL, the Rust forwarder, NATS, and the Python service. The independent Opus source review closed R1–R3. Coordinator acceptance passed the final full-stack and lost-ack Python tests plus the replay-generation, gap-barrier and receipt-lock Rust regressions.

| # | Evidence | State | Remaining exact scenario |
| ---- | ---- | ---- | ---- |
| 1 | Rust `maps_every_github_family`; Python raw-vs-mapped `test_routing_and_projection` for every fixture/action and reviewer path; full-stack signed issue-comment effect | Composed | All action fixtures through signed HTTP in one run. |
| 2 | Rust `webhook_http_receipt_paths`: bad signature, size, other installation, no outbox effect | Direct | None. |
| 3 | Full-stack `event_too_large` receipt survives as `unmappable`; recovery scan cannot advance past it; list becomes `catching-up`; larger cap and `remap` publish it once, then coverage and list recover | Direct | None. |
| 4 | Full-stack same GUID after consumption returns 200 and retains one service receipt; Rust atomic dedup; Python HTTP/bus overlap wakes once | Composed | Exact post-consumption task wake count through signed ingress. |
| 5 | Full-stack PostgreSQL stop returns 503; after restart the fake failed listing triggers one signed redelivery, one receipt, and one service effect | Direct | None. |
| 6 | Full-stack pauses NATS while signed webhook returns 202 and outbox row stays unpublished; after unpause service receives it once | Direct (short outage) | Ten-minute duration is not simulated. |
| 7 | Live `test_backlog_survives_consumer_restart_once` uses real NATS, registry and list index | Direct | None. |
| 8 | Live NATS drops the Python ack after the effect commits, redelivers after `ack_wait`, then injects one list transaction error; the retry commits one receipt/effect | Direct fault injection | Process kill at the exact instruction boundary is represented by a lost ack. |
| 9 | Python bus apply failure/rebuild tests and live service projection | Composed | One unprojectable fact through live NATS into completed rebuild. |
| 10 | Rust initial/expired gap tests; live `test_initial_and_expired_gaps_reconcile_once_each` | Composed | Real recovery scans and service strict repair in one run. |
| 11 | Live real-NATS `max_age` expiry and replay; full-stack real ingress replay on stream replacement | Composed | Real ingress replay on `max_age` expiry. |
| 12 | Full-stack real ingress replay after stream replacement; live pruned-history strict repair; Rust zero-floor and retained selection | Composed | Real ingress for pruned-history and zero-floor variants. |
| 13 | Rust late older-position row selected and pruned boundary; real replay transport/effect in full stack | Composed | Late older row through real service after stream loss. |
| 14 | Rust retained gap replay blocks barrier until published; live strict gap repair; full-stack real replay and gap | Composed | Delete stream before service applies the gap in one run. |
| 15 | Live parked event remains `catching-up` through stream replacement, replayed event and new coverage until operator replay | Direct with replay stub | Real ingress replay of parked event. |
| 16 | Rust position replay selects a row whose fixture time precedes stream creation by one day; real full-stack replay | Composed | Explicit two-minute ingress clock skew with real replay. |
| 17 | Rust replay runs with active forwarder on another connection; retained gap and delivery replay blocks coverage while delivery publishes and gap fails; live gap reconciliation | Composed | Concurrent real NATS gap publish failure, finalizer and service loss resolution. |
| 18 | Rust `coverage_and_receipts_follow_lock_order` pauses coverage behind a writer lock, then queues a writer behind coverage lock; asserts both receipt timestamps | Direct | None. |
| 19 | Live simulated-hour idle coverage and restart backlog freshness | Direct | None. |
| 20 | Python `test_http_and_bus_overlap_wakes_once` | Direct | None. |

Verification: Rust nextest workspace/all features 64/64; Python contracts 20/20; Python service 771/771 across 57 files. Focused later Rust replay/lock tests pass. Clippy all targets/all features `-D warnings`, Rust formatting, Ruff check/format, and diff whitespace checks pass. No production corpus or cutover was run.
