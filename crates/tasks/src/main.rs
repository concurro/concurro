//! Task application runtime for durable GitHub intake and exchange turns.

use std::{
    collections::{HashMap, HashSet},
    fs,
    path::Path,
    sync::Arc,
    time::Duration,
};

use anyhow::{Context, Result, ensure};
use async_nats::jetstream::{self, stream};
use concurro_config::{Validate, load, read_secret};
use concurro_contracts::{
    ConfigLauncher, ConfigTarget, ConfigTargets, Contract, CoverageGap, GithubEvent, GithubIssues,
    IssueCommentPayload, PullRequestPayload, WorkflowRunPayload,
};
use concurro_db::Database;
use concurro_events::Handler;
use concurro_events_nats::{
    ConsumerStream, Forwarder, connect as connect_nats, ensure_pull_consumer, ensure_stream,
    run_consumer,
};
use concurro_events_postgres::{
    ForwarderLease, Outbox, consumer_status, migrate as migrate_events,
};
use concurro_exchange_v1::Operator;
use concurro_github_v1::{AppClient, user_grant::UserGrant};
use concurro_tasks::api::{TaskApi, router as task_api_router};
use concurro_tasks::{
    ArchiveContext, CommentHandler, CoverageGapHandler, DashboardJob, DecisionContext,
    DecisionTarget, ExchangeContext, ExchangeEffectJob, ExchangePollJob, GitPublicationContext,
    GitPublicationTarget, IndexRunHandler, IndexTarget, InitializationContext,
    InitializationTarget, IntakeContext, IntakeTransport, LabelScanJob, PublicationContext,
    PublicationTarget, PublishRequestReplyJob, PublishTaskReportJob, RunnerContext,
    RunnerSessionJob, RunnerTarget, SponsorshipTransitionJob, StageLabelJob,
    StandaloneReviewContext, StandaloneReviewDecisionJob, StandaloneReviewHandler,
    StandaloneReviewPublishJob, StandaloneReviewResultJob, StandaloneReviewRoundJob,
    StandaloneReviewScanJob, StandaloneReviewTarget, TaskClarificationJob, TaskDecisionJob,
    TaskDesignBatchJob, TaskDesignCorrectionJob, TaskDesignResultJob, TaskGatesJob,
    TaskGitPublicationJob, TaskImplementJob, TaskImplementationResultJob, TaskInitializeJob,
    TaskIntakeJob, TaskPlanJob, TaskPrReviewBatchJob, TaskPrReviewCorrectionJob,
    TaskPrReviewPublishJob, TaskPrReviewResultJob, UploadArchiveJob,
    observe_standalone_funding_policy, observe_target_funding_policy, publish_pr_review,
    publish_request_reply, publish_standalone_dispositions, publish_standalone_review,
    publish_task_report, reconcile_coverage_gap, run_standalone_review_decision,
    run_standalone_review_pr_state, run_standalone_review_result, run_standalone_review_round,
    run_task_decision, run_task_gates, scan_label_wake, scan_standalone_review_wake,
    scan_target_index, sync_standalone_review_check, upload_archive,
};
use concurro_tasks_core::IssueKey;
use concurro_tasks_postgres::{
    CommentResolution, cancel_attempt, due_attempt_deadlines, due_comment_readbacks,
    enqueue_dashboard, enqueue_runner_session, enqueue_stage_label, expire_attempt,
    fail_coverage_gap, import_cutover_manifest, import_historical_display, import_python_manifest,
    list_tasks, pending_coverage_gaps, pending_effects, pending_issues, pending_label_wakes,
    pending_pr_review_readbacks, pending_python_import_archives, pending_report_artifact_readbacks,
    pending_standalone_dispositions, pending_standalone_review_commands,
    pending_standalone_review_issues, pending_standalone_review_readbacks,
    pending_standalone_review_results, pending_standalone_review_rounds,
    pending_standalone_review_wakes, recover_retirements, resolve_comment, resolve_pending_write,
    resume_standalone_review_credentials, sync_config_targets, task_detail, watched_dashboards,
    watched_gate_issues, watched_runner_sessions, watched_stage_labels,
    watched_standalone_review_prs,
};
use concurro_vault_v1::MetadataClient;
use graphile_worker::WorkerOptions;
use object_store::aws::AmazonS3Builder;
use serde::{Deserialize, de::DeserializeOwned};
use uuid::Uuid;

/// One configured issue repository and its pinned source target.
#[derive(Clone, Deserialize)]
struct TargetSettings {
    /// Numeric GitHub repository identity used to route events.
    repository_id: i64,
    /// Repositories and permission sets for source and runner reads.
    #[serde(flatten)]
    target: InitializationTarget,
    /// Exact installation permissions requested for issue comment writes.
    issue_write_permissions: std::collections::BTreeMap<String, String>,
    /// Exact installation permissions requested for fork ref and dispatch writes.
    fork_write_permissions: std::collections::BTreeMap<String, String>,
    /// Exact installation permissions requested for upstream PR writes.
    #[serde(default)]
    upstream_write_permissions: std::collections::BTreeMap<String, String>,
    /// Numeric reviewer account used for standalone review requests, when enabled.
    reviewer_user_id: Option<i64>,
    /// Owner-only user-to-server grant for that reviewer account.
    reviewer_grant_file: Option<String>,
    /// Upstream repository ID when it differs from the issue repository ID.
    upstream_repository_id: Option<i64>,
}

/// Private object-store credentials read from a host file.
#[derive(Deserialize)]
struct ArchiveCredentials {
    /// S3 access key ID.
    access_key_id: String,
    /// S3 secret access key.
    secret_access_key: String,
}

/// Local service settings and references to credential files.
#[derive(Clone, Deserialize)]
struct Settings {
    /// PostgreSQL URL stored outside the source tree.
    database_url_file: String,
    /// NATS endpoint hosting ingress and task streams.
    nats_url: String,
    /// Optional private NATS user credential file.
    nats_credentials_file: Option<String>,
    /// GitHub API endpoint, including enterprise hosts when configured.
    github_api_base: String,
    /// Numeric GitHub App identity.
    github_app_id: i64,
    /// GitHub App PEM key path.
    github_private_key_file: String,
    /// OAuth endpoint used to refresh reviewer-account grants.
    reviewer_oauth_base: Option<String>,
    /// Service App OAuth client ID for reviewer-account grants.
    reviewer_oauth_client_id: Option<String>,
    /// Host file containing the App OAuth client secret.
    reviewer_oauth_client_secret_file: Option<String>,
    /// Python exchange operator endpoint.
    exchange_url: String,
    /// Python exchange operator bearer file.
    exchange_token_file: String,
    /// Optional metadata-only Vault endpoint for personal funding.
    vault_metadata_url: Option<String>,
    /// Host file containing only the Vault metadata bearer.
    vault_metadata_token_file: Option<String>,
    /// S3-compatible endpoint for private original transcript archives.
    archive_endpoint: String,
    /// Private bucket containing original transcript archives.
    archive_bucket: String,
    /// Object key prefix within the archive bucket.
    archive_prefix: String,
    /// Signing region accepted by the configured object store.
    archive_region: String,
    /// Host file containing JSON object-store credentials.
    archive_credentials_file: String,
    /// Runner and inference settings used in the exact stored body.
    intake: IntakeTransport,
    /// Configured target repositories.
    targets: Vec<TargetSettings>,
    /// Maximum event size accepted to the task outbox.
    max_message_bytes: usize,
    /// Published task event retention period.
    published_retention_seconds: u64,
    /// Idle outbox poll interval.
    forwarder_poll_ms: u64,
    /// Durable ingress consumer name.
    consumer_name: String,
    /// Maximum failed applications before an event is parked.
    consumer_max_attempts: i32,
    /// Released-event poll interval.
    consumer_poll_ms: u64,
    /// Optional private listener for signed browser requests and read APIs.
    api_listen: Option<String>,
    /// PEM public key matching the Python backoffice's command signer.
    command_proof_public_key_file: Option<String>,
    /// Read-scoped internal bearer for task API reads.
    api_read_bearer_file: Option<String>,
    /// Ingress-only App delivery recovery bearer.
    delivery_recovery_bearer_file: Option<String>,
}

impl Validate for Settings {
    /// Refuse settings that cannot identify or persist a task source.
    fn validate(&self) -> Result<()> {
        ensure!(!self.targets.is_empty(), "task targets are empty");
        ensure!(
            self.targets
                .iter()
                .map(|item| item.repository_id)
                .collect::<HashSet<_>>()
                .len()
                == self.targets.len(),
            "task repository IDs are duplicated"
        );
        let review_targets = self
            .targets
            .iter()
            .filter_map(|item| {
                item.reviewer_user_id.map(|reviewer| {
                    (
                        item.upstream_repository_id.unwrap_or(item.repository_id),
                        reviewer,
                    )
                })
            })
            .collect::<Vec<_>>();
        ensure!(
            review_targets
                .iter()
                .all(|(upstream, reviewer)| *upstream > 0 && *reviewer > 0)
                && review_targets
                    .iter()
                    .map(|(upstream, _)| upstream)
                    .collect::<HashSet<_>>()
                    .len()
                    == review_targets.len(),
            "standalone review upstreams or reviewer identities are invalid"
        );
        ensure!(self.max_message_bytes > 0, "task event limit is empty");
        ensure!(
            self.forwarder_poll_ms > 0 && self.consumer_poll_ms > 0,
            "task polling interval is empty"
        );
        ensure!(
            self.consumer_max_attempts > 0,
            "task consumer attempt limit is empty"
        );
        ensure!(
            !self.consumer_name.is_empty(),
            "task consumer name is empty"
        );
        ensure!(
            !self.archive_endpoint.is_empty()
                && !self.archive_bucket.is_empty()
                && !self.archive_prefix.is_empty()
                && !self.archive_region.is_empty(),
            "archive storage settings are incomplete"
        );
        ensure!(
            self.api_listen.is_none() == self.command_proof_public_key_file.is_none()
                && self.api_listen.is_none() == self.api_read_bearer_file.is_none(),
            "task API listener, proof key and read bearer must be configured together"
        );
        Ok(())
    }
}

/// Reconnect the task event producer and advance only acknowledged outbox rows.
async fn run_forwarder(settings: Settings, database: Database) {
    loop {
        let result: Result<()> = async {
            let client = connect_nats(
                &settings.nats_url,
                settings.nats_credentials_file.as_deref(),
            )
            .await?;
            let context = jetstream::new(client.clone());
            ensure_stream(
                &context,
                stream::Config {
                    name: "TASKS".to_owned(),
                    subjects: vec![
                        "task.>".to_owned(),
                        "request.>".to_owned(),
                        "turn.>".to_owned(),
                        "config.>".to_owned(),
                        "view.>".to_owned(),
                    ],
                    ..Default::default()
                },
            )
            .await?;
            let Some(mut lease) = ForwarderLease::try_acquire(&database).await? else {
                tokio::time::sleep(Duration::from_secs(1)).await;
                return Ok(());
            };
            Forwarder::new(client, Duration::from_millis(settings.forwarder_poll_ms))
                .with_journal_stream("TASKS")
                .run(
                    &database,
                    &mut lease,
                    Duration::from_secs(settings.published_retention_seconds),
                    settings.max_message_bytes,
                )
                .await
        }
        .await;
        if let Err(error) = result {
            tracing::warn!(%error, "task event forwarder disconnected");
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
}

/// Continue readback of uncertain comment creates after a job retry budget is spent.
async fn run_reply_readbacks(database: Database, publication: PublicationContext) {
    loop {
        match due_comment_readbacks(&database).await {
            Ok(ids) => {
                for id in ids {
                    if let Err(error) = publish_request_reply(&database, &publication, id).await {
                        tracing::warn!(%error, %id, "request reply readback pending");
                    }
                }
            }
            Err(error) => tracing::warn!(%error, "request reply readback scan failed"),
        }
        tokio::time::sleep(Duration::from_secs(30)).await;
    }
}

/// Keep uncertain report creates visible and reconcilable after worker retries end.
async fn run_report_readbacks(database: Database, publication: PublicationContext) {
    loop {
        match pending_report_artifact_readbacks(&database).await {
            Ok(ids) => {
                for id in ids {
                    if let Err(error) = publish_task_report(&database, &publication, id).await {
                        tracing::warn!(%error, %id, "report artifact readback pending");
                    }
                }
            }
            Err(error) => tracing::warn!(%error, "report artifact readback scan failed"),
        }
        tokio::time::sleep(Duration::from_secs(30)).await;
    }
}

/// Continue exact PR review readback even after a worker retry limit is spent.
async fn run_pr_review_readbacks(database: Database, publication: GitPublicationContext) {
    loop {
        match pending_pr_review_readbacks(&database).await {
            Ok(turns) => {
                for turn in turns {
                    if let Err(error) = publish_pr_review(&database, &publication, turn).await {
                        tracing::warn!(%error, %turn, "PR review readback pending");
                    }
                }
            }
            Err(error) => tracing::warn!(%error, "pending PR review scan failed"),
        }
        tokio::time::sleep(Duration::from_secs(30)).await;
    }
}

/// Reobserve published PRs for closure and gated PRs for CI changes.
async fn run_gate_recovery(database: Database, publication: GitPublicationContext) {
    loop {
        match watched_gate_issues(&database).await {
            Ok(issues) => {
                for issue in issues {
                    if let Err(error) = run_task_gates(&database, &publication, issue).await {
                        tracing::warn!(%error, ?issue, "PR gate observation pending");
                    }
                }
            }
            Err(error) => tracing::warn!(%error, "PR gate recovery scan failed"),
        }
        tokio::time::sleep(Duration::from_secs(60)).await;
    }
}

/// Retry a coverage scan until every changed source and active task re-read succeeds.
async fn run_gap_recovery(
    database: Database,
    outbox: Outbox,
    decisions: DecisionContext,
    standalone: StandaloneReviewContext,
    publication: GitPublicationContext,
    comments: CommentHandler,
    index_targets: Vec<IndexTarget>,
) {
    loop {
        match pending_coverage_gaps(&database).await {
            Ok(gaps) => {
                for gap in gaps {
                    if let Err(error) = reconcile_coverage_gap(
                        &database,
                        &outbox,
                        &decisions,
                        &standalone,
                        &publication,
                        &comments,
                        &index_targets,
                        &gap,
                    )
                    .await
                    {
                        tracing::warn!(%error, id = %gap.event_id, "coverage gap remains pending");
                        if let Err(record_error) =
                            fail_coverage_gap(&database, gap.event_id, &error.to_string()).await
                        {
                            tracing::warn!(%record_error, id = %gap.event_id, "coverage failure record unavailable");
                        }
                    }
                }
            }
            Err(error) => tracing::warn!(%error, "coverage gap scan unavailable"),
        }
        tokio::time::sleep(Duration::from_secs(30)).await;
    }
}

/// Reauthorize pending issues even if their original decision job exhausted retries.
async fn run_decision_recovery(database: Database, decisions: DecisionContext) {
    loop {
        match pending_issues(&database).await {
            Ok(issues) => {
                for issue in issues {
                    if let Err(error) = run_task_decision(&database, &decisions, issue).await {
                        tracing::warn!(%error, ?issue, "task decision remains pending");
                    }
                }
            }
            Err(error) => tracing::warn!(%error, "pending task decision scan failed"),
        }
        tokio::time::sleep(Duration::from_secs(30)).await;
    }
}

/// Record the funding policy currently served by each configured fork head.
async fn observe_funding_policies(
    database: &Database,
    decisions: &DecisionContext,
    standalone: &StandaloneReviewContext,
) {
    for (repository_id, target) in &decisions.targets {
        let result: Result<()> = async {
            let (_, sha) = decisions
                .github
                .default_head(&target.fork, &target.fork_permissions)
                .await?;
            observe_target_funding_policy(database, decisions, *repository_id, &sha).await
        }
        .await;
        if let Err(error) = result {
            tracing::warn!(%error, %repository_id, "funding policy observation pending");
        }
    }
    for (repository_id, target) in &standalone.targets {
        let result: Result<()> = async {
            let (_, sha) = standalone
                .github
                .default_head(&target.source.fork, &target.source.fork_permissions)
                .await?;
            observe_standalone_funding_policy(database, standalone, *repository_id, &sha).await
        }
        .await;
        if let Err(error) = result {
            tracing::warn!(%error, %repository_id, "standalone funding policy observation pending");
        }
    }
}

/// Refresh current-head funding policies independently of issue commands.
async fn run_funding_policy_observer(
    database: Database,
    decisions: DecisionContext,
    standalone: StandaloneReviewContext,
) {
    loop {
        tokio::time::sleep(Duration::from_secs(60)).await;
        observe_funding_policies(&database, &decisions, &standalone).await;
    }
}

/// Expire live turns even when their submit or poll worker job was exhausted.
async fn run_deadline_recovery(database: Database, outbox: Outbox) {
    loop {
        match due_attempt_deadlines(&database).await {
            Ok(attempts) => {
                for (turn, generation) in attempts {
                    let result: Result<()> = async {
                        let mut work = database.begin().await?;
                        expire_attempt(&mut work, &outbox, turn, generation).await?;
                        work.commit().await?;
                        Ok(())
                    }
                    .await;
                    if let Err(error) = result {
                        tracing::warn!(%error, %turn, "attempt deadline recovery failed");
                    }
                }
            }
            Err(error) => tracing::warn!(%error, "attempt deadline scan failed"),
        }
        tokio::time::sleep(Duration::from_secs(15)).await;
    }
}

/// Restore retirement jobs whose worker retries ended without an acknowledgement.
async fn run_retirement_recovery(database: Database) {
    loop {
        if let Err(error) = recover_retirements(&database).await {
            tracing::warn!(%error, "retirement recovery failed");
        }
        tokio::time::sleep(Duration::from_secs(30)).await;
    }
}

/// Keep uncertain managed labels under readback across job exhaustion and restarts.
async fn run_stage_label_recovery(database: Database) {
    loop {
        match watched_stage_labels(&database).await {
            Ok(tasks) => {
                for task_id in tasks {
                    let result: Result<()> = async {
                        let mut work = database.begin().await?;
                        enqueue_stage_label(&mut work, task_id, "scan").await?;
                        work.commit().await?;
                        Ok(())
                    }
                    .await;
                    if let Err(error) = result {
                        tracing::warn!(%error, %task_id, "stage label recovery failed");
                    }
                }
            }
            Err(error) => tracing::warn!(%error, "stage label watch scan failed"),
        }
        tokio::time::sleep(Duration::from_secs(30)).await;
    }
}

/// Continue timeline readback for label deliveries after Graphile retries end.
async fn run_label_recovery(database: Database, decisions: DecisionContext) {
    loop {
        match pending_label_wakes(&database).await {
            Ok(wakes) => {
                for wake in wakes {
                    let job = LabelScanJob {
                        delivery_id: wake.delivery_id,
                        repository_id: wake.repository_id,
                        issue_number: wake.issue_number,
                        action: wake.action,
                        actor_id: wake.actor_id,
                        updated_at: wake.updated_at,
                    };
                    if let Err(error) = scan_label_wake(&database, &decisions, &job).await
                        && error.to_string() != "label delivery event not in issue timeline yet"
                    {
                        tracing::warn!(%error, issue = job.issue_number, "label timeline readback failed");
                    }
                }
            }
            Err(error) => tracing::warn!(%error, "pending label wake scan failed"),
        }
        tokio::time::sleep(Duration::from_secs(30)).await;
    }
}

/// Continue review timeline and decision readback after worker retries end.
async fn run_standalone_review_recovery(
    database: Database,
    context: StandaloneReviewContext,
    intake: IntakeContext,
) {
    loop {
        match watched_standalone_review_prs(&database).await {
            Ok(prs) => {
                for (task_id, observe_pr) in prs {
                    if observe_pr {
                        if let Err(error) =
                            run_standalone_review_pr_state(&database, &context, task_id).await
                        {
                            tracing::warn!(%error, %task_id, "standalone PR state observation pending");
                        }
                    }
                    if let Err(error) =
                        sync_standalone_review_check(&database, &context, task_id).await
                    {
                        tracing::warn!(%error, %task_id, "standalone activity check pending");
                    }
                }
            }
            Err(error) => tracing::warn!(%error, "standalone PR state scan failed"),
        }
        match pending_standalone_review_results(&database).await {
            Ok(turns) => {
                for turn in turns {
                    if let Err(error) =
                        run_standalone_review_result(&database, &context, turn).await
                    {
                        tracing::warn!(%error, %turn, "standalone result validation pending");
                    }
                }
            }
            Err(error) => tracing::warn!(%error, "standalone result recovery scan failed"),
        }
        match pending_standalone_review_readbacks(&database).await {
            Ok(turns) => {
                for turn in turns {
                    if let Err(error) = publish_standalone_review(&database, &context, turn).await {
                        tracing::warn!(%error, %turn, "standalone review readback pending");
                    }
                }
            }
            Err(error) => tracing::warn!(%error, "standalone review readback scan failed"),
        }
        match pending_standalone_dispositions(&database).await {
            Ok(turns) => {
                for turn in turns {
                    if let Err(error) =
                        publish_standalone_dispositions(&database, &context, turn).await
                    {
                        tracing::warn!(%error, %turn, "standalone finding disposition pending");
                    }
                }
            }
            Err(error) => tracing::warn!(%error, "standalone disposition scan failed"),
        }
        match pending_standalone_review_rounds(&database).await {
            Ok(rounds) => {
                for (task_id, round, repository_id, stage) in rounds {
                    if stage == "awaiting_credentials" {
                        let ready = if let Some(grant) = context
                            .targets
                            .get(&repository_id)
                            .and_then(|target| target.grant.as_ref())
                        {
                            grant.token().await.is_ok()
                        } else {
                            false
                        };
                        if !ready {
                            continue;
                        }
                        let mut work = match database.begin().await {
                            Ok(work) => work,
                            Err(error) => {
                                tracing::warn!(%error, %task_id, "standalone credential resume unavailable");
                                continue;
                            }
                        };
                        if let Err(error) =
                            resume_standalone_review_credentials(&mut work, task_id, round).await
                        {
                            tracing::warn!(%error, %task_id, "standalone credential resume failed");
                            continue;
                        }
                        if let Err(error) = work.commit().await {
                            tracing::warn!(%error, %task_id, "standalone credential resume commit failed");
                            continue;
                        }
                    }
                    if let Err(error) =
                        run_standalone_review_round(&database, &context, &intake, task_id, round)
                            .await
                    {
                        tracing::warn!(%error, %task_id, "standalone round recovery pending");
                    }
                }
            }
            Err(error) => tracing::warn!(%error, "standalone round recovery scan failed"),
        }
        match pending_standalone_review_wakes(&database).await {
            Ok(wakes) => {
                for wake in wakes {
                    if let Err(error) =
                        scan_standalone_review_wake(&database, &context, &wake).await
                    {
                        tracing::warn!(%error, pr = wake.pr_number, "standalone timeline readback failed");
                    }
                }
            }
            Err(error) => tracing::warn!(%error, "standalone wake scan failed"),
        }
        match pending_standalone_review_issues(&database).await {
            Ok(issues) => {
                for (repository_id, upstream, pr_number) in issues {
                    if let Err(error) = run_standalone_review_decision(
                        &database,
                        &context,
                        repository_id,
                        &upstream,
                        pr_number,
                    )
                    .await
                    {
                        tracing::warn!(%error, %upstream, pr_number, "standalone decision readback failed");
                    }
                }
            }
            Err(error) => tracing::warn!(%error, "standalone request scan failed"),
        }
        match pending_standalone_review_commands(&database).await {
            Ok(issues) => {
                for (repository_id, upstream, pr_number) in issues {
                    if let Err(error) = run_standalone_review_decision(
                        &database,
                        &context,
                        repository_id,
                        &upstream,
                        pr_number,
                    )
                    .await
                    {
                        tracing::warn!(%error,%upstream,pr_number,
                            "standalone command decision pending");
                    }
                }
            }
            Err(error) => tracing::warn!(%error,"standalone command scan failed"),
        }
        tokio::time::sleep(Duration::from_secs(30)).await;
    }
}

/// Requeue managed dashboard readback after exhausted jobs and late edits.
async fn run_dashboard_recovery(database: Database) {
    loop {
        match watched_dashboards(&database).await {
            Ok(tasks) => {
                for task_id in tasks {
                    let result: Result<()> = async {
                        let mut work = database.begin().await?;
                        enqueue_dashboard(&mut work, task_id, "scan").await?;
                        work.commit().await?;
                        Ok(())
                    }
                    .await;
                    if let Err(error) = result {
                        tracing::warn!(%error, %task_id, "dashboard recovery failed");
                    }
                }
            }
            Err(error) => tracing::warn!(%error, "dashboard watch scan failed"),
        }
        tokio::time::sleep(Duration::from_secs(30)).await;
    }
}

/// Requeue runner ref and dispatch readbacks after job exhaustion or restart.
async fn run_runner_recovery(database: Database) {
    loop {
        match watched_runner_sessions(&database).await {
            Ok(sessions) => {
                for session_id in sessions {
                    let result: Result<()> = async {
                        let mut work = database.begin().await?;
                        enqueue_runner_session(&mut work, session_id, "scan").await?;
                        work.commit().await?;
                        Ok(())
                    }
                    .await;
                    if let Err(error) = result {
                        tracing::warn!(%error, %session_id, "runner recovery failed");
                    }
                }
            }
            Err(error) => tracing::warn!(%error, "runner watch scan failed"),
        }
        tokio::time::sleep(Duration::from_secs(30)).await;
    }
}

/// Consume one ingress fact family with its own durable application receipts.
async fn run_events<T, H>(
    settings: Settings,
    database: Database,
    handler: H,
    consumer_name: String,
    filter_subject: &'static str,
) where
    T: Contract + DeserializeOwned + Clone + Send,
    H: Handler<T> + Clone + Send + Sync,
{
    loop {
        let result: Result<()> = async {
            let client = connect_nats(
                &settings.nats_url,
                settings.nats_credentials_file.as_deref(),
            )
            .await?;
            let context = jetstream::new(client);
            let consumer = ensure_pull_consumer(
                &context,
                "GITHUB",
                &consumer_name,
                filter_subject,
                Duration::from_secs(60),
                Duration::from_secs(1),
            )
            .await?;
            run_consumer::<T, _>(
                ConsumerStream {
                    pull: &consumer,
                    context: &context,
                    name: "GITHUB",
                },
                &database,
                &consumer_name,
                settings.consumer_max_attempts,
                Duration::from_millis(settings.consumer_poll_ms),
                &handler,
            )
            .await
        }
        .await;
        if let Err(error) = result {
            tracing::warn!(%error, consumer = %consumer_name, "task event consumer disconnected");
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
}

/// Bind the task application's existing original-archive uploader.
fn archive_context(settings: &Settings) -> Result<ArchiveContext> {
    let credentials: ArchiveCredentials =
        serde_json::from_str(&read_secret(&settings.archive_credentials_file)?)?;
    Ok(ArchiveContext {
        store: Arc::new(
            AmazonS3Builder::new()
                .with_endpoint(&settings.archive_endpoint)
                .with_bucket_name(&settings.archive_bucket)
                .with_region(&settings.archive_region)
                .with_access_key_id(credentials.access_key_id)
                .with_secret_access_key(credentials.secret_access_key)
                .with_allow_http(settings.archive_endpoint.starts_with("http://"))
                .build()?,
        ),
        prefix: settings.archive_prefix.clone(),
    })
}

/// Execute a host operator inspection or explicitly requested cancellation.
async fn run_operator(
    arguments: &[String],
    settings: &Settings,
    database: &Database,
    outbox: &Outbox,
) -> Result<()> {
    let value = match arguments {
        [area, action, manifest] if area == "imports" && action == "cutover" => {
            let source: serde_json::Value = serde_json::from_slice(&fs::read(manifest)?)?;
            import_cutover_manifest(database, &source).await?
        }
        [area, action, manifest] if area == "imports" && action == "display" => {
            let source = serde_json::from_slice(&fs::read(manifest)?)?;
            import_historical_display(database, &source).await?
        }
        [area, action, manifest] if area == "imports" && action == "python" => {
            let source: serde_json::Value = serde_json::from_slice(&fs::read(manifest)?)?;
            let result = import_python_manifest(database, &source).await?;
            let deployment = source["deployment"]
                .as_str()
                .context("import deployment missing")?;
            let pending = pending_python_import_archives(database, deployment).await?;
            let archive = archive_context(settings)?;
            for sha in &pending {
                upload_archive(database, &archive, sha).await?;
            }
            serde_json::json!({"inventory":result,"uploads_verified":pending.len()})
        }
        [area, action] if area == "tasks" && action == "list" => {
            serde_json::to_value(list_tasks(database).await?)?
        }
        [area, action, source] if area == "tasks" && action == "show" => {
            let (repository, number) = source
                .split_once('#')
                .context("task source must be repository#issue")?;
            let target = settings
                .targets
                .iter()
                .find(|target| {
                    target.target.issue_repository == repository
                        || target.target.upstream == repository
                })
                .context("task repository is not configured")?;
            let issue_number = number
                .parse::<i64>()
                .context("task issue number is invalid")?;
            ensure!(issue_number > 0, "task issue number is invalid");
            task_detail(
                database,
                IssueKey {
                    repository_id: if target.target.upstream == repository {
                        target
                            .upstream_repository_id
                            .unwrap_or(target.repository_id)
                    } else {
                        target.repository_id
                    },
                    number: issue_number,
                },
            )
            .await?
            .context("task not found")?
        }
        [area, action] if area == "effects" && action == "pending" => {
            serde_json::to_value(pending_effects(database).await?)?
        }
        [area, action] if area == "consumer" && action == "status" => serde_json::to_value(
            consumer_status(
                database,
                &[
                    settings.consumer_name.clone(),
                    format!("{}-closures", settings.consumer_name),
                ],
            )
            .await?,
        )?,
        [area, action, turn] if area == "attempts" && action == "cancel" => {
            let turn = Uuid::parse_str(turn).context("attempt ID is invalid")?;
            let actor = std::env::var("USER").context("host operator user is unavailable")?;
            let mut cancellation = database.begin().await?;
            let cancelled = cancel_attempt(&mut cancellation, outbox, turn, &actor).await?;
            cancellation.commit().await?;
            serde_json::json!({"cancelled": cancelled, "attempt": turn})
        }
        [area, action, sha] if area == "archives" && action == "retry" => {
            let actor = std::env::var("USER").context("host operator user is unavailable")?;
            let mut retry = database.begin().await?;
            let requeued =
                concurro_tasks_postgres::retry_archive_upload(&mut retry, sha, &actor).await?;
            retry.commit().await?;
            serde_json::json!({"requeued": requeued, "gzip_sha256": sha})
        }
        [area, action, id, flag]
            if area == "writes"
                && action == "resolve"
                && (flag == "--failed" || flag == "--resend") =>
        {
            let actor = std::env::var("USER").context("host operator user is unavailable")?;
            let mut change = database.begin().await?;
            let resolved = if let Ok(comment_id) = Uuid::parse_str(id) {
                let resolution = if flag == "--failed" {
                    CommentResolution::Failed
                } else {
                    CommentResolution::Resend
                };
                resolve_comment(&mut change, comment_id, &actor, resolution).await?
            } else {
                let parts = id.split(':').collect::<Vec<_>>();
                ensure!(
                    matches!(parts.as_slice(), [_, _] | [_, _, _]),
                    "write target must be kind:uuid[:sequence]"
                );
                let write_id = Uuid::parse_str(parts[1]).context("write ID is invalid")?;
                let sequence = parts
                    .get(2)
                    .map(|value| value.parse::<i32>().context("action sequence is invalid"))
                    .transpose()?;
                resolve_pending_write(
                    &mut change,
                    outbox,
                    parts[0],
                    write_id,
                    sequence,
                    &actor,
                    flag == "--resend",
                )
                .await?
            };
            change.commit().await?;
            serde_json::json!({"resolved": resolved, "write": id})
        }
        [area, action, id, flag, url]
            if area == "writes" && action == "resolve" && flag == "--written" =>
        {
            let id = Uuid::parse_str(id).context("write ID is invalid")?;
            let (_, suffix) = url
                .split_once("#issuecomment-")
                .context("comment URL must identify an issue comment")?;
            let comment_id = suffix
                .parse::<i64>()
                .context("comment URL has invalid ID")?;
            let actor = std::env::var("USER").context("host operator user is unavailable")?;
            let mut change = database.begin().await?;
            let resolved = resolve_comment(
                &mut change,
                id,
                &actor,
                CommentResolution::Written { comment_id, url },
            )
            .await?;
            change.commit().await?;
            serde_json::json!({"resolved": resolved, "write": id})
        }
        _ => anyhow::bail!(
            "expected imports cutover <manifest>, imports display <manifest>, imports python <manifest>, tasks list, tasks show <repository>#<issue>, consumer status, effects pending, archives retry <gzip-sha256>, attempts cancel <attempt>, or writes resolve <uuid|git:uuid|review:uuid|standalone_review:uuid|action:uuid:sequence|ready:uuid> --written <comment-url>|--failed|--resend"
        ),
    };
    println!("{}", serde_json::to_string_pretty(&value)?);
    Ok(())
}

/// Start migrations, the issue consumer, exchange jobs and task event producer.
#[tokio::main]
async fn main() -> Result<()> {
    concurro_telemetry::init("info").map_err(|error| anyhow::anyhow!(error.to_string()))?;
    let settings: Settings = load("CONCURRO_TASKS_CONFIG")?;
    let database = Database::connect(&read_secret(&settings.database_url_file)?, 10)
        .await
        .context("connect task database")?;
    migrate_events(&database).await?;
    concurro_tasks_postgres::migrate(&database).await?;
    let outbox = Outbox::new(
        settings.max_message_bytes,
        vec![
            "task.>".to_owned(),
            "request.>".to_owned(),
            "turn.>".to_owned(),
            "config.>".to_owned(),
            "view.>".to_owned(),
        ],
    );
    let arguments = std::env::args().skip(1).collect::<Vec<_>>();
    if !arguments.is_empty() {
        return run_operator(&arguments, &settings, &database, &outbox).await;
    }
    let target_config = ConfigTargets {
        targets: settings
            .targets
            .iter()
            .map(|item| ConfigTarget {
                repository_id: item.repository_id,
                upstream_repository_id: item.upstream_repository_id,
                upstream: item.target.upstream.clone(),
                fork: item.target.fork.clone(),
                issue_repository: item.target.issue_repository.clone(),
                reviewer_user_id: item.reviewer_user_id,
                launcher: ConfigLauncher {
                    workflow: "concurro-executor.yml".to_owned(),
                    r#ref: settings.intake.workflow_ref.clone(),
                },
            })
            .collect(),
    };
    sync_config_targets(&database, &outbox, &target_config).await?;
    let client = reqwest::Client::new();
    let github = AppClient::new(
        client.clone(),
        &settings.github_api_base,
        settings.github_app_id,
        read_secret(&settings.github_private_key_file)?.as_bytes(),
    )?;
    let vault = match (
        &settings.vault_metadata_url,
        &settings.vault_metadata_token_file,
    ) {
        (Some(url), Some(file)) => {
            Some(MetadataClient::new(client.clone(), url, read_secret(file)?))
        }
        (None, None) => None,
        _ => anyhow::bail!("Vault metadata URL and token file must be configured together"),
    };
    let operator = Operator::new(
        client,
        &settings.exchange_url,
        read_secret(&settings.exchange_token_file)?,
    );
    let archive = archive_context(&settings)?;
    let targets = settings
        .targets
        .iter()
        .map(|item| (item.repository_id, item.target.clone()))
        .collect::<HashMap<_, _>>();
    let index_targets = settings
        .targets
        .iter()
        .map(|item| IndexTarget {
            issue_repository_id: item.repository_id,
            issue_repository: item.target.issue_repository.clone(),
            upstream: item.target.upstream.clone(),
            fork: item.target.fork.clone(),
            issue_permissions: item.target.issue_permissions.clone(),
            upstream_permissions: item.target.upstream_permissions.clone(),
            fork_permissions: item.target.fork_permissions.clone(),
        })
        .collect::<Vec<_>>();
    let decisions = DecisionContext {
        github: github.clone(),
        targets: targets
            .iter()
            .map(|(id, target)| {
                (
                    *id,
                    DecisionTarget {
                        repository: target.issue_repository.clone(),
                        read_permissions: target.issue_permissions.clone(),
                        fork: target.fork.clone(),
                        fork_permissions: target.fork_permissions.clone(),
                        initialization: Some(target.clone()),
                        publication: settings
                            .targets
                            .iter()
                            .find(|item| item.repository_id == *id)
                            .map(|item| GitPublicationTarget {
                                issue_repository: target.issue_repository.clone(),
                                fork: target.fork.clone(),
                                upstream: target.upstream.clone(),
                                fork_read_permissions: target.fork_permissions.clone(),
                                fork_write_permissions: item.fork_write_permissions.clone(),
                                upstream_read_permissions: target.upstream_permissions.clone(),
                                upstream_write_permissions: item.upstream_write_permissions.clone(),
                                bot_login: target.bot_login.clone(),
                            }),
                    },
                )
            })
            .collect(),
        outbox: outbox.clone(),
    };
    if let (Some(listen), Some(proof_file), Some(bearer_file)) = (
        &settings.api_listen,
        &settings.command_proof_public_key_file,
        &settings.api_read_bearer_file,
    ) {
        let listener = tokio::net::TcpListener::bind(listen).await?;
        let api = TaskApi {
            database: database.clone(),
            decisions: decisions.clone(),
            standalone_targets: settings
                .targets
                .iter()
                .filter(|item| item.reviewer_user_id.is_some())
                .map(|item| {
                    (
                        item.upstream_repository_id.unwrap_or(item.repository_id),
                        DecisionTarget {
                            repository: item.target.upstream.clone(),
                            read_permissions: item.target.upstream_permissions.clone(),
                            fork: item.target.fork.clone(),
                            fork_permissions: item.target.fork_permissions.clone(),
                            initialization: None,
                            publication: None,
                        },
                    )
                })
                .collect(),
            proof_public_key: Arc::new(std::fs::read(proof_file)?),
            read_bearer: Arc::new(read_secret(bearer_file)?),
            delivery_bearer: settings
                .delivery_recovery_bearer_file
                .as_deref()
                .map(read_secret)
                .transpose()?
                .map(Arc::new),
        };
        tokio::spawn(async move {
            if let Err(error) = axum::serve(listener, task_api_router(api)).await {
                tracing::error!(%error, "task API listener stopped");
            }
        });
    }
    let publication = PublicationContext {
        github: github.clone(),
        targets: settings
            .targets
            .iter()
            .map(|item| {
                (
                    item.repository_id,
                    PublicationTarget {
                        repository: item.target.issue_repository.clone(),
                        read_permissions: item.target.issue_permissions.clone(),
                        write_permissions: item.issue_write_permissions.clone(),
                        bot_login: item.target.bot_login.clone(),
                    },
                )
            })
            .collect(),
        standalone_targets: settings
            .targets
            .iter()
            .filter(|item| item.reviewer_user_id.is_some())
            .map(|item| {
                (
                    item.upstream_repository_id.unwrap_or(item.repository_id),
                    PublicationTarget {
                        repository: item.target.upstream.clone(),
                        read_permissions: item.target.upstream_permissions.clone(),
                        write_permissions: item.upstream_write_permissions.clone(),
                        bot_login: item.target.bot_login.clone(),
                    },
                )
            })
            .collect(),
    };
    let git_publication = GitPublicationContext {
        github: github.clone(),
        targets: settings
            .targets
            .iter()
            .map(|item| {
                (
                    item.repository_id,
                    GitPublicationTarget {
                        issue_repository: item.target.issue_repository.clone(),
                        fork: item.target.fork.clone(),
                        upstream: item.target.upstream.clone(),
                        fork_read_permissions: item.target.fork_permissions.clone(),
                        fork_write_permissions: item.fork_write_permissions.clone(),
                        upstream_read_permissions: item.target.upstream_permissions.clone(),
                        upstream_write_permissions: item.upstream_write_permissions.clone(),
                        bot_login: item.target.bot_login.clone(),
                    },
                )
            })
            .collect(),
        outbox: outbox.clone(),
    };
    let runners = RunnerContext {
        github: github.clone(),
        targets: settings
            .targets
            .iter()
            .map(|item| {
                (
                    item.repository_id,
                    RunnerTarget {
                        fork: item.target.fork.clone(),
                        read_permissions: item.target.fork_permissions.clone(),
                        write_permissions: item.fork_write_permissions.clone(),
                    },
                )
            })
            .collect(),
    };
    let mut standalone_targets = HashMap::new();
    for item in &settings.targets {
        let Some(reviewer_id) = item.reviewer_user_id else {
            continue;
        };
        let grant = if let Some(path) = item.reviewer_grant_file.as_deref() {
            if Path::new(path).exists() {
                Some(UserGrant::open(
                    reqwest::Client::new(),
                    settings
                        .reviewer_oauth_base
                        .as_deref()
                        .context("reviewer OAuth endpoint is missing")?,
                    path,
                    settings
                        .reviewer_oauth_client_id
                        .as_deref()
                        .context("reviewer OAuth client ID is missing")?,
                    settings
                        .reviewer_oauth_client_secret_file
                        .as_deref()
                        .context("reviewer OAuth client secret file is missing")?,
                    reviewer_id,
                )?)
            } else {
                None
            }
        } else {
            None
        };
        standalone_targets.insert(
            item.upstream_repository_id.unwrap_or(item.repository_id),
            StandaloneReviewTarget {
                source: item.target.clone(),
                runner_repository_id: item.repository_id,
                reviewer_id,
                upstream_write_permissions: item.upstream_write_permissions.clone(),
                grant,
            },
        );
    }
    let standalone_reviews = StandaloneReviewContext {
        github: github.clone(),
        targets: standalone_targets,
    };
    observe_funding_policies(&database, &decisions, &standalone_reviews).await;
    let initialization = InitializationContext {
        github: github.clone(),
        targets,
        outbox: outbox.clone(),
        vault,
    };
    let intake = IntakeContext {
        initialization: initialization.clone(),
        transport: settings.intake.clone(),
    };
    let worker = WorkerOptions::default()
        .pg_pool(database.pool().clone())
        .schema("graphile_worker")
        .heartbeat_interval(Duration::from_secs(5))
        .sweep_interval(Duration::from_secs(5))
        .sweep_threshold(Duration::from_secs(15))
        .recovery_delay(Duration::ZERO)
        .define_job::<TaskDecisionJob>()
        .define_job::<LabelScanJob>()
        .define_job::<StandaloneReviewScanJob>()
        .define_job::<StandaloneReviewDecisionJob>()
        .define_job::<StandaloneReviewRoundJob>()
        .define_job::<StandaloneReviewResultJob>()
        .define_job::<StandaloneReviewPublishJob>()
        .define_job::<TaskInitializeJob>()
        .define_job::<TaskIntakeJob>()
        .define_job::<TaskPlanJob>()
        .define_job::<TaskClarificationJob>()
        .define_job::<TaskDesignBatchJob>()
        .define_job::<TaskDesignCorrectionJob>()
        .define_job::<TaskDesignResultJob>()
        .define_job::<TaskImplementJob>()
        .define_job::<TaskImplementationResultJob>()
        .define_job::<TaskGitPublicationJob>()
        .define_job::<TaskPrReviewBatchJob>()
        .define_job::<TaskPrReviewCorrectionJob>()
        .define_job::<TaskPrReviewResultJob>()
        .define_job::<TaskPrReviewPublishJob>()
        .define_job::<TaskGatesJob>()
        .define_job::<ExchangeEffectJob>()
        .define_job::<ExchangePollJob>()
        .define_job::<SponsorshipTransitionJob>()
        .define_job::<UploadArchiveJob>()
        .define_job::<StageLabelJob>()
        .define_job::<DashboardJob>()
        .define_job::<RunnerSessionJob>()
        .define_job::<PublishRequestReplyJob>()
        .define_job::<PublishTaskReportJob>()
        .add_extension(decisions.clone())
        .add_extension(standalone_reviews.clone())
        .add_extension(initialization.clone())
        .add_extension(intake.clone())
        .add_extension(ExchangeContext {
            operator,
            outbox: outbox.clone(),
        })
        .add_extension(archive)
        .add_extension(publication.clone())
        .add_extension(git_publication.clone())
        .add_extension(runners)
        .init()
        .await?;
    let owned = settings
        .targets
        .iter()
        .map(|entry| {
            let names = std::iter::once(entry.target.bot_login.clone())
                .chain(entry.target.publisher_logins.clone())
                .collect::<HashSet<_>>();
            (entry.repository_id, names)
        })
        .collect();
    let handler = CommentHandler::new(
        settings.targets.iter().map(|target| target.repository_id),
        outbox.clone(),
    )
    .with_owned_accounts(owned);
    let handler = handler.with_standalone_targets(
        settings
            .targets
            .iter()
            .filter(|target| target.reviewer_user_id.is_some())
            .map(|target| {
                target
                    .upstream_repository_id
                    .unwrap_or(target.repository_id)
            }),
    );
    let index_database = database.clone();
    let index_github = github.clone();
    let gap_index_targets = index_targets.clone();
    tokio::spawn(async move {
        loop {
            for target in &index_targets {
                if let Err(error) = scan_target_index(&index_database, &index_github, target).await
                {
                    tracing::warn!(%error, repository = %target.issue_repository, "target index scan pending");
                }
            }
            tokio::time::sleep(Duration::from_secs(60)).await;
        }
    });
    tokio::spawn(run_gap_recovery(
        database.clone(),
        outbox.clone(),
        decisions.clone(),
        standalone_reviews.clone(),
        git_publication.clone(),
        handler.clone(),
        gap_index_targets,
    ));
    tokio::spawn(run_forwarder(settings.clone(), database.clone()));
    tokio::spawn(run_reply_readbacks(database.clone(), publication.clone()));
    tokio::spawn(run_report_readbacks(database.clone(), publication));
    tokio::spawn(run_pr_review_readbacks(
        database.clone(),
        git_publication.clone(),
    ));
    tokio::spawn(run_gate_recovery(database.clone(), git_publication));
    tokio::spawn(run_funding_policy_observer(
        database.clone(),
        decisions.clone(),
        standalone_reviews.clone(),
    ));
    tokio::spawn(run_label_recovery(database.clone(), decisions.clone()));
    tokio::spawn(run_standalone_review_recovery(
        database.clone(),
        standalone_reviews.clone(),
        intake,
    ));
    tokio::spawn(run_decision_recovery(database.clone(), decisions));
    tokio::spawn(run_deadline_recovery(database.clone(), outbox));
    tokio::spawn(run_retirement_recovery(database.clone()));
    tokio::spawn(run_stage_label_recovery(database.clone()));
    tokio::spawn(run_dashboard_recovery(database.clone()));
    tokio::spawn(run_runner_recovery(database.clone()));
    tokio::spawn(run_events::<GithubEvent<IssueCommentPayload>, _>(
        settings.clone(),
        database.clone(),
        handler.clone(),
        settings.consumer_name.clone(),
        "github.issue_comment.*",
    ));
    tokio::spawn(run_events::<GithubIssues, _>(
        settings.clone(),
        database.clone(),
        handler,
        format!("{}-closures", settings.consumer_name),
        "github.issues.closed",
    ));
    tokio::spawn(run_events::<GithubEvent<PullRequestPayload>, _>(
        settings.clone(),
        database.clone(),
        StandaloneReviewHandler::new(standalone_reviews),
        format!("{}-standalone-reviews", settings.consumer_name),
        "github.pull_request.*",
    ));
    tokio::spawn(run_events::<CoverageGap, _>(
        settings.clone(),
        database.clone(),
        CoverageGapHandler,
        format!("{}-coverage-gaps", settings.consumer_name),
        "ingress.coverage.gap",
    ));
    tokio::spawn(run_events::<GithubEvent<WorkflowRunPayload>, _>(
        settings.clone(),
        database.clone(),
        IndexRunHandler {
            forks: settings
                .targets
                .iter()
                .map(|target| target.target.fork.to_ascii_lowercase())
                .collect(),
        },
        format!("{}-executor-runs", settings.consumer_name),
        "github.workflow_run.*",
    ));
    worker.run().await?;
    Ok(())
}
