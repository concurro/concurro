# Phase 3 M2 acceptance evidence

M2 is accepted for local implementation. It runs one contribution from intake
through a ready pull request, including the task commands and recorded recovery
paths in the [M2 map](proposals/architecture/phase-3-m2-map.md). M3 and cutover
remain in progress. No live GitHub, provider or runner publication is claimed.

## Reviewed source

The signed M1 base is `43f5346fe8d524ce3a2de11f0a5a908d1febba0d`.
The frozen M2 package `m2-acceptance-20261120-142334` has manifest SHA-256
`c0943ecad37b8aba20115071b3ea5cfe1b442beeca0f7cddb7160f32e04d4bd4`.
The eight-file correction `m2-corrective-20261120-1447` has manifest SHA-256
`8d002ec4383340c556e0decc5e5802c79e9b9491f2db4d96c0e32d328da048b3`.
The correction applies to that frozen M2 source; its base and result file hashes
were checked independently. Changes remain uncommitted for review.

## Local checks

| Source | Result |
| ---- | ---- |
| Frozen M2 workspace | Nextest `c7879f6c-f7c9-4b73-85ee-db4bde19a4ee`: 148 passed, none skipped |
| CI event fault injection | 13 passed, none skipped |
| Eight-file correction | Nextest `57d296a4-49ec-4150-94cb-c4b8155ee6cf`: 3 selected tests passed |

The corrected source passed formatting and focused native-review, PostgreSQL
recovery, seat-ordering, operator-write and Git publication checks. The full
workspace suite ran on the frozen source before the correction; it was not
rerun after that delta. Independent review found no remaining material M2
defect under the repository's quality tolerance.

The task process and local fixtures cover durable stage changes and restarts.
The dedicated live test repository was waived for phases 1–3. Deployment,
cutover and live task retries require separate authorization.
