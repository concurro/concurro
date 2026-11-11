# Proposal: fewer, larger issue artifacts

**Status: phases 1–4a committed; phase 4b implemented with passing traffic criteria, awaiting review.
Phase 5 remains a final release review. No deployment authorized.** Builds on
the committed `limits-cleanup` baseline (`064a3b5`), not the deployed engine. No deployment, pilot commands,
artifact deletion or diagnostic writes are authorized by this document.

Make a report's first durable artifact serve both recovery and human reading, and avoid
routine collection scans. Store canonical data inside an HTML comment; derive multipart
packing from the artifact response budget and GitHub's body ceiling. Keep the mutable
audit compact. Larger parts are a side benefit, not the main source of savings.

## Evidence and starting point

Claude's 2026-11-11 probes initially found failures around 1.8 MiB with rendered data.
Follow-up probes isolated rendering as the cause of that practical ceiling:

| Encoding | Body | Rendered HTML | Detail response | Observation |
| ---- | ---- | ---- | ---- | ---- |
| JSON fence | 216 KB | 4.70× body | 2.6 MB | Exact readback |
| Text fence | 216 KB | 2.88× body | 1.8 MB | Exact readback |
| HTML comment | 310 KB | 114 bytes | 0.7 MB | Exact readback |
| Text fence | 1.61 MiB | 2.88× body | 14.0 MB | Exact readback, close to transport ceiling |
| HTML comment | 1.61 MiB | 114 bytes | 3.9 MB | Exact readback, repeated twice |
| HTML comment | 6.2 MiB | 114 bytes | 14.9 MB | Exact readback |
| HTML comment | 12.5 MiB | 114 bytes | 29.8 MB | Exact readback, beyond current Concurro transport |

These are observations, not universal bounds. Comment probes also listed successfully;
Claude reported deleting every probe artifact. The earlier UTF-8 probe established a
16 MiB rejection boundary: the largest permitted body is **16,777,215 bytes**. The comment
probe used a small fixed readable view and escaped `<`, `>` and `&`; it did not test the
proposed `>`-only encoding. Neither the measured 2.3× response ratio nor 4.7× HTML expansion
is a guarantee for arbitrary content. The old 1.5 MiB choice and proposed 32 MiB transport
increase are superseded; there is no separate fixed part-size target.

Encoding measurements above were supplied in Claude's review: report-like JSON with
quotes, `<`, `>` and `&`, a fixed neutral view, POST, exact GET readback, listing and cleanup.
They are reported measurements, not committed response fixtures. Add representative
serialization/rendering fixtures when implementing; session scratch paths are not evidence
dependencies.

The baseline separates capacities (`engine/protocol.py`): 256 KiB data parts,
60,000-byte comment/view allowances and a separate 60,000-byte audit target with immutable
overflow. That audit target is an operating choice, **not GitHub's artifact limit**.

- `Store.checkpoint_reference` keeps history directories and unfinished tails in the audit,
  with immutable completed eight-entry blocks. Retain this design.
- `Controller.complete` checkpoints before semantic validation. Small responses can remain
  inline (`report_store.INLINE` is 12,000); larger ones may already have canonical artifacts.
- `conversation.publish` creates a separate readable format-5 archive after `Store.put`.
- `GitHub.pages` requests ten artifacts per page; `service.transport.LIMITS` allows
  16,000,000 bytes for artifact responses. These are current code settings, not GitHub limits.
- `Store.put` scans both through its legacy archive index and through `matches`; disabling
  only the legacy scan would not remove normal collection reads.
- `design_audit.archived_report` reads view/data links; `measure` deduplicates by artifact
  number. Shared artifacts require event identity to remain separate.

The [current measurements](../reports/limits-cleanup/README.md#artifact-consolidation)
show 54 data artifacts for 48 reports: 48 canonical reports and six history blocks,
excluding readable archives. Bigger parts alone cannot reduce that count.

### Audit version pagination: verified read-only

On 2026-11-11, GETs of
[issue #35's existing audit](https://github.com/concurro-demo-upstream/stacks-core/issues/35/artifacts/1)
confirmed the documented `per_page`/`page` parameters. No artifact or pilot was changed.

| Detail GET query | Returned versions | Response bytes | `has_next_page` |
| ---- | ---- | ---- | ---- |
| None | 134 down to 105 (30 versions) | 5,864,881 | true |
| `per_page=1&page=1` | 134 only | 392,292 | true |
| `per_page=1&page=2` | 133 only | 394,482 | true |
| `per_page=1&page=100` | 35 only | 318,244 | false |
| `per_page=1&page=101` | None | 196,361 | false |

Every response separately contained the same current `artifact.body` (57,433 bytes) and
`body_html` (131,566 bytes). Page two changed the history entry, not the current artifact.
`total_count` was 100 despite the latest revision being 134. Boundary requests expose only
revisions 35–134; version 1 cannot be recovered through this pagination. This establishes
a retrieval window for this artifact, not whether GitHub physically deletes older data.
The latest-only response was identical with Concurro's `2022-11-28` API-version header;
the boundary requests used that header too. The [trimmed evidence](../reports/limits-cleanup/audit-pagination-evidence.json)
records queries, sizes, version numbers and response hashes, without pilot bodies.

[GitHub's API documentation](https://docs.github.com/en/rest/issues/artifacts#get-an-issue-artifact)
specifies newest-first history, default page size 30, and `per_page`/`page` controls.
Thus normal audit reads can request one version; history does not need to inflate every
read. This still returns the current body twice, not a body-only response.

Do not blindly change every read to latest-only. `Checkpoints.load` verifies the dashboard
pointer's revision/digest and legacy migration history; `Checkpoints.read` can need the
plan's first version to establish provenance. Fetch older pages when those existing proofs
need them, preserving actor, identity and digest checks. Missing/unavailable history does
not establish a match. Audit comment encoding must retain readers for old fenced versions
and the exact old-body comparisons used during migration.

### Dashboard witness and uncertain publication

Keep two compact pointer records in the current audit: the last confirmed dashboard pointer
and, when needed, one pending pointer. Both are scoped to the verified audit identity
(`id`, `number`), with `revision` and `digest`. They are evidence about dashboard publication,
not alternate task snapshots: task state always comes from the current verified audit.

1. On load, verify the comment's pointer against a bot-written historical version or an
   exact confirmed/pending record in the current bot-written audit. Preserve repository,
   issue, artifact identity and comment-author checks. A pointer verified from history may
   seed the confirmed witness; never seed it merely by copying an unverified comment.
2. Complete ordinary checkpoint saves, obtaining the newest durable audit revision R and
   its digest. Freeze the dashboard publication pointing to R, retain its pending receipt
   in memory before sending, then write and read back the comment. **Do not save an extra
   audit revision before this write.** The next ordinary audit save records the result as
   confirmed if readback matched, otherwise as pending alongside the previous witness.
   Every intervening checkpoint path must include that receipt; it cannot wait until a
   later successful dashboard publication.
3. A crash before that next save leaves R as the latest durable audit revision, so its
   evidence is still available. Reload verifies the observed comment and reconciles any
   possible publication of R before advancing to another pointer. If the comment still
   shows the old pointer and the write outcome is unknown, retain R as the possible pending
   candidate in the next save; an old-pointer readback does not prove a timed-out write
   cannot still land. This closes the gap where the in-memory receipt was lost.
4. If write or readback is uncertain, carry both records through subsequent audit saves.
   Keep pending until its publication is observed or non-application is definitive.
   Reconcile the same pending publication before advancing to a third pointer. Existing
   ownership and write-recovery rules apply. This serializes dashboard publication, not
   model work or task checkpoints. A load observing pending can confirm it; observing the
   old pointer retains both. Persist promotion in the next ordinary save, then retire the
   old witness. No growing list or per-dashboard immutable artifact is needed.

**Freshness is separate from pointer validity.** Replace `pointer != self.reference()` as
an unconditional reason to rewrite the dashboard. Compare the desired rendered dashboard
with the observed comment, excluding only its validated recovery-pointer line. A digest
of this rendered content may accompany the publication receipt. Do not hash task state
alone: `Dashboard` also uses configuration, plan links and controller-provided display
inputs. Keep the other plan-repair/dirty checks. Witness-only saves must not cause comment
rewrites, while actual display changes still do. Never treat an invalid pointer as valid
merely because the visible text matches.

On recovery, newer visible content may be published with the same pending pointer; this
does not introduce a third pointer. The pointer witnesses publication, while task state
continues to come from the current audit.

This adds readback, but no dedicated pre-publication or confirmation audit save. Record
receipts in ordinary saves; include actual traffic in measurements rather than promising
zero overhead. Failed readbacks can span more than 100 audit updates once both candidates
are carried forward. An old pointer with neither accessible history nor an existing witness
remains unverifiable; this does not retroactively recover already missing evidence.

### Unbound legacy plans with unavailable origin

Modern plans already have an audit-stored `plan_binding`; keep using it. For an unbound
plan, first try to establish managed origin from available history. If version 1 is outside
the accessible history window, **leave that artifact untouched and create a new generated
view from the verified task state**, then bind the new artifact. Creator plus type does not
prove managed origin; do not adopt or overwrite the old document on that basis.

This changes the existing `plan_history` behavior for an otherwise valid unbound artifact,
not identity/tampering failures or unavailable GitHub reads. Preserve existing handling of
unrelated App plans with known origins and of genuinely conflicting managed views.

Persist a replacement intent before creation, recording the identities of legacy artifacts
being preserved and the expected new view's content identity. Replacement discovery must
skip those recorded originals, recover a matching newly created view after a lost reply,
and save `plan_binding` rather than creating another replacement on each restart. Use the
existing artifact write/readback checks and recovery scan. A successful binding directs
later reads to the new view; old artifacts and their links remain unchanged. Never derive
an authoritative plan from the unverifiable artifact: the audit remains its source. If the
audit contains no plan/draft to render, preserve the old artifact without creating a view.

This is view repair only: no repeated intake, plan approval, model call or budget charge.
Legacy comment-to-audit migration still needs its own existing proof; replacing a plan view
does not manufacture missing audit history.

## Proposed design

### 1. Comment encoding and response-budget packing

Use versioned machine records inside HTML comments for audit data, report data, history
blocks and overflow metadata. Keep a small readable view outside the comment. This data
remains public through GitHub's raw API; hiding it in rendered HTML is not access control.

Serialize JSON first, then escape `>` as `\u003e` inside that serialized payload to prevent
comment terminators: `-->`, `--!>`, `<!-->` and `<!--->` all require `>`. JSON emits
this character only inside strings. Decode with the JSON parser only, never a reverse text
replacement: literal `\u003e` input is serialized as `\\u003e` and must stay literal.
Test the minimal encoding against hostile comment syntax, nested
comment openers, literal escapes and Unicode before relying on it. The live probe escaped
all three characters, so its success is not proof of this variant's rendering. Decoding
must recover the original data and logical digest exactly. Do not use JSON/text fences
for new machine payloads; retain legacy readers.

Expose one artifact-response budget, shared by packing and transport, rather than an
independent part-size knob. Keep today's 16,000,000-byte transport setting unless measurements
justify changing it. Pack each complete UTF-8 body against both:

- GitHub's measured hard ceiling of 16,777,215 body bytes, including every marker, link,
  view, layout descriptor and escape.
- The response budget, accounting for POST replies, detail GETs and one-item listings.

Calculate the JSON-escaped raw-body contribution using serialization verified against
captured GitHub responses: a latest-only detail read carries current and version bodies.
Calculate each response shape separately: a one-item listing carries one body/view;
a latest-only detail read carries two. Include envelope/metadata overhead and repeated
rendered views where present. The readable view's HTML
needs a measured allowance; 4.7× is an observed calibration point, not a proven maximum.
Record the chosen estimator and evidence when implementing. Test controlled view shapes,
escaped text, multipart links and actual transport decoding. Do not replace the old limit
with a hardcoded 6.5 MiB part size, assume worst-case JSON escaping without checking, or
promise that a 40 MB response budget admits every body near GitHub's ceiling.

Shorten the readable presentation before splitting canonical data just to accommodate a
duplicate human view. Never shorten canonical data. A small neutral view is the fallback
for malformed or unusually expansive output. Retain separate comment/PR-body capacities
and the compact audit target; do not turn the audit into a growing mutable report archive.
Normal latest-only audit reads use the same two-body accounting; history reads additionally
include the requested historical version, which may use the old rendered encoding.

### 2. One artifact serves the report and its reader

The first durable artifact contains the readable view plus a versioned canonical record.
Reuse `report_view` and `report_store` where they fit. Combining them only at publication
would leave the earlier retention artifact behind. Small inline responses need not be
uploaded early: when publication first stores them, use the same core/digest and format.
Never postpone checkpointing a response until validation or another reviewer finishes.

Label the view **submitted model output**. Controller acceptance/rejection, fixed validation
diagnostics and attempt attribution stay in the audit/index and notices, linking to that
same artifact. A model's own approval must not appear to be controller approval.

Verify the stored view text and its continuation trailer on reads; do not re-render evidence.
Freeze the write-time renderer and its formatting dependencies, with byte-pinned goldens for
interrupted writes. Freeze renderer version and inputs before writing. Keep the view content-derived, independent
of event IDs and mutable controller annotations, so identical evidence remains reusable.
Preserve `report_content`/`report_annotations`, logical digests and approval bindings. Use
core references plus annotation snapshots in existing receipt/history records instead of
another per-report wrapper artifact. Exact responses and published ratings/attribution must
remain reconstructible and survive later changes or trimming; count any additional storage.

`conversation.publish` verifies/reuses the report, records its event receipt/index entry and
fulfils existing notifications without a second readable-archive POST. Native PR reviews,
finding threads and their receipts do not change.

**Artifact identity is not event identity.** Distinct attempts may share content. New index
entries need stable event identity and corresponding audit deduplication, with legacy
interpretation preserved. Never collapse votes, timing, attribution, notifications or source
bindings merely because artifact IDs match. Queued format-3/4/5 events retain frozen bytes.

### 3. Frozen multipart layout and recovery

Upload continuation parts first, then the data-bearing entry part with links to their
verified artifact IDs. Do not edit the entry after creation or create a separate view-only
artifact. Current `Store.put` rejects continuations without part one; the new encoding must
explicitly support recovering this state while keeping old encoding rules intact.

Freeze encoding, renderer inputs and part boundaries before sending the first write.
Every part carries a layout identifier derived from encoding version and boundaries, bound
to the content digest. A digest alone cannot reconstruct boundaries: include a compact,
self-contained layout descriptor in every part so any surviving continuation can recover
them even if the write reply and local receipt were lost. Include descriptor overhead in
packing. The descriptor identifies the encoding and boundary units; do not recompute it
from the current budget. Freeze entry links from recovered continuation receipts before
its POST so a lost entry reply is matched against the same bytes.

Legacy interrupted writes finish their original layout even if configuration changes,
including their original 16,000-character or 256 KiB encoding. A lower transport allowance
must not silently repack an existing upload; report when its artifacts cannot be read with
that allowance. Section 4b leaves partial new-encoding sets unused rather than continuing
them, and uses a new nonce/layout for any replacement upload.

A 500, timeout or lost reply can follow successful creation. Keep exact marker/body,
authorship, version and digest verification. Missing reads or failed scans never prove
absence. Do not resend an uncertain part under a different layout within that upload.
Section 4b permits fresh replacement uploads under a new nonce after incomplete recovery;
legacy uploads keep the original recovery rules.

### 4. Ordinary writes versus discovery

Use known durable references and digest receipts for ordinary saves. A process-cache miss
with a durable reference needs a direct verified read, not collection discovery. This
applies to `Store.put`'s legacy archive lookup, `matches`, and `checkpoint_reference`.

Phase 4a indexes canonical references from the report index and publication receipts,
including small reports that remain inline in the audit. Reuse verifies their recorded
artifact IDs through the existing reader; invalid references never fall back to discovery.
Restored history directories retain their representation across saves and tail appends,
without searching for a hypothetical legacy whole-list copy. These are process-local
lookup hints, not new persisted fields. Known legacy whole-list references stay whole.

Missing references are different: today they do not distinguish fresh content from a
successful write whose reply/checkpoint was lost. Phase 4a keeps discovery on that path.
That preserves correctness but leaves routine first writes too expensive; phase 4b below
specifies attempt-tagged uploads and recovery without a separate storage journal.

Proposed duplicate policy for the new immutable encoding: accept several controller-owned,
unedited artifacts only when marker, name, layout and exact body bytes agree. On discovery,
choose the lowest-numbered verified copy; preserve already recorded valid artifact IDs
and links rather than retargeting published receipts. Different bytes under one marker
remain a conflict. Keep legacy and mutable-audit duplicate rules unchanged. This policy was
implemented in phase 2; phase 4b preserves it within each write identity.

Identical-duplicate acceptance is not a replacement for layout recovery: after a lost reply
and budget change, recover the original descriptor before continuing. Nor does it eliminate
unreferenced copies. Phase 4a and legacy writers still scan ambiguous missing references.
Phase 4b replaces that ordinary-write rule with fresh nonces, reserving discovery for
attempt recovery and legacy paths. Do not promise zero unused uploads or zero cold
recovery requests.

Use one artifact per discovery page for the larger bodies, leaving other endpoints unchanged.
Measure the extra GETs; do not add adaptive pagination speculatively. If page sizing later
changes within recovery, restart at page one rather than changing offsets mid-scan.

Fewer artifacts reduce requests, not necessarily downloaded bytes: a cold collection scan
still returns every body. Larger parts also make interrupted uploads more expensive.
Existing artifacts remain; no deletion, repacking or mutable bulk archive is proposed.

### 4b. Attempt-tagged uploads

Phase 4a's reference reuse is correct but insufficient for release: new outputs still scan.
Use the model attempt already saved before dispatch, rather than adding a storage journal.
`Controller.ask` calls `begin_attempt`, charges and saves before invoking the agent;
`Design.reserve` does the equivalent for each batch member. Both later record session
completion and call `Controller.complete` before applying the result.

**No additional intent PATCH, committed-state copy, activation checkpoint, receipt pages or
one-active-object rule.** Preserve the existing audit and publication ordering. This replaces
the earlier journal design; it changes immutable upload/recovery behavior, not task authority.

#### New writes and identities

- Resolve known references first through the verified reader. Never replace a referenced
  artifact that fails verification. New unreferenced objects use a fresh write nonce, shared
  by all their parts. Markers bind nonce, content digest, part index and frozen layout digest.
  Creation does not build the legacy archive index or call `matches`/`collection`; even
  updating an in-memory collection cache must not lazily trigger a scan.
- A nonce distinguishes a new upload from old copies of the same content. It does **not**
  assert that the content has never existed. Keep content-based reuse through known receipts
  and verified caches. Every part retains its layout descriptor; continuations precede the
  entry, and the entry pins their identities. Freeze rendering/packing for that upload.
- Derived data (history blocks, overflow, preflight data and first publication of a report
  already committed inline) needs no pre-upload journal. Recompute it from retained task
  data after a lost reply, using a new nonce if necessary. This may leave unused complete or
  partial sets. Preserve preflight's rule that storage completes before branch/PR writes;
  never install its hypothetical future task state during recovery.
- Readers group new parts by nonce before checking layout and body commitments. Several
  complete verified sets for one content digest are valid: prefer existing referenced IDs;
  without a reference, choose the complete set with the lowest entry artifact number.
  Never combine parts across nonces. Incomplete unreferenced sets are not usable evidence;
  conflicting copies within one nonce remain errors. Known missing references cannot be
  bypassed by selecting a different set. Legacy markers retain their existing rules.

All new-encoding writers use this path, including nested reports and overflow. Already
queued legacy writers and interrupted old encodings retain their bytes and discovery rules.
Land versioned readers before enabling new writers, under the compatibility rule below.
No separate activation/bootstrap storage protocol is required.

#### Completion record for uploaded model responses

Only the root representing a newly retained model response carries a completion record;
ordinary derived copies must not claim to complete an attempt. Bind repository/issue,
operation ID and attempt, stage/reviewer or batch member, request/contract and session identity.
The attempt tag is discoverable in the new part markers. The entry commits to the completion
record, including:

- The retained logical-response digest plus annotations and rejected-output reconstruction,
  pointing to the canonical record in the same upload. Do not store a second response copy.
  Reconstruct the logical response from those pieces and verify its digest before restoration.
- Session scope/identity, expected previous turn, completed turn and the exact `completed`
  record written by `record_session_turn` (`call`, request digest, response digest).
  That response digest precedes `Controller.complete`'s classification normalization: do not
  recompute it from the normalized retained response.
- Normalized timing, if observed, and the batch member's completion status. Do not fabricate
  timing, restore eligibility to true, or restore an entire stale task snapshot.

These fields are normally small, not subject to a new fixed-size cap. Include all completion
bytes in response-budget packing and integrity checks; large reconstruction metadata can be
part of the multipart record whose entry commits to it. Recovery requires the complete
record and all referenced data. Do not publish credentials, transcripts or raw provider errors.

If a detached response reuses an existing canonical core, upload nothing and do not add a
separate completion entry. The older artifact cannot be edited and is not proof of this
attempt's result. A crash before the checkpoint in this case retains today's retry behavior,
as does an inline response lost before commit. This deliberately leaves those windows open
to avoid a new artifact type and recovery path. Small inline responses are not uploaded
solely for this feature; test both exceptions without claiming that every paid call recovers.

#### Recovery before replacement admission

`Controller.recover` scans abandoned `started` attempts at its existing call site in
`Controller.ask`, **before** `max_model_calls`,
credential acquisition and `begin_attempt`'s uncertainty check: a response already paid for
must remain recoverable at either budget limit. For `plan_qc`, keep `sessions.prepare` and
`design_eligibility.prepare` before recovery. They preserve an unchanged scope; if preparation
rotated the session, the completion record's session/previous-turn check rejects it as stale.

1. Scan only a persisted **`started`** attempt from an abandoned invocation. Do not add scans
   for `uncertain`, `failed` or legacy completed-without-response records; retain their
   existing handling. In `Design.open`, recover a started member before it becomes uncertain
   and before `reserve` checks admission. A changed batch binding discards stale members
   without scanning. Share one collection result across recovering members. Never scan or
   infer failure for a handle still running in the current serial invocation or
   `review_batch.run`'s live set. Keep ownership/lost-generation semantics unchanged; no new
   exchange polling path. Existing completed responses still return through normal recovery.

2. Verify App authorship, unedited parts, namespace, complete layout, content and completion
   record against the durable attempt, contract and session. Different responses or incompatible
   completion records claiming the same attempt are a conflict, not a lowest-number tie-break.
   Identical complete copies may use the selection rule above. Foreign/stale attempts cannot
   restore current work; invalidation or explicit session restart is not reversed.
3. Restore `operation.response` and completed status, its timing and the session completion
   update as one checkpoint. Set the expected next turn, not an unconditional increment:
   replay of an already-applied completion is a no-op. Reject a mismatched session rather than
   overwriting newer work. For batches, restore only the matching member before `reserve`.
   Preserve all call/credential budgets, prior findings and eligibility decisions.
4. Feed the retained response through the existing semantic validation/application path.
   Recovery proves completion, not approval. A lost recovery-checkpoint reply repeats this
   reconciliation without spending another model call or advancing the session twice.
5. If a successful complete scan finds no complete matching result, keep today's source-loss
   and retry behavior. A failed read/list is not a negative result: propagate the read failure
   rather than charge a replacement based on incomplete discovery. A positively conflicting
   matching set remains a storage error, not permission to choose an unrelated copy.

This recovers a complete attempt-tagged upload whose reply or checkpoint commit was lost.
It cannot recover a never-uploaded or inline response, a partial multipart result, or a
reused core without this attempt's completion metadata. Those windows retain phase-2 retry
behavior; no payload spool or additional retry allowance is introduced.

Never resume a partial new-encoding upload after interruption. Leave its nonce and parts
unused. If a verified source is available, create a fresh upload with a new nonce and a
layout fitting the current budget; otherwise use today's source-loss handling. First check
for a complete matching attempt result, so an entry whose creation reply was lost is not
mistaken for a partial upload. Within an upload, boundaries remain frozen; old interrupted
encodings alone retain their continuation/repacking rules. Complete referenced layouts remain
readable without alteration.

#### Measurement and acceptance

The [retention evidence](../reports/limits-cleanup/retention-save-traffic.json) replays phase 2
(`795a639`) and phase 4a with the same 155-artifact fixture. Each simulated call starts a new
`Checkpoints`/`Store`, with no cross-tick cache. The complete storage cycle includes cold
load, retention, deterministic report application, checkpointing, publication, receipt save
and dashboard write/readback. It excludes model dispatch, native PR review and unrelated
controller traffic. Zero models or live GitHub APIs were called.

| First call, 155 existing artifacts | Phase 2 GETs / collection GETs | Phase 4a GETs / collection GETs |
| ---- | ---- | ---- |
| Save newly completed inline response | 18 / 16 | 2 / 0 |
| Save newly completed detached response | 19 / 16 | 159 / 156 |
| Complete inline-response storage cycle | 29 / 16 | 169 / 156 |
| Complete detached-response storage cycle | 29 / 16 | 169 / 156 |

Across six cold calls, the issue grows from 155 to 161 artifacts. Inline cycles total
**175 → 1,029 GETs**, detached cycles **190 → 1,044**; collection GETs are **97 → 951** in
both workloads. POSTs remain six and PATCHes 24. Inline retention is cheap in phase 4a, but
its first publication pays the scan instead. The earlier 48-report script reused one Store
for all calls before testing cold idle ticks; its 576 → 6 result does not measure this path.

A second fixture starts with **ten artifacts**, using five unrelated artifacts instead of
150. Its first-call retention counts are **4 / 2 → 2 / 0** for inline output and
**5 / 2 → 14 / 11** for detached output (GETs / collection GETs, phase 2 → phase 4a).
The complete cycle is **15 / 2 → 24 / 11** for either. Across six calls, total GETs are
**90 → 159** inline and **105 → 174** detached; collection GETs are **12 → 81**.
A design that adds requests can lose even to phase 2 on this small issue, so both sizes
are release comparisons, not only the artifact-heavy case.

The [phase-4b measurements](../reports/limits-cleanup/README.md#phase-4b-attempt-tagged-uploads)
meet the ordinary-write criteria below at both sizes, including production completion
metadata. Recovery traffic is reported separately.

| Case, at both 10 and 150+ existing artifacts | Required result / allowed traffic |
| ---- | ---- |
| Retention-save and complete cold per-call cycle, inline and detached | Zero artifact collection GETs on uninterrupted new-encoding writes; total GET + POST + PATCH no higher than phase 2, including completion records/readbacks |
| Interrupted attempt with complete uploaded result but lost checkpoint | One shared collection scan per recovery invocation, then direct verification and the existing checkpoint sequence; zero model calls or duplicate session advances |
| Several abandoned batch members | Reuse that one collection scan; no scan per member, part or candidate set |
| Partial or absent attempt upload | One scan before existing source-loss handling; report replacement model work separately, never describe it as successful artifact recovery |
| Inline or reused-core result lost before commit | No fabricated completion; preserve the existing retry path and report its model-call cost |
| Active in-memory runner handle | No attempt-recovery scan, no replacement dispatch |
| Derived upload with lost reply | Recompute without collection discovery; count abandoned sets and replacement POSTs |

Recovery is explicitly allowed to cost more requests than phase 2. With one-item pages,
a full collection pass over N artifacts currently takes N+1 GETs, including the final empty
page. Add direct reads of matching parts/dependencies and checkpoint writes/readbacks;
report each component and the whole recovery total. This is not the normal per-call path.
A failed scan is retried through existing error handling, never counted as proof of absence.
Do not impose a new scan cap that could silently miss the result.

Also cover history-block closure and oversized-audit overflow, full-call-budget lifecycles,
large reconstruction metadata, rejected outputs and reused cores with no extra completion
artifact. Test recovery at exhausted call/uncertainty budgets before credential acquisition,
scan eligibility by status, changed batch bindings and plan-review session preparation.
Include first-ever audit creation and queued legacy writers as separate rows; their
existing discovery is not a new-output-write regression. No setup/migration costs may be
hidden in the fixture. Inject interruption at each part POST/readback, entry completion,
response checkpoint and recovery checkpoint. Test changed budgets, multiple write IDs for
one digest, conflicting results for one attempt, stale attempts, session mismatch and replay.

Report total GET/POST/PATCH, collection requests, artifact counts by purpose, completion
metadata bytes, unused complete/partial sets, peak audit size and actual harness calls.
Bytes remain informational modeled traffic. The [phase-4b results](../reports/limits-cleanup/attempt-upload-traffic.json)
record these cases and satisfy both ordinary-write comparisons. Recovery and legacy paths
remain separate; review the results before releasing phases 3–4 together.

## Implementation order and compatibility

1. **Compact audit encoding and reads (committed):** comment-encode new audit data, preserve old readers
   and migration proofs, use latest-only reads plus history on demand. Implement the two
   pointer records and legacy-plan replacement above; pagination cannot restore expired history.
2. **Shared report representation (committed):** comment-encoded core, readable view, annotation receipts,
   event identity and readers. Define frozen multipart descriptors and continuation recovery.
3. **Budget-based packing (committed):** share the artifact transport setting, implement measured response
   accounting and the separate hard body check. Validate every response shape. Release with phase 4.
4. **Avoid ordinary scans:**
   - **4a, reference reuse (committed):** direct known-reference reads and no
     cold-save whole-list scan for verified block directories. Keep its regression tests
     and [measurements](../reports/limits-cleanup/README.md#phase-4-reference-based-ordinary-operations).
   - **4b, attempt-tagged uploads (implemented for review):** versioned readers and serial/batch attempt
     recovery first, then scan-free new-encoding writers. Cover retention, publication,
     nested reports, history blocks, preflight and overflow. Compare cold new-output saves
     and complete per-call storage against phase 2 on small and 150+-artifact issues;
     include completion records and report recovery scan costs separately.

5. **Measure complete lifecycles:** compare all artifacts and traffic, then update operator docs.

**Release phase 3 only with both 4a and 4b and their measurements.** Phase 4a removes
redundant known-reference scans but does not satisfy the request-volume condition for new
model outputs. The cold-per-call results above supersede using the warm 48-report aggregate
as release evidence. Free network traffic does not remove installation request limits.

Land readers before writers. New records may join checkpoint format 7 only while it remains
unreleased; otherwise use a new incompatible wire version that old engines reject. Check
release state at implementation. Artifact and conversation formats have separate versions.
Preserve archived links, queued publication formats, cache identities and recovery paths.

No new workload limits, approval gates or model calls are proposed. This is independent of
[consolidated PR reviews](consolidated-pr-reviews.md): storage consolidation does not change
reviewer ownership or GitHub review submissions. Any diagnostic writes require separate
authorization and must not use a pilot issue.

## Regression tests and measurements

- Exact comment-codec round trips, Unicode, literal escapes, hostile comment syntax and
  malformed output; no payload leakage into rendered HTML. Old audit/report readers remain.
- Budget/body boundaries including layout overhead, readable HTML, metadata and actual JSON
  escaping; changing the response setting changes packing without a second size knob.
- Latest-only audit reads, older dashboard pointers, legacy history and plan provenance;
  multiple pages, unavailable revisions, concurrent changes and both old/new body encodings.
- Inline and detached retention produce the same core; accepted and rejected reports reuse
  it. Crash before validation never drops a response or spends another model call.
- Lost replies at each continuation and entry write; distinguish complete new attempt
  recovery, unused new partial sets and legacy continuation recovery. Cover changed budgets,
  missing local receipts and unchanged saves.
- Exact core/response/published-annotation reconstruction; missing, reordered, edited or
  differently authored parts retain existing verification failures.
- Distinct events sharing an artifact retain separate receipts, votes and timing; cover
  `design_audit`, dashboard and publication, not only `Store.get`.
- Identical duplicate parts select a verified copy without changing pinned references;
  conflicting bodies, edited copies and unknown layouts retain recovery checks.
- More than 100 audit updates with both dashboard candidates retained; lost write and
  readback replies, old-pointer readbacks, crash before the next ordinary receipt save or
  after witness promotion, and restart without publishing a third candidate. Repeated idle
  loads and witness-only saves cause no dashboard rewrites; changed config/plan links still
  refresh the display. No dedicated receipt save or self-referential digest.
- Unbound plans with unavailable version 1 remain byte-identical while a new view is bound;
  lost replacement replies recover one view, unrelated App plans remain untouched, and
  missing task plans do not trigger invented work. No fabricated historical proof.
- Cold/warm known-reference saves and uninterrupted new-encoding first writes avoid
  collection scans; uncertain/legacy recovery still discovers unacknowledged writes. Include
  every phase-4b crash boundary, completion-record restoration and session replay without
  additional model calls.
  Full-call-budget lifecycles include parallel review, repair and publication.

Report all artifacts by purpose: combined reports, annotations, history blocks, overflow,
audit and plan views. Include POST/PATCH/GET counts, collection requests, uploaded/downloaded
bytes, peak audit size and unused artifacts after interruption. Compare frozen identical
inputs across the pending code, audit-only change and complete consolidation; run repository
validation when implementing. Separate measured outcomes from estimates.

A 48-report case could move from 48 data + 48 readable + six history + one audit artifacts
to 48 combined + six history + one audit: **103 → 55**. This is a structural estimate, not a
measured lifecycle result; plans, annotations, corrections and multipart output can add more.
