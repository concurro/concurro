//! Pure task request ordering and M1 start/stop decisions.

pub use design::DesignPhase;
pub use sponsorship::InitialFundingContext;
mod canonical;
mod design;
mod design_review;
mod implementation;
mod plan;
mod policy;
mod pr_review;
mod publication;
mod readiness;
mod sponsorship;
pub use canonical::{escape_non_ascii, python_digest, python_json};
pub use design::{
    DesignReviewer, design_binding, design_criteria, design_roster, legacy_requirements,
    plan_identity, pr_review_roster,
};
pub use design_review::{DesignReviewReport, validate_design_review};
pub use implementation::{
    ImplementationEdit, ImplementationReport, validate_edits, validated_edit_paths,
};
pub use plan::{plan_output_contract, validate_plan};
pub use policy::{
    edit_policy, editable_files, effective_limits, glob_matches, path_rules, plan_scope_contains,
};
pub use pr_review::{
    DesignIssue, DesignIssueKind, PrDisposition, PrFinding, PrReviewReport, pr_review_binding,
    pr_review_body, standalone_pr_review_binding, validate_design_issue, validate_pr_review,
    validate_standalone_pr_review,
};
pub use publication::{
    intake_notice_body, markdown_text, plan_notice_body, task_branch, welcome_body,
};
pub use readiness::{CiObservation, ci_reasons};
pub use sponsorship::{
    ConsentAction, ConsentFact, ConsentProjection, FundingMode, InitialConsent, Provider,
    ProviderConsent, SponsorSlot, consent_facts, initial_sponsorship_transitions,
    initialize_consent, project_consent,
};

use std::{collections::BTreeMap, sync::OnceLock};

use chrono::{DateTime, Utc};
use concurro_contracts::{Envelope, GithubEvent, IssueCommentPayload, RequestCommand, TaskId};
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use uuid::Uuid;

pub use concurro_contracts::{RequestActor as Actor, TaskIssue as IssueKey};

/// GitHub source order, independent of webhook arrival order.
#[derive(Clone, Debug, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
pub struct SourceOrder {
    /// The original GitHub source's creation or edit time.
    pub at: DateTime<Utc>,
    /// Numeric GitHub source identity, used to order equal-time comments.
    pub source_id: i64,
    /// Stable source ID, including revision digest for comment edits.
    pub source: String,
    /// Position among requests emitted from the source.
    pub index: i32,
}

/// M1 command classification, retaining later commands for explicit refusals.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub enum RequestKind {
    /// Begin or restart the issue's task.
    Start,
    /// Stop the issue's current task and supersede earlier starts.
    Stop,
    /// A valid command whose behavior is implemented after M1.
    Later(LaterCommand),
}

/// Parsed command fields retained until their phase can apply them.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub enum LaterCommand {
    /// Supersede earlier label enrollment without changing an active task.
    Unenroll,
    /// Choose a funding provider and mode.
    Funding {
        /// Selected provider.
        provider: String,
        /// Organization or personal funding mode.
        mode: String,
    },
    /// Offer sponsorship, optionally for one provider.
    Sponsor {
        /// Optional provider scope.
        provider: Option<String>,
    },
    /// Withdraw one provider's sponsorship.
    Withdraw {
        /// Provider losing consent.
        provider: String,
    },
    /// Approve the exact plan identifier.
    Approve {
        /// Exact approved plan identity.
        plan_id: String,
    },
    /// Retry, or extend an exhausted plan-revision budget.
    Retry {
        /// Additional plan revisions when this is an extension.
        plan_revisions: Option<u16>,
    },
    /// Resume, optionally changing only the PR description.
    Resume {
        /// Limit resumed work to the PR description.
        description_only: bool,
    },
    /// Invalidate the current task decision.
    Invalidate,
}

impl RequestKind {
    /// Typed wire command for this parsed request.
    pub fn outcome_command(&self) -> RequestCommand {
        match self {
            Self::Start => RequestCommand::Start,
            Self::Stop => RequestCommand::Stop,
            Self::Later(LaterCommand::Unenroll) => RequestCommand::Unenroll,
            Self::Later(LaterCommand::Funding { provider, mode }) => RequestCommand::Funding {
                provider: provider.clone(),
                mode: mode.clone(),
            },
            Self::Later(LaterCommand::Sponsor { provider }) => RequestCommand::Sponsor {
                provider: provider.clone(),
            },
            Self::Later(LaterCommand::Withdraw { provider }) => RequestCommand::Withdraw {
                provider: provider.clone(),
            },
            Self::Later(LaterCommand::Approve { plan_id }) => RequestCommand::Approve {
                plan_id: plan_id.clone(),
            },
            Self::Later(LaterCommand::Retry {
                plan_revisions: Some(count),
            }) => RequestCommand::Extend {
                plan_revisions: *count,
            },
            Self::Later(LaterCommand::Retry {
                plan_revisions: None,
            }) => RequestCommand::Retry,
            Self::Later(LaterCommand::Resume { description_only }) => RequestCommand::Resume {
                description_only: *description_only,
            },
            Self::Later(LaterCommand::Invalidate) => RequestCommand::Invalidate,
        }
    }
}

/// A final channel request ready for an application decision.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct Request {
    /// Durable request ID.
    pub id: Uuid,
    /// Authenticated channel that recorded the source.
    pub channel: String,
    /// Where the channel wants its outcome published.
    pub reply: String,
    /// Stable comment identity for the comment channel, absent for other channels.
    pub comment_id: Option<i64>,
    /// Issue it addresses.
    pub issue: IssueKey,
    /// Issue author, distinct from the request actor.
    pub issue_author: Actor,
    /// GitHub actor who submitted it.
    pub actor: Actor,
    /// Parsed command kind.
    pub kind: RequestKind,
    /// Source order used for control supersession.
    pub order: SourceOrder,
}

/// One verified comment revision, including edits that remove a command.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CommentRevision {
    /// Issue containing the comment.
    pub issue: IssueKey,
    /// Stable GitHub comment ID.
    pub comment_id: i64,
    /// Source order and logical content revision.
    pub order: SourceOrder,
    /// Whether this comment identity is terminally deleted.
    pub deleted: bool,
    /// Parsed command, when this revision contains one.
    pub request: Option<Request>,
}

/// Current task lifecycle stage.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub enum Stage {
    /// Awaiting pinned issue, configuration and base reads.
    Initializing,
    /// An intake attempt is ready or in progress.
    Intake,
    /// Planner turn is queued or in progress.
    Plan,
    /// Independent plan reviewers are assessing the current plan.
    PlanQc,
    /// Reviewed plan waits for a writer's exact approval.
    Planned,
    /// The approved implementation is in progress.
    Implement,
    /// Candidate Git objects and a draft pull request are being published.
    Publish,
    /// Reviewers assess an immutable pull request head.
    Review,
    /// Pull request readiness gates are being observed.
    Gates,
    /// The pull request was marked ready for human review.
    Ready,
    /// A matching external merge completed the contribution.
    ObservedMerged,
    /// The pull request closed without merging.
    Closed,
    /// Intake needs a human answer before retrying.
    Clarify,
    /// Intake rejected the issue as out of scope.
    Rejected,
    /// Intake requires a human decision on consequential risk.
    Escalated,
    /// A limit or failure paused the task for operator or writer action.
    Blocked,
    /// A writer stopped the task.
    Stopped,
}

/// GitHub revisions and call limit frozen when an issue enters intake.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct PinnedInputs {
    /// Fork commit containing the task configuration.
    pub config_sha: String,
    /// Upstream commit containing policy and editable files.
    pub base_sha: String,
    /// Maximum charged model calls under this configuration.
    pub max_model_calls: u64,
    /// Configured fix-round limit, retained for later stages.
    pub max_fix_rounds: u64,
    /// Configured plan-revision limit, retained for later stages.
    pub max_plan_revisions: u64,
}

/// One requirements comment eligible before the start source.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct RequirementComment {
    /// Author who supplied the requirement.
    pub author: Actor,
    /// Exact GitHub comment body.
    pub body: String,
}

/// Issue text and bounded eligible discussion frozen at task start.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct Requirements {
    /// Issue title.
    pub title: String,
    /// Issue body, absent when GitHub has no description.
    pub body: Option<String>,
    /// Last twenty eligible comments before the start source.
    pub discussion: Vec<RequirementComment>,
}

/// Source and bundle inputs frozen with a task's initialization revision.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct InitializationSnapshot {
    /// GitHub commit identities and task call limits.
    pub pins: PinnedInputs,
    /// Exact target configuration at the fork commit.
    pub configuration: Value,
    /// Issue requirements ending at the start source.
    pub requirements: Requirements,
    /// Exact charter text supplied to the runner.
    pub charter: String,
    /// Target instruction overrides by bundle-relative path.
    pub instruction_texts: BTreeMap<String, String>,
    /// Policy file contents from the upstream base.
    pub policy_documents: BTreeMap<String, String>,
    /// Non-tree paths from the upstream base before edit policy filtering.
    pub base_files: Vec<String>,
    /// Exact runtime bundle files, absent only on pre-bundle snapshots.
    #[serde(default)]
    pub engine_resources: BTreeMap<String, String>,
    /// Hash identity of the complete engine and target bundle.
    pub bundle_id: String,
}

/// Intake judgment returned by the triage role.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum IntakeDecision {
    /// Continue to planning.
    Proceed,
    /// Ask the issue author for missing intent.
    Clarify,
    /// Reject the task scope.
    Reject,
    /// Require a human decision.
    Escalate,
}

/// Intake's assessment of consequence and uncertainty.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum IntakeRisk {
    /// Low risk.
    Low,
    /// Medium risk.
    Medium,
    /// High risk requiring escalation.
    High,
}

/// Charter-goal judgment supplied with an intake report.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IntakeScope {
    /// Exact charter goal IDs cited by the report.
    pub goal_ids: Vec<String>,
    /// Explanation of fit to those goals.
    pub fit: String,
    /// Actual charter conflicts.
    pub conflicts: Vec<String>,
}

impl IntakeScope {
    /// Check one scope judgment against the exact pinned charter goals.
    pub fn validate(&self, charter: &str) -> Result<(), String> {
        if self.fit.trim().is_empty()
            || self
                .goal_ids
                .iter()
                .chain(&self.conflicts)
                .any(|value| value.trim().is_empty())
            || self
                .goal_ids
                .iter()
                .collect::<std::collections::BTreeSet<_>>()
                .len()
                != self.goal_ids.len()
        {
            return Err("incomplete charter scope judgment".to_owned());
        }
        let charter: Value =
            serde_json::from_str(charter).map_err(|_| "invalid project charter".to_owned())?;
        let goals = charter["goals"]
            .as_object()
            .ok_or_else(|| "invalid project charter".to_owned())?;
        if self.goal_ids.iter().any(|id| !goals.contains_key(id)) {
            return Err("scope cites an unknown charter goal".to_owned());
        }
        Ok(())
    }
}

/// Validated intake report retained with the task's accepted judgment.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IntakeReport {
    /// Stage decision.
    pub decision: IntakeDecision,
    /// Optional presentation heading.
    pub headline: Option<String>,
    /// Explanation of the decision.
    pub rationale: String,
    /// Consequence and uncertainty assessment.
    pub risk: IntakeRisk,
    /// Outstanding human questions.
    pub questions: Vec<String>,
    /// Advisory remarks for later stages.
    #[serde(alias = "concerns")]
    pub remarks: Vec<String>,
    /// Charter scope judgment.
    pub scope: IntakeScope,
}

impl IntakeReport {
    /// Project advisory classification away and validate a report against the charter.
    pub fn parse(mut raw: Value, charter: &str) -> Result<Self, String> {
        let Some(object) = raw.as_object_mut() else {
            return Err("invalid QC fields".to_owned());
        };
        object.remove("classification");
        let report: Self =
            serde_json::from_value(raw).map_err(|_| "invalid QC fields".to_owned())?;
        if report.rationale.trim().is_empty() {
            return Err("QC rationale missing".to_owned());
        }
        if report
            .questions
            .iter()
            .chain(&report.remarks)
            .any(|value| value.trim().is_empty())
        {
            return Err("invalid QC questions or concerns".to_owned());
        }
        report.scope.validate(charter)?;
        if report.decision == IntakeDecision::Proceed
            && (report.scope.goal_ids.is_empty() || !report.scope.conflicts.is_empty())
        {
            return Err("acceptance contradicts charter scope".to_owned());
        }
        if report.decision == IntakeDecision::Proceed && !report.questions.is_empty() {
            return Err("QC acceptance contradicts outstanding questions".to_owned());
        }
        if report.decision == IntakeDecision::Clarify && report.questions.is_empty() {
            return Err("QC clarification requires questions".to_owned());
        }
        Ok(report)
    }
}

/// Apply an accepted intake judgment only to its current attempt.
pub fn finish_intake(task: &TaskState, attempt: Uuid, report: &IntakeReport) -> Option<TaskState> {
    (task.stage == Stage::Intake && task.current_attempt == Some(attempt)).then(|| {
        let stage =
            if report.risk == IntakeRisk::High || report.decision == IntakeDecision::Escalate {
                Stage::Escalated
            } else {
                match report.decision {
                    IntakeDecision::Proceed => Stage::Plan,
                    IntakeDecision::Clarify => Stage::Clarify,
                    IntakeDecision::Reject => Stage::Rejected,
                    IntakeDecision::Escalate => unreachable!("escalation handled above"),
                }
            };
        TaskState {
            stage,
            revision: task.revision + 1,
            stage_reason: Some(if stage == Stage::Plan {
                "planning queued".to_owned()
            } else if stage == Stage::Escalated {
                format!(
                    "High-risk or consequential work requires human intervention. {}",
                    report.rationale
                )
            } else {
                report.rationale.clone()
            }),
            current_attempt: None,
            ..task.clone()
        }
    })
}

/// Durable task fields changed by start or stop.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct TaskState {
    /// Task identity retained across later starts of the issue.
    pub id: TaskId,
    /// GitHub issue identity.
    pub issue: IssueKey,
    /// Author of the issue the task belongs to.
    pub author: Actor,
    /// Current lifecycle stage.
    pub stage: Stage,
    /// Increases only when task state changes.
    pub revision: u64,
    /// Reason the current stage needs attention, when present.
    pub stage_reason: Option<String>,
    /// Active stage interrupted by a recoverable pause or writer stop.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub retry_stage: Option<Stage>,
    /// Inputs fixed before the first intake admission.
    pub pins: Option<PinnedInputs>,
    /// Monotonic admitted call ordinal; unused contributed reservations may be released.
    pub used_model_calls: u64,
    /// Current attempt for the active operation, if any.
    pub current_attempt: Option<Uuid>,
}

/// Render the M1 task stages into the single managed issue dashboard comment.
pub fn dashboard_body(state: &TaskState) -> String {
    let (activity, alert) = match state.stage {
        Stage::Initializing => ("Preparing the pinned task inputs.", "NOTE"),
        Stage::Intake if state.current_attempt.is_some() => ("Intake is in progress.", "NOTE"),
        Stage::Intake => ("Intake is queued.", "NOTE"),
        Stage::Plan => ("Planning is in progress.", "NOTE"),
        Stage::PlanQc => ("The plan is under independent review.", "NOTE"),
        Stage::Planned => ("The reviewed plan awaits writer approval.", "NOTE"),
        Stage::Implement => ("Implementation is in progress.", "NOTE"),
        Stage::Publish => ("The draft pull request is being published.", "NOTE"),
        Stage::Review => ("The pull request is under review.", "NOTE"),
        Stage::Gates => ("Checking pull request readiness.", "NOTE"),
        Stage::Ready => ("The pull request is ready for human review.", "NOTE"),
        Stage::ObservedMerged => ("The contribution was merged.", "NOTE"),
        Stage::Closed => ("The pull request closed without merging.", "WARNING"),
        Stage::Clarify => ("Waiting for your answers before continuing.", "WARNING"),
        Stage::Rejected => ("This issue was not accepted for implementation.", "WARNING"),
        Stage::Escalated => (
            "Paused — the task needs attention before it can continue.",
            "WARNING",
        ),
        Stage::Blocked => ("Paused — a blocker needs attention.", "CAUTION"),
        Stage::Stopped => ("Work on this issue has stopped.", "NOTE"),
    };
    let intake = match state.stage {
        Stage::Initializing => "Pending",
        Stage::Intake => "▶️ In progress",
        Stage::Plan
        | Stage::PlanQc
        | Stage::Planned
        | Stage::Implement
        | Stage::Publish
        | Stage::Review
        | Stage::Gates
        | Stage::Ready
        | Stage::ObservedMerged
        | Stage::Closed => "✅ Proceed",
        Stage::Clarify => "❓ Input needed",
        Stage::Rejected => "⛔ Rejected",
        Stage::Escalated => "⚠️ Escalated",
        Stage::Blocked => "⛔ Blocked",
        Stage::Stopped => "⏹️ Stopped",
    };
    let plan = match state.stage {
        Stage::Plan => "▶️ In progress",
        Stage::PlanQc => "🔎 Under review",
        Stage::Planned => "✅ Reviewed",
        Stage::Implement
        | Stage::Publish
        | Stage::Review
        | Stage::Gates
        | Stage::Ready
        | Stage::ObservedMerged
        | Stage::Closed => "✅ Approved",
        _ => "Pending",
    };
    let reason = publication::markdown_text(state.stage_reason.as_deref().unwrap_or(""), 160);
    let limit = state.pins.as_ref().map_or(0, |pins| pins.max_model_calls);
    format!(
        "<!-- concurro:v1 -->\n## Concurro\n\n> [!{alert}]\n> {activity}\n\n\
         Intake → Plan → Design review → Implement → Code review → Handoff\n\n\
         | Step | Status | Detail |\n| ---- | ---- | ---- |\n\
         | Intake | {intake} | {reason} |\n| Plan | {plan} | |\n\
         | Design review | Pending | |\n| Implement | Pending | |\n\
         | Code review | Pending | |\n| Handoff | Pending | |\n\n\
         Model calls: {} / {limit}\n",
        state.used_model_calls
    )
}

/// One exact exchange submission proposed by a task decision.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct AttemptSpec {
    /// Durable attempt ID, also the exchange turn ID.
    pub id: Uuid,
    /// Task that owns the attempt.
    pub task: TaskId,
    /// Task revision whose pinned inputs and next call number built this body.
    pub expected_revision: u64,
    /// Exchange staffing group reservation key.
    pub staffing_group: String,
    /// Fork pinned for this attempt's runner ref and workflow dispatch.
    pub fork: String,
    /// Configuration commit pinned before this attempt was admitted.
    pub config_sha: String,
    /// Exact serialized v1 submit body reused on every send.
    pub submit_body: Vec<u8>,
    /// Reconstructed prompt and response schema for a diagnostic artifact.
    pub prompt_body: Option<Vec<u8>>,
    /// Deadline including queueing and the turn timeout.
    pub deadline: DateTime<Utc>,
    /// Clear the deadline once this turn has begun running.
    pub unbounded_running: bool,
}

/// Reason an intake attempt did not enter the durable effect queue.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Admission {
    /// Attempt and charged call were recorded together.
    Admitted,
    /// The same attempt was already admitted.
    Existing,
    /// The required staffing group is still owned by another turn.
    WaitingForGroup,
    /// The task's configured call limit has been reached.
    BudgetExhausted,
    /// Exact retained predecessor was unavailable or inconsistent.
    RetainedUnavailable,
    /// Task stage or current attempt changed before admission.
    Stale,
}

/// Advance a newly started task after its pinned GitHub reads complete.
pub fn initialize(task: &TaskState, pins: PinnedInputs) -> Option<TaskState> {
    (task.stage == Stage::Initializing).then(|| TaskState {
        stage: Stage::Intake,
        revision: task.revision + 1,
        stage_reason: None,
        pins: Some(pins),
        ..task.clone()
    })
}

/// Status of one applied request.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub enum RequestStatus {
    /// The request changed state or recorded an authorized control.
    Applied,
    /// The request lacked authority or is unavailable in M1.
    Refused,
    /// A later applied control made the request obsolete.
    Superseded,
}

/// Render one bounded, source-marked result for a final issue-comment command.
pub fn request_reply_body(
    request: &Request,
    status: RequestStatus,
    reason: Option<&str>,
) -> String {
    let command = match &request.kind {
        RequestKind::Start => "start",
        RequestKind::Stop => "stop",
        RequestKind::Later(LaterCommand::Unenroll) => "unenroll",
        RequestKind::Later(LaterCommand::Funding { .. }) => "funding",
        RequestKind::Later(LaterCommand::Sponsor { .. }) => "sponsor",
        RequestKind::Later(LaterCommand::Withdraw { .. }) => "withdraw",
        RequestKind::Later(LaterCommand::Approve { .. }) => "approve",
        RequestKind::Later(LaterCommand::Retry {
            plan_revisions: Some(_),
        }) => "retry --plan-revisions",
        RequestKind::Later(LaterCommand::Retry {
            plan_revisions: None,
        }) => "retry",
        RequestKind::Later(LaterCommand::Resume { .. }) => "resume",
        RequestKind::Later(LaterCommand::Invalidate) => "invalidate",
    };
    let disposition = match status {
        RequestStatus::Applied => "applied",
        RequestStatus::Refused => "refused",
        RequestStatus::Superseded => "superseded",
    };
    let detail = reason.map_or(String::new(), |reason| format!(" Reason: `{reason}`."));
    format!(
        "<!-- concurro:reply:v1:{} -->\nConcurro `{command}` request {disposition}.{detail}",
        request.id
    )
}

/// Result of one pure request decision.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Decision {
    /// Final request status.
    pub status: RequestStatus,
    /// Stable reason when refused or superseded.
    pub reason: Option<&'static str>,
    /// New task state, if the request changed it.
    pub task: Option<TaskState>,
    /// New greatest applied stop key, if one was committed.
    pub stop_order: Option<SourceOrder>,
}

/// Read-time authority needed to apply a command.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Authority {
    /// The actor currently has upstream write permission.
    pub writer: bool,
    /// GitHub currently reports the issue open.
    pub issue_open: bool,
}

/// Parse the existing exact `/concurro` grammar; M1 explicitly refuses later commands.
pub fn parse_command(body: &str) -> Option<RequestKind> {
    static COMMAND: OnceLock<Regex> = OnceLock::new();
    let grammar = COMMAND.get_or_init(|| {
        Regex::new(
            r"\A/concurro (?:funding (?:openai|anthropic) (?:org|personal)|sponsor(?: (?:openai|anthropic))?|withdraw (?:openai|anthropic)|start|approve [a-f0-9]{64}|stop|resume(?: --description-only)?|retry(?: --plan-revisions [0-9]{1,3})?|invalidate)\z",
        )
        .expect("command grammar")
    });
    let command = body.trim();
    if !grammar.is_match(command) {
        return None;
    }
    let parts: Vec<&str> = command[10..].split_whitespace().collect();
    let kind = match parts.as_slice() {
        ["start"] => RequestKind::Start,
        ["stop"] => RequestKind::Stop,
        ["funding", provider, mode] => RequestKind::Later(LaterCommand::Funding {
            provider: (*provider).to_owned(),
            mode: (*mode).to_owned(),
        }),
        ["sponsor"] => RequestKind::Later(LaterCommand::Sponsor { provider: None }),
        ["sponsor", provider] => RequestKind::Later(LaterCommand::Sponsor {
            provider: Some((*provider).to_owned()),
        }),
        ["withdraw", provider] => RequestKind::Later(LaterCommand::Withdraw {
            provider: (*provider).to_owned(),
        }),
        ["approve", plan_id] => RequestKind::Later(LaterCommand::Approve {
            plan_id: (*plan_id).to_owned(),
        }),
        ["retry"] => RequestKind::Later(LaterCommand::Retry {
            plan_revisions: None,
        }),
        ["retry", "--plan-revisions", count] => RequestKind::Later(LaterCommand::Retry {
            plan_revisions: Some(count.parse().ok()?),
        }),
        ["resume"] => RequestKind::Later(LaterCommand::Resume {
            description_only: false,
        }),
        ["resume", "--description-only"] => RequestKind::Later(LaterCommand::Resume {
            description_only: true,
        }),
        ["invalidate"] => RequestKind::Later(LaterCommand::Invalidate),
        _ => unreachable!("validated command grammar"),
    };
    Some(kind)
}

/// Translate a verified issue comment delivery, including command removal and deletion.
pub fn comment_revision(
    event: &Envelope<GithubEvent<IssueCommentPayload>>,
) -> Option<CommentRevision> {
    parse_comment_revision(event, false)
}

/// Parse a PR conversation revision for the independent review command channel.
pub fn pr_comment_revision(
    event: &Envelope<GithubEvent<IssueCommentPayload>>,
) -> Option<CommentRevision> {
    parse_comment_revision(event, true)
}

/// Share source identity and author rules across issue and PR conversations.
fn parse_comment_revision(
    event: &Envelope<GithubEvent<IssueCommentPayload>>,
    allow_pr: bool,
) -> Option<CommentRevision> {
    let comment = &event.data.payload.comment;
    let actor = match event.data.action.as_deref()? {
        "created" => &comment.user,
        "edited" => &event.data.sender,
        "deleted" => &event.data.sender,
        _ => return None,
    };
    let deleted = event.data.action.as_deref() == Some("deleted");
    if event.data.payload.issue.is_pull_request != allow_pr {
        return None;
    }
    let body = comment.body.as_deref().unwrap_or_default();
    let revision = hex::encode(Sha256::digest(body.as_bytes()));
    let at = if deleted || event.data.action.as_deref() == Some("edited") {
        comment.updated_at.0
    } else {
        comment.created_at.0
    };
    let source = format!("{}:{}:{revision}", comment.id, at.to_rfc3339());
    let issue = IssueKey {
        repository_id: event.data.repository.id,
        number: event.data.payload.issue.number,
    };
    let order = SourceOrder {
        at,
        source_id: comment.id,
        source,
        index: 0,
    };
    let request = (!deleted)
        .then(|| parse_command(body))
        .flatten()
        .filter(|kind| {
            !matches!(
                kind,
                RequestKind::Later(
                    LaterCommand::Sponsor { .. }
                        | LaterCommand::Funding { .. }
                        | LaterCommand::Withdraw { .. }
                )
            ) || event.data.action.as_deref() != Some("edited")
                || event.data.sender.id == comment.user.id
        })
        .map(|kind| Request {
            id: Uuid::now_v7(),
            channel: "comment".to_owned(),
            reply: format!("issue_comment:{}", comment.id),
            comment_id: Some(comment.id),
            issue,
            issue_author: Actor {
                id: event.data.payload.issue.user.id,
                login: event.data.payload.issue.user.login.clone(),
            },
            actor: Actor {
                id: actor.id,
                login: actor.login.clone(),
            },
            kind,
            order: order.clone(),
        });
    Some(CommentRevision {
        issue,
        comment_id: comment.id,
        order,
        deleted,
        request,
    })
}

/// Apply one request against the latest task and durable control cutoff.
pub fn decide(
    request: &Request,
    task: Option<&TaskState>,
    applied_stop: Option<&SourceOrder>,
    applied_lifecycle: Option<&SourceOrder>,
    authority: Authority,
) -> Decision {
    let outcome = |status, reason| Decision {
        status,
        reason,
        task: None,
        stop_order: None,
    };
    if !authority.writer {
        return outcome(RequestStatus::Refused, Some("not_writer"));
    }
    if applied_stop.is_some_and(|stop| request.order <= *stop) {
        return outcome(RequestStatus::Superseded, Some("superseded_by_stop"));
    }
    if applied_lifecycle.is_some_and(|latest| request.order <= *latest) {
        return outcome(
            RequestStatus::Superseded,
            Some("superseded_by_newer_command"),
        );
    }
    if matches!(
        request.kind,
        RequestKind::Later(LaterCommand::Sponsor { provider: None })
    ) {
        return outcome(
            RequestStatus::Refused,
            Some("usage: /concurro sponsor <openai|anthropic>"),
        );
    }
    if request.kind == RequestKind::Later(LaterCommand::Unenroll) {
        return outcome(RequestStatus::Applied, None);
    }
    if matches!(request.kind, RequestKind::Later(_)) {
        return outcome(RequestStatus::Refused, Some("not_supported"));
    }
    match request.kind {
        RequestKind::Stop => Decision {
            status: RequestStatus::Applied,
            reason: None,
            task: task.and_then(|current| {
                (current.stage != Stage::Stopped).then(|| TaskState {
                    stage: Stage::Stopped,
                    revision: current.revision + 1,
                    stage_reason: None,
                    retry_stage: Some(current.stage),
                    current_attempt: None,
                    ..current.clone()
                })
            }),
            stop_order: Some(request.order.clone()),
        },
        RequestKind::Start if !authority.issue_open => {
            outcome(RequestStatus::Refused, Some("issue_closed"))
        }
        RequestKind::Start if task.is_some_and(|current| current.stage != Stage::Stopped) => {
            outcome(RequestStatus::Refused, Some("already_active"))
        }
        RequestKind::Start => Decision {
            status: RequestStatus::Applied,
            reason: None,
            task: Some(task.map_or_else(
                || TaskState {
                    id: TaskId::mint(),
                    issue: request.issue,
                    author: request.issue_author.clone(),
                    stage: Stage::Initializing,
                    revision: 1,
                    stage_reason: None,
                    retry_stage: None,
                    pins: None,
                    used_model_calls: 0,
                    current_attempt: None,
                },
                |current| TaskState {
                    stage: Stage::Initializing,
                    revision: current.revision + 1,
                    stage_reason: None,
                    retry_stage: None,
                    pins: None,
                    current_attempt: None,
                    ..current.clone()
                },
            )),
            stop_order: None,
        },
        RequestKind::Later(_) => unreachable!("later command was refused above"),
    }
}

#[cfg(test)]
mod tests {
    use chrono::{DateTime, Utc};
    use concurro_contracts::{ActorKind, Envelope, GithubEvent, IssueCommentPayload};
    use serde_json::json;
    use uuid::Uuid;

    use super::{
        Actor, Authority, IntakeReport, IssueKey, LaterCommand, Request, RequestKind,
        RequestStatus, SourceOrder, Stage, comment_revision, consent_facts, dashboard_body, decide,
        finish_intake, intake_notice_body, parse_command, welcome_body,
    };

    /// Make one request with its source time independent of arrival order.
    fn request(at: i64, kind: RequestKind) -> Request {
        Request {
            id: Uuid::now_v7(),
            channel: "comment".to_owned(),
            reply: format!("issue_comment:{at}"),
            comment_id: Some(at),
            issue: IssueKey {
                repository_id: 7,
                number: 12,
            },
            issue_author: Actor {
                id: 7,
                login: "author".to_owned(),
            },
            actor: Actor {
                id: 42,
                login: "writer".to_owned(),
            },
            kind,
            order: SourceOrder {
                at: DateTime::<Utc>::from_timestamp(at, 0).expect("source time"),
                source_id: at,
                source: format!("comment:{at}"),
                index: 0,
            },
        }
    }

    #[test]
    fn parses_exact_current_command_grammar() {
        assert_eq!(
            parse_command(" /concurro start\n"),
            Some(RequestKind::Start)
        );
        assert_eq!(parse_command("/concurro stop"), Some(RequestKind::Stop));
        assert_eq!(
            parse_command("/concurro approve ".to_owned().as_str()),
            None
        );
        assert_eq!(
            parse_command(&format!("/concurro approve {}", "a".repeat(64))),
            Some(RequestKind::Later(LaterCommand::Approve {
                plan_id: "a".repeat(64)
            }))
        );
        assert_eq!(parse_command("/concurro retry --plan-revisions 1000"), None);
        assert_eq!(parse_command("prefix /concurro start"), None);
    }

    #[test]
    fn intake_judgments_validate_charter_and_transition_once() {
        let charter = r#"{"goals":{"G-1":"Goal"}}"#;
        let mut state = decide(
            &request(1, RequestKind::Start),
            None,
            None,
            None,
            Authority {
                writer: true,
                issue_open: true,
            },
        )
        .task
        .expect("new task");
        let attempt = Uuid::now_v7();
        state.stage = Stage::Intake;
        state.current_attempt = Some(attempt);
        let base = json!({"decision":"proceed", "rationale":"Fits", "risk":"low",
            "questions":[], "concerns":["Watch compatibility"],
            "scope":{"goal_ids":["G-1"], "fit":"Matches goal", "conflicts":[]},
            "classification":{"advisory":true}});
        for (decision, risk, questions, expected) in [
            ("proceed", "low", vec![], Stage::Plan),
            ("clarify", "low", vec!["Which API?"], Stage::Clarify),
            ("reject", "low", vec![], Stage::Rejected),
            ("proceed", "high", vec![], Stage::Escalated),
            ("escalate", "medium", vec![], Stage::Escalated),
        ] {
            let mut raw = base.clone();
            raw["decision"] = json!(decision);
            raw["risk"] = json!(risk);
            raw["questions"] = json!(questions);
            let report = IntakeReport::parse(raw, charter).expect("valid report");
            let next = finish_intake(&state, attempt, &report).expect("current attempt");
            assert_eq!(next.stage, expected);
            assert_eq!(next.current_attempt, None);
            let dashboard = dashboard_body(&next);
            assert!(dashboard.starts_with("<!-- concurro:v1 -->\n## Concurro"));
            assert!(dashboard.contains("| Intake |"));
            let notice = intake_notice_body(&next, Some(&report));
            assert_eq!(notice.is_some(), expected != Stage::Intake);
            assert!(finish_intake(&next, attempt, &report).is_none());
        }
        for (field, value) in [
            ("questions", json!(["Unanswered"])),
            (
                "scope",
                json!({"goal_ids":["unknown"], "fit":"Fit", "conflicts":[]}),
            ),
        ] {
            let mut raw = base.clone();
            raw[field] = value;
            assert!(IntakeReport::parse(raw, charter).is_err());
        }
    }

    #[test]
    fn publication_text_contains_pinned_team_and_escapes_report_markup() {
        let welcome = welcome_body(
            &json!({"agents":{"triage":"triage-agent"},
                "models":{"triage-agent":{"model":"model-a","effort":"high"}}}),
            "writer",
        );
        assert!(welcome.starts_with("@writer I've picked up this issue"));
        assert!(welcome.contains("| Intake | model-a (high) |"));
        let mut state = decide(
            &request(1, RequestKind::Start),
            None,
            None,
            None,
            Authority {
                writer: true,
                issue_open: true,
            },
        )
        .task
        .expect("task");
        state.stage = Stage::Blocked;
        state.stage_reason = Some("owner | review\n<script>".to_owned());
        let dashboard = dashboard_body(&state);
        assert!(dashboard.contains("owner \\| review &lt;script&gt;"));
        let notice = intake_notice_body(&state, None).expect("blocked notice");
        assert!(notice.contains("owner \\| review &lt;script&gt;"));
    }

    #[test]
    fn comment_revision_has_stable_source_and_verified_actor() {
        let fixture = include_str!(
            "../../../contracts/fixtures/github.issue_comment.v1/valid/representative.json"
        );
        let mut event: Envelope<GithubEvent<IssueCommentPayload>> =
            serde_json::from_str(fixture).expect("typed comment");
        event.data.payload.comment.body = Some("/concurro start".to_owned());
        let first = comment_revision(&event)
            .expect("revision")
            .request
            .expect("command");
        let redelivery = comment_revision(&event)
            .expect("same source revision")
            .request
            .expect("command");
        assert_eq!(redelivery.order, first.order);
        assert_ne!(redelivery.id, first.id);
        assert_eq!(first.id.get_version_num(), 7);
        assert_eq!(first.actor.id, 10);
        event.data.action = Some("edited".to_owned());
        event.data.sender.id = 20;
        event.data.sender.login = "editor".to_owned();
        event.data.delivery_id = "edit-delivery".to_owned();
        event.data.payload.comment.body = Some("/concurro stop".to_owned());
        let edited = comment_revision(&event)
            .expect("edited revision")
            .request
            .expect("edited command");
        assert_eq!(
            (edited.actor.id, edited.actor.login.as_str()),
            (20, "editor")
        );
        assert_ne!(first.id, edited.id);
        event.data.payload.comment.body = Some("/concurro sponsor openai".to_owned());
        assert!(
            comment_revision(&event)
                .expect("changed source")
                .request
                .is_none(),
            "another actor's edit cannot claim their consent"
        );
        event.data.sender = event.data.payload.comment.user.clone();
        assert!(
            comment_revision(&event)
                .expect("owner revision")
                .request
                .is_some()
        );
        event.data.payload.issue.is_pull_request = true;
        assert!(comment_revision(&event).is_none());
        event.data.payload.issue.is_pull_request = false;
        event.data.sender.kind = ActorKind::Bot;
        event.data.payload.comment.user.kind = ActorKind::Bot;
        assert!(
            comment_revision(&event).is_some(),
            "external bot comments remain eligible"
        );
    }

    #[test]
    fn stop_precedes_pending_start_and_supersedes_late_delivery_after_restart() {
        let authority = Authority {
            writer: true,
            issue_open: true,
        };
        let old_start = request(10, RequestKind::Start);
        let stop = request(20, RequestKind::Stop);
        let newer_start = request(30, RequestKind::Start);
        let stopped = decide(&stop, None, None, None, authority);
        assert_eq!(stopped.status, RequestStatus::Applied);
        let encoded = serde_json::to_vec(&stopped.stop_order).expect("persist cutoff");
        let restored: Option<SourceOrder> =
            serde_json::from_slice(&encoded).expect("restore cutoff");
        let delayed = decide(&old_start, None, restored.as_ref(), None, authority);
        assert_eq!(delayed.status, RequestStatus::Superseded);
        assert_eq!(delayed.reason, Some("superseded_by_stop"));
        assert!(delayed.task.is_none());
        let started = decide(&newer_start, None, restored.as_ref(), None, authority);
        assert_eq!(started.status, RequestStatus::Applied);
        assert_eq!(started.task.expect("task").stage, Stage::Initializing);
    }

    #[test]
    fn refusal_never_changes_task_or_stop_cutoff() {
        let start = request(10, RequestKind::Start);
        let no_writer = Authority {
            writer: false,
            issue_open: true,
        };
        let refused = decide(&start, None, None, None, no_writer);
        assert_eq!(
            (refused.status, refused.reason),
            (RequestStatus::Refused, Some("not_writer"))
        );
        assert!(refused.task.is_none());
        let closed = decide(
            &start,
            None,
            None,
            None,
            Authority {
                writer: true,
                issue_open: false,
            },
        );
        assert_eq!(
            (closed.status, closed.reason),
            (RequestStatus::Refused, Some("issue_closed"))
        );
        let later = request(
            11,
            RequestKind::Later(LaterCommand::Sponsor {
                provider: Some("openai".to_owned()),
            }),
        );
        let unsupported = decide(
            &later,
            None,
            None,
            None,
            Authority {
                writer: true,
                issue_open: true,
            },
        );
        assert_eq!(unsupported.reason, Some("not_supported"));
        let bare = request(
            12,
            RequestKind::Later(LaterCommand::Sponsor { provider: None }),
        );
        assert!(consent_facts(&bare).is_empty());
        let usage = decide(
            &bare,
            None,
            None,
            None,
            Authority {
                writer: true,
                issue_open: true,
            },
        );
        assert_eq!(
            usage.reason,
            Some("usage: /concurro sponsor <openai|anthropic>")
        );
        assert!(unsupported.stop_order.is_none());
        let stop = request(12, RequestKind::Stop);
        let refused_stop = decide(&stop, None, None, None, no_writer);
        assert!(refused_stop.stop_order.is_none());
    }

    #[test]
    fn restart_after_stop_reuses_task_identity_and_increases_revision() {
        let authority = Authority {
            writer: true,
            issue_open: true,
        };
        let first = request(10, RequestKind::Start);
        let initial = decide(&first, None, None, None, authority)
            .task
            .expect("task");
        let stop = request(20, RequestKind::Stop);
        let stopped = decide(&stop, Some(&initial), None, Some(&first.order), authority)
            .task
            .expect("stopped");
        assert_eq!((stopped.stage, stopped.revision), (Stage::Stopped, 2));
        let restarted = decide(
            &request(30, RequestKind::Start),
            Some(&stopped),
            Some(&stop.order),
            Some(&stop.order),
            authority,
        )
        .task
        .expect("restarted");
        assert_eq!(
            (restarted.id, restarted.stage, restarted.revision),
            (initial.id, Stage::Initializing, 3)
        );
    }
}
