# Scope envelope

Status: implemented; audited 2026-11-07. The envelope authorizes paths and a cumulative
file count. It does not prescribe builds or impose a changed-line budget.

## Plan contract

A plan names 1–8 distinct primary files and may define a wider envelope:

```json
{
  "files": ["stacks-common/src/util/example.rs"],
  "scope": {
    "paths": ["stacks-common/src/util/**", "changelog.d/"],
    "max_files": 12
  }
}
```

`scope.paths` contains 1–8 distinct relative patterns of at most 200 characters each.
`*` matches within a path segment; `**` spans zero or more segments. A trailing slash
is normalized to a recursive directory pattern. Empty components, absolute roots,
backslashes and `.`/`..` components are refused. Every primary file must match the
envelope. `max_files` is an integer at least the primary-file count and at most 32.
Without `scope`, the primary files remain the exact allowed file set.

Older plans may contain `max_changed_lines`. It is ignored without mutating the stored
plan or approval digest, even if a new response supplies the obsolete key. New schemas
omit it. Added/removed line counts remain review evidence, never authorization limits.

## Candidate enforcement

`engine/checks.py` defines matching; `policy.validate_edits` checks each proposal and
`candidates.validate` checks the complete approved-base-to-candidate change. Path policy
and protected files apply inside the envelope too. All dot-prefixed path components are
refused, including `.github` and `.concurro`; an approved glob does not override that.

Originals for an edit come from its parent (the last published head during repair).
Cumulative changed paths are judged against the approved base. Previous repair edits
therefore continue to count. An out-of-scope proposal is refused as a whole before
publication and returned as feedback; no permitted subset is silently applied.
A revised plan against an open PR must cover the accumulated changed paths.

Additions, replacements and deletions must be supported regular UTF-8 text files without
NUL bytes. A deletion names an existing parent file; a rename counts as deletion plus
addition. Modes are preserved, and symlinks, binary and mode-only changes are refused.
Generated paths still count as files and obey the same path and transport rules.
There is no execution-profile approval, mandatory check catalog or requirement to edit
every primary file merely because the plan names it.

## Review and publication capacity

Source file size is not a policy rejection. Design review receives planned-file metadata
(path, blob identity, size and mode); agents inspect relevant ranges in their checkout.
Citation validation reads the pinned source independently of the prompt preview.

PR reviews cover every changed file, including deletions and accumulated repairs.
Changed-file identities, statistics and findings remain even when diff previews shrink.
Generated-file summaries can replace raw previews. The repository remains available for
full inspection. Full selected feedback can travel as verified runner files rather than
being duplicated inside a prompt or checkpoint.

Large replacements are uploaded as Git blobs before tree creation; deletions use explicit
`sha: null` entries with a base tree. Aggregate exchange, model-response, tool-output and
HTTP bounds still protect resources. The file-count and context limits remain independent
of source size. See [execution](mvp-execution.md) and [report storage](issue-dashboard.md#report-archive).

The engine does not certify behavior by inspecting a diff. Agent validation is advisory;
independent current-head review and project CI govern readiness. Meaningful design growth
may reopen planning. Parent/sub-issue decomposition remains [future work](mvp.md#future-parent-and-sub-issue-planning).
