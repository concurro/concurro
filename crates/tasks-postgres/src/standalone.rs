//! Durable review-request timeline readback and standalone PR ownership.

use chrono::{DateTime, Utc};
use concurro_contracts::TaskId;
use concurro_db::{Database, UnitOfWork};
use concurro_events_postgres::Outbox;
use concurro_exchange_v1::TurnSubmission;
use concurro_tasks_core::{
    Actor, Admission, AttemptSpec, DesignReviewer, PrReviewReport, SourceOrder, markdown_text,
    pr_review_body, python_digest,
};
use serde_json::Value;
use sqlx::types::Json;
use uuid::Uuid;

use crate::{
    effects::operational_json,
    enqueue_effect, events,
    read_model::append_standalone_view,
    report_publication::record_standalone_review_report_artifact,
    sponsorship::{has_credential_exclusion, validate_standalone_funding_admission},
};

/// One delivery awaiting its matching issue-timeline event.
#[derive(Clone, Debug, sqlx::FromRow)]
pub struct StandaloneReviewWake {
    /// GitHub delivery GUID.
    pub delivery_id: String,
    /// Numeric upstream repository identity.
    pub repository_id: i64,
    /// Upstream owner/repository.
    pub upstream: String,
    /// Repository-local PR number.
    pub pr_number: i64,
    /// Requested or removed action.
    pub action: String,
    /// Numeric actor identity.
    pub actor_id: i64,
    /// Numeric requested reviewer identity.
    pub reviewer_id: i64,
    /// PR update time on the authenticated delivery.
    pub updated_at: DateTime<Utc>,
}

/// One source-ordered review-request timeline fact awaiting authority checks.
#[derive(Clone, Debug)]
pub struct StandaloneReviewRequest {
    /// Durable request identity.
    pub id: Uuid,
    /// Upstream repository identity.
    pub repository_id: i64,
    /// Upstream owner/repository.
    pub upstream: String,
    /// PR number.
    pub pr_number: i64,
    /// Timeline event identity.
    pub source_id: i64,
    /// Timeline event creation time.
    pub source_at: DateTime<Utc>,
    /// Verified timeline actor candidate.
    pub actor: Actor,
    /// Requested or removed action.
    pub action: String,
}

/// One independent PR review round with its source frozen at enrollment.
pub struct StandaloneReviewRound {
    /// Stable standalone task identity.
    pub task_id: Uuid,
    /// Numeric upstream repository identity.
    pub repository_id: i64,
    /// Configured runner target identity.
    pub runner_repository_id: i64,
    /// Upstream owner/repository.
    pub upstream: String,
    /// PR number in that repository.
    pub pr_number: i64,
    /// Numeric reviewer account identity.
    pub reviewer_id: i64,
    /// One-based review round number.
    pub number: i32,
    /// Source request that opened this round.
    pub request_id: Uuid,
    /// Current lifecycle stage.
    pub stage: String,
    /// Charged model calls across all rounds.
    pub used_model_calls: i64,
    /// Source and configuration frozen for this round.
    pub snapshot: Value,
    /// Whether a non-reviewer removal cancelled further admissions.
    pub cancelled: bool,
}

/// A raw standalone report awaiting validation against its pinned round.
pub struct StandaloneReviewResult {
    /// Current round that owns the response.
    pub round: StandaloneReviewRound,
    /// Reviewer identity frozen in that round.
    pub reviewer: DesignReviewer,
    /// Exact model turn identity.
    pub turn: Uuid,
    /// Original response body before PostgreSQL-safe projection.
    pub report: Value,
}

/// Read one completed reviewer response and its immutable source binding.
pub async fn standalone_review_result(
    database: &Database,
    turn: Uuid,
) -> anyhow::Result<Option<StandaloneReviewResult>> {
    let row: Option<(Uuid, i32, String, Vec<u8>, Vec<u8>)> = sqlx::query_as(
        "SELECT s.task_id,s.round,s.reviewer_id,s.report_raw,a.submit_body \
         FROM tasks.standalone_review_attempts s \
         JOIN tasks.attempts a ON a.id=s.attempt_id \
         WHERE s.attempt_id=$1 AND s.status='completed'",
    )
    .bind(turn)
    .fetch_optional(database.pool())
    .await?;
    let Some((task_id, number, reviewer_id, raw, submit_body)) = row else {
        return Ok(None);
    };
    let Some(round) = standalone_review_round(database, task_id).await? else {
        return Ok(None);
    };
    if round.number != number {
        return Ok(None);
    }
    let submission: TurnSubmission = serde_json::from_slice(&submit_body)?;
    let reviewer: DesignReviewer = serde_json::from_value(
        submission.payload["request"]["context"]["reviewer_identity"].clone(),
    )?;
    anyhow::ensure!(
        reviewer.id == reviewer_id,
        "standalone reviewer identity changed"
    );
    Ok(Some(StandaloneReviewResult {
        round,
        reviewer,
        turn,
        report: serde_json::from_slice(&raw)?,
    }))
}

/// Find retained reports whose result job exhausted retries or was interrupted.
pub async fn pending_standalone_review_results(
    database: &Database,
) -> Result<Vec<Uuid>, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT attempt_id FROM tasks.standalone_review_attempts \
         WHERE status='completed' ORDER BY task_id,round,reviewer_id,attempt_number",
    )
    .fetch_all(database.pool())
    .await
}

/// Read only findings that this reviewer must answer in the next assessment.
pub async fn standalone_review_outstanding_findings(
    database: &Database,
    task_id: Uuid,
    reviewer_id: &str,
) -> Result<(Vec<String>, Vec<Value>), sqlx::Error> {
    let rows: Vec<(String, Json<Value>)> = sqlx::query_as(
        "SELECT id,jsonb_build_object('id',id,'status',status, \
         'path',finding->'path','start_line',finding->'start_line', \
         'line',finding->'line','detail',finding->'detail') \
         FROM tasks.standalone_review_findings \
         WHERE task_id=$1 AND reviewer_id=$2 AND status IN ('open','withdrawn') \
         ORDER BY sequence",
    )
    .bind(task_id)
    .bind(reviewer_id)
    .fetch_all(database.pool())
    .await?;
    Ok((
        rows.iter().map(|(id, _)| id.clone()).collect(),
        rows.into_iter().map(|(_, Json(finding))| finding).collect(),
    ))
}

/// Queue the frozen reviewer-account publication after report validation.
pub(crate) async fn enqueue_standalone_review_publish(
    uow: &mut UnitOfWork<'_>,
    turn: Uuid,
) -> Result<(), sqlx::Error> {
    let key = format!("standalone-review-publish:{turn}");
    let _: i64 = sqlx::query_scalar(
        "SELECT (graphile_worker.add_job( \
         identifier => 'standalone_review_publish', \
         payload => json_build_object('turn',$1), \
         max_attempts => 25,job_key => $2)).id",
    )
    .bind(turn)
    .bind(key)
    .fetch_one(uow.connection())
    .await?;
    Ok(())
}

/// Accept an independently validated report and freeze its native review body.
pub async fn apply_standalone_review_result(
    uow: &mut UnitOfWork<'_>,
    turn: Uuid,
    validated: Result<PrReviewReport, String>,
) -> anyhow::Result<bool> {
    let Some(crate::ExecutionOwner::Standalone { task_id, .. }) =
        crate::lock_execution_owner(uow, turn).await?
    else {
        return Ok(false);
    };
    let row: Option<(i32, String, Json<Value>, i64, i64, i64, String, Vec<u8>, i32, Vec<u8>)> = sqlx::query_as(
        "SELECT a.round,a.reviewer_id,r.snapshot,t.repository_id,t.pr_number,t.reviewer_id,t.stage, \
             a.report_raw,a.attempt_number,i.submit_body \
         FROM tasks.standalone_review_attempts a \
         JOIN tasks.attempts i ON i.id=a.attempt_id \
         JOIN tasks.standalone_review_rounds r ON r.task_id=a.task_id AND r.number=a.round \
         JOIN tasks.standalone_review_tasks t ON t.id=a.task_id \
         WHERE a.attempt_id=$1 AND a.status='completed' AND t.round=a.round \
         FOR UPDATE OF a,r,t",
    )
    .bind(turn)
    .fetch_optional(uow.connection())
    .await?;
    let Some((
        round,
        reviewer_id,
        Json(source),
        repository_id,
        pr_number,
        reviewer_user_id,
        stage,
        raw,
        attempt_number,
        submit_body,
    )) = row
    else {
        return Ok(false);
    };
    if !matches!(
        stage.as_str(),
        "review" | "publishing" | "cancelled" | "stopped"
    ) {
        return Ok(false);
    }
    let report_body = serde_json::json!({
        "kind":"standalone_pr_review", "task":task_id, "round":round,
        "turn":turn, "reviewer":reviewer_id,"head":source["head"],
        "validation":if validated.is_ok() { "accepted" } else { "rejected" },
        "reason":validated.as_ref().err(),
        "report":serde_json::from_slice::<Value>(&raw)?,
    });
    record_standalone_review_report_artifact(
        uow,
        task_id,
        concurro_tasks_core::IssueKey {
            repository_id,
            number: pr_number,
        },
        turn,
        &report_body,
    )
    .await?;
    let report = match validated {
        Ok(report) => report,
        Err(error) => {
            let signature = python_digest(&serde_json::json!([
                error,
                serde_json::from_slice::<Value>(&raw)?
            ]))?;
            let previous: Option<String> = sqlx::query_scalar(
                "SELECT repair_signature FROM tasks.standalone_review_attempts \
                 WHERE task_id=$1 AND round=$2 AND reviewer_id=$3 \
                 AND attempt_number=$4",
            )
            .bind(task_id)
            .bind(round)
            .bind(&reviewer_id)
            .bind(attempt_number - 1)
            .fetch_optional(uow.connection())
            .await?
            .flatten();
            let max_repairs = source["configuration"]["max_review_repairs"]
                .as_i64()
                .unwrap_or(0);
            let current_member = source["panel"]
                .as_array()
                .is_some_and(|panel| panel.iter().any(|reviewer| reviewer["id"] == reviewer_id));
            let correctable = current_member
                && stage == "review"
                && i64::from(attempt_number) <= max_repairs
                && previous.as_deref() != Some(&signature);
            sqlx::query(
                "UPDATE tasks.standalone_review_attempts \
                 SET status=$2,failure_reason=$3,repair_signature=$4 WHERE attempt_id=$1",
            )
            .bind(turn)
            .bind(if correctable { "correcting" } else { "failed" })
            .bind(error.replace('\0', "\\u0000"))
            .bind(signature)
            .execute(uow.connection())
            .await?;
            if correctable {
                enqueue_standalone_review_round(uow, task_id, round).await?;
                return Ok(true);
            }
            if !current_member {
                finish_cancelled_round(uow, task_id, round).await?;
                if matches!(stage.as_str(), "review" | "publishing") {
                    enqueue_standalone_review_round(uow, task_id, round).await?;
                }
                return Ok(true);
            }
            if !matches!(stage.as_str(), "cancelled" | "stopped") {
                sqlx::query("UPDATE tasks.standalone_review_tasks SET stage='blocked' WHERE id=$1")
                    .bind(task_id)
                    .execute(uow.connection())
                    .await?;
                sqlx::query("UPDATE tasks.standalone_review_rounds SET status='blocked' WHERE task_id=$1 AND number=$2")
                    .bind(task_id)
                    .bind(round)
                    .execute(uow.connection())
                    .await?;
            }
            finish_cancelled_round(uow, task_id, round).await?;
            append_standalone_view(uow, task_id).await?;
            return Ok(true);
        }
    };
    let submission: TurnSubmission = serde_json::from_slice(&submit_body)?;
    let reviewer: DesignReviewer = serde_json::from_value(
        submission.payload["request"]["context"]["reviewer_identity"].clone(),
    )?;
    anyhow::ensure!(
        reviewer.id == reviewer_id,
        "reviewer identity changed before result admission"
    );
    let head = source["head"]
        .as_str()
        .ok_or_else(|| anyhow::anyhow!("review head is missing"))?;
    let mut sequence: i64 = sqlx::query_scalar(
        "SELECT COALESCE(max(sequence),0) FROM tasks.standalone_review_findings WHERE task_id=$1",
    )
    .bind(task_id)
    .fetch_one(uow.connection())
    .await?;
    for disposition in &report.dispositions {
        let thread: Option<Option<String>> = sqlx::query_scalar(
            "UPDATE tasks.standalone_review_findings SET status=$4,disposition=$5 \
             WHERE task_id=$1 AND id=$2 AND reviewer_id=$3 AND status IN ('open','withdrawn') \
             RETURNING thread_node_id",
        )
        .bind(task_id)
        .bind(&disposition.id)
        .bind(&reviewer_id)
        .bind(&disposition.status)
        .bind(Json(disposition))
        .fetch_optional(uow.connection())
        .await?;
        anyhow::ensure!(thread.is_some(), "standalone finding owner changed");
        if thread.flatten().is_some() {
            let reply_marker =
                format!("<!-- concurro:pr-disposition:{turn}:{} -->", disposition.id);
            let reply = format!(
                "{reply_marker}\n**{}**: {}",
                markdown_text(&disposition.status, 30),
                markdown_text(&disposition.evidence, 55_000)
            );
            sqlx::query(
                "INSERT INTO tasks.standalone_review_dispositions \
                 (turn_id,task_id,finding_id,marker,body,resolve_status) \
                 VALUES ($1,$2,$3,$4,$5,$6)",
            )
            .bind(turn)
            .bind(task_id)
            .bind(&disposition.id)
            .bind(reply_marker)
            .bind(reply)
            .bind(
                if matches!(disposition.status.as_str(), "fixed" | "obsolete") {
                    "recorded"
                } else {
                    "none"
                },
            )
            .execute(uow.connection())
            .await?;
        }
    }
    let mut finding_ids = Vec::with_capacity(report.findings.len());
    for finding in &report.findings {
        sequence += 1;
        let id = format!("F{sequence}");
        sqlx::query(
            "INSERT INTO tasks.standalone_review_findings \
             (task_id,sequence,id,reviewer_id,status,head,finding) \
             VALUES ($1,$2,$3,$4,'open',$5,$6)",
        )
        .bind(task_id)
        .bind(sequence)
        .bind(&id)
        .bind(&reviewer_id)
        .bind(head)
        .bind(Json(finding))
        .execute(uow.connection())
        .await?;
        finding_ids.push(id);
    }
    let marker = format!("<!-- concurro:pr-review:{task_id}:{turn} -->");
    let body = pr_review_body(&marker, &reviewer, &report, &finding_ids);
    let comments = report
        .findings
        .iter()
        .zip(&finding_ids)
        .filter_map(|(finding, id)| {
            let (path, line) = (finding.path.as_ref()?, finding.line?);
            let comment_marker = format!("<!-- concurro:pr-finding:{task_id}:{id} -->");
            let title = finding.title.as_deref().unwrap_or("Finding");
            Some(serde_json::json!({
                "finding_id":id,
                "marker":comment_marker,
                "body":format!("{comment_marker}\n**{} · {id}**\n\n{}",
                    markdown_text(title, 160), markdown_text(&finding.detail, 55_000)),
                "path":path,
                "line":line,
                "side":"RIGHT",
                "start_line":finding.start_line,
                "start_side":finding.start_line.map(|_| "RIGHT"),
            }))
        })
        .collect::<Vec<_>>();
    sqlx::query(
        "UPDATE tasks.standalone_review_attempts \
         SET status='validated',validated_report=$2 WHERE attempt_id=$1",
    )
    .bind(turn)
    .bind(Json(&report))
    .execute(uow.connection())
    .await?;
    sqlx::query(
        "INSERT INTO tasks.standalone_review_publications \
         (turn_id,task_id,round,reviewer_id,pr_number,head,marker,body,comments,reviewer_user_id) \
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
    )
    .bind(turn)
    .bind(task_id)
    .bind(round)
    .bind(&reviewer_id)
    .bind(pr_number)
    .bind(head)
    .bind(marker)
    .bind(body)
    .bind(Json(comments))
    .bind(reviewer_user_id)
    .execute(uow.connection())
    .await?;
    enqueue_standalone_review_publish(uow, turn).await?;
    append_standalone_view(uow, task_id).await?;
    Ok(true)
}

/// Queue source-aware validation of the original standalone report.
async fn enqueue_standalone_review_result(
    uow: &mut UnitOfWork<'_>,
    turn: Uuid,
) -> Result<(), sqlx::Error> {
    let key = format!("standalone-review-result:{turn}");
    let _: i64 = sqlx::query_scalar(
        "SELECT (graphile_worker.add_job( \
         identifier => 'standalone_review_result', \
         payload => json_build_object('turn',$1), \
         max_attempts => 25,job_key => $2)).id",
    )
    .bind(turn)
    .bind(key)
    .fetch_one(uow.connection())
    .await?;
    Ok(())
}

/// Load the current round before source reads and model preparation.
pub async fn standalone_review_round(
    database: &Database,
    task_id: Uuid,
) -> Result<Option<StandaloneReviewRound>, sqlx::Error> {
    let row: Option<(
        Uuid,
        i64,
        i64,
        String,
        i64,
        i64,
        i32,
        Uuid,
        String,
        i64,
        Json<Value>,
        bool,
    )> = sqlx::query_as(
        "SELECT t.id,t.repository_id,t.runner_repository_id,t.upstream,t.pr_number, \
         t.reviewer_id,r.number,r.request_id,t.stage,t.used_model_calls,r.snapshot, \
         r.cancellation_request_id IS NOT NULL \
         FROM tasks.standalone_review_tasks t \
         JOIN tasks.standalone_review_rounds r ON r.task_id=t.id AND r.number=t.round \
         WHERE t.id=$1",
    )
    .bind(task_id)
    .fetch_optional(database.pool())
    .await?;
    Ok(row.map(
        |(
            task_id,
            repository_id,
            runner_repository_id,
            upstream,
            pr_number,
            reviewer_id,
            number,
            request_id,
            stage,
            used_model_calls,
            Json(snapshot),
            cancelled,
        )| StandaloneReviewRound {
            task_id,
            repository_id,
            runner_repository_id,
            upstream,
            pr_number,
            reviewer_id,
            number,
            request_id,
            stage,
            used_model_calls,
            snapshot,
            cancelled,
        },
    ))
}

/// Read the immutable source order and actor of a round-opening request.
pub async fn standalone_review_boundary(
    database: &Database,
    request_id: Uuid,
) -> anyhow::Result<(SourceOrder, Actor)> {
    let (at, id, Json(actor)): (DateTime<Utc>, i64, Json<Actor>) = sqlx::query_as(
        "SELECT source_at,source_id,actor FROM tasks.standalone_review_requests WHERE id=$1",
    )
    .bind(request_id)
    .fetch_one(database.pool())
    .await?;
    Ok((
        SourceOrder {
            at,
            source_id: id,
            source: format!("review_request:{id}"),
            index: 0,
        },
        actor,
    ))
}

/// Watch unfinished PRs and terminal tasks with unresolved review or check writes.
pub async fn watched_standalone_review_prs(
    database: &Database,
) -> Result<Vec<(Uuid, bool)>, sqlx::Error> {
    sqlx::query_as(
        "SELECT t.id, (t.stage NOT IN ('complete','cancelled','closed') OR \
             t.stage='closed' AND COALESCE(t.closed_from_stage NOT IN ('complete','cancelled'),false)) \
         FROM tasks.standalone_review_tasks t \
         WHERE t.stage NOT IN ('complete','cancelled','closed') \
         OR t.stage='closed' AND t.closed_from_stage NOT IN ('complete','cancelled') \
         OR EXISTS (SELECT 1 FROM tasks.standalone_review_publications p \
             WHERE p.task_id=t.id AND p.status IN ('recorded','sent')) \
         OR EXISTS (SELECT 1 FROM tasks.standalone_review_checks c \
             WHERE c.task_id=t.id AND (NOT c.completed OR c.recheck_until>now())) \
         ORDER BY t.enrolled_at,t.id",
    )
    .fetch_all(database.pool())
    .await
}

/// Complete a round only when each member of its current adopted panel has published.
pub(crate) async fn standalone_panel_complete(
    uow: &mut UnitOfWork<'_>,
    task_id: Uuid,
    round: i32,
    snapshot: &Value,
) -> anyhow::Result<bool> {
    let panel: Vec<DesignReviewer> = serde_json::from_value(snapshot["panel"].clone())?;
    anyhow::ensure!(!panel.is_empty(), "standalone panel is empty");
    let ids = panel
        .into_iter()
        .map(|reviewer| reviewer.id)
        .collect::<Vec<_>>();
    let published: i64 = sqlx::query_scalar(
        "SELECT count(DISTINCT reviewer_id) FROM tasks.standalone_review_attempts \
         WHERE task_id=$1 AND round=$2 AND reviewer_id=ANY($3::text[]) \
         AND status='published'",
    )
    .bind(task_id)
    .bind(round)
    .bind(&ids)
    .fetch_one(uow.connection())
    .await?;
    Ok(usize::try_from(published)? == ids.len())
}

/// Pause a standalone round on PR closure and restore its prior stage on reopening.
pub async fn observe_standalone_review_pr_state(
    uow: &mut UnitOfWork<'_>,
    task_id: Uuid,
    open: bool,
) -> anyhow::Result<bool> {
    let row: Option<(String, Option<String>, i32)> = sqlx::query_as(
        "SELECT stage,closed_from_stage,round FROM tasks.standalone_review_tasks \
         WHERE id=$1 FOR UPDATE",
    )
    .bind(task_id)
    .fetch_optional(uow.connection())
    .await?;
    let Some((stage, previous, round)) = row else {
        return Ok(false);
    };
    if !open && stage != "closed" {
        sqlx::query(
            "UPDATE tasks.standalone_review_tasks \
             SET stage='closed',closed_from_stage=$2 WHERE id=$1",
        )
        .bind(task_id)
        .bind(&stage)
        .execute(uow.connection())
        .await?;
        sqlx::query(
            "UPDATE tasks.standalone_review_rounds SET status='closed' \
             WHERE task_id=$1 AND number=$2 AND status NOT IN ('complete','cancelled')",
        )
        .bind(task_id)
        .bind(round)
        .execute(uow.connection())
        .await?;
        append_standalone_view(uow, task_id).await?;
        return Ok(true);
    }
    if open && stage == "closed" {
        let round_state: Option<(bool, Json<Value>)> = sqlx::query_as(
            "SELECT cancellation_request_id IS NOT NULL,snapshot \
             FROM tasks.standalone_review_rounds WHERE task_id=$1 AND number=$2 FOR UPDATE",
        )
        .bind(task_id)
        .bind(round)
        .fetch_optional(uow.connection())
        .await?;
        let Some((cancelled, Json(snapshot))) = round_state else {
            return Ok(false);
        };
        let unresolved: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM tasks.standalone_review_attempts \
             WHERE task_id=$1 AND round=$2 AND status IN ('admitted','completed','validated')",
        )
        .bind(task_id)
        .bind(round)
        .fetch_one(uow.connection())
        .await?;
        let restore = if cancelled && unresolved == 0 {
            "cancelled"
        } else if standalone_panel_complete(uow, task_id, round, &snapshot).await? {
            "complete"
        } else {
            previous.as_deref().unwrap_or("complete")
        };
        sqlx::query(
            "UPDATE tasks.standalone_review_tasks \
             SET stage=$2,closed_from_stage=NULL WHERE id=$1",
        )
        .bind(task_id)
        .bind(restore)
        .execute(uow.connection())
        .await?;
        sqlx::query(
            "UPDATE tasks.standalone_review_rounds \
             SET status=$3,completed_at=CASE WHEN $3 IN ('complete','cancelled') \
                 THEN COALESCE(completed_at,now()) ELSE completed_at END \
             WHERE task_id=$1 AND number=$2 AND status='closed'",
        )
        .bind(task_id)
        .bind(round)
        .bind(restore)
        .execute(uow.connection())
        .await?;
        if matches!(restore, "queued" | "review") {
            enqueue_standalone_review_round(uow, task_id, round).await?;
        }
        append_standalone_view(uow, task_id).await?;
        return Ok(true);
    }
    Ok(false)
}

/// Find current rounds that can be resumed after worker exhaustion or grant repair.
pub async fn pending_standalone_review_rounds(
    database: &Database,
) -> Result<Vec<(Uuid, i32, i64, String)>, sqlx::Error> {
    sqlx::query_as(
        "SELECT id,round,repository_id,stage FROM tasks.standalone_review_tasks \
         WHERE stage IN ('queued','review','awaiting_credentials') \
         ORDER BY enrolled_at,id",
    )
    .fetch_all(database.pool())
    .await
}

/// Reopen a credential wait after the configured grant is usable.
pub async fn resume_standalone_review_credentials(
    uow: &mut UnitOfWork<'_>,
    task_id: Uuid,
    round: i32,
) -> anyhow::Result<bool> {
    let next: Option<String> = sqlx::query_scalar(
        "UPDATE tasks.standalone_review_tasks SET stage=CASE WHEN EXISTS ( \
             SELECT 1 FROM tasks.standalone_review_attempts a \
             WHERE a.task_id=$1 AND a.round=$2) THEN 'review' ELSE 'queued' END \
         WHERE id=$1 AND round=$2 AND stage='awaiting_credentials' \
         AND NOT EXISTS (SELECT 1 FROM tasks.standalone_review_rounds r \
             WHERE r.task_id=$1 AND r.number=$2 AND r.cancellation_request_id IS NOT NULL) \
         RETURNING stage",
    )
    .bind(task_id)
    .bind(round)
    .fetch_optional(uow.connection())
    .await?;
    let resumed = next.is_some();
    if let Some(next) = next {
        sqlx::query(
            "UPDATE tasks.standalone_review_rounds SET status=$3 \
             WHERE task_id=$1 AND number=$2 AND status='awaiting_credentials'",
        )
        .bind(task_id)
        .bind(round)
        .bind(next)
        .execute(uow.connection())
        .await?;
        append_standalone_view(uow, task_id).await?;
    }
    Ok(resumed)
}

/// Read the latest state of each reviewer seat on a pinned round.
pub async fn standalone_review_attempts(
    database: &Database,
    task_id: Uuid,
    round: i32,
) -> Result<Vec<(String, String, Uuid)>, sqlx::Error> {
    sqlx::query_as(
        "SELECT DISTINCT ON (reviewer_id) reviewer_id,status,attempt_id \
         FROM tasks.standalone_review_attempts \
         WHERE task_id=$1 AND round=$2 \
         ORDER BY reviewer_id,attempt_number DESC",
    )
    .bind(task_id)
    .bind(round)
    .fetch_all(database.pool())
    .await
}

/// Read the previous invalid report and feedback for one bounded correction.
pub async fn standalone_review_correction(
    database: &Database,
    task_id: Uuid,
    round: i32,
    reviewer_id: &str,
) -> anyhow::Result<Option<(Value, String)>> {
    let row: Option<(Vec<u8>, String)> = sqlx::query_as(
        "SELECT report_raw,failure_reason FROM tasks.standalone_review_attempts \
         WHERE task_id=$1 AND round=$2 AND reviewer_id=$3 AND status='correcting' \
         ORDER BY attempt_number DESC LIMIT 1",
    )
    .bind(task_id)
    .bind(round)
    .bind(reviewer_id)
    .fetch_optional(database.pool())
    .await?;
    row.map(|(raw, reason)| Ok((serde_json::from_slice(&raw)?, reason)))
        .transpose()
}

/// Pause model admission until the configured reviewer grant can publish reviews.
pub async fn await_standalone_review_credentials(
    uow: &mut UnitOfWork<'_>,
    task_id: Uuid,
    round: i32,
) -> anyhow::Result<bool> {
    let changed = sqlx::query(
        "UPDATE tasks.standalone_review_tasks SET stage='awaiting_credentials' \
         WHERE id=$1 AND round=$2 AND stage IN ('queued','review') \
         AND NOT EXISTS (SELECT 1 FROM tasks.standalone_review_rounds r \
             WHERE r.task_id=$1 AND r.number=$2 AND r.cancellation_request_id IS NOT NULL)",
    )
    .bind(task_id)
    .bind(round)
    .execute(uow.connection())
    .await?;
    if changed.rows_affected() > 0 {
        sqlx::query(
            "UPDATE tasks.standalone_review_rounds SET status='awaiting_credentials' \
             WHERE task_id=$1 AND number=$2",
        )
        .bind(task_id)
        .bind(round)
        .execute(uow.connection())
        .await?;
        append_standalone_view(uow, task_id).await?;
    }
    Ok(changed.rows_affected() > 0)
}

/// Queue the next reviewer decision for a stable task and round identity.
pub(crate) async fn enqueue_standalone_review_round(
    uow: &mut UnitOfWork<'_>,
    task_id: Uuid,
    round: i32,
) -> Result<(), sqlx::Error> {
    let key = format!("standalone-review-round:{task_id}:{round}");
    let queue = format!("standalone-review:{task_id}");
    let _: i64 = sqlx::query_scalar(
        "SELECT (graphile_worker.add_job( \
         identifier => 'standalone_review_round', \
         payload => json_build_object('task_id',$1,'round',$2), \
         queue_name => $3,max_attempts => 25,job_key => $4)).id",
    )
    .bind(task_id)
    .bind(round)
    .bind(queue)
    .bind(key)
    .fetch_one(uow.connection())
    .await?;
    Ok(())
}

/// Finish a cancelled round after every retained report and native write settles.
pub(crate) async fn finish_cancelled_round(
    uow: &mut UnitOfWork<'_>,
    task_id: Uuid,
    round: i32,
) -> anyhow::Result<bool> {
    let finished: Option<i32> = sqlx::query_scalar(
        "UPDATE tasks.standalone_review_rounds r \
         SET status='cancelled',completed_at=now() \
         WHERE r.task_id=$1 AND r.number=$2 AND r.cancellation_request_id IS NOT NULL \
         AND r.status NOT IN ('cancelled','complete','closed') \
         AND NOT EXISTS (SELECT 1 FROM tasks.standalone_review_attempts a \
             WHERE a.task_id=$1 AND a.round=$2 \
             AND a.status IN ('admitted','completed','validated')) \
         RETURNING number",
    )
    .bind(task_id)
    .bind(round)
    .fetch_optional(uow.connection())
    .await?;
    if finished.is_some() {
        sqlx::query(
            "UPDATE tasks.standalone_review_tasks SET stage='cancelled' \
             WHERE id=$1 AND round=$2 AND stage NOT IN ('complete','closed','cancelled')",
        )
        .bind(task_id)
        .bind(round)
        .execute(uow.connection())
        .await?;
        append_standalone_view(uow, task_id).await?;
    }
    Ok(finished.is_some())
}

/// Charge and admit one funded reviewer turn on an uncancelled pinned round.
pub async fn admit_standalone_review_attempt(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    task_id: Uuid,
    reviewer_id: &str,
    spec: &AttemptSpec,
) -> anyhow::Result<Admission> {
    let current: Option<(String, i32, i64, String, i64, Json<Value>, bool)> = sqlx::query_as(
        "SELECT t.stage,t.round,t.used_model_calls,t.upstream,t.pr_number, \
         r.snapshot,r.cancellation_request_id IS NOT NULL \
         FROM tasks.standalone_review_tasks t \
         JOIN tasks.standalone_review_rounds r ON r.task_id=t.id AND r.number=t.round \
         WHERE t.id=$1 FOR UPDATE OF t,r",
    )
    .bind(task_id)
    .fetch_optional(uow.connection())
    .await?;
    let Some((stage, round, used, upstream, pr_number, Json(snapshot), cancelled)) = current else {
        return Ok(Admission::Stale);
    };
    if spec.task != TaskId(task_id)
        || spec.expected_revision != u64::try_from(round)?
        || !matches!(stage.as_str(), "queued" | "review")
        || cancelled
    {
        return Ok(Admission::Stale);
    }
    let existing: Option<(Uuid, String, i32)> = sqlx::query_as(
        "SELECT attempt_id,status,attempt_number FROM tasks.standalone_review_attempts \
         WHERE task_id=$1 AND round=$2 AND reviewer_id=$3 \
         ORDER BY attempt_number DESC LIMIT 1",
    )
    .bind(task_id)
    .bind(round)
    .bind(reviewer_id)
    .fetch_optional(uow.connection())
    .await?;
    if let Some((existing, status, _)) = &existing {
        if *existing == spec.id {
            return Ok(Admission::Existing);
        }
        if status != "correcting" && status != "retrying" {
            return Ok(Admission::Stale);
        }
    }
    let correction_number = existing.as_ref().map_or(1, |(_, _, number)| number + 1);
    if existing
        .as_ref()
        .is_some_and(|(_, status, _)| status == "correcting")
    {
        let submission: TurnSubmission = serde_json::from_slice(&spec.submit_body)?;
        anyhow::ensure!(
            submission.payload["request"]["context"]["review_validation_feedback"].is_object(),
            "standalone reviewer correction feedback is missing"
        );
    }
    let limit = snapshot["configuration"]["max_model_calls"]
        .as_i64()
        .ok_or_else(|| anyhow::anyhow!("standalone model-call limit is missing"))?;
    if used >= limit {
        sqlx::query(
            "UPDATE tasks.standalone_review_tasks SET stage='budget_exhausted' WHERE id=$1",
        )
        .bind(task_id)
        .execute(uow.connection())
        .await?;
        sqlx::query("UPDATE tasks.standalone_review_rounds SET status='budget_exhausted' WHERE task_id=$1 AND number=$2")
            .bind(task_id)
            .bind(round)
            .execute(uow.connection())
            .await?;
        append_standalone_view(uow, task_id).await?;
        return Ok(Admission::BudgetExhausted);
    }
    let submission: TurnSubmission = serde_json::from_slice(&spec.submit_body)?;
    anyhow::ensure!(
        spec.config_sha == snapshot["config_sha"]
            && submission.turn == spec.id
            && submission.binding["repository"] == upstream
            && submission.binding["issue"] == pr_number
            && submission.binding["generation"] == round
            && submission.payload["request"]["context"]["head"] == snapshot["head"],
        "standalone attempt differs from its frozen source"
    );
    validate_standalone_funding_admission(uow, task_id, used + 1, spec).await?;
    sqlx::query("INSERT INTO tasks.staffing_groups (staffing_group,turn_id) VALUES ($1,$2) ON CONFLICT DO NOTHING")
        .bind(&spec.staffing_group)
        .bind(spec.id)
        .execute(uow.connection())
        .await?;
    let group_owner: Uuid =
        sqlx::query_scalar("SELECT turn_id FROM tasks.staffing_groups WHERE staffing_group=$1")
            .bind(&spec.staffing_group)
            .fetch_one(uow.connection())
            .await?;
    if group_owner != spec.id {
        return Ok(Admission::WaitingForGroup);
    }
    sqlx::query(
        "INSERT INTO tasks.attempts \
         (id,standalone_task_id,operation,expected_revision,staffing_group,fork,config_sha, \
          submit_body,prompt_body,initial_deadline,deadline,unbounded_running,status) \
         VALUES ($1,$2,'standalone_review',$3,$4,$5,$6,$7,$8,$9,$9,$10,'ready')",
    )
    .bind(spec.id)
    .bind(task_id)
    .bind(i64::try_from(spec.expected_revision)?)
    .bind(&spec.staffing_group)
    .bind(&spec.fork)
    .bind(&spec.config_sha)
    .bind(&spec.submit_body)
    .bind(&spec.prompt_body)
    .bind(spec.deadline)
    .bind(spec.unbounded_running)
    .execute(uow.connection())
    .await?;
    sqlx::query("INSERT INTO tasks.standalone_review_attempts (attempt_id,task_id,round,reviewer_id,attempt_number) VALUES ($1,$2,$3,$4,$5)")
        .bind(spec.id)
        .bind(task_id)
        .bind(round)
        .bind(reviewer_id)
        .bind(correction_number)
        .execute(uow.connection())
        .await?;
    let admission = &submission.payload["request"]["admission"];
    let personal = submission.binding["funding"] == "contributed";
    let payer = if personal {
        Some(
            admission["sponsor"]
                .as_str()
                .ok_or_else(|| anyhow::anyhow!("standalone sponsor is missing"))?
                .parse::<i64>()?,
        )
    } else {
        None
    };
    sqlx::query(
        "INSERT INTO tasks.standalone_review_funding_grants \
         (attempt_id,task_id,purpose,payer_id,provider,slot_epoch,slot_grant_id, \
          credential_record_id,credential_version,route) \
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
    )
    .bind(spec.id)
    .bind(task_id)
    .bind(if personal { "contributed" } else { "org" })
    .bind(payer)
    .bind(admission["sponsorship_provider"].as_str())
    .bind(admission["epoch"].as_i64())
    .bind(admission["grant_id"].as_str())
    .bind(admission["credential"]["record_id"].as_str())
    .bind(admission["credential"]["version"].as_i64())
    .bind(admission["route"].as_str())
    .execute(uow.connection())
    .await?;
    events::append_turn_submitted(uow, outbox, TaskId(task_id), spec, "review").await?;
    let effect: i64 = sqlx::query_scalar(
        "INSERT INTO tasks.effects (turn_id,kind,status) VALUES ($1,'submit','recorded') RETURNING id",
    )
    .bind(spec.id)
    .fetch_one(uow.connection())
    .await?;
    enqueue_effect(uow, effect, 0, 0).await?;
    sqlx::query("UPDATE tasks.standalone_review_tasks SET stage='review',used_model_calls=used_model_calls+1 WHERE id=$1")
        .bind(task_id)
        .execute(uow.connection())
        .await?;
    sqlx::query(
        "UPDATE tasks.standalone_review_rounds SET status='review' WHERE task_id=$1 AND number=$2",
    )
    .bind(task_id)
    .bind(round)
    .execute(uow.connection())
    .await?;
    append_standalone_view(uow, task_id).await?;
    Ok(Admission::Admitted)
}

/// Record a relevant delivery and schedule its source-timeline scan.
pub async fn enqueue_standalone_review_scan(
    uow: &mut UnitOfWork<'_>,
    wake: &StandaloneReviewWake,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO tasks.standalone_review_wakes \
         (delivery_id,repository_id,upstream,pr_number,action,actor_id,reviewer_id,updated_at) \
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT (delivery_id) DO NOTHING",
    )
    .bind(&wake.delivery_id)
    .bind(wake.repository_id)
    .bind(&wake.upstream)
    .bind(wake.pr_number)
    .bind(&wake.action)
    .bind(wake.actor_id)
    .bind(wake.reviewer_id)
    .bind(wake.updated_at)
    .execute(uow.connection())
    .await?;
    let queue = format!(
        "standalone-review:{}:{}",
        wake.repository_id, wake.pr_number
    );
    let key = format!("standalone-review-scan:{}", wake.delivery_id);
    let _: i64 = sqlx::query_scalar(
        "SELECT (graphile_worker.add_job( \
         identifier => 'standalone_review_scan', \
         payload => json_build_object('delivery_id',$1,'repository_id',$2, \
             'upstream',$3,'pr_number',$4,'action',$5,'actor_id',$6, \
             'reviewer_id',$7,'updated_at',$8), \
         queue_name => $9,max_attempts => 25,job_key => $10)).id",
    )
    .bind(&wake.delivery_id)
    .bind(wake.repository_id)
    .bind(&wake.upstream)
    .bind(wake.pr_number)
    .bind(&wake.action)
    .bind(wake.actor_id)
    .bind(wake.reviewer_id)
    .bind(wake.updated_at)
    .bind(queue)
    .bind(key)
    .fetch_one(uow.connection())
    .await?;
    Ok(())
}

/// Find due timeline scans left by exhausted or interrupted workers.
pub async fn pending_standalone_review_wakes(
    database: &Database,
) -> Result<Vec<StandaloneReviewWake>, sqlx::Error> {
    sqlx::query_as(
        "SELECT delivery_id,repository_id,upstream,pr_number,action,actor_id,reviewer_id,updated_at \
         FROM tasks.standalone_review_wakes WHERE observed_at IS NULL AND exhausted_at IS NULL \
         AND retry_at<=now() ORDER BY retry_at,delivery_id",
    )
    .fetch_all(database.pool())
    .await
}

/// Settle a wake only after its exact source appears in the timeline.
pub async fn observe_standalone_review_wake(
    database: &Database,
    delivery_id: &str,
) -> Result<(), sqlx::Error> {
    sqlx::query("UPDATE tasks.standalone_review_wakes SET observed_at=now() WHERE delivery_id=$1")
        .bind(delivery_id)
        .execute(database.pool())
        .await?;
    Ok(())
}

/// Back off a failed timeline read while retaining an exhausted diagnostic.
pub async fn defer_standalone_review_wake(
    database: &Database,
    delivery_id: &str,
    error: &str,
) -> Result<bool, sqlx::Error> {
    let exhausted: Option<bool> = sqlx::query_scalar(
        "UPDATE tasks.standalone_review_wakes SET scan_attempts=scan_attempts+1, \
         retry_at=now()+(LEAST(3600,5*power(2,LEAST(scan_attempts,10)))::integer * interval '1 second'), \
         exhausted_at=CASE WHEN scan_attempts+1>=48 THEN now() ELSE NULL END, \
         last_error=left($2,500) \
         WHERE delivery_id=$1 AND observed_at IS NULL AND exhausted_at IS NULL \
         RETURNING exhausted_at IS NOT NULL",
    )
    .bind(delivery_id)
    .bind(error)
    .fetch_optional(database.pool())
    .await?;
    Ok(exhausted.unwrap_or(false))
}

/// Add one stable reviewer-account timeline event once, regardless of delivery order.
pub async fn record_standalone_review_request(
    uow: &mut UnitOfWork<'_>,
    request: &StandaloneReviewRequest,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO tasks.standalone_review_controls (upstream,pr_number) \
         VALUES ($1,$2) ON CONFLICT DO NOTHING",
    )
    .bind(&request.upstream)
    .bind(request.pr_number)
    .execute(uow.connection())
    .await?;
    sqlx::query(
        "INSERT INTO tasks.standalone_review_requests \
         (id,repository_id,upstream,pr_number,source_id,source_at,actor,action) \
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8) \
         ON CONFLICT (upstream,pr_number,source_id) DO NOTHING",
    )
    .bind(request.id)
    .bind(request.repository_id)
    .bind(&request.upstream)
    .bind(request.pr_number)
    .bind(request.source_id)
    .bind(request.source_at)
    .bind(Json(&request.actor))
    .bind(&request.action)
    .execute(uow.connection())
    .await?;
    let queue = format!(
        "standalone-review:{}:{}",
        request.repository_id, request.pr_number
    );
    let key = format!(
        "standalone-review-decision:{}:{}",
        request.repository_id, request.pr_number
    );
    let _: i64 = sqlx::query_scalar(
        "SELECT (graphile_worker.add_job( \
         identifier => 'standalone_review_decision', \
         payload => json_build_object('repository_id',$1,'upstream',$2,'pr_number',$3), \
         queue_name => $4,max_attempts => 25,job_key => $5)).id",
    )
    .bind(request.repository_id)
    .bind(&request.upstream)
    .bind(request.pr_number)
    .bind(queue)
    .bind(key)
    .fetch_one(uow.connection())
    .await?;
    Ok(())
}

/// Read the oldest undecided review request for one PR.
pub async fn next_standalone_review_request(
    database: &Database,
    upstream: &str,
    pr_number: i64,
) -> Result<Option<StandaloneReviewRequest>, sqlx::Error> {
    let row: Option<(
        Uuid,
        i64,
        String,
        i64,
        i64,
        DateTime<Utc>,
        Json<Actor>,
        String,
    )> = sqlx::query_as(
        "SELECT id,repository_id,upstream,pr_number,source_id,source_at,actor,action \
             FROM tasks.standalone_review_requests \
             WHERE upstream=$1 AND pr_number=$2 AND status='pending' \
             ORDER BY source_at,source_id LIMIT 1",
    )
    .bind(upstream)
    .bind(pr_number)
    .fetch_optional(database.pool())
    .await?;
    Ok(row.map(
        |(id, repository_id, upstream, pr_number, source_id, source_at, Json(actor), action)| {
            StandaloneReviewRequest {
                id,
                repository_id,
                upstream,
                pr_number,
                source_id,
                source_at,
                actor,
                action,
            }
        },
    ))
}

/// Avoid external snapshot reads for a source already superseded by recorded history.
pub async fn standalone_review_source_is_current(
    database: &Database,
    request: &StandaloneReviewRequest,
) -> Result<bool, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT NOT EXISTS (SELECT 1 FROM tasks.standalone_review_requests newer \
         WHERE newer.upstream=$1 AND newer.pr_number=$2 \
         AND (newer.source_at,newer.source_id)>($3,$4)) \
         AND NOT EXISTS (SELECT 1 FROM tasks.standalone_review_controls control \
         WHERE control.upstream=$1 AND control.pr_number=$2 \
         AND (control.last_source_at,control.last_source_id)>=($3,$4))",
    )
    .bind(&request.upstream)
    .bind(request.pr_number)
    .bind(request.source_at)
    .bind(request.source_id)
    .fetch_one(database.pool())
    .await
}

/// Find PRs with undecided sources after a decision worker has stopped retrying.
pub async fn pending_standalone_review_issues(
    database: &Database,
) -> Result<Vec<(i64, String, i64)>, sqlx::Error> {
    sqlx::query_as(
        "SELECT DISTINCT repository_id,upstream,pr_number \
         FROM tasks.standalone_review_requests WHERE status='pending' \
         ORDER BY repository_id,upstream,pr_number",
    )
    .fetch_all(database.pool())
    .await
}

/// Apply the first pending source under the PR lock, preserving its unique owner.
pub async fn apply_standalone_review_request(
    uow: &mut UnitOfWork<'_>,
    candidate: Uuid,
    upstream: &str,
    pr_number: i64,
    reviewer_id: i64,
    runner_repository_id: i64,
    actor_writer: bool,
    pr_open: bool,
    reviewer_requested: bool,
    source_defect: Option<&str>,
    snapshot: Option<&Value>,
) -> anyhow::Result<bool> {
    let control: Option<(Option<DateTime<Utc>>, Option<i64>)> = sqlx::query_as(
        "SELECT last_source_at,last_source_id FROM tasks.standalone_review_controls \
         WHERE upstream=$1 AND pr_number=$2 FOR UPDATE",
    )
    .bind(upstream)
    .bind(pr_number)
    .fetch_optional(uow.connection())
    .await?;
    let Some((last_at, last_id)) = control else {
        return Ok(false);
    };
    let next: Option<(Uuid, DateTime<Utc>, i64, Json<Actor>, String, i64)> = sqlx::query_as(
        "SELECT id,source_at,source_id,actor,action,repository_id \
         FROM tasks.standalone_review_requests WHERE upstream=$1 AND pr_number=$2 \
         AND status='pending' ORDER BY source_at,source_id LIMIT 1 FOR UPDATE",
    )
    .bind(upstream)
    .bind(pr_number)
    .fetch_optional(uow.connection())
    .await?;
    let Some((id, at, source_id, Json(actor), action, repository_id)) = next else {
        return Ok(false);
    };
    if id != candidate {
        return Ok(false);
    }
    let existing: Option<(Uuid, String, i32, i64)> = sqlx::query_as(
        "SELECT id,stage,round,reviewer_id FROM tasks.standalone_review_tasks \
         WHERE upstream=$1 AND pr_number=$2 FOR UPDATE",
    )
    .bind(upstream)
    .bind(pr_number)
    .fetch_optional(uow.connection())
    .await?;
    let newer_source: Option<(DateTime<Utc>, i64)> = sqlx::query_as(
        "SELECT source_at,source_id FROM tasks.standalone_review_requests \
         WHERE upstream=$1 AND pr_number=$2 ORDER BY source_at DESC,source_id DESC LIMIT 1",
    )
    .bind(upstream)
    .bind(pr_number)
    .fetch_optional(uow.connection())
    .await?;
    let (status, reason, round) = if last_at
        .zip(last_id)
        .is_some_and(|prior| (at, source_id) <= prior)
        || newer_source.is_some_and(|latest| (at, source_id) < latest)
    {
        ("superseded", Some("superseded_by_newer_request"), None)
    } else if action == "review_requested" {
        if !actor_writer {
            ("refused", Some("not_writer"), None)
        } else if !pr_open {
            ("refused", Some("pr_closed"), None)
        } else if !reviewer_requested {
            ("refused", Some("review_request_removed"), None)
        } else if let Some(defect) = source_defect {
            ("refused", Some(defect), None)
        } else if existing.as_ref().is_some_and(|row| row.3 != reviewer_id) {
            ("refused", Some("reviewer_changed"), None)
        } else if let Some((task, stage, current_round, _)) = existing.as_ref() {
            let cancelled: bool = sqlx::query_scalar(
                "SELECT cancellation_request_id IS NOT NULL \
                 FROM tasks.standalone_review_rounds WHERE task_id=$1 AND number=$2",
            )
            .bind(task)
            .bind(current_round)
            .fetch_one(uow.connection())
            .await?;
            if cancelled
                && stage != "cancelled"
                && !finish_cancelled_round(uow, *task, *current_round).await?
            {
                return Ok(false);
            }
            if matches!(stage.as_str(), "complete" | "cancelled") || cancelled {
                let frozen =
                    snapshot.ok_or_else(|| anyhow::anyhow!("review snapshot is missing"))?;
                let new_round = current_round + 1;
                sqlx::query(
                    "UPDATE tasks.standalone_review_tasks SET stage='queued',round=$2 WHERE id=$1",
                )
                .bind(task)
                .bind(new_round)
                .execute(uow.connection())
                .await?;
                sqlx::query(
                    "INSERT INTO tasks.standalone_review_rounds \
                     (task_id,number,request_id,snapshot) VALUES ($1,$2,$3,$4)",
                )
                .bind(task)
                .bind(new_round)
                .bind(id)
                .bind(Json(frozen))
                .execute(uow.connection())
                .await?;
                enqueue_standalone_review_round(uow, *task, new_round).await?;
                ("applied", None, Some(new_round))
            } else if matches!(
                stage.as_str(),
                "queued"
                    | "review"
                    | "publishing"
                    | "blocked"
                    | "stopped"
                    | "awaiting_credentials"
                    | "budget_exhausted"
                    | "closed"
            ) {
                (
                    "applied",
                    Some("joined_current_round"),
                    Some(*current_round),
                )
            } else {
                ("refused", Some("round_not_open"), None)
            }
        } else {
            let frozen = snapshot.ok_or_else(|| anyhow::anyhow!("review snapshot is missing"))?;
            let task = Uuid::now_v7();
            let reserved: Option<Uuid> = sqlx::query_scalar(
                "INSERT INTO tasks.pr_ownership (upstream,pr_number,owner_kind,owner_id) \
                 VALUES (lower($1),$2,'standalone_pr_review',$3) \
                 ON CONFLICT (upstream,pr_number) DO NOTHING RETURNING owner_id",
            )
            .bind(upstream)
            .bind(pr_number)
            .bind(task)
            .fetch_optional(uow.connection())
            .await?;
            if reserved.is_none() {
                ("refused", Some("pr_owned_elsewhere"), None)
            } else {
                sqlx::query(
                    "INSERT INTO tasks.standalone_review_tasks \
                     (id,repository_id,runner_repository_id,upstream,pr_number,reviewer_id,stage,round) \
                     VALUES ($1,$2,$3,$4,$5,$6,'queued',1)",
                )
                .bind(task)
                .bind(repository_id)
                .bind(runner_repository_id)
                .bind(upstream)
                .bind(pr_number)
                .bind(reviewer_id)
                .execute(uow.connection())
                .await?;
                sqlx::query(
                    "INSERT INTO tasks.standalone_review_rounds \
                     (task_id,number,request_id,snapshot) VALUES ($1,1,$2,$3)",
                )
                .bind(task)
                .bind(id)
                .bind(Json(frozen))
                .execute(uow.connection())
                .await?;
                enqueue_standalone_review_round(uow, task, 1).await?;
                ("applied", None, Some(1))
            }
        }
    } else if let Some((task, stage, current_round, _)) = existing.as_ref() {
        if actor.id != reviewer_id
            && matches!(
                stage.as_str(),
                "queued"
                    | "review"
                    | "publishing"
                    | "blocked"
                    | "stopped"
                    | "awaiting_credentials"
                    | "budget_exhausted"
                    | "closed"
            )
        {
            sqlx::query(
                "UPDATE tasks.standalone_review_rounds \
                 SET cancellation_request_id=$3,cancelled_at=$4,cancelled_by=$5 \
                 WHERE task_id=$1 AND number=$2 AND cancellation_request_id IS NULL",
            )
            .bind(task)
            .bind(current_round)
            .bind(id)
            .bind(at)
            .bind(Json(&actor))
            .execute(uow.connection())
            .await?;
            finish_cancelled_round(uow, *task, *current_round).await?;
        }
        ("applied", None, Some(*current_round))
    } else {
        ("applied", None, None)
    };
    sqlx::query(
        "UPDATE tasks.standalone_review_requests SET status=$2,reason=$3,round=$4,decided_at=now() \
         WHERE id=$1",
    )
    .bind(id)
    .bind(status)
    .bind(reason)
    .bind(round)
    .execute(uow.connection())
    .await?;
    if status != "superseded" {
        sqlx::query(
            "UPDATE tasks.standalone_review_controls \
             SET last_source_at=$3,last_source_id=$4 WHERE upstream=$1 AND pr_number=$2",
        )
        .bind(upstream)
        .bind(pr_number)
        .bind(at)
        .bind(source_id)
        .execute(uow.connection())
        .await?;
    }
    if let Some(task_id) = sqlx::query_scalar::<_, Uuid>(
        "SELECT id FROM tasks.standalone_review_tasks WHERE upstream=$1 AND pr_number=$2",
    )
    .bind(upstream)
    .bind(pr_number)
    .fetch_optional(uow.connection())
    .await?
    {
        append_standalone_view(uow, task_id).await?;
    }
    Ok(true)
}

/// Return the current standalone state for operator and decision reads.
pub async fn standalone_review_task(
    database: &Database,
    upstream: &str,
    pr_number: i64,
) -> Result<Option<Value>, sqlx::Error> {
    let row: Option<(Uuid, String, i32, i64)> = sqlx::query_as(
        "SELECT id,stage,round,used_model_calls FROM tasks.standalone_review_tasks \
         WHERE upstream=$1 AND pr_number=$2",
    )
    .bind(upstream)
    .bind(pr_number)
    .fetch_optional(database.pool())
    .await?;
    Ok(row.map(|(id, stage, round, used_model_calls)| {
        serde_json::json!({"id":id,"upstream":upstream,"pr_number":pr_number,
            "stage":stage,"round":round,"used_model_calls":used_model_calls})
    }))
}

/// Resolve one standalone task's upstream repository identity for gap refresh.
pub async fn standalone_task_repository_id(
    database: &Database,
    task_id: Uuid,
) -> Result<i64, sqlx::Error> {
    sqlx::query_scalar("SELECT repository_id FROM tasks.standalone_review_tasks WHERE id=$1")
        .bind(task_id)
        .fetch_one(database.pool())
        .await
}

/// Retain the first terminal reviewer report or failure on its own round.
pub(crate) async fn settle_standalone_review_attempt(
    uow: &mut UnitOfWork<'_>,
    task_id: Uuid,
    turn: Uuid,
) -> anyhow::Result<()> {
    let seat: Option<(i32, String)> = sqlx::query_as(
        "SELECT round,status FROM tasks.standalone_review_attempts \
         WHERE attempt_id=$1 AND task_id=$2 FOR UPDATE",
    )
    .bind(turn)
    .bind(task_id)
    .fetch_optional(uow.connection())
    .await?;
    let Some((round, seat_status)) = seat else {
        anyhow::bail!("standalone attempt has no owning round: {turn}");
    };
    if seat_status != "admitted" {
        return Ok(());
    }
    let (status, result, raw_result, reason): (
        String,
        Option<Json<Value>>,
        Option<Vec<u8>>,
        Option<String>,
    ) = sqlx::query_as(
        "SELECT status,result,result_raw,outcome_reason FROM tasks.attempts WHERE id=$1 FOR UPDATE",
    )
    .bind(turn)
    .fetch_one(uow.connection())
    .await?;
    if !matches!(status.as_str(), "finished" | "unknown" | "cancelled") {
        return Ok(());
    }
    if status == "finished"
        && reason.is_none()
        && result
            .as_ref()
            .is_some_and(|value| value.0["outcome"] == "ok")
    {
        let result: Value = raw_result.map_or_else(
            || Ok(result.expect("successful result checked").0),
            |raw| serde_json::from_slice(&raw),
        )?;
        let report = result.get("response").cloned().unwrap_or(Value::Null);
        let raw = serde_json::to_vec(&report)?;
        let mut projected = report;
        operational_json(&mut projected);
        sqlx::query(
            "UPDATE tasks.standalone_review_attempts \
             SET status='completed',report=$3,report_raw=$4 \
             WHERE attempt_id=$1 AND task_id=$2 AND status='admitted'",
        )
        .bind(turn)
        .bind(task_id)
        .bind(Json(projected))
        .bind(raw)
        .execute(uow.connection())
        .await?;
        enqueue_standalone_review_result(uow, turn).await?;
    } else {
        let failure =
            reason.unwrap_or_else(|| format!("reviewer turn ended with status: {status}"));
        let retry_credential = status == "finished" && has_credential_exclusion(uow, turn).await?;
        sqlx::query(
            "UPDATE tasks.standalone_review_attempts \
             SET status=$4,failure_reason=$3 \
             WHERE attempt_id=$1 AND task_id=$2 AND status='admitted'",
        )
        .bind(turn)
        .bind(task_id)
        .bind(&failure)
        .bind(if retry_credential {
            "retrying"
        } else {
            "failed"
        })
        .execute(uow.connection())
        .await?;
        if retry_credential {
            enqueue_standalone_review_round(uow, task_id, round).await?;
        } else {
            sqlx::query(
                "UPDATE tasks.standalone_review_rounds SET status='blocked' \
             WHERE task_id=$1 AND number=$2 AND status='review'",
            )
            .bind(task_id)
            .bind(round)
            .execute(uow.connection())
            .await?;
            sqlx::query(
                "UPDATE tasks.standalone_review_tasks SET stage='blocked' \
             WHERE id=$1 AND round=$2 AND stage='review'",
            )
            .bind(task_id)
            .bind(round)
            .execute(uow.connection())
            .await?;
        }
    }
    finish_cancelled_round(uow, task_id, round).await?;
    append_standalone_view(uow, task_id).await?;
    Ok(())
}
