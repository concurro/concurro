//! Task event capture and exchange effect execution.

pub mod api;

mod archive;
mod bundle;
mod coverage;
mod dashboard;
mod design_payload;
mod funding;
mod gates;
mod git_publication;
mod implementation_payload;
mod implementation_result;
mod initialization;
mod intake_payload;
mod label;
mod plan_payload;
mod pr_review;
mod pr_review_payload;
mod pr_review_publication;
mod pr_review_threads;
mod publication;
mod report_publication;
mod runner;
mod stage_label;
mod standalone;
mod standalone_checks;
mod standalone_payload;
mod standalone_publication;
mod target_config;
mod target_index;

pub use archive::{ArchiveContext, UploadArchiveJob, upload_archive};
pub use bundle::bundle_identity;
pub use coverage::reconcile_coverage_gap;
pub use dashboard::{DashboardJob, publish_dashboard};
pub use gates::{TaskGatesJob, run_task_gates};
pub use git_publication::{
    GitPublicationContext, GitPublicationTarget, TaskGitPublicationJob, run_task_git_publication,
};
pub use implementation_result::{TaskImplementationResultJob, run_task_implementation_result};
pub use initialization::{InitializationTarget, read_initialization, read_requirements};
pub use intake_payload::IntakeTransport;
pub use label::{LabelScanJob, scan_label_wake};
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
pub use standalone::{
    StandaloneReviewContext, StandaloneReviewDecisionJob, StandaloneReviewHandler,
    StandaloneReviewResultJob, StandaloneReviewRoundJob, StandaloneReviewScanJob,
    StandaloneReviewTarget, observe_standalone_funding_policy, run_standalone_review_decision,
    run_standalone_review_pr_state, run_standalone_review_result, run_standalone_review_round,
    scan_standalone_review_wake,
};
pub use standalone_checks::sync_standalone_review_check;
pub use standalone_publication::{
    StandaloneReviewPublishJob, publish_standalone_dispositions, publish_standalone_review,
};
pub use target_config::{TargetConfig, decode_target_config};
pub use target_index::{IndexRunHandler, IndexTarget, scan_target_index};

use std::{
    collections::{BTreeMap, HashMap, HashSet},
    fmt,
    time::Duration,
};

use anyhow::Result;
use concurro_contracts::{
    CoverageGap, Envelope, GithubEvent, GithubIssues, IssueAction, IssueCommentPayload,
    KnownIssueAction, TaskId,
};
use concurro_db::{Database, UnitOfWork};
use concurro_events::{Handler, Outcome};
use concurro_events_postgres::Outbox;
use concurro_exchange_v1::{ExchangeError, Operator};
use concurro_github_v1::{AppClient, response_status};
use concurro_tasks_core::{
    Actor, Admission, Authority, ConsentAction, FundingMode, IssueKey, LaterCommand, Provider,
    Request, RequestKind, RequestStatus, RequirementComment, SourceOrder, Stage, comment_revision,
    consent_facts, initial_sponsorship_transitions, initialize_consent, pr_comment_revision,
    python_digest, validate_design_review,
};
use concurro_tasks_postgres::{
    CommandEvidence, EffectKind, IndexRow, acknowledge_retirement,
    acknowledge_sponsorship_transition, activate_funding_policy, admit_design_batch,
    admit_design_correction, admit_implementation, admit_intake, admit_plan, applied_request,
    apply_design_result, apply_request_with_evidence, browser_retry_feedback, claim_effect,
    claim_sponsorship_transition, confirm_current_sponsorship_sources, consent_sources,
    continue_planner_answer, cutover_floor, design_batch, design_correction, design_result,
    enqueue_label_scan, expire_attempt, fail_sponsorship_transition, finish_unapplied_result,
    funding_policy_at, implementation_inputs, implementation_session_epoch,
    initialization_snapshot, initialize_task, latest_implementation_correction,
    latest_intake_correction, latest_plan_correction, latest_plan_revision, next_pending,
    open_plan_objections, pin_start_config, planner_session_epoch, pollable_generation,
    record_comment_revision, record_coverage_gap, record_initial_sponsorship, record_observation,
    record_poll_failure, record_request, record_sponsorship_transition,
    record_standalone_review_command, record_submit_failure, record_withdraw_transition,
    refuse_request, refuse_stage_preflight, reviewer_objections, reviewer_session_epoch,
    settle_submit, settle_submit_refusal, source_after_activation, start_config_pin, task,
    terminal_pr_inputs, upsert_index_event,
};
use concurro_vault_v1::MetadataClient;
use graphile_worker::{IntoTaskHandlerResult, TaskHandler, WorkerContext};
use reqwest::StatusCode;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use uuid::Uuid;

/// Persist a recovery interval before acknowledging its ingress event.
#[derive(Clone)]
pub struct CoverageGapHandler;

impl Handler<CoverageGap> for CoverageGapHandler {
    /// Queue the interval for restartable GitHub source reconciliation.
    async fn handle(&self, work: &mut UnitOfWork<'_>, event: Envelope<CoverageGap>) -> Outcome {
        match record_coverage_gap(
            work,
            event.id.0,
            event.data.from.0,
            event.data.to.0,
            event.data.installation_id,
        )
        .await
        {
            Ok(()) => Outcome::Done,
            Err(error) => Outcome::Retry(error.to_string()),
        }
    }
}

/// Capture configured target comments and issue closures.
#[derive(Clone)]
pub struct CommentHandler {
    targets: HashSet<i64>,
    standalone_targets: HashSet<i64>,
    owned_accounts: HashMap<i64, HashSet<String>>,
    outbox: Outbox,
}

impl CommentHandler {
    /// Bind the channel to the task application's configured repository IDs.
    pub fn new(targets: impl IntoIterator<Item = i64>, outbox: Outbox) -> Self {
        Self {
            targets: targets.into_iter().collect(),
            standalone_targets: HashSet::new(),
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

    /// Route configured upstream PR comments through the standalone command ledger.
    pub fn with_standalone_targets(mut self, targets: impl IntoIterator<Item = i64>) -> Self {
        self.standalone_targets = targets.into_iter().collect();
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
        let is_pr = event.data.payload.issue.is_pull_request;
        if !(if is_pr {
            self.standalone_targets.contains(&event.data.repository.id)
        } else {
            self.targets.contains(&event.data.repository.id)
        }) {
            return Outcome::Done;
        }
        let Some(mut revision) = (if is_pr {
            pr_comment_revision(&event)
        } else {
            comment_revision(&event)
        }) else {
            return Outcome::Done;
        };
        match source_after_activation(uow, event.data.repository.id, revision.order.at).await {
            Ok(true) => {}
            Ok(false) => return Outcome::Done,
            Err(error) => return Outcome::Retry(error.to_string()),
        }
        if !is_pr {
            let issue = &event.data.payload.issue;
            let row = IndexRow {
                repository_id: event.data.repository.id,
                kind: "issue",
                item_id: issue.number.to_string(),
                source_at: issue.updated_at.0,
                body: json!({"repository":event.data.repository.full_name,
                    "number":issue.number,"title":issue.title,"state":issue.state,
                    "labels":issue.labels,"updated_at":issue.updated_at,
                    "user":issue.user}),
            };
            if let Err(error) = upsert_index_event(uow, &row).await {
                return Outcome::Retry(error.to_string());
            }
        }
        if event.data.action.as_deref() != Some("deleted")
            && self
                .owned_accounts
                .get(&event.data.repository.id)
                .is_some_and(|names| {
                    names.contains(&event.data.payload.comment.user.login.to_ascii_lowercase())
                        || names.contains(&event.data.sender.login.to_ascii_lowercase())
                })
        {
            if is_pr {
                return Outcome::Done;
            }
            revision.request = None;
        }
        let recorded = if is_pr {
            let actor = if event.data.action.as_deref() == Some("created") {
                &event.data.payload.comment.user
            } else {
                &event.data.sender
            };
            record_standalone_review_command(
                uow,
                &revision,
                &event.data.repository.full_name,
                &Actor {
                    id: actor.id,
                    login: actor.login.clone(),
                },
                event.data.payload.comment.user.id,
                event.data.payload.comment.body.as_deref().unwrap_or(""),
            )
            .await
        } else {
            record_comment_revision(uow, &self.outbox, &revision).await
        };
        match recorded {
            Ok(()) => Outcome::Done,
            Err(error) => Outcome::Retry(error.to_string()),
        }
    }
}

impl Handler<GithubIssues> for CommentHandler {
    /// Record a closed issue as a pending stop under its verified source identity.
    async fn handle(&self, uow: &mut UnitOfWork<'_>, event: Envelope<GithubIssues>) -> Outcome {
        let issue = &event.data.issue;
        if !self.targets.contains(&event.data.repository.id) || issue.is_pull_request {
            return Outcome::Done;
        }
        match source_after_activation(uow, event.data.repository.id, issue.updated_at.0).await {
            Ok(true) => {}
            Ok(false) => return Outcome::Done,
            Err(error) => return Outcome::Retry(error.to_string()),
        }
        let row = IndexRow {
            repository_id: event.data.repository.id,
            kind: "issue",
            item_id: issue.number.to_string(),
            source_at: issue.updated_at.0,
            body: json!({"repository":event.data.repository.full_name,
                "number":issue.number,"title":issue.title,"state":issue.state,
                "labels":issue.labels,"updated_at":issue.updated_at,
                "user":issue.user}),
        };
        if let Err(error) = upsert_index_event(uow, &row).await {
            return Outcome::Retry(error.to_string());
        }
        let label_action = match &event.data.action {
            IssueAction::Known(KnownIssueAction::Labeled { label }) if label.name == "concurro" => {
                Some("labeled")
            }
            IssueAction::Known(KnownIssueAction::Unlabeled { label })
                if label.name == "concurro" =>
            {
                Some("unlabeled")
            }
            _ => None,
        };
        if let Some(action) = label_action {
            let result = enqueue_label_scan(
                uow,
                IssueKey {
                    repository_id: event.data.repository.id,
                    number: issue.number,
                },
                &event.data.delivery_id,
                action,
                event.data.sender.id,
                issue.updated_at.0,
            )
            .await;
            return match result {
                Ok(()) => Outcome::Done,
                Err(error) => Outcome::Retry(error.to_string()),
            };
        }
        if !matches!(
            event.data.action,
            IssueAction::Known(KnownIssueAction::Closed)
        ) {
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
    /// Metadata-only Vault reader for first-boundary credential availability.
    pub vault: Option<MetadataClient>,
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
        let actor_login = if consent_facts(&request).is_empty() && request.channel != "label" {
            Some(request.actor.login.clone())
        } else {
            match context
                .github
                .public_user_id(
                    &target.repository,
                    &target.read_permissions,
                    &request.actor.login,
                )
                .await
            {
                Ok(id) if id == request.actor.id => Some(request.actor.login.clone()),
                Ok(_) => {
                    context
                        .github
                        .public_user_login_by_id(
                            &target.repository,
                            &target.read_permissions,
                            request.actor.id,
                        )
                        .await?
                }
                Err(error) if response_status(&error) == Some(StatusCode::NOT_FOUND) => {
                    context
                        .github
                        .public_user_login_by_id(
                            &target.repository,
                            &target.read_permissions,
                            request.actor.id,
                        )
                        .await?
                }
                Err(error) => return Err(error),
            }
        };
        let Some(actor_login) = actor_login else {
            let mut refused = database.begin().await?;
            refuse_request(
                &mut refused,
                &context.outbox,
                request.id,
                issue,
                "source_actor_unverified",
            )
            .await?;
            refused.commit().await?;
            continue;
        };
        if request.channel == "label"
            && request.kind == RequestKind::Start
            && !label::current_label_source(
                &context.github,
                &target.repository,
                &target.read_permissions,
                &request,
            )
            .await?
        {
            let mut refused = database.begin().await?;
            refuse_request(
                &mut refused,
                &context.outbox,
                request.id,
                issue,
                "label_not_current",
            )
            .await?;
            refused.commit().await?;
            continue;
        }
        let observed = if request.channel == "issue_close" {
            let open = context
                .github
                .issue_open(&target.repository, issue.number, &target.read_permissions)
                .await?;
            concurro_github_v1::IssueAuthority { open, writer: true }
        } else if request.channel == "label" {
            let source = target
                .initialization
                .as_ref()
                .ok_or_else(|| anyhow::anyhow!("label source target unavailable"))?;
            let open = context
                .github
                .issue_open(&target.repository, issue.number, &target.read_permissions)
                .await?;
            let writer = context
                .github
                .writer_permission(&source.upstream, &actor_login, &source.upstream_permissions)
                .await?;
            concurro_github_v1::IssueAuthority { open, writer }
        } else {
            context
                .github
                .issue_authority(
                    &target.repository,
                    issue.number,
                    &actor_login,
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
        if let Some(sha) = config_sha
            .as_deref()
            .filter(|_| target.initialization.is_some())
        {
            observe_target_funding_policy(database, context, issue.repository_id, sha).await?;
        }
        if request.channel == "label"
            && request.kind == RequestKind::Start
            && observed.writer
            && observed.open
        {
            let source = target
                .initialization
                .as_ref()
                .ok_or_else(|| anyhow::anyhow!("label source target unavailable"))?;
            let config = initialization::read_target_config(
                &context.github,
                source,
                config_sha
                    .as_deref()
                    .expect("authorized label has fork head"),
            )
            .await?;
            if config.raw["autonomous"] != true {
                let mut refused = database.begin().await?;
                refuse_request(
                    &mut refused,
                    &context.outbox,
                    request.id,
                    issue,
                    "autonomous_disabled",
                )
                .await?;
                refused.commit().await?;
                continue;
            }
        }
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
            let mut snapshot = initialization::read_initialization(
                &context.github,
                source,
                &request,
                config_sha
                    .as_deref()
                    .expect("recovery source has fork head"),
            )
            .await?;
            if request.channel == "browser"
                && matches!(request.kind, RequestKind::Later(LaterCommand::Retry { .. }))
                && let Some(feedback) = browser_retry_feedback(database, request.id).await?
            {
                snapshot.requirements.discussion.push(RequirementComment {
                    author: request.actor.clone(),
                    body: feedback,
                });
                if snapshot.requirements.discussion.len() > 20 {
                    snapshot.requirements.discussion.remove(0);
                }
            }
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

/// Observe one target's current-head funding policy without backdating source time.
pub async fn observe_target_funding_policy(
    database: &Database,
    context: &DecisionContext,
    repository_id: i64,
    config_sha: &str,
) -> Result<()> {
    let target = context
        .targets
        .get(&repository_id)
        .and_then(|target| target.initialization.as_ref())
        .ok_or_else(|| anyhow::anyhow!("funding policy source target unavailable"))?;
    observe_source_funding_policy(database, &context.github, target, repository_id, config_sha)
        .await
}

/// Record the current funding policy of one configured source target.
pub(crate) async fn observe_source_funding_policy(
    database: &Database,
    github: &AppClient,
    target: &InitializationTarget,
    repository_id: i64,
    config_sha: &str,
) -> Result<()> {
    let config = initialization::read_target_config(github, target, config_sha).await?;
    let mode = match config.raw.get("funding_mode").and_then(Value::as_str) {
        None | Some("org") => FundingMode::Org,
        Some("contributed") => FundingMode::Contributed,
        Some(_) => anyhow::bail!("target funding_mode is invalid"),
    };
    let digest = python_digest(&config.raw)?;
    let mut activation = database.begin().await?;
    activate_funding_policy(&mut activation, repository_id, config_sha, &digest, mode).await?;
    activation.commit().await?;
    Ok(())
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
    let mut read = database.begin().await?;
    let observed = consent_sources(&mut read, issue).await?;
    read.commit().await?;
    let snapshot = read_initialization(&context.github, target, &request, &config_sha).await?;
    let default_funding = match snapshot
        .configuration
        .get("funding_mode")
        .and_then(Value::as_str)
    {
        None | Some("org") => FundingMode::Org,
        Some("contributed") => FundingMode::Contributed,
        Some(_) => anyhow::bail!("target funding_mode is invalid"),
    };
    let config_digest = python_digest(&snapshot.configuration)?;
    let source_funding = funding_policy_at(
        database,
        issue.repository_id,
        &config_digest,
        request.order.at,
    )
    .await?;
    let visible =
        initialization::read_current_sponsorship_sources(&context.github, target, &request).await?;
    let available = if let Some(vault) = &context.vault {
        let at = request.order.at.to_rfc3339();
        let metadata = vault.read(request.actor.id, Some(&at)).await?;
        funding::boundary_eligibility(
            &snapshot.configuration,
            &metadata,
            &target.upstream,
            request.order.at,
        )?
    } else {
        anyhow::ensure!(
            default_funding == FundingMode::Org,
            "contributed funding requires the Vault metadata reader"
        );
        BTreeMap::from([
            (Provider::Openai, Some(false)),
            (Provider::Anthropic, Some(false)),
        ])
    };
    let mut apply = database.begin().await?;
    let sources = consent_sources(&mut apply, issue).await?;
    anyhow::ensure!(
        sources.revision == observed.revision,
        "sponsorship source changed during boundary read"
    );
    anyhow::ensure!(
        confirm_current_sponsorship_sources(&mut apply, issue, &request.order, &visible).await?,
        "sponsorship source history changed or remains undecided"
    );
    let initial = initialize_consent(&sources.facts, &request.order, &request.actor, &available)
        .ok_or_else(|| anyhow::anyhow!("Vault history does not cover the funding boundary"))?;
    let mut later = sources
        .facts
        .iter()
        .filter(|fact| fact.order > request.order)
        .collect::<Vec<_>>();
    later.sort_by(|left, right| left.order.cmp(&right.order));
    let mut unresolved = later
        .iter()
        .filter(|fact| !matches!(fact.action, ConsentAction::Withdraw { .. }))
        .map(|fact| fact.provider)
        .collect::<Vec<_>>();
    unresolved.sort();
    unresolved.dedup();
    if let Some(initialized) =
        initialize_task(&mut apply, &context.outbox, issue, revision, &snapshot).await?
    {
        let first = record_initial_sponsorship(
            &mut apply,
            &initialized,
            request_id,
            &request.order,
            &initial,
            &available,
            sources.revision,
            &unresolved,
            &config_digest,
            default_funding,
            source_funding,
        )
        .await?;
        let floor = cutover_floor(&mut apply, issue.repository_id, issue.number).await?;
        if first
            && (default_funding == FundingMode::Contributed
                || !sources.facts.is_empty()
                || floor.is_some())
        {
            let epochs = floor.map(|floor| {
                BTreeMap::from([
                    (Provider::Openai, floor.openai_epoch),
                    (Provider::Anthropic, floor.anthropic_epoch),
                ])
            });
            let transitions = initial_sponsorship_transitions(
                &initial,
                &target.issue_repository,
                issue,
                &request.actor,
                floor.map_or(1, |floor| floor.generation + 1),
                epochs.as_ref(),
                default_funding,
                source_funding,
                &request.channel,
            )?;
            for transition in transitions {
                record_sponsorship_transition(&mut apply, initialized.id.0, &transition).await?;
            }
            for fact in later {
                if matches!(fact.action, ConsentAction::Withdraw { .. }) {
                    record_withdraw_transition(&mut apply, initialized.id.0, fact).await?;
                }
            }
        }
    }
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
    let mut attempt = intake_payload::prepare_intake_attempt(
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
    if !funding::fund_or_block_attempt(
        database,
        &context.initialization,
        &current,
        &snapshot,
        target,
        current.used_model_calls + 1,
        &mut attempt,
    )
    .await?
    {
        return Ok(());
    }
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
    let mut attempt = plan_payload::prepare_plan_attempt(
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
    if !funding::fund_or_block_attempt(
        database,
        &context.initialization,
        &current,
        &snapshot,
        target,
        current.used_model_calls + 1,
        &mut attempt,
    )
    .await?
    {
        return Ok(());
    }
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
    let mut attempt = implementation_payload::prepare_implementation_attempt(
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
    if !funding::fund_or_block_attempt(
        database,
        &context.initialization,
        &current,
        &snapshot,
        target,
        current.used_model_calls + 1,
        &mut attempt,
    )
    .await?
    {
        return Ok(());
    }
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
        let mut spec = design_payload::prepare_design_attempt(
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
        if !funding::fund_or_block_attempt(
            database,
            &context.initialization,
            &current,
            &snapshot,
            target,
            current.used_model_calls + specs.len() as u64 + 1,
            &mut spec,
        )
        .await?
        {
            return Ok(());
        }
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
    let mut attempt = design_payload::prepare_design_attempt(
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
    if !funding::fund_or_block_attempt(
        database,
        &context.initialization,
        &current,
        &snapshot,
        target,
        current.used_model_calls + 1,
        &mut attempt,
    )
    .await?
    {
        return Ok(());
    }
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

/// A durable provider-slot write to the Python Exchange.
#[derive(Deserialize, Serialize)]
pub struct SponsorshipTransitionJob {
    /// Stable transition identity whose exact body is in PostgreSQL.
    pub transition: String,
}

impl TaskHandler for SponsorshipTransitionJob {
    const IDENTIFIER: &'static str = "sponsorship_transition";

    /// Replay one claimed transition and persist its verified acknowledgement.
    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let exchange = context
            .get_ext::<ExchangeContext>()
            .ok_or("exchange unavailable")?;
        let database = Database::from_pool(context.pg_pool().clone());
        run_sponsorship_transition(&database, &exchange.operator, &self.transition)
            .await
            .map_err(|error| error.to_string())
    }
}

/// Send one persisted provider transition with fenced acknowledgement and retry.
pub async fn run_sponsorship_transition(
    database: &Database,
    exchange: &Operator,
    transition: &str,
) -> Result<()> {
    let mut claim = database.begin().await?;
    let write = claim_sponsorship_transition(&mut claim, transition).await?;
    claim.commit().await?;
    let Some(write) = write else {
        return Ok(());
    };
    match exchange.sponsorship(&write.body).await {
        Ok(ack) => {
            let mut settle = database.begin().await?;
            acknowledge_sponsorship_transition(&mut settle, &write, &ack).await?;
            settle.commit().await?;
            Ok(())
        }
        Err(
            error @ ExchangeError::Refused {
                status: 408 | 429 | 500..=599,
                ..
            },
        ) => Err(error.into()),
        Err(ExchangeError::Refused { status, .. }) => {
            let mut settle = database.begin().await?;
            fail_sponsorship_transition(&mut settle, &write, &format!("exchange_refused_{status}"))
                .await?;
            settle.commit().await?;
            Ok(())
        }
        Err(ExchangeError::StaleSponsorshipGeneration { .. }) => {
            let mut settle = database.begin().await?;
            fail_sponsorship_transition(&mut settle, &write, "stale_generation").await?;
            settle.commit().await?;
            Ok(())
        }
        Err(error) => Err(error.into()),
    }
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
