//! Timeline-backed enrollment for reviews of pull requests Concurro did not create.

use std::{
    collections::{BTreeMap, HashMap},
    fmt,
};

use anyhow::{Context, Result, ensure};
use chrono::{DateTime, Utc};
use concurro_contracts::{Envelope, GithubEvent, PullRequestPayload};
use concurro_db::{Database, UnitOfWork};
use concurro_events::{Handler, Outcome};
use concurro_events_postgres::Outbox;
use concurro_github_v1::{AppClient, user_grant::UserGrant};
use concurro_tasks_core::InitialFundingContext;
use concurro_tasks_core::{
    Actor, Admission, DesignReviewer, FundingMode, LaterCommand, Provider, RequestKind,
    initial_sponsorship_transitions, initialize_consent, pr_review_roster, python_digest,
    validate_standalone_pr_review,
};
use concurro_tasks_postgres::InitialSponsorship;
use concurro_tasks_postgres::StandaloneReviewAuthority;
use concurro_tasks_postgres::{
    IndexRow, StandaloneReviewCommand, StandaloneReviewRequest, StandaloneReviewWake,
    admit_standalone_review_attempt, apply_standalone_review_control,
    apply_standalone_review_request, apply_standalone_review_result,
    await_standalone_review_credentials, defer_standalone_review_wake,
    enqueue_standalone_review_scan, funding_policy_at, next_standalone_review_command,
    next_standalone_review_request, observe_standalone_review_pr_state,
    observe_standalone_review_wake, pr_is_owned, record_sponsorship_transition,
    record_standalone_funding_fact_transition, record_standalone_funding_revision,
    record_standalone_initial_sponsorship, record_standalone_review_request,
    record_standalone_withdrawal_steps, settle_standalone_review_command, source_after_activation,
    standalone_consent_sources, standalone_review_attempts, standalone_review_boundary,
    standalone_review_correction, standalone_review_outstanding_findings, standalone_review_result,
    standalone_review_round, standalone_review_source_is_current,
    standalone_sponsorship_initialized, upsert_index_event,
};
use graphile_worker::{IntoTaskHandlerResult, TaskHandler, WorkerContext};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use uuid::Uuid;

use crate::bundle::ResolvedBundle;
use crate::standalone_payload::StandaloneReviewAttemptInputs;
use crate::{
    InitializationTarget, IntakeContext, funding, git_publication::read_pr_files, initialization,
    label::read_events, observe_source_funding_policy,
    standalone_payload::prepare_standalone_review_attempt, target_index::task_branch,
};

/// The upstream repository and reviewer identity configured for one target.
#[derive(Clone)]
pub struct StandaloneReviewTarget {
    /// Source and installation permissions used to freeze a review round.
    pub source: InitializationTarget,
    /// Configured fork target used for runner dispatch and OIDC claims.
    pub runner_repository_id: i64,
    /// Numeric reviewer account whose request enrolls this channel.
    pub reviewer_id: i64,
    /// App permissions for PR artifacts, checks, replies and thread resolution.
    pub upstream_write_permissions: std::collections::BTreeMap<String, String>,
    /// Bound reviewer-account grant for native review mutations.
    pub grant: Option<UserGrant>,
}

/// GitHub and configured upstreams shared by review intake jobs.
#[derive(Clone)]
pub struct StandaloneReviewContext {
    /// App client for source and authority reads.
    pub github: AppClient,
    /// Targets keyed by numeric upstream repository ID.
    pub targets: HashMap<i64, StandaloneReviewTarget>,
    /// Durable request event writer.
    pub outbox: Outbox,
    /// Whether Rust exchange acknowledgements drive later withdrawal steps.
    pub rust_exchange: bool,
}

/// Observe a standalone target's current fork funding policy.
pub async fn observe_standalone_funding_policy(
    database: &Database,
    context: &StandaloneReviewContext,
    repository_id: i64,
    config_sha: &str,
) -> Result<()> {
    let target = context
        .targets
        .get(&repository_id)
        .context("standalone funding policy source target unavailable")?;
    observe_source_funding_policy(
        database,
        &context.github,
        &target.source,
        repository_id,
        config_sha,
    )
    .await
}

impl fmt::Debug for StandaloneReviewContext {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("StandaloneReviewContext")
            .field("target_count", &self.targets.len())
            .finish_non_exhaustive()
    }
}

/// Record authenticated review-request deliveries for a configured reviewer.
#[derive(Clone)]
pub struct StandaloneReviewHandler {
    context: StandaloneReviewContext,
}

impl StandaloneReviewHandler {
    /// Bind webhook intake to the same upstream identities used by the workers.
    pub fn new(context: StandaloneReviewContext) -> Self {
        Self { context }
    }
}

impl Handler<GithubEvent<PullRequestPayload>> for StandaloneReviewHandler {
    /// Wake a source scan only for an individual request of our reviewer.
    async fn handle(
        &self,
        uow: &mut UnitOfWork<'_>,
        event: Envelope<GithubEvent<PullRequestPayload>>,
    ) -> Outcome {
        let Some(target) = self.context.targets.get(&event.data.repository.id) else {
            return Outcome::Done;
        };
        let pr = &event.data.payload.pull_request;
        let owned = match pr_is_owned(uow, &target.source.upstream, pr.number).await {
            Ok(value) => value,
            Err(error) => return Outcome::Retry(error.to_string()),
        };
        if task_branch(&pr.head.branch)
            || owned
            || matches!(
                event.data.action.as_deref(),
                Some("review_requested" | "review_request_removed")
            )
        {
            let row = IndexRow {
                repository_id: event.data.repository.id,
                kind: "pull",
                item_id: pr.number.to_string(),
                source_at: pr.updated_at.0,
                body: json!({"repository":event.data.repository.full_name,
                    "number":pr.number,"document":pr}),
            };
            if let Err(error) = upsert_index_event(uow, &row).await {
                return Outcome::Retry(error.to_string());
            }
        }
        let Some(action @ ("review_requested" | "review_request_removed")) =
            event.data.action.as_deref()
        else {
            return Outcome::Done;
        };
        if !event
            .data
            .payload
            .requested_reviewer
            .as_ref()
            .is_some_and(|reviewer| reviewer.id == target.reviewer_id)
            || !event
                .data
                .repository
                .full_name
                .eq_ignore_ascii_case(&target.source.upstream)
        {
            return Outcome::Done;
        }
        match source_after_activation(
            uow,
            event.data.repository.id,
            event.data.payload.pull_request.updated_at.0,
        )
        .await
        {
            Ok(true) => {}
            Ok(false) => return Outcome::Done,
            Err(error) => return Outcome::Retry(error.to_string()),
        }
        let wake = StandaloneReviewWake {
            delivery_id: event.data.delivery_id,
            repository_id: event.data.repository.id,
            upstream: target.source.upstream.clone(),
            pr_number: event.data.payload.pull_request.number,
            action: action.to_owned(),
            actor_id: event.data.sender.id,
            reviewer_id: target.reviewer_id,
            updated_at: event.data.payload.pull_request.updated_at.0,
        };
        match enqueue_standalone_review_scan(uow, &wake).await {
            Ok(()) => Outcome::Done,
            Err(error) => Outcome::Retry(error.to_string()),
        }
    }
}

/// Graphile wake for one authenticated review-request delivery.
#[derive(Deserialize, Serialize)]
pub struct StandaloneReviewScanJob {
    /// Delivery GUID.
    pub delivery_id: String,
    /// Numeric upstream repository identity.
    pub repository_id: i64,
    /// Upstream owner/repository.
    pub upstream: String,
    /// PR number.
    pub pr_number: i64,
    /// Requested or removed action.
    pub action: String,
    /// Numeric actor identity.
    pub actor_id: i64,
    /// Numeric reviewer identity.
    pub reviewer_id: i64,
    /// PR update time in the delivery.
    pub updated_at: DateTime<Utc>,
}

impl TaskHandler for StandaloneReviewScanJob {
    const IDENTIFIER: &'static str = "standalone_review_scan";

    /// Record timeline sources and retain failures for bounded readback.
    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let intake = context
            .get_ext::<StandaloneReviewContext>()
            .ok_or("standalone review context unavailable")?;
        let database = Database::from_pool(context.pg_pool().clone());
        let wake = StandaloneReviewWake {
            delivery_id: self.delivery_id,
            repository_id: self.repository_id,
            upstream: self.upstream,
            pr_number: self.pr_number,
            action: self.action,
            actor_id: self.actor_id,
            reviewer_id: self.reviewer_id,
            updated_at: self.updated_at,
        };
        scan_standalone_review_wake(&database, intake, &wake)
            .await
            .map_err(|error| error.to_string())
    }
}

/// Read each matching timeline event, preserving event identity and source order.
pub async fn scan_standalone_review_events(
    database: &Database,
    context: &StandaloneReviewContext,
    wake: &StandaloneReviewWake,
) -> Result<()> {
    let target = context
        .targets
        .get(&wake.repository_id)
        .context("standalone review target unavailable")?;
    ensure!(
        wake.upstream.eq_ignore_ascii_case(&target.source.upstream)
            && wake.reviewer_id == target.reviewer_id,
        "standalone review delivery target changed"
    );
    let events = read_events(
        &context.github,
        &target.source.upstream,
        &target.source.upstream_permissions,
        wake.pr_number,
    )
    .await?;
    let mut observed = false;
    let mut work = database.begin().await?;
    for event in events {
        let Some(action @ ("review_requested" | "review_request_removed")) =
            event["event"].as_str()
        else {
            continue;
        };
        if event["requested_reviewer"]["id"] != target.reviewer_id || event["actor"].is_null() {
            continue;
        }
        let id = event["id"].as_i64().context("review event ID missing")?;
        let at = DateTime::parse_from_rfc3339(
            event["created_at"]
                .as_str()
                .context("review event time missing")?,
        )?
        .with_timezone(&Utc);
        if !source_after_activation(&mut work, wake.repository_id, at).await? {
            continue;
        }
        let actor: Actor = serde_json::from_value(event["actor"].clone())?;
        let request = StandaloneReviewRequest {
            id: Uuid::now_v7(),
            repository_id: wake.repository_id,
            upstream: wake.upstream.clone(),
            pr_number: wake.pr_number,
            source_id: id,
            source_at: at,
            actor: actor.clone(),
            action: action.to_owned(),
        };
        record_standalone_review_request(&mut work, &request).await?;
        observed |= at == wake.updated_at && actor.id == wake.actor_id && action == wake.action;
    }
    work.commit().await?;
    ensure!(observed, "review delivery event not in issue timeline yet");
    observe_standalone_review_wake(database, &wake.delivery_id).await?;
    Ok(())
}

/// Back off every failed review timeline read and retain exhausted wakes.
pub async fn scan_standalone_review_wake(
    database: &Database,
    context: &StandaloneReviewContext,
    wake: &StandaloneReviewWake,
) -> Result<()> {
    if let Err(error) = scan_standalone_review_events(database, context, wake).await {
        let exhausted =
            defer_standalone_review_wake(database, &wake.delivery_id, &error.to_string()).await?;
        if exhausted {
            tracing::warn!(delivery_id = %wake.delivery_id, %error, "review wake exhausted; reconciliation required");
        }
        return Err(error);
    }
    Ok(())
}

/// Graphile decision job for one upstream PR's source-ordered review requests.
#[derive(Deserialize, Serialize)]
pub struct StandaloneReviewDecisionJob {
    /// Numeric upstream repository identity.
    pub repository_id: i64,
    /// Upstream owner/repository.
    pub upstream: String,
    /// PR number.
    pub pr_number: i64,
}

impl TaskHandler for StandaloneReviewDecisionJob {
    const IDENTIFIER: &'static str = "standalone_review_decision";

    /// Recheck requester authority and the PR before applying a source.
    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let intake = context
            .get_ext::<StandaloneReviewContext>()
            .ok_or("standalone review context unavailable")?;
        let database = Database::from_pool(context.pg_pool().clone());
        run_standalone_review_decision(
            &database,
            intake,
            self.repository_id,
            &self.upstream,
            self.pr_number,
        )
        .await
        .map_err(|error| error.to_string())
    }
}

/// A pinned standalone review round ready for its next independent reviewer.
#[derive(Deserialize, Serialize)]
pub struct StandaloneReviewRoundJob {
    /// Stable standalone task identity.
    pub task_id: Uuid,
    /// Round identity, fenced against later requests.
    pub round: i32,
}

impl TaskHandler for StandaloneReviewRoundJob {
    const IDENTIFIER: &'static str = "standalone_review_round";

    /// Prepare and admit only the next reviewer of the current round.
    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let standalone = context
            .get_ext::<StandaloneReviewContext>()
            .ok_or("standalone review context unavailable")?;
        let intake = context
            .get_ext::<IntakeContext>()
            .ok_or("task intake context unavailable")?;
        let database = Database::from_pool(context.pg_pool().clone());
        run_standalone_review_round(&database, standalone, intake, self.task_id, self.round)
            .await
            .map_err(|error| error.to_string())
    }
}

/// A completed standalone reviewer turn awaiting source-aware validation.
#[derive(Deserialize, Serialize)]
pub struct StandaloneReviewResultJob {
    /// Exact Exchange turn that retained the original report.
    pub turn: Uuid,
}

impl TaskHandler for StandaloneReviewResultJob {
    const IDENTIFIER: &'static str = "standalone_review_result";

    /// Validate the pinned source before recording findings and publication.
    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let standalone = context
            .get_ext::<StandaloneReviewContext>()
            .ok_or("standalone review context unavailable")?;
        let database = Database::from_pool(context.pg_pool().clone());
        run_standalone_review_result(&database, standalone, self.turn)
            .await
            .map_err(|error| error.to_string())
    }
}

/// Validate a retained reviewer report against the round's original PR head.
pub async fn run_standalone_review_result(
    database: &Database,
    standalone: &StandaloneReviewContext,
    turn: Uuid,
) -> Result<()> {
    let Some(result) = standalone_review_result(database, turn).await? else {
        return Ok(());
    };
    let target = standalone
        .targets
        .get(&result.round.repository_id)
        .context("standalone review source target unavailable")?;
    let (owned, _) =
        standalone_review_outstanding_findings(database, result.round.task_id, &result.reviewer.id)
            .await?;
    let mut validated = validate_standalone_pr_review(result.report, &owned);
    if let Ok(report) = &validated {
        let paths = result.round.snapshot["changed_paths"]
            .as_array()
            .context("standalone changed paths missing")?;
        for finding in &report.findings {
            if finding
                .path
                .as_ref()
                .is_some_and(|path| !paths.iter().any(|known| known.as_str() == Some(path)))
            {
                validated = Err("review finding cites a path outside the frozen PR".to_owned());
                break;
            }
        }
    }
    if let Ok(report) = &validated {
        let cited = report
            .findings
            .iter()
            .filter(|finding| finding.line.is_some())
            .filter_map(|finding| finding.path.clone())
            .collect::<std::collections::BTreeSet<_>>()
            .into_iter()
            .collect::<Vec<_>>();
        if !cited.is_empty() {
            let head = result.round.snapshot["head"]
                .as_str()
                .context("standalone review head missing")?;
            let source = initialization::read_source_files(
                &standalone.github,
                &target.source.upstream,
                &target.source.upstream_permissions,
                head,
                &cited,
            )
            .await?;
            match source {
                Ok(source) => {
                    for finding in &report.findings {
                        if let (Some(path), Some(line)) = (&finding.path, finding.line) {
                            let valid = source
                                .get(path)
                                .and_then(Option::as_deref)
                                .is_some_and(|content| line <= content.lines().count() as u64);
                            if !valid {
                                validated = Err(format!(
                                    "review finding cites a missing source line: {path}:{line}"
                                ));
                                break;
                            }
                        }
                    }
                }
                Err(reason) => validated = Err(reason),
            }
        }
    }
    let mut work = database.begin().await?;
    apply_standalone_review_result(&mut work, turn, validated).await?;
    work.commit().await?;
    Ok(())
}

/// Freeze the first review request's consent and boundary-time Vault answer.
async fn initialize_standalone_funding(
    database: &Database,
    round: &concurro_tasks_postgres::StandaloneReviewRound,
    target: &StandaloneReviewTarget,
    intake: &IntakeContext,
) -> Result<bool> {
    if standalone_sponsorship_initialized(database, round.task_id).await? {
        return Ok(true);
    }
    let (boundary, initiator) = standalone_review_boundary(database, round.request_id).await?;
    let config = &round.snapshot["configuration"];
    let default_funding = match config["funding_mode"].as_str() {
        None | Some("org") => FundingMode::Org,
        Some("contributed") => FundingMode::Contributed,
        Some(_) => anyhow::bail!("standalone funding mode is invalid"),
    };
    let digest = python_digest(config)?;
    let source_funding =
        funding_policy_at(database, round.repository_id, &digest, boundary.at).await?;
    let mut read = database.begin().await?;
    let (revision, _) =
        standalone_consent_sources(&mut read, round.repository_id, round.pr_number, &boundary)
            .await?;
    read.commit().await?;
    let available = if let Some(vault) = &intake.initialization.vault {
        let metadata = vault
            .read(initiator.id, Some(&boundary.at.to_rfc3339()))
            .await?;
        funding::boundary_eligibility(config, &metadata, &target.source.upstream, boundary.at)?
    } else if default_funding == FundingMode::Org {
        BTreeMap::from([
            (Provider::Openai, Some(false)),
            (Provider::Anthropic, Some(false)),
        ])
    } else {
        let mut work = database.begin().await?;
        await_standalone_review_credentials(&mut work, round.task_id, round.number).await?;
        work.commit().await?;
        return Ok(false);
    };
    let mut work = database.begin().await?;
    let (current, mut facts) =
        standalone_consent_sources(&mut work, round.repository_id, round.pr_number, &boundary)
            .await?;
    ensure!(
        current == revision,
        "standalone funding source changed during Vault read"
    );
    let Some(initial) = initialize_consent(&facts, &boundary, &initiator, &available) else {
        await_standalone_review_credentials(&mut work, round.task_id, round.number).await?;
        work.commit().await?;
        return Ok(false);
    };
    if record_standalone_initial_sponsorship(
        &mut work,
        round.task_id,
        round.request_id,
        &boundary,
        InitialSponsorship {
            initial: &initial,
            available: &available,
            config_digest: &digest,
            default_funding,
            source_funding,
        },
    )
    .await?
    {
        let transitions = initial_sponsorship_transitions(
            &initial,
            &round.upstream,
            concurro_tasks_core::IssueKey {
                repository_id: round.repository_id,
                number: round.pr_number,
            },
            &initiator,
            InitialFundingContext {
                generation: round.number.into(),
                prior_epochs: None,
                default_funding,
                source_funding,
                source_channel: "review_request",
            },
        )?;
        for transition in transitions {
            record_sponsorship_transition(&mut work, round.task_id, &transition).await?;
        }
        facts.sort_by(|left, right| left.order.cmp(&right.order));
        for fact in facts.iter().filter(|fact| fact.order > boundary) {
            record_standalone_funding_fact_transition(&mut work, round.task_id, fact).await?;
        }
    }
    work.commit().await?;
    Ok(true)
}

/// Continue one immutable standalone panel through the shared model runner.
pub async fn run_standalone_review_round(
    database: &Database,
    standalone: &StandaloneReviewContext,
    intake: &IntakeContext,
    task_id: Uuid,
    number: i32,
) -> Result<()> {
    if !run_standalone_review_pr_state(database, standalone, task_id).await? {
        return Ok(());
    }
    let Some(round) = standalone_review_round(database, task_id).await? else {
        return Ok(());
    };
    if round.number != number
        || round.cancelled
        || !matches!(round.stage.as_str(), "queued" | "review")
    {
        return Ok(());
    }
    let target = standalone
        .targets
        .get(&round.repository_id)
        .context("standalone review target unavailable")?;
    ensure!(
        round.reviewer_id == target.reviewer_id
            && round.runner_repository_id == target.runner_repository_id
            && round.upstream.eq_ignore_ascii_case(&target.source.upstream),
        "standalone review target changed"
    );
    let panel: Vec<DesignReviewer> = serde_json::from_value(round.snapshot["panel"].clone())?;
    let seats = standalone_review_attempts(database, round.task_id, round.number).await?;
    let next = panel.iter().find(|reviewer| {
        seats
            .iter()
            .find(|(id, _, _)| id == &reviewer.id)
            .is_none_or(|(_, status, _)| matches!(status.as_str(), "correcting" | "retrying"))
    });
    let Some(reviewer) = next else {
        return Ok(());
    };
    if seats.iter().any(|(id, status, _)| {
        id != &reviewer.id && matches!(status.as_str(), "admitted" | "completed" | "validated")
    }) {
        return Ok(());
    }
    if !initialize_standalone_funding(database, &round, target, intake).await? {
        return Ok(());
    }
    let grant_ready = match target.grant.as_ref() {
        Some(grant) => grant.token().await.is_ok(),
        None => false,
    };
    if !grant_ready {
        let mut work = database.begin().await?;
        await_standalone_review_credentials(&mut work, round.task_id, round.number).await?;
        work.commit().await?;
        return Ok(());
    }
    let correction =
        standalone_review_correction(database, round.task_id, round.number, &reviewer.id).await?;
    let config_sha = round.snapshot["config_sha"]
        .as_str()
        .context("standalone config commit missing")?;
    let merge_base = round.snapshot["merge_base"]
        .as_str()
        .context("standalone merge base missing")?;
    let title = round.snapshot["title"]
        .as_str()
        .context("standalone PR title missing")?;
    let snapshot = initialization::read_standalone_review_snapshot(
        &standalone.github,
        &target.source,
        config_sha,
        merge_base,
        title,
        round.snapshot["body"].as_str(),
    )
    .await?;
    let (_, own_findings) =
        standalone_review_outstanding_findings(database, round.task_id, &reviewer.id).await?;
    let expected = standalone
        .github
        .runner_claims(
            &target.source.fork,
            &target.source.fork_permissions,
            &intake.transport.workflow_ref,
            &target.source.bot_login,
        )
        .await?;
    let mut spec = prepare_standalone_review_attempt(
        &round,
        &snapshot,
        &target.source,
        &intake.transport,
        StandaloneReviewAttemptInputs {
            bundle: ResolvedBundle::resolve(&snapshot)?,
            expected,
            reviewer,
            own_findings: &own_findings,
            correction: correction
                .as_ref()
                .map(|(report, reason)| (report, reason.as_str())),
            turn: Uuid::now_v7(),
            now: Utc::now(),
        },
    )?;
    if let Err(error) = funding::fund_attempt(
        database,
        &intake.initialization,
        round.task_id,
        &snapshot.configuration,
        &target.source,
        u64::try_from(round.used_model_calls + 1)?,
        &mut spec,
    )
    .await
    {
        if error
            .downcast_ref::<funding::FundingPreflightRefusal>()
            .is_some()
        {
            let mut work = database.begin().await?;
            await_standalone_review_credentials(&mut work, round.task_id, round.number).await?;
            work.commit().await?;
            return Ok(());
        }
        return Err(error);
    }
    if !run_standalone_review_pr_state(database, standalone, task_id).await? {
        return Ok(());
    }
    let mut work = database.begin().await?;
    let admission = admit_standalone_review_attempt(
        &mut work,
        &intake.initialization.outbox,
        round.task_id,
        &reviewer.id,
        &spec,
    )
    .await?;
    work.commit().await?;
    if admission == Admission::WaitingForGroup {
        anyhow::bail!("standalone reviewer staffing group is still retiring");
    }
    Ok(())
}

/// Observe PR closure or reopening before admitting another standalone reviewer.
pub async fn run_standalone_review_pr_state(
    database: &Database,
    context: &StandaloneReviewContext,
    task_id: Uuid,
) -> Result<bool> {
    let Some(round) = standalone_review_round(database, task_id).await? else {
        return Ok(false);
    };
    let target = context
        .targets
        .get(&round.repository_id)
        .context("standalone review source target unavailable")?;
    ensure!(
        round.upstream.eq_ignore_ascii_case(&target.source.upstream),
        "standalone PR target changed"
    );
    let (owner, name) = round
        .upstream
        .split_once('/')
        .context("upstream must be owner/name")?;
    let number = round.pr_number.to_string();
    let pr: Value = context
        .github
        .read(
            &round.upstream,
            &target.source.upstream_permissions,
            &["repos", owner, name, "pulls", &number],
            &[],
        )
        .await?;
    ensure!(
        pr["number"] == round.pr_number,
        "standalone PR identity changed"
    );
    let open = pr["state"] == "open" && pr["merged"] != true;
    let mut work = database.begin().await?;
    observe_standalone_review_pr_state(&mut work, task_id, open).await?;
    work.commit().await?;
    Ok(open)
}

/// Apply one PR command revision after numeric-identity writer verification.
async fn run_standalone_review_command(
    database: &Database,
    context: &StandaloneReviewContext,
    target: &StandaloneReviewTarget,
    command: &StandaloneReviewCommand,
) -> Result<()> {
    let login = context
        .github
        .public_user_login_by_id(
            &command.upstream,
            &target.source.upstream_permissions,
            command.actor.id,
        )
        .await?;
    let writer = if let Some(login) = login {
        context
            .github
            .writer_permission(
                &command.upstream,
                &login,
                &target.source.upstream_permissions,
            )
            .await?
    } else {
        false
    };
    let funding = matches!(
        command.kind,
        Some(RequestKind::Later(
            LaterCommand::Funding { .. }
                | LaterCommand::Sponsor { .. }
                | LaterCommand::Withdraw { .. }
        ))
    );
    let self_withdraw = matches!(
        command.kind,
        Some(RequestKind::Later(LaterCommand::Withdraw { .. }))
    );
    let adoption: Result<Option<Value>> = async {
        if writer
            && !command.deleted
            && matches!(
                command.kind,
                Some(RequestKind::Later(LaterCommand::Resume {
                    description_only: false
                }))
            )
        {
            let task = concurro_tasks_postgres::standalone_review_task(
                database,
                &command.upstream,
                command.pr_number,
            )
            .await?;
            if let Some(task) = task.filter(|task| {
                matches!(
                    task["stage"].as_str(),
                    Some("stopped" | "blocked" | "awaiting_credentials" | "budget_exhausted")
                )
            }) {
                let task_id: Uuid = serde_json::from_value(task["id"].clone())?;
                let round = standalone_review_round(database, task_id)
                    .await?
                    .context("standalone round missing")?;
                let (_, config_sha) = context
                    .github
                    .default_head(&target.source.fork, &target.source.fork_permissions)
                    .await?;
                let config = initialization::read_target_config(
                    &context.github,
                    &target.source,
                    &config_sha,
                )
                .await?;
                let panel = pr_review_roster(&config.raw).map_err(|error| {
                    initialization::TargetConfigurationError(anyhow::Error::msg(error))
                })?;
                initialization::read_standalone_review_snapshot(
                    &context.github,
                    &target.source,
                    &config_sha,
                    round.snapshot["merge_base"]
                        .as_str()
                        .context("merge base missing")?,
                    round.snapshot["title"]
                        .as_str()
                        .context("PR title missing")?,
                    round.snapshot["body"].as_str(),
                )
                .await?;
                Ok(Some(
                    json!({"config_sha":config_sha,"configuration":config.raw,
                "panel":panel}),
                ))
            } else {
                Ok(None)
            }
        } else {
            Ok(None)
        }
    }
    .await;
    let (adoption, invalid_config) = match adoption {
        Ok(adoption) => (adoption, false),
        Err(error)
            if error
                .downcast_ref::<initialization::TargetConfigurationError>()
                .is_some() =>
        {
            (None, true)
        }
        Err(error) => return Err(error),
    };
    let mut work = database.begin().await?;
    record_standalone_funding_revision(
        &mut work,
        command,
        (writer || self_withdraw) && funding && !command.deleted,
    )
    .await?;
    let (status, reason) = if command.deleted {
        ("noop", "The command source was deleted.")
    } else if !writer && !self_withdraw {
        ("refused", "The actor is not a current upstream writer.")
    } else if funding {
        (
            "applied",
            "Funding choice or consent recorded for this pull request.",
        )
    } else if !command.body.trim_start().starts_with("/concurro") {
        ("noop", "Previous command removed by this edit.")
    } else if command.kind.is_none() {
        (
            "refused",
            "Supported commands: stop, retry, resume, and funding commands.",
        )
    } else if invalid_config {
        ("refused", "Current review configuration is invalid.")
    } else {
        apply_standalone_review_control(&mut work, command, adoption.as_ref()).await?
    };
    if context.rust_exchange && status == "applied" {
        record_standalone_withdrawal_steps(&mut work, &context.outbox, command).await?;
    }
    settle_standalone_review_command(&mut work, command, status, reason).await?;
    work.commit().await?;
    Ok(())
}

/// Apply pending review sources after fresh upstream writer and PR reads.
pub async fn run_standalone_review_decision(
    database: &Database,
    context: &StandaloneReviewContext,
    repository_id: i64,
    upstream: &str,
    pr_number: i64,
) -> Result<()> {
    let target = context
        .targets
        .get(&repository_id)
        .context("standalone review target unavailable")?;
    ensure!(
        upstream.eq_ignore_ascii_case(&target.source.upstream),
        "standalone review target changed"
    );
    let (owner, name) = upstream
        .split_once('/')
        .context("upstream must be owner/name")?;
    let number = pr_number.to_string();
    loop {
        let request = next_standalone_review_request(database, upstream, pr_number).await?;
        let command = next_standalone_review_command(database, upstream, pr_number).await?;
        if let Some(command) = command.as_ref() {
            let before = request.as_ref().is_none_or(|request| {
                (command.source_at, command.comment_id) <= (request.source_at, request.source_id)
            });
            if before {
                run_standalone_review_command(database, context, target, command).await?;
                continue;
            }
        }
        let Some(request) = request else {
            break;
        };
        if !standalone_review_source_is_current(database, &request).await? {
            let mut work = database.begin().await?;
            apply_standalone_review_request(
                &mut work,
                request.id,
                StandaloneReviewAuthority {
                    upstream,
                    pr_number,
                    reviewer_id: target.reviewer_id,
                    runner_repository_id: target.runner_repository_id,
                    actor_writer: false,
                    pr_open: false,
                    reviewer_requested: false,
                    source_defect: None,
                    snapshot: None,
                },
            )
            .await?;
            work.commit().await?;
            continue;
        }
        let login = context
            .github
            .public_user_login_by_id(
                upstream,
                &target.source.upstream_permissions,
                request.actor.id,
            )
            .await?;
        let writer = if let Some(login) = login {
            context
                .github
                .writer_permission(upstream, &login, &target.source.upstream_permissions)
                .await?
        } else {
            false
        };
        let pr: Value = context
            .github
            .read(
                upstream,
                &target.source.upstream_permissions,
                &["repos", owner, name, "pulls", &number],
                &[],
            )
            .await?;
        ensure!(pr["number"] == pr_number, "review PR identity changed");
        let open = pr["state"] == "open" && pr["merged"] != true;
        if let Some(existing) =
            concurro_tasks_postgres::standalone_review_task(database, upstream, pr_number).await?
            && existing["stage"] == "closed"
            && open
        {
            let task_id: Uuid = serde_json::from_value(existing["id"].clone())?;
            run_standalone_review_pr_state(database, context, task_id).await?;
        }
        let requested = pr["requested_reviewers"]
            .as_array()
            .is_some_and(|reviewers| {
                reviewers
                    .iter()
                    .any(|person| person["id"] == target.reviewer_id)
            });
        let mut source_defect = None;
        let snapshot = if request.action == "review_requested" && writer && open && requested {
            let frozen: Result<Value> = async {
                let config_sha = context
                    .github
                    .default_head(&target.source.fork, &target.source.fork_permissions)
                    .await?
                    .1;
                let config = initialization::read_target_config(
                    &context.github,
                    &target.source,
                    &config_sha,
                )
                .await?;
                let panel = pr_review_roster(&config.raw).map_err(|error| {
                    initialization::TargetConfigurationError(anyhow::Error::msg(error))
                })?;
                let base_tip = pr["base"]["sha"].as_str().context("PR base tip missing")?;
                let head = pr["head"]["sha"].as_str().context("PR head missing")?;
                let compare = format!("{base_tip}...{head}");
                let compared: Value = context
                    .github
                    .read(
                        upstream,
                        &target.source.upstream_permissions,
                        &["repos", owner, name, "compare", &compare],
                        &[],
                    )
                    .await?;
                let merge_base = compared["merge_base_commit"]["sha"]
                    .as_str()
                    .context("PR merge base missing")?;
                let files = read_pr_files(
                    &context.github,
                    upstream,
                    &target.source.upstream_permissions,
                    pr_number,
                )
                .await?;
                let paths = files
                    .iter()
                    .flat_map(|file| {
                        [
                            file["filename"].as_str(),
                            file["previous_filename"].as_str(),
                        ]
                        .into_iter()
                        .flatten()
                    })
                    .collect::<std::collections::BTreeSet<_>>();
                let current: Value = context
                    .github
                    .read(
                        upstream,
                        &target.source.upstream_permissions,
                        &["repos", owner, name, "pulls", &number],
                        &[],
                    )
                    .await?;
                ensure!(
                    current["head"]["sha"] == head && current["base"]["sha"] == base_tip,
                    "review PR changed while freezing its file inventory"
                );
                Ok(json!({
                    "pr_node":pr["node_id"], "pr_id":pr["id"],
                    "head":head,"base_ref":pr["base"]["ref"],
                    "base_tip":base_tip,"merge_base":merge_base,
                    "title":pr["title"],"body":pr["body"],"author":pr["user"]["login"],
                    "changed_paths":paths,"files":files,"panel":panel,
                    "config_sha":config_sha,"configuration":config.raw,
                    "requested_at":request.source_at,
                    "requested_by":request.actor,
                }))
            }
            .await;
            match frozen {
                Ok(snapshot) => Some(snapshot),
                Err(error)
                    if error
                        .downcast_ref::<initialization::TargetConfigurationError>()
                        .is_some() =>
                {
                    source_defect = Some("invalid_target_configuration");
                    None
                }
                Err(error) => return Err(error),
            }
        } else {
            None
        };
        let mut work = database.begin().await?;
        let applied = apply_standalone_review_request(
            &mut work,
            request.id,
            StandaloneReviewAuthority {
                upstream,
                pr_number,
                reviewer_id: target.reviewer_id,
                runner_repository_id: target.runner_repository_id,
                actor_writer: writer,
                pr_open: open,
                reviewer_requested: requested,
                source_defect,
                snapshot: snapshot.as_ref(),
            },
        )
        .await?;
        work.commit().await?;
        if !applied {
            break;
        }
    }
    Ok(())
}
