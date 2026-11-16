# Proposal: retrospective tuning

**Status: proposal, not implemented.**

Analyse past tasks and open a pull request in the target's fork with recommended changes
to its `.concurro/` configuration. Each recommendation states a hypothesis and cites the
recorded facts behind it. Humans merge or close the PR; Concurro never applies its own
recommendations.

## Today

- **Turn diagnostics.** With `diagnostics_dir` set, `service/diagnostics.py` records each
  turn in `diagnostics.sqlite3`: stage, provider, model, effort, agent, status, outcome,
  termination reason, timings, request, reconstructed prompt, correction feedback,
  response and a transcript archive. Recording is best effort and optional.
- **Token usage.** The exchange stores sanitized usage per inference request in
  `inference_observations`, keyed by turn, repository and issue
  (`gateway/exchange/inference.py`).
- **Task state.** Checkpoints and their issue artifacts hold the finding ledger
  (`engine/reviews.py`): ID, raising profile, severity, path and lines, the head it was
  raised on, and dispositions (`fixed`, `open`, `obsolete`, `withdrawn`). They also hold
  votes, repair rounds, plan revisions, call counts, writer commands, invalidations and
  an `outcomes` record (`Controller.record_outcome`). That record is written at merged,
  rejected, escalated, blocked, stopped, awaiting-credentials and budget-exhausted
  stages. It deliberately makes no policy edits.
- **Configuration history.** A task pins its fork configuration commit and digest.
  `/concurro resume` can adopt a newer configuration during a task. `bundle_resume` and
  `resume_history` record the old and new commit and digest at each resume
  (`engine/bundle_resume.py`).
- **PR lifecycle.** `Controller.observe_merge` and `_reconcile_external` record merges
  and closes. Human review comments, commits pushed by humans after handoff, and later
  CI results are visible on GitHub only.
- **Audit measurement.** `engine/design_audit.py` measures design reviews from issue
  artifacts without model calls. It captures GitHub reads once into a `Snapshot` and
  replays them offline, verifies artifact authorship, and returns provenance and an
  explicit `limitations` list.
- **Target loading.** `service.target_loader.load_target` validates a target's
  configuration: config structure, supported harnesses, repository, App `bot_login` and
  engine pin, then the bundle.
- **Fork permissions.** `service.targets.permissions()` requests fork Contents, Actions
  and Workflows write, but not Pull requests write.

## Goals

- Turn observed task facts into concrete, reviewable `.concurro/` changes: role
  instructions, model profiles and effort, reviewer panels, budgets and, once they land,
  reviewer lenses.
- Keep facts, interpretation and recommendation distinct, so a reviewer can tell what
  was measured from what was inferred.
- Keep the operator in control: a manual trigger, one PR per run, human merge.

## Non-goals

- Changing engine skills, schemas or shared instructions in this repository.
- Applying, merging or auto-adopting recommendations, or resuming tasks onto them.
- Claims about general model quality.
- Diagnosing CI failures.

## Design

### Overview

```text
retro run ──► collector ──► dossiers + metrics ──► analyst turn ──► proposals
              (snapshot,      (publication-safe)     (one session)       │
               no models)                                                ▼
                  fork PR ◄── publisher ◄── verifier (evidence, combined target)
```

1. **Collector**: deterministic and replayable. Builds one dossier per task plus
   cross-task metrics.
2. **Analyst**: one agent turn under a new `concurro-retrospect` skill and `retro.json`
   response schema.
3. **Verifier**: checks evidence references and validates the combined patched target.
4. **Publisher**: one branch and one PR in the fork per run.

### Trigger and session

An operator command, alongside `evaluate`:

```bash
python3 -m service --settings /absolute/path/to/settings.json retro OWNER/REPO --since 2026-11-01
```

Each run belongs to a dedicated retrospective issue in the target's issue repository.
That issue provides the session binding runner turns need, and its dashboard shows the
run and links the PR. The run makes live model calls, so it needs operator
authorization, like `evaluate`.

### Population

A run covers every task in the target whose latest state change falls in the window and
whose stage is in `TERMINAL_STAGES`, `SUSPENDED_STAGES` or `JUDGED_STAGES`. Suspended
and judged tasks may continue later, so each dossier records its **observation cutoff**:
the snapshot time and the stage then. A later run re-reads them. Tasks still in active
stages are excluded.

### Collector

The collector extends `design_audit`. It captures GitHub reads into a snapshot, verifies
artifact authorship, and reads diagnostics and exchange records for the same tasks. The
same snapshot reproduces the dossiers offline.

**Configuration segments.** Each task is split at its resume boundaries into segments,
each with its configuration commit, digest and profiles per role. Every turn, finding and
vote is attributed to the segment it ran under, never to the task's latest
configuration.

**Coverage.** Each metric records how many tasks and turns it covers. Missing
diagnostics, token usage or timing are unknown, not zero, and appear in the run's
`limitations`.

**Observations and candidates.** The dossier separates two kinds of entry:

| Kind | Meaning | Examples |
| ---- | ---- | ---- |
| Observation | A recorded fact | Disposition history, correction counts, termination reasons, tokens, elapsed time, budgets used up, PR merged or closed, writer commands |
| Candidate | A fact whose meaning needs interpretation | A human commit or review comment after every reviewer approved; two findings from different reviewers on overlapping lines |

A human change after approval may be a missed defect, a new requirement, a preference or
a fix for something introduced later. Overlapping findings may or may not be the same
defect. The collector reports candidates; it does not classify them.

**Publication-safe content.** The fork is public. Dossiers contain only:

- content already public on GitHub (issue artifacts, reports, findings, PR comments and
  commits), with URLs
- enumerated or numeric fields from private stores: status, termination reason,
  correction codes, timings, token counts

Prompts, responses, transcripts and correction prose are not included. Anything the
analyst writes, whether claims, patches or explanations, can only draw on public material.
This loses transcript-level insight; see [Open questions](#open-questions).

### Analyst

The analyst reads the dossiers, metrics, current `.concurro/` files and the run's
`limitations`. It interprets candidates explicitly. For example, it can classify a
post-approval human commit as a likely miss and give the reason. It then returns
proposals:

```json
{
  "interpretations": [
    {"id": "I1", "candidate": "C7", "reading": "likely_miss", "reason": "..."}
  ],
  "proposals": [
    {
      "id": "P1",
      "kind": "instruction",
      "summary": "Reviewers check lock ordering in the mempool",
      "hypothesis": "Reviewers don't check lock ordering in src/mempool unless asked.",
      "evidence": [
        {"type": "observation", "ref": "O12"},
        {"type": "interpretation", "ref": "I1"}
      ],
      "success_measure": "Post-approval human fixes under src/mempool",
      "possible_regression": "More low-value findings on unrelated mempool changes",
      "patch": "<unified diff limited to .concurro/>",
      "loosens_review": false
    }
  ]
}
```

`kind` is one of `instruction`, `lens`, `model`, `panel`, `budget`. A narrowly framed
hypothesis may rest on one strong example. The PR shows each proposal's evidence by kind,
so a proposal resting only on interpretations is visible as such.

### Prior runs

The collector reads earlier retro PRs in the fork. A closed PR's closing comment and
any per-proposal review comments are its rejection reasons. The analyst receives them
and must say what new evidence justifies repeating a rejected proposal.

### Verification

- Every evidence `ref` resolves to an observation, candidate or interpretation in this
  run. A cited `quote` appears literally in the referenced public text.
- Patches touch only `.concurro/` and apply cleanly to the analysed configuration
  commit.
- The **combined** patch is validated through the `load_target` contract, using a
  reader over the patched tree. This catches proposals that are individually valid but
  conflict.
- If the combined patch fails, the verifier drops proposals in reverse order until it
  passes. The PR lists the dropped ones under "Not applied" with the failure reason.

### Publication

- Branch `concurro/retro/<run-id>` from the analysed configuration commit, one commit
  per applied proposal, so a reviewer can revert one proposal without losing the rest.
- The PR body holds the run's coverage and `limitations`, then each proposal's
  hypothesis, evidence by kind, success measure and possible regression, then the
  unapplied proposals.
- Proposals with `loosens_review` (removing a reviewer or lens, lowering effort, raising
  a budget) are listed first under their own heading.
- Merging changes the configuration for new tasks only.

## Invariants

- **Concurro never merges or adopts its recommendations.** Configuration changes reach
  tasks only through a human merge and, for existing tasks, `/concurro resume`.
- **The analyst sees only publication-safe content.** Nothing private can reach the
  public PR, because it is never in the analyst's input.
- **Work is attributed to the configuration it ran under.**
- **Unknown is not zero.** Missing records reduce coverage; they never count as absence.
- **Dossier content is untrusted.** Issue text, code and PR comments can carry
  instructions aimed at the analyst. Human review of the PR is the control.
- **Recorded facts aren't modified.** The collector only reads.
- **The run is budgeted** through the gateway, like `evaluate`.

## Phasing

1. **Phase 0: signal check.** Confirm diagnostics are enabled and retained for the
   window. Build the collector and run it over existing tasks. Read the metrics and
   candidates by hand and decide whether there is enough signal to justify the analyst.
2. **Phase 1: MVP.** Retrospective issue binding, analyst skill and schema, verifier,
   publisher, App permission change. Manual trigger, one target, `.concurro/` only.
3. **Phase 2: validation**, advisory and not a merge gate:
   - Replay baseline and proposed configurations on the same cases.
   - Cases include the motivating miss, held-out misses not used to form the hypothesis,
     and clean cases to measure false positives. This reuses the `design_eval` fixture
     machinery.
   - Production before/after comparisons by configuration segment, reported with the
     task mix on each side.
   - Engine-level findings are filed as issues in this repository.
4. **Phase 3: routine use.** Scheduled runs and a backoffice view of runs and proposals.

## Open questions

- **Private signals.** Correction prose and transcripts would explain *why* output was
  invalid or a reviewer missed something. Using them needs a way to keep their content
  out of a public PR, for example operator review of the full PR before publication, or a
  private destination. Is that worth it after Phase 0?
- **Permissions.** Adding fork Pull requests write changes the App's grants and token
  permissions. Confirm the installation accepts it before deployment.
- **Retrospective issue lifecycle.** One long-lived issue for all runs, or one issue
  per run?
