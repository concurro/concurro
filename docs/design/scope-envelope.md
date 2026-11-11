# Scope envelope

The envelope authorizes paths. Optional target limits bound contribution size; it does
not prescribe builds or impose a changed-line budget.

## Plan contract

A plan names distinct primary files and may define a wider envelope:

```json
{
  "files": ["stacks-common/src/util/example.rs"],
  "scope": {
    "paths": ["stacks-common/src/util/**", "changelog.d/"]
  }
}
```

`scope.paths` contains distinct relative patterns. `limits.scope_paths` and
`limits.scope_path_characters` bound their count and length when configured.
`*` matches within a path segment; `**` spans zero or more segments. A trailing slash
is normalized to a recursive directory pattern. Empty components, absolute roots,
backslashes and `.`/`..` components are refused. Every primary file must match the
envelope. New plans do not choose a file-count budget.
Without `scope`, the primary files remain the exact allowed file set.

The planner receives `plan_output_contract` on every attempt: these limits, the
target's edit policy and required publication fields. Publication bullets should be concise;
there is no fixed bullet count or per-field prose allowance. Aggregate response transport still applies. Validation
reports independent violations together where the report shape permits it, so one
correction can address publication, primary-file and scope problems. Questions may
leave the envelope incomplete; they stop implementation rather than grant permission.

Older approved plans may contain `scope.max_files`. That allowance remains enforced,
without rewriting the plan or its approval digest. New schemas omit it. A configured
`limits.changed_files` applies in addition; neither limit overrides the other.

Older plans may contain `max_changed_lines`. It is ignored without mutating the stored
plan or approval digest, even if a new response supplies the obsolete key. New schemas
omit it. Added/removed line counts remain review evidence, never authorization limits.

## Candidate enforcement

`engine/checks.py` defines matching; `policy.validate_edits` checks each proposal and
`candidates.validate` checks the complete approved-base-to-candidate change. Path policy
and protected files apply inside the envelope too. The default-open policy allows
dotfiles, workflows and governance documents; `.concurro` and Git metadata remain
reserved. Legacy allow-list configs retain their previous hidden-path and governance
protections. See [configuration](../concurro.md#edit-policy-and-workload-limits).

Originals for an edit come from its parent (the last published head during repair).
Cumulative changed paths are judged against the approved base. Previous repair edits
therefore continue to count toward any configured or legacy approved allowance. An out-of-scope proposal is refused as a whole before
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
HTTP bounds still protect resources. Optional file-count limits and context limits remain independent
of source size. See [execution](mvp-execution.md) and [report storage](issue-dashboard.md#report-archive).

The engine does not certify behavior by inspecting a diff. Agent validation is advisory;
independent current-head review and project CI govern readiness. Meaningful design growth
may reopen planning. Parent/sub-issue decomposition remains [future work](mvp.md#future-parent-and-sub-issue-planning).
