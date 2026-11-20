//! Task application runtime for durable GitHub intake and exchange turns.

use std::{
    collections::{HashMap, HashSet},
    sync::Arc,
    time::Duration,
};

use anyhow::{Context, Result, ensure};
use async_nats::jetstream::{self, stream};
use concurro_config::{Validate, load, read_secret};
use concurro_contracts::{Contract, GithubEvent, GithubIssues, IssueCommentPayload};
use concurro_db::Database;
use concurro_events::Handler;
use concurro_events_nats::{
    ConsumerStream, Forwarder, ensure_pull_consumer, ensure_stream, run_consumer,
};
use concurro_events_postgres::{
    ForwarderLease, Outbox, consumer_status, migrate as migrate_events,
};
use concurro_exchange_v1::Operator;
use concurro_github_v1::AppClient;
use concurro_tasks::{
    ArchiveContext, CommentHandler, DashboardJob, DecisionContext, DecisionTarget, ExchangeContext,
    ExchangeEffectJob, ExchangePollJob, InitializationContext, InitializationTarget, IntakeContext,
    IntakeTransport, PublicationContext, PublicationTarget, PublishRequestReplyJob, RunnerContext,
    RunnerSessionJob, RunnerTarget, StageLabelJob, TaskDecisionJob, TaskInitializeJob,
    TaskIntakeJob, UploadArchiveJob, publish_request_reply, run_task_decision,
};
use concurro_tasks_core::IssueKey;
use concurro_tasks_postgres::{
    CommentResolution, cancel_attempt, due_attempt_deadlines, due_comment_readbacks,
    enqueue_dashboard, enqueue_runner_session, enqueue_stage_label, expire_attempt, list_tasks,
    pending_effects, pending_issues, recover_retirements, resolve_comment, task_detail,
    watched_dashboards, watched_runner_sessions, watched_stage_labels,
};
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
}

/// NATS user credentials read only from a private file.
#[derive(Deserialize)]
struct NatsCredentials {
    /// NATS account user.
    user: String,
    /// NATS account password.
    password: String,
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
    /// Python exchange operator endpoint.
    exchange_url: String,
    /// Python exchange operator bearer file.
    exchange_token_file: String,
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
        Ok(())
    }
}

/// Connect to the shared broker with only the configured NATS identity.
async fn connect_nats(settings: &Settings) -> Result<async_nats::Client> {
    match &settings.nats_credentials_file {
        Some(path) => {
            let credentials: NatsCredentials = serde_json::from_str(&read_secret(path)?)?;
            Ok(async_nats::ConnectOptions::with_user_and_password(
                credentials.user,
                credentials.password,
            )
            .connect(&settings.nats_url)
            .await?)
        }
        None => Ok(async_nats::connect(&settings.nats_url).await?),
    }
}

/// Reconnect the task event producer and advance only acknowledged outbox rows.
async fn run_forwarder(settings: Settings, database: Database) {
    loop {
        let result: Result<()> = async {
            let client = connect_nats(&settings).await?;
            let context = jetstream::new(client.clone());
            ensure_stream(
                &context,
                stream::Config {
                    name: "TASKS".to_owned(),
                    subjects: vec![
                        "task.>".to_owned(),
                        "request.>".to_owned(),
                        "turn.>".to_owned(),
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
async fn run_events<T>(
    settings: Settings,
    database: Database,
    handler: CommentHandler,
    consumer_name: String,
    filter_subject: &'static str,
) where
    T: Contract + DeserializeOwned + Clone + Send,
    CommentHandler: Handler<T>,
{
    loop {
        let result: Result<()> = async {
            let client = connect_nats(&settings).await?;
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

/// Execute a host operator inspection or explicitly requested cancellation.
async fn run_operator(
    arguments: &[String],
    settings: &Settings,
    database: &Database,
    outbox: &Outbox,
) -> Result<()> {
    let value = match arguments {
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
                .find(|target| target.target.issue_repository == repository)
                .context("task repository is not configured")?;
            let issue_number = number
                .parse::<i64>()
                .context("task issue number is invalid")?;
            ensure!(issue_number > 0, "task issue number is invalid");
            task_detail(
                database,
                IssueKey {
                    repository_id: target.repository_id,
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
            let id = Uuid::parse_str(id).context("write ID is invalid")?;
            let actor = std::env::var("USER").context("host operator user is unavailable")?;
            let resolution = if flag == "--failed" {
                CommentResolution::Failed
            } else {
                CommentResolution::Resend
            };
            let mut change = database.begin().await?;
            let resolved = resolve_comment(&mut change, id, &actor, resolution).await?;
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
            "expected tasks list, tasks show <repository>#<issue>, consumer status, effects pending, archives retry <gzip-sha256>, attempts cancel <attempt>, or writes resolve <write> --written <url>|--failed|--resend"
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
        ],
    );
    let arguments = std::env::args().skip(1).collect::<Vec<_>>();
    if !arguments.is_empty() {
        return run_operator(&arguments, &settings, &database, &outbox).await;
    }
    let client = reqwest::Client::new();
    let github = AppClient::new(
        client.clone(),
        &settings.github_api_base,
        settings.github_app_id,
        read_secret(&settings.github_private_key_file)?.as_bytes(),
    )?;
    let operator = Operator::new(
        client,
        &settings.exchange_url,
        read_secret(&settings.exchange_token_file)?,
    );
    let archive_credentials: ArchiveCredentials =
        serde_json::from_str(&read_secret(&settings.archive_credentials_file)?)?;
    let archive = ArchiveContext {
        store: Arc::new(
            AmazonS3Builder::new()
                .with_endpoint(&settings.archive_endpoint)
                .with_bucket_name(&settings.archive_bucket)
                .with_region(&settings.archive_region)
                .with_access_key_id(archive_credentials.access_key_id)
                .with_secret_access_key(archive_credentials.secret_access_key)
                .with_allow_http(settings.archive_endpoint.starts_with("http://"))
                .build()?,
        ),
        prefix: settings.archive_prefix.clone(),
    };
    let targets = settings
        .targets
        .iter()
        .map(|item| (item.repository_id, item.target.clone()))
        .collect::<HashMap<_, _>>();
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
                    },
                )
            })
            .collect(),
        outbox: outbox.clone(),
    };
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
    let initialization = InitializationContext {
        github,
        targets,
        outbox: outbox.clone(),
    };
    let worker = WorkerOptions::default()
        .pg_pool(database.pool().clone())
        .schema("graphile_worker")
        .heartbeat_interval(Duration::from_secs(5))
        .sweep_interval(Duration::from_secs(5))
        .sweep_threshold(Duration::from_secs(15))
        .recovery_delay(Duration::ZERO)
        .define_job::<TaskDecisionJob>()
        .define_job::<TaskInitializeJob>()
        .define_job::<TaskIntakeJob>()
        .define_job::<ExchangeEffectJob>()
        .define_job::<ExchangePollJob>()
        .define_job::<UploadArchiveJob>()
        .define_job::<StageLabelJob>()
        .define_job::<DashboardJob>()
        .define_job::<RunnerSessionJob>()
        .define_job::<PublishRequestReplyJob>()
        .add_extension(decisions.clone())
        .add_extension(initialization.clone())
        .add_extension(IntakeContext {
            initialization,
            transport: settings.intake.clone(),
        })
        .add_extension(ExchangeContext {
            operator,
            outbox: outbox.clone(),
        })
        .add_extension(archive)
        .add_extension(publication.clone())
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
    tokio::spawn(run_forwarder(settings.clone(), database.clone()));
    tokio::spawn(run_reply_readbacks(database.clone(), publication));
    tokio::spawn(run_decision_recovery(database.clone(), decisions));
    tokio::spawn(run_deadline_recovery(database.clone(), outbox));
    tokio::spawn(run_retirement_recovery(database.clone()));
    tokio::spawn(run_stage_label_recovery(database.clone()));
    tokio::spawn(run_dashboard_recovery(database.clone()));
    tokio::spawn(run_runner_recovery(database.clone()));
    tokio::spawn(run_events::<GithubEvent<IssueCommentPayload>>(
        settings.clone(),
        database.clone(),
        handler.clone(),
        settings.consumer_name.clone(),
        "github.issue_comment.*",
    ));
    tokio::spawn(run_events::<GithubIssues>(
        settings.clone(),
        database,
        handler,
        format!("{}-closures", settings.consumer_name),
        "github.issues.closed",
    ));
    worker.run().await?;
    Ok(())
}
