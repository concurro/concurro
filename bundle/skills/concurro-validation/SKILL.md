---
name: concurro-validation
description: Plan and assess executable validation for Concurro candidate changes using the controller's check catalog and repository evidence.
---

# Concurro validation

Use the supplied `validation_capabilities` as the executable contract. Select named checks and declarative file rules; do not substitute shell snippets, remote commit hashes, or assumptions about the inspection snapshot. Report an unavailable capability instead of claiming it will run. Separate automated guarantees from semantic checks requiring source inspection.

For each requested behavior, describe the observable success condition and a plausible failing example. Consider empty changes, missing or extra files, wrong change types, uncommitted content and an assertion that merely prints a result. Prefer the supplied complete-candidate checks over recreating them. Actual check results come from the workbench, never from planning or review assertions.

Distinguish a missing required guarantee from an optional improvement. Literal text assertions and line limits are optional unless the request needs them. Source-based review can validate prose accuracy; keyword presence cannot prove it. An empty `required_text` alone is not a defect when the plan includes a concrete source comparison. Before blocking, explain which requested behavior lacks an effective automated or review check. Do not impose a new acceptance requirement merely because an extra check is available.

Review every controller-supplied criterion against the exact current plan. Cite pinned repository paths with one-based line numbers and short literal quotes. A quote may be a fragment within that exact line; a multiline quote must begin there and remain contiguous. Explain how each source supports the assessment and which counterexample was considered. Use `defect` or `uncertain` when warranted; do not approve by repeating the planner's claims or a previous vote. A valid plan needs no invented finding.

Respect the response schema's field limits and `review_output_budget`. When `review_validation_feedback` is supplied, correct your own response using its diagnostic and field path. Shorten wording without dropping criteria, sources or objection dispositions. Re-read a disputed citation. Technical output correction does not change the design or authorize approval; reassess the same plan independently. Full issue context remains authoritative, including substantive short clarification answers that were not filtered as courtesy replies.

Reassess each own open objection independently. Resolve the specific ID when its closure condition is met, even if another finding remains open. Refine the existing ID for the same defect; use an empty ID only for a new finding. Every open disposition needs a concrete counterexample and observable closure condition. The planner may explain disagreement; only the owning reviewer resolves the objection. Fresh reviewers inspect the design without earlier verdicts, responses, or publication summaries.
