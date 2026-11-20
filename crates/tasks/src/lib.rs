//! Task event capture and exchange effect execution.

mod archive;
mod bundle;
mod dashboard;
mod edit_policy;
mod initialization;
mod intake_payload;
mod publication;
mod runner;
mod stage_label;
mod target_config;

pub use archive::{ArchiveContext, UploadArchiveJob, upload_archive};
pub use bundle::bundle_identity;
pub use dashboard::{DashboardJob, publish_dashboard};
pub use initialization::{InitializationTarget, read_initialization};
pub use intake_payload::IntakeTransport;
pub use publication::{
    PublicationContext, PublicationTarget, PublishRequestReplyJob, publish_request_reply,
};
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
    Envelope, GithubEvent, GithubIssues, IssueAction, IssueCommentPayload, KnownIssueAction,
};
use concurro_db::{Database, UnitOfWork};
use concurro_events::{Handler, Outcome};
use concurro_events_postgres::Outbox;
use concurro_exchange_v1::{ExchangeError, Operator};
use concurro_github_v1::AppClient;
use concurro_tasks_core::{
    Actor, Admission, Authority, IssueKey, Request, RequestKind, RequestStatus, SourceOrder, Stage,
    comment_revision,
};
use concurro_tasks_postgres::{
    EffectKind, acknowledge_retirement, admit_intake, applied_request, apply_request, claim_effect,
    expire_attempt, initialization_snapshot, initialize_task, latest_intake_correction,
    next_pending, pin_start_config, pollable_generation, record_comment_revision,
    record_observation, record_poll_failure, record_request, record_submit_failure, settle_submit,
    settle_submit_refusal, start_config_pin, task,
};
use graphile_worker::{IntoTaskHandlerResult, TaskHandler, WorkerContext};
use serde::{Deserialize, Serialize};
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
        let config_sha = if request.kind == RequestKind::Start && observed.writer && observed.open {
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
        let mut apply = database.begin().await?;
        let decision = apply_request(
            &mut apply,
            &context.outbox,
            request.id,
            issue,
            Authority {
                writer: observed.writer,
                issue_open: observed.open,
            },
        )
        .await?;
        if let Some((applied, outcome)) = decision {
            if applied.kind == RequestKind::Start && outcome.status == RequestStatus::Applied {
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
