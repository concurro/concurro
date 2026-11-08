"""Shared in-memory GitHub and model responses for offline engine and service tests."""

import base64
from copy import deepcopy
from types import SimpleNamespace
import difflib
import json
from support import CONFIG as CONFIG
import classification as ratings
from controller import MARKER
from credentials import Credential
from github import GitHub, NotFound
from policy import Blocked, digest


PLAN = {"summary": "Clarify the guide.", "questions": [], "files": ["docs/guide.md"],
        "steps": ["Add the missing explanation."], "tests": ["Check wording and links."], "responses": [],
        "publication": {"changes": ["Clarify the guide."], "review_focus": ["Verify wording and links."]}}


APPROVE = {"verdict": "approve", "summary": "The change satisfies the plan.", "findings": [], "design_issue": None}


FINDING = {"path": "docs/guide.md", "start_line": None, "line": None, "detail": "Define the term."}


def design_report(report, context):
    """Supply explicit deterministic evidence for fake review decisions."""
    own = context.get('own_objections', [])
    concerns = report['concerns']
    updates = []
    for index in range(max(len(own), len(concerns))):
        detail = concerns[index] if index < len(concerns) else own[index]['text']
        updates.append({'id': own[index]['id'] if index < len(own) else '',
                        'status': 'open' if index < len(concerns) else 'resolved', 'detail': detail,
                        'counterexample': 'An incomplete explanation misses the requested behavior.',
                        'closure': 'The guide explains the requested behavior.', 'evidence': 'The current plan addresses the requested explanation.'})
    return {**report, 'sources': [{'id': 'S1', 'path': 'CONTRIBUTING.md', 'line': 1, 'quote': 'Use docs.'}],
            'assessments': [{'criterion': v['id'], 'status': 'satisfied' if report['decision'] == 'approve' else
                             'uncertain' if report['questions'] else 'defect' if concerns else 'satisfied',
                             'sources': ['S1'], 'reason': 'The plan explains the requested guide behavior.',
                             'counterexample': 'Omitting the explanation would fail this criterion.'}
                            for v in context['review_criteria']], 'objection_updates': updates}


class FakeAgents:
    """Deterministic responses with a switch for one requested fix."""

    def __init__(self, fix=False):
        """Keep call history to assert independent context and budgets."""
        self.calls = []
        self.fix = fix
        self.qc_reports = []

    def credential(self, provider, stage, task):
        """Select one fixture operator credential per provider."""
        return Credential("fixture:" + provider, provider, "org", "direct-api-key")

    def ask(self, provider, stage, context):
        """Supply a plan, a distinct edit per attempt, or independent reviews."""
        self.calls.append((provider, stage, deepcopy(context)))
        from feedback import restore
        context = restore(context)
        if stage in ("intake", "plan_qc"):
            if self.qc_reports:
                report = deepcopy(self.qc_reports.pop(0))
            else:
                report = {"decision": "proceed" if stage == "intake" else "approve",
                    "rationale": "Scope and acceptance criteria are sufficient.",
                    "risk": "low", "questions": [], "concerns": [],
                    "scope": {"goal_ids": ["G-USABILITY"], "fit": "Clarifies requested operator guidance.", "conflicts": []}}
            return design_report(report, context) if stage == 'plan_qc' else report
        if stage == "plan":
            return {**deepcopy(PLAN), "responses": [{"id": v["id"], "response": "Updated the plan to address this finding."} for v in context.get("open_objections", [])]}
        if stage == "develop":
            return {"summary": "Explain the guide.", "edits": [{"path": "docs/guide.md",
                    "content": "Updated guide " + str(len(self.calls)) + "\n"}], "validation": "Read the edited documentation.", "design_issue": None}
        if stage == "review":
            # Each earlier finding of this provider is answered as fixed by the new head.
            dispositions = [{"id": f["id"], "status": "fixed", "evidence": "The updated guide defines the term."}
                            for f in context.get("own_findings", [])]
            if self.fix and provider == "claude":
                self.fix = False
                return {"verdict": "changes_requested", "summary": "One ambiguity remains.",
                        "findings": [FINDING], "dispositions": dispositions, "design_issue": None}
            return {**deepcopy(APPROVE), "dispositions": dispositions}
        return deepcopy(APPROVE)


RULES = [{"type": "required_status_checks", "ruleset_id": 1, "parameters": {
             "strict_required_status_checks_policy": True,
             "required_status_checks": [{"context": name} for name in ["Concurro Rust"]]}},
         {"type": "pull_request", "ruleset_id": 1, "parameters": {"required_review_thread_resolution": True}}]


class FakeGitHub:
    """Model GitHub's durable state, Git objects and compare-and-swap merge guard."""

    create_tree = GitHub.create_tree

    def __init__(self, config):
        """Seed one opted-in issue and a writer's start command."""
        self.config = config
        self.base = "a" * 40
        self.issue = {"number": 7, "node_id": "issue-7", "title": "Improve guide", "body": "Explain this term.", "state": "open",
                      "user": {"login": "writer"}, "labels": [{"name": "concurro"}]}
        self.events = [{"id": 2, "event": "labeled", "label": {"name": "concurro"}, "actor": {"login": "writer"}}]
        self.comments = [{"id": 1, "user": {"login": "writer"}, "body": "/concurro start"}]
        self.refs = {}
        self.publication_source = self
        self.linked_branches = set()
        self.artifacts = {}
        self.blobs = {}
        self.artifact_counter = 0
        self.fail_after_link = False
        self.pr = None
        self.commits = {self.base: {"tree": {"sha": self.base}}}
        self.trees = {self.base: {"docs/guide.md": "Original guide\n", "CONTRIBUTING.md": "Use docs."}}
        self.counter = 10
        self.merges = 0
        self.thread_list = []
        self.human_reviews = []
        self.app_reviews = []
        self.review_comments = []
        self.protection = {"required_status_checks": {"strict": True, "contexts": config.get("required_checks", ["Concurro Rust"])},
                           "required_conversation_resolution": {"enabled": True}, "enforce_admins": {"enabled": True}}
        self.rules = []
        self.rulesets = {}
        self.checks_green = True
        self.authorized = True
        self.fail_after_ref = False

    def mark_ready(self, node_id):
        """Apply draft-to-ready; simulate a lost response without replaying the transition."""
        assert node_id == self.pr["node_id"]
        self.pr["draft"] = False
        self.ready_calls = getattr(self, "ready_calls", 0) + 1
        if getattr(self, "fail_after_ready", False):
            self.fail_after_ready = False
            raise RuntimeError("lost readiness response")

    def archives(self):
        """The bodies of the report archive artifacts, oldest first."""
        items = self.artifacts.get(7, {}).values()
        from conversation import REPORT_LINK
        from report_store import Store, MARKER as DATA_MARKER
        store = Store(self, 7, self.config['bot_login'])
        bodies = []
        for i in items:
            if not i['artifact']['name'].startswith('Concurro report '):
                continue
            body = i['artifact']['body']
            # Follow format-4 links as an archive reader would; legacy archives remain verbatim.
            numbers = [int(match['number']) for match in REPORT_LINK.finditer(body)]
            if numbers:
                first = self.artifacts[7][numbers[0]]['artifact']['body']
                key = first[len(DATA_MARKER):].split(':', 1)[0]
                value = store.get(key, numbers)
                body += '\n\n```json\n' + json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + '\n```'
            bodies.append(body)
        return bodies

    def summary(self):
        """Find the current-state comment independently of conversation chronology."""
        return next(c for c in self.comments if c["body"].startswith(MARKER))

    def sync_stage(self, number, stage):
        """Mirror native stage labels without modifying the enrollment event."""
        label = "concurro:stage:" + stage.replace("_", "-")
        self.issue["labels"] = [v for v in self.issue["labels"] if not v["name"].startswith("concurro:stage:")]
        self.issue["labels"].append({"name": label})

    def sync_attention(self, number, failed):
        """Mirror checkpoint health without changing stage or enrollment."""
        self.issue["labels"] = [v for v in self.issue["labels"] if v["name"] != "concurro:attention"]
        if failed:
            self.issue["labels"].append({"name": "concurro:attention"})

    def branch_linked(self, number, branch):
        """Expose the fixture's issue Development links."""
        return branch in self.linked_branches

    def create_linked_branch(self, issue_id, branch, sha):
        """Simulate native branch creation, including a lost success response."""
        if branch in self.refs:
            raise AssertionError("branch already exists")
        self.refs[branch] = sha
        self.linked_branches.add(branch)
        if self.fail_after_link:
            self.fail_after_link = False
            raise RuntimeError("lost response after linked branch creation")

    def permitted(self, login):
        """Only the seeded writer has permission, unless access was revoked."""
        return login == "writer" and self.authorized

    def archive(self, sha):
        """Supply source contents to the in-memory local workbench."""
        return json.dumps(self.trees[self.commits[sha]["tree"]["sha"]]).encode()

    def comment(self, body, login="writer"):
        """Append a human command with a monotonically increasing ID."""
        self.counter += 1
        self.comments.append({"id": self.counter, "body": body, "user": {"login": login}})

    def pages(self, path, key=None):
        """Expose fully paginated evidence as the real adapter does."""
        if path.endswith("/artifacts"):
            return [deepcopy(item["artifact"]) for item in self.artifacts.get(int(path.split("/")[1]), {}).values()]
        if path == "issues/7/comments":
            return deepcopy(self.comments)
        if path == "issues/7/events":
            return deepcopy(self.events)
        if path.startswith("pulls?"):
            return [deepcopy(self.pr)] if self.pr else []
        if path == "pulls/9/comments":
            return deepcopy(self.review_comments)
        if path == "pulls/9/files":
            return self.pull_files()
        if path == "pulls/9/reviews":
            return deepcopy(self.human_reviews + self.app_reviews)
        if path == "rules/branches/main":
            return deepcopy(self.rules)
        if path.endswith(("/statuses", "/check-suites")) or path.startswith("actions/runs?"):
            return []
        if path.startswith("commits/"):
            return [{"name": name, "head_sha": self.pr["head"]["sha"], "status": "completed",
                     "conclusion": "success" if self.checks_green else "failure",
                     "app": {"slug": "github-actions"}} for name in self.config.get("required_checks", ["Concurro Rust"])]
        raise AssertionError(path)

    def outcome_candidates(self, limit):
        """Return a bounded historical state window for learning tests."""
        return deepcopy(getattr(self, "past_outcomes", []))[:limit]

    def pull_files(self):
        """Each changed file with a unified patch against the base, as GitHub lists them."""
        base = self.trees[self.base]
        head = self.publication_source.trees[self.publication_source.commits[self.pr["head"]["sha"]]["tree"]["sha"]] if self.pr else base
        files = []
        for name in sorted(set(base) | set(head)):
            if base.get(name) != head.get(name):
                diff = difflib.unified_diff((base.get(name) or "").splitlines(), (head.get(name) or "").splitlines(),
                                            lineterm="", n=3)
                files.append({"filename": name, "patch": "\n".join(list(diff)[2:])})
        return files or [{"filename": "docs/guide.md", "patch": None}]

    def thread(self, comment):
        """Open a review thread on one new comment."""
        self.thread_list.append({"id": f"T{len(self.thread_list) + 1}", "isResolved": False,
                                    "isOutdated": False, "comments": {"nodes": [{"databaseId": comment["id"]}]}})

    def review_comment(self, data, **fields):
        """Record one App review comment."""
        self.counter += 1
        comment = {"id": self.counter, "user": {"login": self.config["bot_login"]}, "body": data["body"], **fields}
        self.review_comments.append(comment)
        return comment

    def review_threads(self, number):
        """Threads as the engine's adapter returns them."""
        return [{"id": t["id"], "isResolved": t["isResolved"],
                 "comment": t["comments"]["nodes"][0]["databaseId"] if t.get("comments") else None}
                for t in self.thread_list]

    def resolve_thread(self, thread_id):
        """Resolve one thread."""
        next(t for t in self.thread_list if t["id"] == thread_id)["isResolved"] = True
        return True

    def threads(self, number):
        """Return human review threads without resolving them."""
        return deepcopy(self.thread_list)

    def repo(self, path, method="GET", data=None):
        """Implement only the controller's explicit GitHub operations."""
        if "/artifacts" in path:
            parts = path.split("/")
            items = self.artifacts.setdefault(int(parts[1]), {})
            if method == "POST":
                self.artifact_counter += 1
                number = self.artifact_counter
                item = {"id": number + 1000, "number": number, **deepcopy(data),
                        "creator": {"login": self.config["bot_login"]},
                        "updated_by_actor": {"login": self.config["bot_login"]}}
                items[number] = {"artifact": item, "versions": []}
            else:
                number = int(parts[3])
                if method == "GET":
                    return deepcopy(items[number])
                item = items[number]["artifact"]
                item.update(deepcopy(data))
            item["updated_by_actor"] = {"login": self.config["bot_login"]}
            versions = items[number]["versions"]
            versions.insert(0, {"version": len(versions) + 1, "name": item["name"], "body": item["body"],
                                "actor": deepcopy(item["updated_by_actor"])})
            return deepcopy(item)
        if path == "issues/7":
            return deepcopy(self.issue)
        if path == "git/ref/heads/main":
            return {"object": {"sha": self.base}}
        if path.startswith("issues/comments/"):
            target = next(c for c in self.comments if c["id"] == int(path.rsplit("/", 1)[1]))
            if method == "PATCH":
                target["body"] = data["body"]
            return deepcopy(target)
        if path == "issues/7/comments":
            self.comment(data["body"], self.config["bot_login"])
            return deepcopy(self.comments[-1])
        if path.startswith("git/trees/"):
            sha = path.split("/")[2].split("?")[0]
            sha = self.commits.get(sha, {"tree": {"sha": sha}})["tree"]["sha"]
            return {"truncated": False, "tree": [{"path": p, "mode": "100644", "type": "blob",
                     "sha": sha + ":" + p, "size": len(content)} for p, content in self.trees[sha].items()]}
        if path.startswith("git/blobs/"):
            sha, name = path[len("git/blobs/"):].split(":", 1)
            return {"encoding": "base64", "content": base64.b64encode(self.trees[sha][name].encode()).decode()}
        if path.startswith("git/commits/"):
            return deepcopy(self.commits[path.rsplit("/", 1)[1]])
        if path == "git/blobs":
            assert data["encoding"] == "utf-8"
            sha = digest(data["content"])[:40]
            self.blobs[sha] = data["content"]
            return {"sha": sha}
        if path == "git/trees":
            self.counter += 1
            sha = f"{self.counter:040x}"
            self.trees[sha] = deepcopy(self.trees[data["base_tree"]])
            for entry in data["tree"]:
                if "sha" in entry and entry["sha"] is None:
                    self.trees[sha].pop(entry["path"], None)
                else:
                    self.trees[sha][entry["path"]] = (entry["content"] if "content" in entry else self.blobs[entry["sha"]])
            return {"sha": sha}
        if path == "git/commits":
            assert data["tree"] in self.trees, "commit references a nonexistent tree"
            self.counter += 1
            sha = f"{self.counter:040x}"
            self.commits[sha] = {"tree": {"sha": data["tree"]}}
            return {"sha": sha}
        if path.startswith("git/matching-refs/heads/"):
            return [{"ref": "refs/heads/" + b, "object": {"sha": s}} for b, s in self.refs.items()]
        if path == "git/refs" or path.startswith("git/refs/heads/"):
            branch = data["ref"][len("refs/heads/"):] if path == "git/refs" else path[len("git/refs/heads/"):]
            self.refs[branch] = data["sha"]
            if self.pr:
                self.pr["head"]["sha"] = data["sha"]
            if self.fail_after_ref:
                self.fail_after_ref = False
                raise RuntimeError("lost response after successful write")
            return {}
        if path == "pulls/9/reviews" and method == "POST":
            self.counter += 1
            review = {"id": self.counter, "user": {"login": self.config["bot_login"]}, "body": data["body"],
                      "state": "COMMENTED", "commit_id": data["commit_id"], "event": data["event"]}
            self.app_reviews.append(review)
            for item in data["comments"]:
                self.thread(self.review_comment(item, path=item["path"], line=item["line"], review=review["id"]))
            return deepcopy(review)
        if path == "pulls/9/comments" and method == "POST":
            comment = self.review_comment(data, path=data["path"], subject_type=data["subject_type"])
            self.thread(comment)
            return deepcopy(comment)
        if path.startswith("pulls/9/comments/") and path.endswith("/replies"):
            parent = int(path.split("/")[3])
            comment = self.review_comment(data, in_reply_to_id=parent)
            return deepcopy(comment)
        if path == "pulls":
            self.pr = {"user": {"login": self.config["bot_login"]}, "node_id": "PR_9", "number": 9, "title": data["title"], "body": data["body"], "state": "open", "draft": data["draft"], "mergeable": True,
                       "mergeable_state": "clean", "head": {"sha": self.publication_source.refs[data["head"].split(":", 1)[-1]],
                        "ref": data["head"].split(":", 1)[-1], "repo": {"full_name": self.config.get("fork", self.config["repository"])}},
                       "base": {"ref": "main", "sha": self.base}, "merged": False}
            return deepcopy(self.pr)
        if path == "pulls/9":
            return deepcopy(self.pr)
        if path == "branches/main/protection":
            if self.protection is None:
                raise NotFound("Branch not protected")
            return deepcopy(self.protection)
        if path.startswith("rulesets/"):
            return deepcopy(self.rulesets[int(path.split("/")[1])])
        if path == "pulls/9/merge":
            if data["sha"] != self.pr["head"]["sha"]:
                return {"merged": False}
            self.merges += 1
            self.pr.update(merged=True, merge_commit_sha="f" * 40, state="closed",
                           merged_by={"login": self.config["bot_login"]})
            return {"merged": True, "sha": "f" * 40}
        raise AssertionError((path, method, data))


def decision(value, **overrides):
    """Create a valid QC response with explicitly controlled test decisions."""
    return {"decision": value, "rationale": "Evidence supports this decision.", "risk": "low",
            "questions": [], "concerns": [], "scope": {"goal_ids": ["G-USABILITY"],
            "fit": "Explains requested developer guidance.", "conflicts": []}, **overrides}


def named_config():
    """Two reviewers share OpenAI while triage, planning and implementation have other assignments."""
    config = deepcopy(CONFIG)
    for key in ("qc", "qc_model", "planner", "implementer", "reviewers"):
        config.pop(key)
    config["models"] = {
        "opus-high": {"provider": "anthropic", "model": "claude-opus-5-5", "effort": "high"},
        "sol-medium": {"provider": "openai", "model": "gpt-6.1-sol", "effort": "medium"},
        "sol-high": {"provider": "openai", "model": "gpt-6.1-sol", "effort": "high"},
        "astra-high": {"provider": "openai", "model": "gpt-6-astra", "effort": "high"}}
    config["agents"] = {"triage": "sol-medium", "planner": "opus-high", "implementer": "opus-high",
                        "plan_reviewers": ["sol-high", "astra-high"], "pr_reviewers": ["sol-high", "astra-high"]}
    return config


def assessment(complexity='medium', risk='high', **fields):
    """Return valid metadata with deliberately high consequence risk and ordinary QC risk."""
    return {'complexity': complexity, 'change_risk': risk, 'confidence': 'high',
            'rationale': 'Interacting states; mistakes can lose persistent data.', 'unknowns': [], **fields}


def intake(**fields):
    """Build a valid core without an assessment for compatibility and boundary tests."""
    return {'decision': 'proceed', 'rationale': 'Clearly specified.', 'risk': 'low', 'questions': [],
            'concerns': [], 'scope': {'goal_ids': ['G-USABILITY'], 'fit': 'Usability.', 'conflicts': []}, **fields}


class ClassifiedAgents(FakeAgents):
    """Attach independent evidence to the ordinary simple and repair lifecycle fixtures."""

    def __init__(self, fix=False):
        """Track the first reviewed head to exercise lowering on a complete repair round."""
        super().__init__(fix)
        self.first_head = None

    def ask(self, provider, stage, context):
        """The repaired head receives a lower estimate, without changing its core verdict."""
        report = super().ask(provider, stage, context)
        if stage == 'review' and self.first_head is None:
            self.first_head = context['head']
        if stage in ratings.STAGES:
            report['classification'] = assessment('medium' if provider in ('claude', 'sol-high') else 'low',
                                                   'low' if stage == 'review' and context['head'] != self.first_head else 'high')
        return report


def sized_report(context, width=300):
    """Build bounded but realistically wordy evidence for every supplied criterion."""
    report = design_report(decision('approve'), context)
    report['rationale'] = 'The source supports the requested design. ' * 25
    report['scope']['fit'] = 'Documentation supports operator usability. ' * 12
    for assessment in report['assessments']:
        assessment['reason'] = 'Source and plan satisfy this requirement. ' * 10
        assessment['reason'] = assessment['reason'][:400]
        assessment['counterexample'] = ('An omitted behavior would contradict this criterion. ' * 8)[:width]
    return report


DESIGN_ISSUE = {"kind": "plan", "explanation": "Acceptance requires another documented example.",
                "evidence": ["The plan only names the happy-path example; the requirement includes errors."]}



class Crash(BaseException):
    """Simulate process loss outside normal exception handling."""


class BatchAgents(FakeAgents):
    """A controllable lifecycle transport; normal non-review stages stay synchronous."""

    parallel = True
    generation = 1

    def __init__(self):
        """Configure controllable delays, failures and ownership for local tests."""
        super().__init__()
        self.events = []
        self.handles = []
        self.effects = {}
        self.delay = {}
        self.fail_submit = None
        self.owner = True
        self.clock = 100

    def prepare(self, provider, stage, context):
        """Freeze the input without consuming a fake response."""
        return {'provider': provider, 'stage': stage, 'context': deepcopy(context)}

    def submit(self, turn):
        """Record admission and return before a result is available."""
        provider = turn['provider']
        self.events.append(('submit', provider))
        if self.fail_submit and len(self.handles) == self.fail_submit:
            raise Crash()
        handle = SimpleNamespace(turn=turn, status='queued', timing={
            'turn': f'{len(self.handles):08x}-1234-1234-1234-123456789abc', 'status': 'queued',
            'created': self.clock, 'started': None, 'finished': None}, polls=0, cancelled=False)
        self.handles.append(handle)
        return handle

    def observe(self, handle):
        """Advance one observation without blocking a sibling."""
        handle.polls += 1
        handle.status = 'running'
        handle.timing.update(status='running', started=handle.timing['started'] or self.clock)
        self.events.append(('poll', handle.turn['provider']))
        if handle.polls <= self.delay.get(handle.turn['provider'], 0):
            return None
        handle.status = 'done'
        handle.timing.update(status='done', finished=self.clock)
        return {'ok': True}

    def decode(self, turn, result):
        """Return a report or an injected provider failure."""
        provider = turn['provider']
        self.events.append(('decode', provider))
        effects = self.effects.get(provider, [])
        effect = effects.pop(0) if effects else None
        if isinstance(effect, BaseException):
            raise effect
        report = super().ask(provider, turn['stage'], turn['context'])
        return effect(report) if effect else report

    def cancel(self, handle):
        """Record scoped cancellation without touching siblings."""
        handle.cancelled = True
        self.events.append(('cancel', handle.turn['provider']))

    def check_owner(self):
        """Reject every operation after simulated ownership loss."""
        if not self.owner:
            raise Blocked('ownership changed')

    def idle(self):
        """Advance the shared test clock between sweeps."""
        self.events.append(('idle', None))
        self.clock += 1
