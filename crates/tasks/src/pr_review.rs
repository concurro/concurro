//! Published-head verification and atomic independent reviewer admission.

use anyhow::{Context, Result, ensure};
use concurro_db::Database;
use concurro_tasks_core::{Admission, IssueKey, Stage, validate_pr_review};
use concurro_tasks_postgres::{
    admit_pr_review_batch, admit_pr_review_correction, apply_pr_review_result,
    block_pr_review_batch, finish_unapplied_result, pr_review_batch, pr_review_correction,
    pr_review_result, task,
};
use graphile_worker::{IntoTaskHandlerResult, TaskHandler, WorkerContext};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use uuid::Uuid;

use crate::{
    ArchiveContext, GitPublicationContext, IntakeContext,
    funding::fund_or_block_attempt,
    git_publication::pr_paths,
    initialization::read_source_files,
    plan_payload::prepare_retained_attempt,
    pr_review_payload::{prepare_pr_review_attempt, review_change},
};

/// A queued independent reviewer roster for one immutable published PR head.
#[derive(Deserialize, Serialize)]
pub struct TaskPrReviewBatchJob {
    /// Numeric GitHub repository identity.
    pub repository_id: i64,
    /// Repository-local issue number.
    pub issue_number: i64,
    /// Task revision when publication completed.
    pub revision: u64,
    /// Durable review batch identity.
    pub batch: Uuid,
}

/// A queued source-validation pass for one completed independent PR review.
#[derive(Deserialize, Serialize)]
pub struct TaskPrReviewResultJob {
    /// Completed reviewer turn identity.
    pub turn: Uuid,
}

/// A queued private repair or retained-session restart for one reviewer seat.
#[derive(Deserialize, Serialize)]
pub struct TaskPrReviewCorrectionJob {
    /// Review batch containing the original seat.
    pub batch: Uuid,
    /// Pinned reviewer identity.
    pub reviewer: String,
    /// Correction or failure generation.
    pub count: i32,
}

impl TaskHandler for TaskPrReviewCorrectionJob {
    const IDENTIFIER: &'static str = "task_pr_review_correction";

    /// Recheck the published head before charging one replacement turn.
    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let intake = context
            .get_ext::<IntakeContext>()
            .ok_or("PR review intake context unavailable")?;
        let publication = context
            .get_ext::<GitPublicationContext>()
            .ok_or("PR publication context unavailable")?;
        let archive = context
            .get_ext::<ArchiveContext>()
            .ok_or("PR review archive context unavailable")?;
        let database = Database::from_pool(context.pg_pool().clone());
        run_task_pr_review_correction(
            &database,
            intake,
            publication,
            archive,
            self.batch,
            &self.reviewer,
            self.count,
        )
        .await
        .map_err(|error| error.to_string())
    }
}

impl TaskHandler for TaskPrReviewResultJob {
    const IDENTIFIER: &'static str = "task_pr_review_result";

    /// Validate the original report against the immutable published source.
    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let intake = context
            .get_ext::<IntakeContext>()
            .ok_or("PR review intake context unavailable")?;
        let publication = context
            .get_ext::<GitPublicationContext>()
            .ok_or("PR publication context unavailable")?;
        let database = Database::from_pool(context.pg_pool().clone());
        run_task_pr_review_result(&database, intake, publication, self.turn)
            .await
            .map_err(|error| error.to_string())
    }
}

impl TaskHandler for TaskPrReviewBatchJob {
    const IDENTIFIER: &'static str = "task_pr_review_batch";

    /// Admit only a batch whose PR still points at the recorded commit.
    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let intake = context
            .get_ext::<IntakeContext>()
            .ok_or("PR review intake context unavailable")?;
        let publication = context
            .get_ext::<GitPublicationContext>()
            .ok_or("PR publication context unavailable")?;
        let archive = context
            .get_ext::<ArchiveContext>()
            .ok_or("PR review archive context unavailable")?;
        let database = Database::from_pool(context.pg_pool().clone());
        run_task_pr_review_batch(
            &database,
            intake,
            publication,
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

/// Recheck the published PR and fund its entire pinned reviewer roster.
pub async fn run_task_pr_review_batch(
    database: &Database,
    intake: &IntakeContext,
    publication: &GitPublicationContext,
    archive: &ArchiveContext,
    issue: IssueKey,
    revision: u64,
    batch_id: Uuid,
) -> Result<()> {
    let Some(current) = task(database, issue).await? else {
        return Ok(());
    };
    if current.stage != Stage::Review || current.revision != revision {
        return Ok(());
    }
    let Some(batch) = pr_review_batch(database, batch_id).await? else {
        return Ok(());
    };
    ensure!(
        batch.task_id == current.id.0 && batch.issue == issue,
        "PR review batch belongs to another task"
    );
    let target = intake
        .initialization
        .targets
        .get(&issue.repository_id)
        .context("PR review target unavailable")?;
    let published = publication
        .targets
        .get(&issue.repository_id)
        .context("published PR target unavailable")?;
    let (owner, repository) = published
        .upstream
        .split_once('/')
        .context("upstream must be owner/name")?;
    let number = batch.pr_number.to_string();
    let pr: Value = publication
        .github
        .read(
            &published.upstream,
            &published.upstream_write_permissions,
            &["repos", owner, repository, "pulls", &number],
            &[],
        )
        .await?;
    if pr["number"] != batch.pr_number
        || pr["head"]["sha"] != batch.head
        || pr["head"]["ref"] != batch.branch
        || pr["head"]["repo"]["full_name"]
            .as_str()
            .is_none_or(|name| !name.eq_ignore_ascii_case(&published.fork))
        || pr["base"]["ref"] != batch.snapshot.configuration["base"]
        || pr["user"]["login"] != published.bot_login
    {
        let mut work = database.begin().await?;
        block_pr_review_batch(
            &mut work,
            &intake.initialization.outbox,
            issue,
            batch_id,
            "published PR identity changed before reviewer admission",
        )
        .await?;
        work.commit().await?;
        return Ok(());
    }
    let paths = pr_paths(publication, published, batch.pr_number).await?;
    if paths.iter().cloned().collect::<Vec<_>>() != batch.changed_paths {
        let mut work = database.begin().await?;
        block_pr_review_batch(
            &mut work,
            &intake.initialization.outbox,
            issue,
            batch_id,
            "published PR file inventory changed before reviewer admission",
        )
        .await?;
        work.commit().await?;
        return Ok(());
    }
    let before = read_source_files(
        &intake.initialization.github,
        &target.upstream,
        &target.upstream_permissions,
        &batch.snapshot.pins.base_sha,
        &batch.changed_paths,
    )
    .await?
    .map_err(anyhow::Error::msg)?;
    let after = read_source_files(
        &intake.initialization.github,
        &target.fork,
        &target.fork_permissions,
        &batch.head,
        &batch.changed_paths,
    )
    .await?
    .map_err(anyhow::Error::msg)?;
    let changes = batch
        .changed_paths
        .iter()
        .map(|path| review_change(path, before[path].as_deref(), after[path].as_deref()))
        .collect::<Vec<_>>();
    let expected = intake
        .initialization
        .github
        .runner_claims(
            &target.fork,
            &target.fork_permissions,
            &intake.transport.workflow_ref,
            &target.bot_login,
        )
        .await?;
    let now = chrono::Utc::now();
    let mut specs = Vec::with_capacity(batch.roster.len());
    for reviewer in &batch.roster {
        let own_findings = batch
            .own_findings
            .get(&reviewer.id)
            .map(Vec::as_slice)
            .unwrap_or(&[]);
        let session_epoch = *batch
            .session_epochs
            .get(&reviewer.id)
            .context("PR reviewer session identity missing")?;
        let provider = reviewer
            .agent
            .as_ref()
            .and_then(|agent| agent["harness"].as_str())
            .unwrap_or(&reviewer.provider);
        let retained = prepare_retained_attempt(
            database,
            archive,
            &intake.initialization.outbox,
            &current,
            &batch.snapshot,
            "review",
            provider,
            &reviewer.model,
            Some(&reviewer.id),
            session_epoch,
        )
        .await?;
        let mut spec = prepare_pr_review_attempt(
            issue,
            &current,
            target,
            &intake.transport,
            expected.clone(),
            &batch,
            reviewer,
            &changes,
            pr["body"].as_str().unwrap_or(""),
            &batch.prior_outcomes,
            own_findings,
            None,
            &retained,
            Uuid::now_v7(),
            now,
        )?;
        if !fund_or_block_attempt(
            database,
            &intake.initialization,
            &current,
            &batch.snapshot,
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
    let mut work = database.begin().await?;
    let admission = admit_pr_review_batch(
        &mut work,
        &intake.initialization.outbox,
        issue,
        batch_id,
        revision,
        &specs,
    )
    .await?;
    work.commit().await?;
    if admission == Admission::WaitingForGroup {
        anyhow::bail!("PR reviewer staffing group is still retiring");
    }
    Ok(())
}

/// Prepare only the rejected reviewer's replacement against the unchanged PR head.
pub async fn run_task_pr_review_correction(
    database: &Database,
    intake: &IntakeContext,
    publication: &GitPublicationContext,
    archive: &ArchiveContext,
    batch_id: Uuid,
    reviewer_id: &str,
    count: i32,
) -> Result<()> {
    let Some(correction) = pr_review_correction(database, batch_id, reviewer_id, count).await?
    else {
        return Ok(());
    };
    let batch = &correction.batch;
    let Some(current) = task(database, batch.issue).await? else {
        return Ok(());
    };
    if current.id.0 != batch.task_id || current.stage != Stage::Review {
        return Ok(());
    }
    let published = publication
        .targets
        .get(&batch.issue.repository_id)
        .context("PR review publication target unavailable")?;
    let target = intake
        .initialization
        .targets
        .get(&batch.issue.repository_id)
        .context("PR review source target unavailable")?;
    let (owner, repository) = published
        .upstream
        .split_once('/')
        .context("upstream must be owner/name")?;
    let pr: Value = publication
        .github
        .read(
            &published.upstream,
            &published.upstream_write_permissions,
            &[
                "repos",
                owner,
                repository,
                "pulls",
                &batch.pr_number.to_string(),
            ],
            &[],
        )
        .await?;
    if pr["number"] != batch.pr_number
        || pr["head"]["sha"] != batch.head
        || pr["head"]["ref"] != batch.branch
        || pr["head"]["repo"]["full_name"]
            .as_str()
            .is_none_or(|name| !name.eq_ignore_ascii_case(&published.fork))
        || pr["base"]["ref"] != batch.snapshot.configuration["base"]
        || pr["user"]["login"] != published.bot_login
        || pr_paths(publication, published, batch.pr_number)
            .await?
            .iter()
            .cloned()
            .collect::<Vec<_>>()
            != batch.changed_paths
    {
        let mut work = database.begin().await?;
        block_pr_review_batch(
            &mut work,
            &intake.initialization.outbox,
            batch.issue,
            batch_id,
            "published PR changed before reviewer correction",
        )
        .await?;
        work.commit().await?;
        return Ok(());
    }
    let before = read_source_files(
        &intake.initialization.github,
        &target.upstream,
        &target.upstream_permissions,
        &batch.snapshot.pins.base_sha,
        &batch.changed_paths,
    )
    .await?
    .map_err(anyhow::Error::msg)?;
    let after = read_source_files(
        &intake.initialization.github,
        &target.fork,
        &target.fork_permissions,
        &batch.head,
        &batch.changed_paths,
    )
    .await?
    .map_err(anyhow::Error::msg)?;
    let changes = batch
        .changed_paths
        .iter()
        .map(|path| review_change(path, before[path].as_deref(), after[path].as_deref()))
        .collect::<Vec<_>>();
    let expected = intake
        .initialization
        .github
        .runner_claims(
            &target.fork,
            &target.fork_permissions,
            &intake.transport.workflow_ref,
            &target.bot_login,
        )
        .await?;
    let provider = correction
        .reviewer
        .agent
        .as_ref()
        .and_then(|agent| agent["harness"].as_str())
        .unwrap_or(&correction.reviewer.provider);
    let retained = prepare_retained_attempt(
        database,
        archive,
        &intake.initialization.outbox,
        &current,
        &batch.snapshot,
        "review",
        provider,
        &correction.reviewer.model,
        Some(reviewer_id),
        *batch
            .session_epochs
            .get(reviewer_id)
            .context("PR reviewer session generation missing")?,
    )
    .await?;
    let own_findings = batch
        .own_findings
        .get(reviewer_id)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let feedback = (!correction.retry).then_some((&correction.report, correction.reason.as_str()));
    let mut spec = prepare_pr_review_attempt(
        batch.issue,
        &current,
        target,
        &intake.transport,
        expected,
        batch,
        &correction.reviewer,
        &changes,
        pr["body"].as_str().unwrap_or(""),
        &batch.prior_outcomes,
        own_findings,
        feedback,
        &retained,
        Uuid::now_v7(),
        chrono::Utc::now(),
    )?;
    if !fund_or_block_attempt(
        database,
        &intake.initialization,
        &current,
        &batch.snapshot,
        target,
        current.used_model_calls + 1,
        &mut spec,
    )
    .await?
    {
        return Ok(());
    }
    let mut work = database.begin().await?;
    let admission = admit_pr_review_correction(
        &mut work,
        &intake.initialization.outbox,
        batch.issue,
        batch_id,
        reviewer_id,
        count,
        &spec,
    )
    .await?;
    work.commit().await?;
    if admission == Admission::WaitingForGroup {
        anyhow::bail!("PR reviewer staffing group is still retiring");
    }
    Ok(())
}

/// Check a completed review's PR head and cited source, then apply its owner-bound findings.
pub async fn run_task_pr_review_result(
    database: &Database,
    intake: &IntakeContext,
    publication: &GitPublicationContext,
    turn: Uuid,
) -> Result<()> {
    let Some(result) = pr_review_result(database, turn).await? else {
        let mut work = database.begin().await?;
        finish_unapplied_result(&mut work, &intake.initialization.outbox, turn).await?;
        work.commit().await?;
        return Ok(());
    };
    let published = publication
        .targets
        .get(&result.issue.repository_id)
        .context("PR review publication target unavailable")?;
    let (owner, repository) = published
        .upstream
        .split_once('/')
        .context("upstream must be owner/name")?;
    let number = result.pr_number.to_string();
    let pr: Value = publication
        .github
        .read(
            &published.upstream,
            &published.upstream_write_permissions,
            &["repos", owner, repository, "pulls", &number],
            &[],
        )
        .await?;
    if pr["head"]["sha"] != result.head {
        let mut work = database.begin().await?;
        block_pr_review_batch(
            &mut work,
            &intake.initialization.outbox,
            result.issue,
            result.batch_id,
            "PR head moved before review result validation",
        )
        .await?;
        finish_unapplied_result(&mut work, &intake.initialization.outbox, turn).await?;
        work.commit().await?;
        return Ok(());
    }
    let mut validated = validate_pr_review(result.report, &result.open_ids, &result.withdrawn_ids);
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
            let target = intake
                .initialization
                .targets
                .get(&result.issue.repository_id)
                .context("PR review source target unavailable")?;
            let source = read_source_files(
                &intake.initialization.github,
                &target.fork,
                &target.fork_permissions,
                &result.head,
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
    let applied =
        apply_pr_review_result(&mut work, &intake.initialization.outbox, turn, validated).await?;
    if !applied {
        finish_unapplied_result(&mut work, &intake.initialization.outbox, turn).await?;
    }
    work.commit().await?;
    Ok(())
}
