"""Shared inputs for comment-format goldens: one sample per event kind and PR publication."""

FINDING = {"path": "docs/guide.md", "start_line": 3, "line": 4, "detail": "The term is undefined. Define it before use."}
REPOSITORY = "o/r"


def qc_state():
    """A plan QC revision with one resolved and one refined objection."""
    report = {"decision": "revise", "rationale": "One boundary is unspecified. The rest of the plan holds.",
              "risk": "low", "questions": [], "concerns": ["Specify the `<limit>` boundary."], "scope": {},
              "objection_updates": [
                  {"id": "O1", "status": "resolved", "detail": "Old.", "evidence": "Step 2 now covers it.",
                   "counterexample": "", "closure": ""},
                  {"id": "O2", "status": "open", "detail": "Specify the `<limit>` boundary.",
                   "evidence": "Step 3 is silent.", "counterexample": "A request at the limit.",
                   "closure": "Step 3 names the limit."}]}
    details = {"plan": "a" * 64, "round": 2, "phase": "cooperative", "reviewer": "b" * 64, "resolved": [],
               "disposition_ids": ["O1-bbbbbbbb", "O2-bbbbbbbb"]}
    return {"issue": 7, "qc": {"plan_qc": report}}, ("qc", "plan_qc"), details


def local_state():
    """Local validation with one failing check and a requested change."""
    status = {"attempt": 0, "local_commit": "c" * 40,
              "checks": [{"name": "diff", "exit_code": 0}, {"name": "text|lint", "exit_code": 1}],
              "review": {"verdict": "changes_requested", "summary": "One defect remains. Fix the heading.",
                         "findings": [{"path": "docs/guide.md", "detail": "Heading level skips from 1 to 3."}],
                         "design_issue": None}}
    return {"issue": 7, "local_status": status}, ("local_status",), {"reviewer": "claude"}


def review_state():
    """A recorded PR review with two new findings and one answer, as the ledger holds it."""
    findings = [{**FINDING, "severity": "low", "title": "Wording"},
                {**FINDING, "path": None, "start_line": None, "line": None, "severity": "high",
                 "title": "Missing *changelog* [fragment](x)", "detail": "No fragment.\n\n```\n</details>"}]
    report = {"verdict": "changes_requested", "headline": "Two defects remain; one earlier finding is fixed.",
              "summary": "The guide is close.\n\n<!-- unclosed", "findings": findings,
              "dispositions": [{"id": "F1", "status": "fixed", "evidence": "Lines 3-4 define it."}],
              "design_issue": None, "head": "d" * 40, "model": "m", "sequence": 2, "finding_ids": ["F2", "F3"]}
    state = {"issue": 7, "pr": 9, "plan_id": "a" * 64, "reviews": {"claude": report}, "findings": [
        {"id": "F1", "provider": "claude", **FINDING, "status": "fixed", "comment": 101, "thread": "T1"},
        *({"id": i, "provider": "claude", **f, "status": "open"} for i, f in zip(report["finding_ids"], findings))]}
    details = {"head": "d" * 40, "round": 2, "pr": 9}
    return state, ("reviews", "claude"), details


def plan_state():
    """A plan summary event."""
    plan = {"summary": "Document locked builds. Add a fragment.", "publication": {
        "changes": ["Add a note under step 5."], "review_focus": ["Is `--locked` accurate?"]}}
    details = {"plan": "a" * 64, "round": 1, "revision": 1, "url": "https://github.com/o/r/issues/7/artifacts/2"}
    return {"issue": 7, "plan": plan}, ("plan",), details


def v2_details(details, **extra):
    """Format 2 event details add the actor and links."""
    return {**details, "provider": "claude", "model": "m", "actor": "claude / m",
            "pr_url": f"https://github.com/{REPOSITORY}/pull/9", **extra}


def event_cases(conversation, version):
    """Each sourced event kind rendered in `version`."""
    cases = {}
    for name, (state, source, details) in {"qc": qc_state(), "local": local_state(), "review": review_state(),
                                           "plan_summary": plan_state()}.items():
        if version == 1:
            cases[name] = conversation.render(state, name, source, details)
        else:
            title, body, meta = conversation.render_v2(state, name, source, v2_details(details))
            cases[name] = conversation.content_v2({"id": "e" * 64, "sequence": 3}, title, body, meta)
    return cases


def publication_cases(reviews, version):
    """A PR review body, a finding opening and a reply rendered in `version`."""
    state, _, _ = review_state()
    report = state["reviews"]["claude"]
    config = {"repository": REPOSITORY}
    item = {"provider": "claude", "sequence": 2, "head": "d" * 40}
    if version != 1:
        item["format"] = version
    findings = [f for f in state["findings"] if f["id"] in report["finding_ids"]]
    for finding in findings:
        if version != 1:
            finding["format"] = version
    placed = {"F2": "inline", "F3": "body"}
    return {"review": reviews.review_body(state, config, item, report, findings, placed),
            "opening": reviews.finding_body(findings[0]),
            "reply": reviews.disposition_body(state, item, report["dispositions"][0])}
