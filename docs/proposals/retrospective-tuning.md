# Proposal: retrospective tuning

**Status: proposal, not implemented.**

Analyse finished tasks after the fact and open a pull request in the target's fork with
recommended changes to its `.concurro/` configuration. Every recommendation cites the
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
- **Task state.** Checkpoints hold the finding ledger (`engine/reviews.py`): ID, raising
  profile, severity, path and lines, the head it was raised on, and dispositions
  (`fixed`, `open`, `obsolete`, `withdrawn`). They also hold votes, repair rounds, plan
  revisions, call counts, writer commands and an `outcomes` record written at terminal
  stages (`Controller.record_outcome`).
- **Prior outcomes.** `Controller.retrieve_outcomes` already feeds related past outcomes
  to the planner as untrusted context. It records outcomes "without policy edits"; nothing
  turns outcomes into configuration changes.
- **GitHub.** After handoff, the PR's merge or close, human review comments, commits
  pushed by humans and CI results are visible on GitHub. Concurro doesn't record them.
- **Target configuration.** The fork's default branch holds `.concurro/config.yaml`,
  `charter.json` and `instructions/`. Tasks pin the fork configuration commit; new
  tasks adopt changes, existing tasks only on `/concurro resume`. `.concurro/` is
  normally a protected path, so the contribution pipeline cannot edit it.
- **Fork permissions.** `service.targets.permissions()` requests fork Contents, Actions
  and Workflows write, but not Pull requests write.

## Goals

- Turn observed task facts into concrete, reviewable `.concurro/` changes: role
  instructions, model profiles and effort, reviewer panels, budgets and, once they land,
  reviewer lenses.
- Tie each recommendation to evidence that a reviewer can check.
- Keep the operator in control: a manual trigger, one PR per run, human merge.

## Non-goals

- Changing engine skills, schemas or shared instructions in this repository. Phase 2
  reports those as issues instead.
- Applying, merging or auto-adopting recommendations, or resuming tasks onto them.
- Claims about general model quality. A handful of tasks can't support them.
- Diagnosing CI failures.

## Design

### Overview

```text
retro run ──► collector ──► task dossiers + metrics ──► analyst turn ──► proposals
                 (no model calls)                         (one session)       │
                                                                              ▼
                     fork PR ◄── publisher ◄── verifier (evidence + config validation)
```

1. **Collector**: deterministic, no model calls. Builds one dossier per finished task
   and cross-task metrics.
2. **Analyst**: one agent turn under a new `concurro-retrospect` skill and `retro.json`
   response schema. It reads the dossiers, the metrics and the current `.concurro/`
   files and returns structured proposals.
3. **Verifier**: checks every evidence reference and validates the patched
   configuration.
4. **Publisher**: one branch and one PR in the fork per run.

### Trigger and scope

An operator command, alongside `evaluate`:

```bash
python3 -m service --settings /absolute/path/to/settings.json retro OWNER/REPO --since 2026-11-01
```

It covers tasks in that target that reached a terminal stage in the window. It makes
live model calls, so it needs operator authorization, like `evaluate`.

### Signals

| Signal | Facts | Typical recommendation |
| ---- | ---- | ---- |
| Post-approval misses | Human review comments and human commits on a PR after every agent reviewer approved, mapped to paths | Reviewer instruction or lens covering that area |
| Reviewer yield | Per profile: findings raised, fixed, `obsolete`, withdrawn; findings only one reviewer raised versus both | Panel change; drop or replace a reviewer whose findings the other reviewer always duplicates |
| Invalid output | Correction feedback and correction counts per profile and stage | Effort or profile change for that role; instruction clarifying the contract |
| Cost and duration | Tokens, elapsed time and calls per stage and profile | Lower effort where outcomes don't differ |
| Budget pressure | Budgets used up, repair rounds and plan revisions spent | Budget change, or instructions that cut repeated repairs |
| Friction | Planner clarifications, writer feedback and retries, and the stated reasons | Shared or role instruction filling the recurring gap |

### Dossier

One JSON document per task, with structured facts and short excerpts. Full transcripts
stay in service storage.

- Task: issue, terminal stage, reason, configuration commit and digest, bundle ID,
  profiles per role, lens set.
- Turns: ID, stage, profile, status, termination reason, elapsed time, tokens,
  correction count and correction feedback.
- Findings: ID, raising profile, severity, path and lines, disposition history.
- PR: final state, human review comments (URL, author, path, body), human commits
  (SHA, paths), CI results at handoff.
- Writer inputs: commands and feedback comments (URLs).

### Proposals

```json
{
  "proposals": [
    {
      "id": "P1",
      "kind": "instruction",
      "summary": "Reviewers check lock ordering in the mempool",
      "claim": "Two merged PRs needed human fixes for lock ordering after both reviewers approved.",
      "patch": "<unified diff limited to .concurro/>",
      "evidence": [
        {"type": "pr_comment", "ref": "https://github.com/OWNER/REPO/pull/12#discussion_r1", "quote": "..."},
        {"type": "pr_commit", "ref": "OWNER/REPO@abc1234"},
        {"type": "metric", "ref": "post_approval_misses.path:src/mempool"}
      ],
      "expected_effect": "Fewer post-approval human fixes under src/mempool",
      "loosens_review": false
    }
  ]
}
```

`kind` is one of `instruction`, `lens`, `model`, `panel`, `budget`. Evidence types are
`turn`, `finding`, `pr_comment`, `pr_commit`, `writer_comment` and `metric`.

### Verification

- Every evidence `ref` must resolve to an entry in the run's dossiers or metrics. A
  `quote` must appear literally in the referenced public text. This follows the
  design-review `sources[]` check (`design.validate_evidence`).
- Patches apply cleanly to the analysed configuration commit and touch only
  `.concurro/`.
- The patched configuration passes `controller.validate_config`, and the patched target
  loads through `Bundle(target=...)`, as the offline `engine/bundle.py` check does.
- A proposal that fails a check isn't applied. The PR lists it under "Not applied" with
  the failure reason.

### Publication

- Branch `concurro/retro/<run-id>` from the analysed configuration commit, one commit
  per applied proposal, so a reviewer can revert one proposal without losing the rest.
- The PR body holds a metrics snapshot, then each proposal's claim, expected effect and
  evidence table, then the unapplied proposals.
- Proposals that remove a reviewer or lens, lower effort, or raise a budget have
  `loosens_review` set and are listed first under their own heading.
- Merging changes the configuration for new tasks only.

## Invariants

- **Concurro never merges or adopts its recommendations.** Configuration changes reach
  tasks only through a human merge and, for existing tasks, `/concurro resume`.
- **Nothing private reaches the PR.** The fork is public. The PR may cite public GitHub
  URLs, finding IDs, turn IDs and aggregate metrics. It may quote public text only, never
  prompts, responses or transcripts.
- **Dossier content is untrusted.** Issue text, code, PR comments and transcripts can
  carry instructions aimed at the analyst. The verifier checks evidence, not intent;
  human review of the PR is the control.
- **Recorded facts aren't modified.** The collector only reads checkpoints, diagnostics
  and the exchange.
- **The run is budgeted.** The analyst turn uses the target's gateway budget like
  `evaluate`.

## Phasing

1. **Phase 0: signal check.** Confirm diagnostics are enabled and retained for the
   window. Build the collector and run it over existing tasks. Read the metrics by hand
   and decide whether there is enough signal to justify the analyst.
2. **Phase 1: MVP.** Analyst skill and schema, verifier, publisher, App permission
   change. Manual trigger, one target, `.concurro/` only.
3. **Phase 2: validation.** Replay a proposed instruction or lens against the historical
   diff where a human found the miss, reusing the `design_eval` fixture machinery, and
   report whether it is now caught. Compare metrics before and after a merged
   proposal's configuration digest. File engine-level findings as issues in this
   repository.
4. **Phase 3: routine use.** Scheduled runs and a backoffice view of runs and proposals.

## Open questions

- **Session binding.** Runner sessions bind to a repository and issue. Should a run use
  a dedicated retrospective issue (which also gives it a dashboard), or a new binding
  kind with no issue?
- **Minimum evidence.** Should a proposal need evidence from at least two tasks, or is
  one well-evidenced miss enough?
- **Dossier transport.** Does the analyst runner receive full dossiers, or a bounded
  digest with excerpts fetched on demand through an MCP tool?
- **Rejected proposals.** Should a closed retro PR feed back into the next run, so the
  analyst doesn't propose the same change again?
- **Permissions.** Adding fork Pull requests write changes the App's grants and token
  permissions. Confirm the installation accepts it before deployment.
