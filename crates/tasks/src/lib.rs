//! Task event capture and exchange effect execution.

mod archive;
mod bundle;
mod dashboard;
mod design_payload;
mod gates;
mod git_publication;
mod implementation_payload;
mod implementation_result;
mod initialization;
mod intake_payload;
mod plan_payload;
mod pr_review;
mod pr_review_payload;
mod pr_review_publication;
mod pr_review_threads;
mod publication;
mod report_publication;
mod runner;
mod stage_label;
mod target_config;

pub use archive::{ArchiveContext, UploadArchiveJob, upload_archive};
pub use bundle::bundle_identity;
pub use dashboard::{DashboardJob, publish_dashboard};
pub use gates::{TaskGatesJob, run_task_gates};
pub use git_publication::{
    GitPublicationContext, GitPublicationTarget, TaskGitPublicationJob, run_task_git_publication,
};
pub use implementation_result::{TaskImplementationResultJob, run_task_implementation_result};
pub use initialization::{InitializationTarget, read_initialization, read_requirements};
pub use intake_payload::IntakeTransport;
pub use pr_review::{
    TaskPrReviewBatchJob, TaskPrReviewCorrectionJob, TaskPrReviewResultJob,
    run_task_pr_review_batch, run_task_pr_review_correction, run_task_pr_review_result,
};
pub use pr_review_publication::{TaskPrReviewPublishJob, publish_pr_review};
pub use publication::{
    PublicationContext, PublicationTarget, PublishRequestReplyJob, publish_request_reply,
};
pub use report_publication::{PublishTaskReportJob, publish_task_report};
pub use runner::{RunnerContext, RunnerSessionJob, RunnerTarget, publish_runner_session};
pub use stage_label::{StageLabelJob, publish_stage_label};
pub use target_config::{TargetConfig, decode_target_config};

use std::{
    collections::{BTreeMap, HashMap, HashSet},
    fmt,
    time::Duration,
};

use anyhow::Result;
use concurro_contracts::{
    Envelope, GithubEvent, GithubIssues, IssueAction, IssueCommentPayload, KnownIssueAction, TaskId,
};
use concurro_db::{Database, UnitOfWork};
use concurro_events::{Handler, Outcome};
use concurro_events_postgres::Outbox;
use concurro_exchange_v1::{ExchangeError, Operator};
use concurro_github_v1::AppClient;
use concurro_tasks_core::{
    Actor, Admission, Authority, IssueKey, LaterCommand, Request, RequestKind, RequestStatus,
    SourceOrder, Stage, comment_revision, validate_design_review,
};
use concurro_tasks_postgres::{
    CommandEvidence, EffectKind, acknowledge_retirement, admit_design_batch,
    admit_design_correction, admit_implementation, admit_intake, admit_plan, applied_request,
    apply_design_result, apply_request_with_evidence, claim_effect, continue_planner_answer,
    design_batch, design_correction, design_result, expire_attempt, finish_unapplied_result,
    implementation_inputs, implementation_session_epoch, initialization_snapshot, initialize_task,
    latest_implementation_correction, latest_intake_correction, latest_plan_correction,
    latest_plan_revision, next_pending, open_plan_objections, pin_start_config,
    planner_session_epoch, pollable_generation, record_comment_revision, record_observation,
    record_poll_failure, record_request, record_submit_failure, refuse_stage_preflight,
    reviewer_objections, reviewer_session_epoch, settle_submit, settle_submit_refusal,
    start_config_pin, task, terminal_pr_inputs,
};
use graphile_worker::{IntoTaskHandlerResult, TaskHandler, WorkerContext};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use uuid::Uuid;

/// Capture configured target comments and issue closures.
#[derive(Clone)]
pub struct CommentHandler {
    targets: HashSet<i64>,
    owned_accounts: HashMap<i64, HashSet<String>>,
    outbox: Outbox,
}

impl CommentHandler {
    /// Bind the channel to the task application's configured repository IDs.
    pub fn new(targets: impl IntoIterator<Item = i64>, outbox: Outbox) -> Self {
        Self {
            targets: targets.into_iter().collect(),
            owned_accounts: HashMap::new(),
            outbox,
        }
    }

    /// Record configured publisher identities whose own text cannot command a task.
    pub fn with_owned_accounts(mut self, accounts: HashMap<i64, HashSet<String>>) -> Self {
        self.owned_accounts = accounts
            .into_iter()
            .map(|(id, names)| {
                (
                    id,
                    names
                        .into_iter()
                        .map(|name| name.to_ascii_lowercase())
                        .collect(),
                )
            })
            .collect();
        self
    }
}

impl Handler<GithubEvent<IssueCommentPayload>> for CommentHandler {
    /// Record a stable request in the shared event-consumer transaction.
    async fn handle(
        &self,
        uow: &mut UnitOfWork<'_>,
        event: Envelope<GithubEvent<IssueCommentPayload>>,
    ) -> Outcome {
        if !self.targets.contains(&event.data.repository.id) {
            return Outcome::Done;
        }
        let Some(mut revision) = comment_revision(&event) else {
            return Outcome::Done;
        };
        if event.data.action.as_deref() != Some("deleted")
            && self
                .owned_accounts
                .get(&event.data.repository.id)
                .is_some_and(|names| {
                    names.contains(&event.data.payload.comment.user.login.to_ascii_lowercase())
                        || names.contains(&event.data.sender.login.to_ascii_lowercase())
                })
        {
            revision.request = None;
        }
        match record_comment_revision(uow, &self.outbox, &revision).await {
            Ok(()) => Outcome::Done,
            Err(error) => Outcome::Retry(error.to_string()),
        }
    }
}

impl Handler<GithubIssues> for CommentHandler {
    /// Record a closed issue as a pending stop under its verified source identity.
    async fn handle(&self, uow: &mut UnitOfWork<'_>, event: Envelope<GithubIssues>) -> Outcome {
        let issue = &event.data.issue;
        if !self.targets.contains(&event.data.repository.id)
            || issue.is_pull_request
            || !matches!(
                event.data.action,
                IssueAction::Known(KnownIssueAction::Closed)
            )
        {
            return Outcome::Done;
        }
        let request = Request {
            id: Uuid::now_v7(),
            channel: "issue_close".to_owned(),
            reply: format!("issue_closed:{}", issue.number),
            comment_id: None,
            issue: IssueKey {
                repository_id: event.data.repository.id,
                number: issue.number,
            },
            issue_author: Actor {
                id: issue.user.id,
                login: issue.user.login.clone(),
            },
            actor: Actor {
                id: event.data.sender.id,
                login: event.data.sender.login.clone(),
            },
            kind: RequestKind::Stop,
            order: SourceOrder {
                at: issue.updated_at.0,
                source_id: issue.number,
                source: format!("issue_closed:{}", event.data.delivery_id),
                index: 0,
            },
        };
        match record_request(uow, &request).await {
            Ok(_) => Outcome::Done,
            Err(error) => Outcome::Retry(error.to_string()),
        }
    }
}

/// One target's repository name and required App read-token permissions.
#[derive(Clone)]
pub struct DecisionTarget {
    /// GitHub owner and repository name.
    pub repository: String,
    /// Exact permission set requested for a read token.
    pub read_permissions: BTreeMap<String, String>,
    /// Executor fork whose default head pins a new task's configuration.
    pub fork: String,
    /// Exact read permissions requested on that fork.
    pub fork_permissions: BTreeMap<String, String>,
    /// Complete source reader for commands that reassess an existing task.
    pub initialization: Option<InitializationTarget>,
    /// PR reader for commands that must preserve a published contribution.
    pub publication: Option<GitPublicationTarget>,
}

/// Dependencies shared by per-issue task decision jobs.
#[derive(Clone)]
pub struct DecisionContext {
    /// GitHub App token and read client.
    pub github: AppClient,
    /// Configured target repositories by numeric GitHub ID.
    pub targets: HashMap<i64, DecisionTarget>,
    /// Durable request and task event writer.
    pub outbox: Outbox,
}

/// Dependencies shared by pinned task initialization jobs.
#[derive(Clone)]
pub struct InitializationContext {
    /// GitHub App read client.
    pub github: AppClient,
    /// Configured issue targets by numeric GitHub repository ID.
    pub targets: HashMap<i64, InitializationTarget>,
    /// Durable task event writer.
    pub outbox: Outbox,
}

/// GitHub source reads and gateway settings for one pinned intake decision.
#[derive(Clone)]
pub struct IntakeContext {
    /// Source and runner identity reader for configured targets.
    pub initialization: InitializationContext,
    /// Exchange payload and runner routing settings.
    pub transport: IntakeTransport,
}

/// Exchange operator and transactional task event writer for turn jobs.
#[derive(Clone, Debug)]
pub struct ExchangeContext {
    /// Python exchange operator client.
    pub operator: Operator,
    /// Task event writer used when an observation changes stage.
    pub outbox: Outbox,
}

impl fmt::Debug for InitializationContext {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("InitializationContext")
            .field("target_count", &self.targets.len())
            .finish_non_exhaustive()
    }
}

impl fmt::Debug for IntakeContext {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("IntakeContext")
            .field("initialization", &self.initialization)
            .finish_non_exhaustive()
    }
}

impl fmt::Debug for DecisionContext {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("DecisionContext")
            .field("target_count", &self.targets.len())
            .finish_non_exhaustive()
    }
}

/// A serial Graphile Worker decision for one issue.
#[derive(Deserialize, Serialize)]
pub struct TaskDecisionJob {
    /// Numeric GitHub repository ID.
    pub repository_id: i64,
    /// Repository-local issue number.
    pub issue_number: i64,
}

impl TaskHandler for TaskDecisionJob {
    const IDENTIFIER: &'static str = "task_decision";

    /// Read current GitHub authority and apply the first pending request.
    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let decisions = context
            .get_ext::<DecisionContext>()
            .ok_or("task decision context unavailable")?;
        let database = Database::from_pool(context.pg_pool().clone());
        run_task_decision(
            &database,
            decisions,
            IssueKey {
                repository_id: self.repository_id,
                number: self.issue_number,
            },
        )
        .await
        .map_err(|error| error.to_string())
    }
}

/// Apply the issue's first pending request with a fresh GitHub authority read.
pub async fn run_task_decision(
    database: &Database,
    context: &DecisionContext,
    issue: IssueKey,
) -> Result<()> {
    let target = context
        .targets
        .get(&issue.repository_id)
        .ok_or_else(|| anyhow::anyhow!("task target unavailable"))?;
    loop {
        let Some(request) = next_pending(database, issue).await? else {
            return Ok(());
        };
        let observed = if request.channel == "issue_close" {
            let open = context
                .github
                .issue_open(&target.repository, issue.number, &target.read_permissions)
                .await?;
            concurro_github_v1::IssueAuthority { open, writer: true }
        } else {
            context
                .github
                .issue_authority(
                    &target.repository,
                    issue.number,
                    &request.actor.login,
                    &target.read_permissions,
                )
                .await?
        };
        let needs_source = matches!(
            request.kind,
            RequestKind::Start
                | RequestKind::Later(LaterCommand::Retry { .. })
                | RequestKind::Later(LaterCommand::Resume { .. })
                | RequestKind::Later(LaterCommand::Invalidate)
        );
        let config_sha = if needs_source && observed.writer && observed.open {
            Some(
                context
                    .github
                    .default_head(&target.fork, &target.fork_permissions)
                    .await?
                    .1,
            )
        } else {
            None
        };
        let evidence = if observed.writer
            && observed.open
            && matches!(
                request.kind,
                RequestKind::Later(LaterCommand::Retry { .. })
                    | RequestKind::Later(LaterCommand::Resume { .. })
                    | RequestKind::Later(LaterCommand::Invalidate)
            )
            && task(database, issue).await?.is_some()
        {
            let source = target
                .initialization
                .as_ref()
                .ok_or_else(|| anyhow::anyhow!("command source target unavailable"))?;
            let snapshot = initialization::read_initialization(
                &context.github,
                source,
                &request,
                config_sha
                    .as_deref()
                    .expect("recovery source has fork head"),
            )
            .await?;
            let pr_unchanged = if let Some(saved) = terminal_pr_inputs(database, issue).await? {
                let published = target
                    .publication
                    .as_ref()
                    .ok_or_else(|| anyhow::anyhow!("command PR target unavailable"))?;
                let (owner, name) = published
                    .upstream
                    .split_once('/')
                    .ok_or_else(|| anyhow::anyhow!("upstream must be owner/name"))?;
                let number = saved.pr_number.to_string();
                let pr: Value = context
                    .github
                    .read(
                        &published.upstream,
                        &published.upstream_write_permissions,
                        &["repos", owner, name, "pulls", &number],
                        &[],
                    )
                    .await?;
                pr["number"] == saved.pr_number
                    && pr["state"] == "open"
                    && pr["merged"] != true
                    && pr["head"]["sha"] == saved.head
                    && pr["head"]["ref"] == saved.branch
                    && pr["head"]["repo"]["full_name"]
                        .as_str()
                        .is_some_and(|name| name.eq_ignore_ascii_case(&published.fork))
                    && pr["base"]["ref"] == saved.base
                    && pr["user"]["login"] == published.bot_login
            } else {
                true
            };
            let implementer_skill_update = if matches!(
                request.kind,
                RequestKind::Later(LaterCommand::Retry {
                    plan_revisions: None
                })
            ) {
                let current = task(database, issue).await?.expect("recovery task");
                initialization_snapshot(database, current.id)
                    .await?
                    .map(|saved| bundle::implementer_skill_update(&saved, &snapshot))
                    .transpose()?
                    .flatten()
            } else {
                None
            };
            Some(CommandEvidence {
                snapshot,
                pr_unchanged,
                implementer_skill_update,
            })
        } else {
            None
        };
        let mut apply = database.begin().await?;
        let decision = apply_request_with_evidence(
            &mut apply,
            &context.outbox,
            request.id,
            issue,
            Authority {
                writer: observed.writer,
                issue_open: observed.open,
            },
            evidence.as_ref(),
        )
        .await?;
        if let Some((applied, outcome)) = decision {
            if applied.kind == RequestKind::Start
                && outcome.status == RequestStatus::Applied
                && outcome
                    .task
                    .as_ref()
                    .is_some_and(|task| matches!(task.stage, Stage::Initializing | Stage::Clarify))
            {
                pin_start_config(
                    &mut apply,
                    applied.id,
                    outcome.task.as_ref().expect("applied start has task"),
                    config_sha
                        .as_deref()
                        .expect("authorized start has fork head"),
                )
                .await?;
            }
        }
        apply.commit().await?;
    }
}

/// A Graphile Worker job that freezes one applied start's source inputs.
#[derive(Deserialize, Serialize)]
pub struct TaskInitializeJob {
    /// Applied start request identity.
    pub request_id: Uuid,
    /// Numeric GitHub repository ID.
    pub repository_id: i64,
    /// Repository-local issue number.
    pub issue_number: i64,
    /// Task revision created by the start request.
    pub revision: u64,
}

impl TaskHandler for TaskInitializeJob {
    const IDENTIFIER: &'static str = "task_initialize";

    /// Read source files and commit only if this start revision remains current.
    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let initialization = context
            .get_ext::<InitializationContext>()
            .ok_or("task initialization context unavailable")?;
        let database = Database::from_pool(context.pg_pool().clone());
        run_task_initialize(
            &database,
            initialization,
            self.request_id,
            IssueKey {
                repository_id: self.repository_id,
                number: self.issue_number,
            },
            self.revision,
        )
        .await
        .map_err(|error| error.to_string())
    }
}

/// Freeze GitHub inputs for a current applied start and advance it to intake.
pub async fn run_task_initialize(
    database: &Database,
    context: &InitializationContext,
    request_id: Uuid,
    issue: IssueKey,
    revision: u64,
) -> Result<()> {
    let Some(request) = applied_request(database, request_id).await? else {
        return Ok(());
    };
    anyhow::ensure!(request.issue == issue, "initialization job issue mismatch");
    let Some(state) = task(database, issue).await? else {
        return Ok(());
    };
    if state.revision != revision || state.stage != Stage::Initializing {
        return Ok(());
    }
    let target = context
        .targets
        .get(&issue.repository_id)
        .ok_or_else(|| anyhow::anyhow!("initialization target unavailable"))?;
    let config_sha = start_config_pin(database, issue, request_id, revision)
        .await?
        .ok_or_else(|| anyhow::anyhow!("applied start configuration pin unavailable"))?;
    let snapshot = read_initialization(&context.github, target, &request, &config_sha).await?;
    let mut apply = database.begin().await?;
    initialize_task(&mut apply, &context.outbox, issue, revision, &snapshot).await?;
    apply.commit().await?;
    Ok(())
}

/// A Graphile Worker job that checks one writer-backed planner answer.
#[derive(Deserialize, Serialize)]
pub struct TaskClarificationJob {
    /// Applied start request identifying the answer cutoff.
    pub request_id: Uuid,
    /// Numeric GitHub repository ID.
    pub repository_id: i64,
    /// Repository-local issue number.
    pub issue_number: i64,
    /// Task revision that requested the source read.
    pub revision: u64,
}

impl TaskHandler for TaskClarificationJob {
    const IDENTIFIER: &'static str = "task_clarification";

    /// Recheck the pinned source and advance only a matching planner answer.
    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let initialization = context
            .get_ext::<InitializationContext>()
            .ok_or("clarification source context unavailable")?;
        let database = Database::from_pool(context.pg_pool().clone());
        run_task_clarification(
            &database,
            initialization,
            self.request_id,
            IssueKey {
                repository_id: self.repository_id,
                number: self.issue_number,
            },
            self.revision,
        )
        .await
        .map_err(|error| error.to_string())
    }
}

/// Read one source-ordered answer without changing the current configuration pin.
pub async fn run_task_clarification(
    database: &Database,
    context: &InitializationContext,
    request_id: Uuid,
    issue: IssueKey,
    revision: u64,
) -> Result<()> {
    let Some(request) = applied_request(database, request_id).await? else {
        return Ok(());
    };
    anyhow::ensure!(request.issue == issue, "clarification job issue mismatch");
    let Some(current) = task(database, issue).await? else {
        return Ok(());
    };
    if current.stage != Stage::Clarify || current.revision != revision {
        return Ok(());
    }
    let original = initialization_snapshot(database, current.id)
        .await?
        .ok_or_else(|| anyhow::anyhow!("planner source snapshot unavailable"))?;
    let target = context
        .targets
        .get(&issue.repository_id)
        .ok_or_else(|| anyhow::anyhow!("clarification target unavailable"))?;
    let mut fresh = original;
    let (requirements, comment_ids) =
        initialization::read_requirements(&context.github, target, &request).await?;
    fresh.requirements = requirements;
    let mut work = database.begin().await?;
    continue_planner_answer(
        &mut work,
        &context.outbox,
        issue,
        revision,
        &fresh,
        comment_ids.last().copied().unwrap_or_default(),
    )
    .await?;
    work.commit().await?;
    Ok(())
}

/// A Graphile Worker job that prepares and charges one pinned intake turn.
#[derive(Deserialize, Serialize)]
pub struct TaskIntakeJob {
    /// Numeric GitHub repository ID.
    pub repository_id: i64,
    /// Repository-local issue number.
    pub issue_number: i64,
    /// Task revision that requested an intake attempt.
    pub revision: u64,
}

impl TaskHandler for TaskIntakeJob {
    const IDENTIFIER: &'static str = "task_intake";

    /// Read the current runner identity and commit an exact submit body.
    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let intake = context
            .get_ext::<IntakeContext>()
            .ok_or("intake context unavailable")?;
        let database = Database::from_pool(context.pg_pool().clone());
        run_task_intake(
            &database,
            intake,
            IssueKey {
                repository_id: self.repository_id,
                number: self.issue_number,
            },
            self.revision,
        )
        .await
        .map_err(|error| error.to_string())
    }
}

/// Recheck a queued intake revision, then atomically fund and record its turn.
pub async fn run_task_intake(
    database: &Database,
    context: &IntakeContext,
    issue: IssueKey,
    revision: u64,
) -> Result<()> {
    let Some(current) = task(database, issue).await? else {
        return Ok(());
    };
    if current.revision != revision
        || current.stage != Stage::Intake
        || current.current_attempt.is_some()
    {
        return Ok(());
    }
    let snapshot = initialization_snapshot(database, current.id)
        .await?
        .ok_or_else(|| anyhow::anyhow!("intake source snapshot unavailable"))?;
    let target = context
        .initialization
        .targets
        .get(&issue.repository_id)
        .ok_or_else(|| anyhow::anyhow!("intake target unavailable"))?;
    let correction = latest_intake_correction(database, current.id.0).await?;
    let expected = context
        .initialization
        .github
        .runner_claims(
            &target.fork,
            &target.fork_permissions,
            &context.transport.workflow_ref,
            &target.bot_login,
        )
        .await?;
    let attempt = intake_payload::prepare_intake_attempt(
        issue,
        &current,
        &snapshot,
        target,
        &context.transport,
        expected,
        correction.as_ref(),
        Uuid::now_v7(),
        chrono::Utc::now(),
    )?;
    let mut commit = database.begin().await?;
    let admission =
        admit_intake(&mut commit, &context.initialization.outbox, issue, &attempt).await?;
    commit.commit().await?;
    if admission == Admission::WaitingForGroup {
        anyhow::bail!("intake staffing group is still retiring");
    }
    Ok(())
}

/// A Graphile Worker job that prepares and charges one pinned planner turn.
#[derive(Deserialize, Serialize)]
pub struct TaskPlanJob {
    /// Numeric GitHub repository ID.
    pub repository_id: i64,
    /// Repository-local issue number.
    pub issue_number: i64,
    /// Task revision that requested planning.
    pub revision: u64,
}

impl TaskHandler for TaskPlanJob {
    const IDENTIFIER: &'static str = "task_plan";

    /// Admit the planner only while this task revision still needs a plan.
    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let intake = context
            .get_ext::<IntakeContext>()
            .ok_or("planner context unavailable")?;
        let archive = context
            .get_ext::<ArchiveContext>()
            .ok_or("retained archive context unavailable")?;
        let database = Database::from_pool(context.pg_pool().clone());
        run_task_plan(
            &database,
            intake,
            archive,
            IssueKey {
                repository_id: self.repository_id,
                number: self.issue_number,
            },
            self.revision,
        )
        .await
        .map_err(|error| error.to_string())
    }
}

/// Recheck a queued plan revision, then atomically fund its exact planner turn.
pub async fn run_task_plan(
    database: &Database,
    context: &IntakeContext,
    archive: &ArchiveContext,
    issue: IssueKey,
    revision: u64,
) -> Result<()> {
    let Some(current) = task(database, issue).await? else {
        return Ok(());
    };
    if current.revision != revision
        || current.stage != Stage::Plan
        || current.current_attempt.is_some()
    {
        return Ok(());
    }
    let snapshot = initialization_snapshot(database, current.id)
        .await?
        .ok_or_else(|| anyhow::anyhow!("plan source snapshot unavailable"))?;
    let target = context
        .initialization
        .targets
        .get(&issue.repository_id)
        .ok_or_else(|| anyhow::anyhow!("plan target unavailable"))?;
    let expected = context
        .initialization
        .github
        .runner_claims(
            &target.fork,
            &target.fork_permissions,
            &context.transport.workflow_ref,
            &target.bot_login,
        )
        .await?;
    let correction = latest_plan_correction(database, current.id.0).await?;
    let revision_context = latest_plan_revision(database, current.id.0).await?;
    let open_objections = open_plan_objections(database, current.id.0).await?;
    let (provider, model, _, _) =
        intake_payload::assigned_agent(&snapshot.configuration, "planner")?;
    let session_epoch = planner_session_epoch(database, current.id.0).await?;
    let retained = plan_payload::prepare_retained_attempt(
        database,
        archive,
        &context.initialization.outbox,
        &current,
        &snapshot,
        "plan",
        &provider,
        &model,
        None,
        session_epoch,
    )
    .await?;
    let attempt = plan_payload::prepare_plan_attempt(
        issue,
        &current,
        &snapshot,
        target,
        &context.transport,
        expected,
        correction.as_ref(),
        revision_context.as_ref(),
        &open_objections,
        &retained,
        Uuid::now_v7(),
        chrono::Utc::now(),
    )?;
    let mut commit = database.begin().await?;
    let admission =
        admit_plan(&mut commit, &context.initialization.outbox, issue, &attempt).await?;
    commit.commit().await?;
    if admission == Admission::WaitingForGroup {
        anyhow::bail!("planner staffing group is still retiring");
    }
    Ok(())
}

/// A Graphile Worker job that admits the writer-approved implementer.
#[derive(Deserialize, Serialize)]
pub struct TaskImplementJob {
    /// Numeric GitHub repository ID.
    pub repository_id: i64,
    /// Repository-local issue number.
    pub issue_number: i64,
    /// Task revision that received the writer approval.
    pub revision: u64,
}

impl TaskHandler for TaskImplementJob {
    const IDENTIFIER: &'static str = "task_implement";

    /// Admit the implementer only while the exact approval remains current.
    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let intake = context
            .get_ext::<IntakeContext>()
            .ok_or("implementation context unavailable")?;
        let archive = context
            .get_ext::<ArchiveContext>()
            .ok_or("retained archive context unavailable")?;
        let database = Database::from_pool(context.pg_pool().clone());
        run_task_implement(
            &database,
            intake,
            archive,
            IssueKey {
                repository_id: self.repository_id,
                number: self.issue_number,
            },
            self.revision,
        )
        .await
        .map_err(|error| error.to_string())
    }
}

/// Prepare and charge one implementer against the current approved design.
pub async fn run_task_implement(
    database: &Database,
    context: &IntakeContext,
    archive: &ArchiveContext,
    issue: IssueKey,
    revision: u64,
) -> Result<()> {
    let Some(current) = task(database, issue).await? else {
        return Ok(());
    };
    if current.revision != revision
        || current.stage != Stage::Implement
        || current.current_attempt.is_some()
    {
        return Ok(());
    }
    let snapshot = initialization_snapshot(database, current.id)
        .await?
        .ok_or_else(|| anyhow::anyhow!("implementation source snapshot unavailable"))?;
    if bundle_identity(&snapshot.charter, &snapshot.instruction_texts)? != snapshot.bundle_id {
        let mut work = database.begin().await?;
        refuse_stage_preflight(
            &mut work,
            &context.initialization.outbox,
            current.id.0,
            current.revision,
            "Implementation instructions changed; a writer may retry a verified implementer-only update",
        )
        .await?;
        work.commit().await?;
        return Ok(());
    }
    let inputs = implementation_inputs(database, current.id.0)
        .await?
        .ok_or_else(|| anyhow::anyhow!("writer-approved design unavailable"))?;
    let target = context
        .initialization
        .targets
        .get(&issue.repository_id)
        .ok_or_else(|| anyhow::anyhow!("implementation target unavailable"))?;
    let expected = context
        .initialization
        .github
        .runner_claims(
            &target.fork,
            &target.fork_permissions,
            &context.transport.workflow_ref,
            &target.bot_login,
        )
        .await?;
    let (provider, model, _, _) =
        intake_payload::assigned_agent(&snapshot.configuration, "implementer")?;
    let correction = latest_implementation_correction(database, current.id.0).await?;
    let session_epoch = implementation_session_epoch(database, current.id.0).await?;
    let retained = plan_payload::prepare_retained_attempt(
        database,
        archive,
        &context.initialization.outbox,
        &current,
        &snapshot,
        "develop",
        &provider,
        &model,
        None,
        session_epoch,
    )
    .await?;
    let attempt = implementation_payload::prepare_implementation_attempt(
        issue,
        &current,
        &snapshot,
        target,
        &context.transport,
        expected,
        &inputs,
        correction.as_ref(),
        &retained,
        Uuid::now_v7(),
        chrono::Utc::now(),
    )?;
    let mut commit = database.begin().await?;
    let admission =
        admit_implementation(&mut commit, &context.initialization.outbox, issue, &attempt).await?;
    commit.commit().await?;
    if admission == Admission::WaitingForGroup {
        anyhow::bail!("implementer staffing group is still retiring");
    }
    Ok(())
}

/// A Graphile Worker job that admits one frozen independent design roster.
#[derive(Deserialize, Serialize)]
pub struct TaskDesignBatchJob {
    /// Numeric GitHub repository ID.
    pub repository_id: i64,
    /// Repository-local issue number.
    pub issue_number: i64,
    /// Task revision that recorded the review batch.
    pub revision: u64,
    /// Durable batch identity.
    pub batch: Uuid,
}

impl TaskHandler for TaskDesignBatchJob {
    const IDENTIFIER: &'static str = "task_design_batch";

    /// Prepare all private reviewer turns before one funding transaction.
    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let intake = context
            .get_ext::<IntakeContext>()
            .ok_or("design review context unavailable")?;
        let archive = context
            .get_ext::<ArchiveContext>()
            .ok_or("retained archive context unavailable")?;
        let database = Database::from_pool(context.pg_pool().clone());
        run_task_design_batch(
            &database,
            intake,
            archive,
            IssueKey {
                repository_id: self.repository_id,
                number: self.issue_number,
            },
            self.revision,
            self.batch,
        )
        .await
        .map_err(|error| error.to_string())
    }
}

/// Recheck one recorded batch, then charge its reviewers atomically.
pub async fn run_task_design_batch(
    database: &Database,
    context: &IntakeContext,
    archive: &ArchiveContext,
    issue: IssueKey,
    revision: u64,
    batch_id: Uuid,
) -> Result<()> {
    let Some(current) = task(database, issue).await? else {
        return Ok(());
    };
    if current.stage != Stage::PlanQc || current.revision != revision {
        return Ok(());
    }
    let Some(batch) = design_batch(database, batch_id).await? else {
        return Ok(());
    };
    if batch.task_id != current.id.0 {
        anyhow::bail!("review batch belongs to another task");
    }
    let snapshot = initialization_snapshot(database, current.id)
        .await?
        .ok_or_else(|| anyhow::anyhow!("design source snapshot unavailable"))?;
    let target = context
        .initialization
        .targets
        .get(&issue.repository_id)
        .ok_or_else(|| anyhow::anyhow!("design target unavailable"))?;
    let expected = context
        .initialization
        .github
        .runner_claims(
            &target.fork,
            &target.fork_permissions,
            &context.transport.workflow_ref,
            &target.bot_login,
        )
        .await?;
    let paths = batch.plan["files"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("accepted plan paths unavailable"))?
        .iter()
        .map(|path| {
            path.as_str()
                .map(str::to_owned)
                .ok_or_else(|| anyhow::anyhow!("accepted plan path is invalid"))
        })
        .collect::<Result<Vec<_>>>()?;
    let manifest = initialization::read_file_manifest(
        &context.initialization.github,
        &target.upstream,
        &target.upstream_permissions,
        &snapshot.pins.base_sha,
        &paths,
    )
    .await?;
    let now = chrono::Utc::now();
    let mut specs = Vec::with_capacity(batch.roster.len());
    for reviewer in &batch.roster {
        let session_epoch = reviewer_session_epoch(database, current.id.0, &reviewer.id).await?;
        let own_objections = if batch.phase == "cooperative" {
            reviewer_objections(database, current.id.0, &reviewer.id).await?
        } else {
            Vec::new()
        };
        let retained = if batch.phase == "fresh" {
            None
        } else {
            let provider = reviewer
                .agent
                .as_ref()
                .and_then(|agent| agent["harness"].as_str())
                .unwrap_or(&reviewer.provider);
            Some(
                plan_payload::prepare_retained_attempt(
                    database,
                    archive,
                    &context.initialization.outbox,
                    &current,
                    &snapshot,
                    "plan_qc",
                    provider,
                    &reviewer.model,
                    Some(&reviewer.id),
                    session_epoch,
                )
                .await?,
            )
        };
        let spec = design_payload::prepare_design_attempt(
            issue,
            &current,
            &snapshot,
            target,
            &context.transport,
            expected.clone(),
            &batch,
            reviewer,
            &own_objections,
            &manifest,
            None,
            retained.as_ref(),
            Uuid::now_v7(),
            now,
        )?;
        specs.push((reviewer.id.clone(), spec));
    }
    let mut commit = database.begin().await?;
    let admission = admit_design_batch(
        &mut commit,
        &context.initialization.outbox,
        issue,
        batch_id,
        revision,
        &specs,
    )
    .await?;
    commit.commit().await?;
    if admission == Admission::WaitingForGroup {
        anyhow::bail!("design reviewer staffing group is still retiring");
    }
    Ok(())
}

/// A Graphile Worker job that admits one bounded private review correction.
#[derive(Deserialize, Serialize)]
pub struct TaskDesignCorrectionJob {
    /// Durable review batch identity.
    pub batch: Uuid,
    /// Bound reviewer seat identity.
    pub reviewer: String,
    /// One-based correction count.
    pub count: i32,
}

impl TaskHandler for TaskDesignCorrectionJob {
    const IDENTIFIER: &'static str = "task_design_correction";

    /// Prepare only the rejected reviewer's next turn.
    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let intake = context
            .get_ext::<IntakeContext>()
            .ok_or("design correction context unavailable")?;
        let archive = context
            .get_ext::<ArchiveContext>()
            .ok_or("retained archive context unavailable")?;
        let database = Database::from_pool(context.pg_pool().clone());
        run_task_design_correction(
            &database,
            intake,
            archive,
            self.batch,
            &self.reviewer,
            self.count,
        )
        .await
        .map_err(|error| error.to_string())
    }
}

/// Recheck the seat, then fund only its exact corrective assessment.
pub async fn run_task_design_correction(
    database: &Database,
    context: &IntakeContext,
    archive: &ArchiveContext,
    batch_id: Uuid,
    reviewer_id: &str,
    count: i32,
) -> Result<()> {
    let Some(correction) = design_correction(database, batch_id, reviewer_id, count).await? else {
        return Ok(());
    };
    let Some(current) = task(database, correction.issue).await? else {
        return Ok(());
    };
    if current.stage != Stage::PlanQc || current.id.0 != correction.batch.task_id {
        return Ok(());
    }
    let snapshot = initialization_snapshot(database, current.id)
        .await?
        .ok_or_else(|| anyhow::anyhow!("design source snapshot unavailable"))?;
    let target = context
        .initialization
        .targets
        .get(&correction.issue.repository_id)
        .ok_or_else(|| anyhow::anyhow!("design target unavailable"))?;
    let expected = context
        .initialization
        .github
        .runner_claims(
            &target.fork,
            &target.fork_permissions,
            &context.transport.workflow_ref,
            &target.bot_login,
        )
        .await?;
    let paths = correction.batch.plan["files"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("accepted plan paths unavailable"))?
        .iter()
        .map(|path| {
            path.as_str()
                .map(str::to_owned)
                .ok_or_else(|| anyhow::anyhow!("accepted plan path is invalid"))
        })
        .collect::<Result<Vec<_>>>()?;
    let manifest = initialization::read_file_manifest(
        &context.initialization.github,
        &target.upstream,
        &target.upstream_permissions,
        &snapshot.pins.base_sha,
        &paths,
    )
    .await?;
    let own_objections = if correction.batch.phase == "cooperative" {
        reviewer_objections(database, current.id.0, reviewer_id).await?
    } else {
        Vec::new()
    };
    let retained = if correction.batch.phase == "fresh" {
        None
    } else {
        let provider = correction
            .reviewer
            .agent
            .as_ref()
            .and_then(|agent| agent["harness"].as_str())
            .unwrap_or(&correction.reviewer.provider);
        Some(
            plan_payload::prepare_retained_attempt(
                database,
                archive,
                &context.initialization.outbox,
                &current,
                &snapshot,
                "plan_qc",
                provider,
                &correction.reviewer.model,
                Some(&correction.reviewer.id),
                correction.session_epoch,
            )
            .await?,
        )
    };
    let attempt = design_payload::prepare_design_attempt(
        correction.issue,
        &current,
        &snapshot,
        target,
        &context.transport,
        expected,
        &correction.batch,
        &correction.reviewer,
        &own_objections,
        &manifest,
        Some(&correction),
        retained.as_ref(),
        Uuid::now_v7(),
        chrono::Utc::now(),
    )?;
    let mut commit = database.begin().await?;
    let admission = admit_design_correction(
        &mut commit,
        &context.initialization.outbox,
        correction.issue,
        batch_id,
        reviewer_id,
        count,
        &attempt,
    )
    .await?;
    commit.commit().await?;
    if admission == Admission::WaitingForGroup {
        anyhow::bail!("design correction staffing group is still retiring");
    }
    Ok(())
}

/// A Graphile Worker job that validates one retained review against pinned source.
#[derive(Deserialize, Serialize)]
pub struct TaskDesignResultJob {
    /// Exchange turn supplying the saved response.
    pub turn: Uuid,
}

impl TaskHandler for TaskDesignResultJob {
    const IDENTIFIER: &'static str = "task_design_result";

    /// Fetch only cited pinned source files, then apply one fenced vote.
    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let intake = context
            .get_ext::<IntakeContext>()
            .ok_or("design result context unavailable")?;
        let database = Database::from_pool(context.pg_pool().clone());
        run_task_design_result(&database, intake, self.turn)
            .await
            .map_err(|error| error.to_string())
    }
}

/// Validate a saved reviewer result after source reads, preserving sibling isolation.
pub async fn run_task_design_result(
    database: &Database,
    context: &IntakeContext,
    turn: Uuid,
) -> Result<()> {
    let Some(result) = design_result(database, turn).await? else {
        let mut commit = database.begin().await?;
        finish_unapplied_result(&mut commit, &context.initialization.outbox, turn).await?;
        commit.commit().await?;
        return Ok(());
    };
    let snapshot = initialization_snapshot(database, TaskId(result.task_id))
        .await?
        .ok_or_else(|| anyhow::anyhow!("design source snapshot unavailable"))?;
    let target = context
        .initialization
        .targets
        .get(&result.issue.repository_id)
        .ok_or_else(|| anyhow::anyhow!("design target unavailable"))?;
    let mut files = BTreeMap::new();
    let paths = result.report["sources"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|source| source["path"].as_str())
        .filter(|path| {
            !path.is_empty()
                && !path.starts_with('/')
                && !path.contains('\\')
                && path
                    .split('/')
                    .all(|part| !matches!(part, "" | "." | ".." | ".git"))
        })
        .map(str::to_owned)
        .collect::<HashSet<_>>()
        .into_iter()
        .collect::<Vec<_>>();
    if !paths.is_empty() {
        let manifest = initialization::read_file_manifest(
            &context.initialization.github,
            &target.upstream,
            &target.upstream_permissions,
            &snapshot.pins.base_sha,
            &paths,
        )
        .await?;
        for path in paths {
            let entry = &manifest[&path];
            if entry["type"] != "blob"
                || !matches!(entry["mode"].as_str(), Some("100644" | "100755"))
            {
                continue;
            }
            if let Some(content) = context
                .initialization
                .github
                .file_text(
                    &target.upstream,
                    &target.upstream_permissions,
                    &path,
                    &snapshot.pins.base_sha,
                )
                .await?
            {
                files.insert(path, content);
            }
        }
    }
    let own_ids = if result.phase == "cooperative" {
        reviewer_objections(database, result.task_id, &result.reviewer.id)
            .await?
            .into_iter()
            .filter_map(|objection| objection["id"].as_str().map(str::to_owned))
            .collect::<Vec<_>>()
    } else {
        Vec::new()
    };
    let validated = validate_design_review(
        result.report,
        &snapshot.requirements,
        &snapshot.charter,
        &files,
        &own_ids,
    );
    let mut commit = database.begin().await?;
    let applied =
        apply_design_result(&mut commit, &context.initialization.outbox, turn, validated).await?;
    if !applied {
        finish_unapplied_result(&mut commit, &context.initialization.outbox, turn).await?;
    }
    commit.commit().await?;
    Ok(())
}

/// A Graphile Worker job for one durable exchange effect.
#[derive(Deserialize, Serialize)]
pub struct ExchangeEffectJob {
    /// Effect row ID, also the stable queue key suffix.
    pub id: i64,
}

impl TaskHandler for ExchangeEffectJob {
    const IDENTIFIER: &'static str = "exchange_effect";

    /// Run one effect with Graphile's retry and recovery lifecycle.
    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let exchange = context
            .get_ext::<ExchangeContext>()
            .ok_or("exchange unavailable")?;
        let database = Database::from_pool(context.pg_pool().clone());
        run_exchange_effect(&database, &exchange.operator, &exchange.outbox, self.id)
            .await
            .map_err(|error| error.to_string())
    }
}

/// A Graphile Worker job for one generation of a turn's long poll.
#[derive(Deserialize, Serialize)]
pub struct ExchangePollJob {
    /// Turn to observe.
    pub turn: Uuid,
    /// Generation fenced by the attempt row.
    pub generation: i64,
}

impl TaskHandler for ExchangePollJob {
    const IDENTIFIER: &'static str = "exchange_poll";

    /// Poll a live turn and persist its next scheduled retry on failure.
    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let exchange = context
            .get_ext::<ExchangeContext>()
            .ok_or("exchange unavailable")?;
        let database = Database::from_pool(context.pg_pool().clone());
        run_exchange_poll(
            &database,
            &exchange.operator,
            &exchange.outbox,
            self.turn,
            self.generation,
        )
        .await
        .map_err(|error| error.to_string())
    }
}

/// Execute one queued exchange effect and settle its response in a fresh transaction.
pub async fn run_exchange_effect(
    database: &Database,
    exchange: &Operator,
    outbox: &Outbox,
    effect_id: i64,
) -> Result<()> {
    let mut claim = database.begin().await?;
    let Some(job) = claim_effect(&mut claim, effect_id).await? else {
        claim.commit().await?;
        return Ok(());
    };
    claim.commit().await?;
    match job.kind {
        EffectKind::Submit => {
            if job.recovered_exhaustion {
                let mut exhausted = database.begin().await?;
                record_submit_failure(&mut exhausted, outbox, &job).await?;
                exhausted.commit().await?;
                return Ok(());
            }
            let mut due = database.begin().await?;
            let expired = expire_attempt(&mut due, outbox, job.turn, 0).await?;
            due.commit().await?;
            if expired {
                return Ok(());
            }
            let body = job.submit_body.as_deref().expect("submit body is stored");
            let observation = match exchange.submit(body).await {
                Ok(observation) => observation,
                Err(ExchangeError::Refused { status, reason })
                    if (400..500).contains(&status) && !matches!(status, 408 | 429) =>
                {
                    let mut settle = database.begin().await?;
                    settle_submit_refusal(&mut settle, outbox, &job, status, &reason).await?;
                    settle.commit().await?;
                    return Ok(());
                }
                Err(error) => {
                    let mut retry = database.begin().await?;
                    record_submit_failure(&mut retry, outbox, &job).await?;
                    retry.commit().await?;
                    tracing::warn!(%error, turn = %job.turn, "exchange submit failed");
                    return Ok(());
                }
            };
            anyhow::ensure!(observation.turn == job.turn, "exchange turn mismatch");
            let mut settle = database.begin().await?;
            if settle_submit(
                &mut settle,
                outbox,
                &job,
                serde_json::to_value(&observation)?,
                observation.raw_body.as_deref(),
            )
            .await?
            {
                record_observation(&mut settle, outbox, &observation, None).await?;
            }
            settle.commit().await?;
        }
        EffectKind::Retire => {
            let retirement = exchange.retire(job.turn).await?;
            let mut settle = database.begin().await?;
            acknowledge_retirement(&mut settle, &job, &retirement.sessions).await?;
            settle.commit().await?;
        }
    }
    Ok(())
}

/// Observe one queued turn generation and persist its successor or outage count.
pub async fn run_exchange_poll(
    database: &Database,
    exchange: &Operator,
    outbox: &Outbox,
    turn: Uuid,
    generation: i64,
) -> Result<()> {
    if !pollable_generation(database, turn, generation).await? {
        return Ok(());
    }
    let mut due = database.begin().await?;
    let expired = expire_attempt(&mut due, outbox, turn, generation).await?;
    due.commit().await?;
    if expired {
        return Ok(());
    }
    let observation = match exchange.poll(turn, Duration::from_secs(50)).await {
        Ok(observation) => observation,
        Err(error) => {
            let mut commit = database.begin().await?;
            let definitive_status = match &error {
                ExchangeError::Refused {
                    status: 404,
                    reason,
                } if reason == "unknown turn" => Some(404),
                ExchangeError::Refused {
                    status: 410,
                    reason,
                } if reason == "turn_expired" => Some(410),
                _ => None,
            };
            let outcome =
                record_poll_failure(&mut commit, outbox, turn, generation, definitive_status)
                    .await?;
            commit.commit().await?;
            if outcome.is_some() {
                tracing::warn!(%error, %turn, generation, "exchange poll failed");
            }
            return Ok(());
        }
    };
    let mut commit = database.begin().await?;
    record_observation(&mut commit, outbox, &observation, Some(generation)).await?;
    commit.commit().await?;
    Ok(())
}
