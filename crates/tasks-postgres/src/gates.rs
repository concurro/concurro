//! Pinned PR gate inputs and one durable ready-for-review intent.

use anyhow::{Result, ensure};
use concurro_db::{Database, UnitOfWork};
use concurro_events_postgres::Outbox;
use concurro_tasks_core::{InitializationSnapshot, IssueKey, Stage, TaskState};
use serde_json::Value;
use sqlx::types::Json;
use uuid::Uuid;

use crate::{append_task_snapshot, queue_task_comment};

/// Columns read by `gate_inputs`.
#[derive(sqlx::FromRow)]
struct GateInputsRow {
    /// Persisted state.
    state: Json<TaskState>,
    /// Persisted snapshot.
    snapshot: Json<InitializationSnapshot>,
    /// Persisted batch id.
    batch_id: Uuid,
    /// Persisted pr number.
    pr_number: i64,
    /// Persisted head.
    head: String,
    /// Persisted branch.
    branch: String,
    /// Persisted changed paths.
    changed_paths: Json<Vec<String>>,
    /// Persisted plan report.
    plan_report: Vec<u8>,
}

/// Approved PR head and governance inputs for one current readiness observation.
pub struct GateInputs {
    /// Current task state.
    pub state: TaskState,
    /// Immutable task configuration and base.
    pub snapshot: InitializationSnapshot,
    /// PR review batch that approved this head.
    pub batch_id: Uuid,
    /// Upstream PR number.
    pub pr_number: i64,
    /// Exact current reviewed commit.
    pub head: String,
    /// Task-owned fork branch.
    pub branch: String,
    /// Approved changed paths.
    pub changed_paths: Vec<String>,
    /// Writer-approved plan.
    pub plan: Value,
    /// Any finding still open or withdrawn.
    pub unresolved_findings: bool,
    /// Whether a ready PR was ever observed for this task.
    pub ready_seen: bool,
    /// Current ready intent status for this head, when present.
    pub intent_status: Option<String>,
    /// PR number, base and test-merge identity frozen before a ready write.
    pub intent_identity: Option<(i64, String, Option<String>)>,
}

/// Latest published PR identity available before or during the ready gate.
pub struct TerminalPrInputs {
    /// Current task state and revision to fence the observation.
    pub state: TaskState,
    /// Upstream PR number.
    pub pr_number: i64,
    /// Latest published head known to the task.
    pub head: String,
    /// Task-owned fork branch.
    pub branch: String,
    /// Pinned upstream base branch.
    pub base: String,
}

/// Record a human PR closure or merge against the exact published head.
#[allow(clippy::too_many_arguments)]
pub async fn record_terminal_pr(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    issue: IssueKey,
    expected_revision: u64,
    pr_number: i64,
    published_head: &str,
    identity_ok: bool,
    pr: &Value,
) -> Result<bool> {
    sqlx::query(
        "SELECT 1 FROM tasks.issue_controls WHERE repository_id=$1 AND issue_number=$2 FOR UPDATE",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_optional(uow.connection())
    .await?;
    let current: Option<Json<TaskState>> = sqlx::query_scalar(
        "SELECT state FROM tasks.task_state WHERE repository_id=$1 AND issue_number=$2 FOR UPDATE",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_optional(uow.connection())
    .await?;
    let Some(Json(mut state)) = current else {
        return Ok(false);
    };
    if !matches!(
        state.stage,
        Stage::Review
            | Stage::Implement
            | Stage::Plan
            | Stage::PlanQc
            | Stage::Planned
            | Stage::Clarify
            | Stage::Publish
            | Stage::Gates
            | Stage::Ready
    ) || state.revision != expected_revision
        || pr["state"] != "closed"
    {
        return Ok(false);
    }
    let bound: Option<(i64, String)> = sqlx::query_as(
        "SELECT pr_number,head FROM tasks.pr_review_batches WHERE task_id=$1 \
         AND status IN ('recorded','admitted','settled') \
         ORDER BY round DESC,id DESC LIMIT 1",
    )
    .bind(state.id.0)
    .fetch_optional(uow.connection())
    .await?;
    if bound
        .as_ref()
        .is_none_or(|(number, head)| *number != pr_number || head != published_head)
    {
        return Ok(false);
    }
    let observed_head = pr["head"]["sha"].as_str().unwrap_or_default();
    let merged = pr["merged"] == true;
    let merge_sha = pr["merge_commit_sha"].as_str();
    let valid_merge_sha = merge_sha
        .is_some_and(|sha| sha.len() == 40 && sha.bytes().all(|byte| byte.is_ascii_hexdigit()));
    if merged {
        if identity_ok && observed_head == published_head && valid_merge_sha {
            state.stage = Stage::ObservedMerged;
            state.stage_reason = Some(format!(
                "External merge observed for PR #{pr_number}; gates_verified=false"
            ));
        } else {
            state.retry_stage = Some(state.stage);
            state.stage = Stage::Blocked;
            state.stage_reason =
                Some("Merged PR identity, published head or merge SHA changed".to_owned());
        }
    } else if identity_ok {
        state.stage = Stage::Closed;
        state.stage_reason = Some(format!("PR #{pr_number} closed without merge"));
    } else {
        state.retry_stage = Some(state.stage);
        state.stage = Stage::Blocked;
        state.stage_reason = Some("Closed PR identity changed".to_owned());
    }
    sqlx::query(
        "INSERT INTO tasks.pr_terminal_observations \
         (task_id,pr_number,published_head,observed_head,merged,merge_commit_sha,pr) \
         VALUES ($1,$2,$3,$4,$5,$6,$7)",
    )
    .bind(state.id.0)
    .bind(pr_number)
    .bind(published_head)
    .bind(observed_head)
    .bind(merged)
    .bind(merge_sha)
    .bind(Json(pr))
    .execute(uow.connection())
    .await?;
    state.revision += 1;
    sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
        .bind(state.id.0)
        .bind(i64::try_from(state.revision)?)
        .bind(Json(&state))
        .execute(uow.connection())
        .await?;
    append_task_snapshot(uow, outbox, &state).await?;
    Ok(true)
}

/// Read the latest published PR for active work, including stages before CI gates.
pub async fn terminal_pr_inputs(
    database: &Database,
    issue: IssueKey,
) -> Result<Option<TerminalPrInputs>> {
    let row: Option<(Json<TaskState>, i64, String, String, String)> = sqlx::query_as(
        "SELECT t.state,b.pr_number,b.head,b.branch,i.snapshot->'configuration'->>'base' \
         FROM tasks.task_state t \
         JOIN tasks.initialization_inputs i ON i.task_id=t.task_id \
         JOIN tasks.pr_review_batches b ON b.task_id=t.task_id \
             AND b.status IN ('recorded','admitted','settled') \
         WHERE t.repository_id=$1 AND t.issue_number=$2 \
         ORDER BY b.round DESC,b.id DESC LIMIT 1",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_optional(database.pool())
    .await?;
    Ok(row.map(
        |(Json(state), pr_number, head, branch, base)| TerminalPrInputs {
            state,
            pr_number,
            head,
            branch,
            base,
        },
    ))
}

/// Read only a fully published unanimous review against the current approved plan.
pub async fn gate_inputs(database: &Database, issue: IssueKey) -> Result<Option<GateInputs>> {
    let row: Option<GateInputsRow> = sqlx::query_as(
        "SELECT t.state, i.snapshot, b.id AS batch_id, b.pr_number, b.head, b.branch, b.changed_paths, p.raw_report_bytes AS plan_report \
         FROM tasks.task_state t JOIN tasks.initialization_inputs i ON i.task_id=t.task_id \
         JOIN tasks.pr_review_batches b ON b.task_id=t.task_id AND b.status='settled' \
         JOIN tasks.implementation_reports r ON r.attempt_id=b.publication_attempt \
             AND r.task_id=b.task_id AND r.status='accepted' \
         JOIN tasks.plan_approvals a ON a.task_id=t.task_id AND a.plan_id=b.plan_id \
             AND a.binding=r.candidate->>'binding' \
             AND a.request_id=(SELECT request_id FROM tasks.plan_approvals \
                 WHERE task_id=t.task_id ORDER BY recorded_at DESC,request_id DESC LIMIT 1) \
         JOIN tasks.plan_reports p ON p.task_id=t.task_id AND p.plan_id=b.plan_id \
             AND p.accepted AND p.current \
         WHERE t.repository_id=$1 AND t.issue_number=$2 \
         ORDER BY b.round DESC,b.id DESC LIMIT 1",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_optional(database.pool())
    .await?;
    let Some(GateInputsRow {
        state: Json(state),
        snapshot: Json(snapshot),
        batch_id,
        pr_number,
        head,
        branch,
        changed_paths: Json(changed_paths),
        plan_report: plan_raw,
    }) = row
    else {
        return Ok(None);
    };
    if !matches!(state.stage, Stage::Gates | Stage::Ready) {
        return Ok(None);
    }
    let unresolved_findings: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM tasks.pr_review_findings WHERE task_id=$1 \
         AND status IN ('open','withdrawn'))",
    )
    .bind(state.id.0)
    .fetch_one(database.pool())
    .await?;
    let intent: Option<(String, String, bool, i64, String, Option<String>)> = sqlx::query_as(
        "SELECT head,status,ready_seen,pr_number,base_sha,merge_sha \
         FROM tasks.pr_ready_intents WHERE task_id=$1",
    )
    .bind(state.id.0)
    .fetch_optional(database.pool())
    .await?;
    let ready_seen = intent.as_ref().is_some_and(|(_, _, seen, _, _, _)| *seen);
    let (intent_status, intent_identity) = intent.map_or(
        (None, None),
        |(saved_head, status, _, number, base, merge)| {
            if saved_head == head {
                (Some(status), Some((number, base, merge)))
            } else {
                (None, None)
            }
        },
    );
    Ok(Some(GateInputs {
        state,
        snapshot,
        batch_id,
        pr_number,
        head,
        branch,
        changed_paths,
        plan: serde_json::from_slice(&plan_raw)?,
        unresolved_findings,
        ready_seen,
        intent_status,
        intent_identity,
    }))
}

/// Find active published PRs that need closure or gate observation.
pub async fn watched_gate_issues(database: &Database) -> Result<Vec<IssueKey>> {
    let rows: Vec<(i64, i64)> = sqlx::query_as(
        "SELECT DISTINCT t.repository_id,t.issue_number FROM tasks.task_state t \
         JOIN tasks.pr_review_batches b ON b.task_id=t.task_id \
             AND b.status IN ('recorded','admitted','settled') \
         WHERE t.state->>'stage' IN \
             ('Review','Implement','Intake','Plan','PlanQc','Planned','Clarify','Publish','Gates','Ready') \
         ORDER BY t.repository_id,t.issue_number",
    )
    .fetch_all(database.pool())
    .await?;
    Ok(rows
        .into_iter()
        .map(|(repository_id, number)| IssueKey {
            repository_id,
            number,
        })
        .collect())
}

/// Record current gate blockers without emitting duplicate state revisions.
pub async fn record_gate_reason(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    issue: IssueKey,
    expected_revision: u64,
    reason: &str,
) -> Result<bool> {
    let Some(mut state) = lock_gate_task(uow, issue).await? else {
        return Ok(false);
    };
    if state.revision != expected_revision || !matches!(state.stage, Stage::Gates | Stage::Ready) {
        return Ok(false);
    }
    if state.stage == Stage::Gates && state.stage_reason.as_deref() == Some(reason) {
        return Ok(true);
    }
    state.stage = Stage::Gates;
    state.stage_reason = Some(reason.to_owned());
    state.revision += 1;
    sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
        .bind(state.id.0)
        .bind(i64::try_from(state.revision)?)
        .bind(Json(&state))
        .execute(uow.connection())
        .await?;
    append_task_snapshot(uow, outbox, &state).await?;
    Ok(true)
}

/// Freeze an exact head, base and test-merge identity before any ready mutation.
pub async fn reserve_ready_intent(
    uow: &mut UnitOfWork<'_>,
    issue: IssueKey,
    expected_revision: u64,
    pr_number: i64,
    head: &str,
    base_sha: &str,
    merge_sha: Option<&str>,
) -> Result<bool> {
    let Some(state) = lock_gate_task(uow, issue).await? else {
        return Ok(false);
    };
    if state.revision != expected_revision || state.stage != Stage::Gates {
        return Ok(false);
    }
    sqlx::query(
        "INSERT INTO tasks.pr_ready_intents \
         (task_id,pr_number,head,base_sha,merge_sha) VALUES ($1,$2,$3,$4,$5) \
         ON CONFLICT (task_id) DO UPDATE SET \
           pr_number=EXCLUDED.pr_number,head=EXCLUDED.head,base_sha=EXCLUDED.base_sha, \
           merge_sha=EXCLUDED.merge_sha,status='recorded' \
         WHERE pr_ready_intents.head<>EXCLUDED.head \
         OR (pr_ready_intents.status='recorded' AND ( \
             pr_ready_intents.pr_number<>EXCLUDED.pr_number \
             OR pr_ready_intents.base_sha<>EXCLUDED.base_sha \
             OR pr_ready_intents.merge_sha IS DISTINCT FROM EXCLUDED.merge_sha))",
    )
    .bind(state.id.0)
    .bind(pr_number)
    .bind(head)
    .bind(base_sha)
    .bind(merge_sha)
    .execute(uow.connection())
    .await?;
    let saved: (i64, String, String, Option<String>) = sqlx::query_as(
        "SELECT pr_number,head,base_sha,merge_sha FROM tasks.pr_ready_intents WHERE task_id=$1",
    )
    .bind(state.id.0)
    .fetch_one(uow.connection())
    .await?;
    ensure!(
        saved
            == (
                pr_number,
                head.to_owned(),
                base_sha.to_owned(),
                merge_sha.map(str::to_owned)
            ),
        "ready intent differs from current observed PR"
    );
    Ok(true)
}

/// Remember a human-visible ready PR even when another readiness gate still fails.
#[allow(clippy::too_many_arguments)]
pub async fn observe_ready_seen(
    uow: &mut UnitOfWork<'_>,
    issue: IssueKey,
    expected_revision: u64,
    pr_number: i64,
    head: &str,
    base_sha: &str,
    merge_sha: Option<&str>,
) -> Result<bool> {
    let Some(state) = lock_gate_task(uow, issue).await? else {
        return Ok(false);
    };
    if state.revision != expected_revision || !matches!(state.stage, Stage::Gates | Stage::Ready) {
        return Ok(false);
    }
    let changed = sqlx::query(
        "INSERT INTO tasks.pr_ready_intents \
         (task_id,pr_number,head,base_sha,merge_sha,ready_seen) \
         VALUES ($1,$2,$3,$4,$5,true) \
         ON CONFLICT (task_id) DO UPDATE SET ready_seen=true",
    )
    .bind(state.id.0)
    .bind(pr_number)
    .bind(head)
    .bind(base_sha)
    .bind(merge_sha)
    .execute(uow.connection())
    .await?;
    Ok(changed.rows_affected() == 1)
}

/// Fence an uncertain mark-ready mutation so later workers only read the PR.
pub async fn mark_ready_sent(
    uow: &mut UnitOfWork<'_>,
    issue: IssueKey,
    head: &str,
) -> Result<bool> {
    let Some(state) = lock_gate_task(uow, issue).await? else {
        return Ok(false);
    };
    if state.stage != Stage::Gates {
        return Ok(false);
    }
    let changed = sqlx::query(
        "UPDATE tasks.pr_ready_intents SET status='sent' WHERE task_id=$1 AND head=$2 \
         AND status='recorded' AND NOT ready_seen",
    )
    .bind(state.id.0)
    .bind(head)
    .execute(uow.connection())
    .await?;
    Ok(changed.rows_affected() == 1)
}

/// Release a ready send fence after GitHub proves a retryable refusal.
pub async fn retry_ready_intent(
    uow: &mut UnitOfWork<'_>,
    issue: IssueKey,
    head: &str,
) -> Result<bool> {
    let Some(state) = lock_gate_task(uow, issue).await? else {
        return Ok(false);
    };
    if state.stage != Stage::Gates {
        return Ok(false);
    }
    let changed = sqlx::query(
        "UPDATE tasks.pr_ready_intents SET status='recorded' \
         WHERE task_id=$1 AND head=$2 AND status='sent'",
    )
    .bind(state.id.0)
    .bind(head)
    .execute(uow.connection())
    .await?;
    Ok(changed.rows_affected() == 1)
}

/// Accept GitHub's ready readback and queue the once-per-head handoff notice.
pub async fn confirm_ready(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    issue: IssueKey,
    head: &str,
    upstream: &str,
) -> Result<bool> {
    let Some(mut state) = lock_gate_task(uow, issue).await? else {
        return Ok(false);
    };
    if !matches!(state.stage, Stage::Gates | Stage::Ready) {
        return Ok(false);
    }
    let intent: Option<(i64, String)> = sqlx::query_as(
        "UPDATE tasks.pr_ready_intents SET status='confirmed',ready_seen=true \
         WHERE task_id=$1 AND head=$2 AND status IN ('recorded','sent','confirmed') \
         RETURNING pr_number,status",
    )
    .bind(state.id.0)
    .bind(head)
    .fetch_optional(uow.connection())
    .await?;
    let Some((pr_number, _)) = intent else {
        return Ok(false);
    };
    if state.stage == Stage::Ready {
        return Ok(true);
    }
    state.stage = Stage::Ready;
    state.stage_reason =
        Some("CI passed and current reviewers approved; ready for human review".to_owned());
    state.revision += 1;
    sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
        .bind(state.id.0)
        .bind(i64::try_from(state.revision)?)
        .bind(Json(&state))
        .execute(uow.connection())
        .await?;
    queue_task_comment(
        uow, &state, "notice", &format!("ready:{head}"),
        &format!("Ready for review\n\n[PR #{pr_number}](https://github.com/{upstream}/pull/{pr_number}) has passing CI and current Concurro reviewer approvals. Human review and merge remain yours."),
    ).await?;
    append_task_snapshot(uow, outbox, &state).await?;
    Ok(true)
}

/// Stop a definitively refused ready write with its GitHub status visible.
pub async fn fail_ready_intent(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    issue: IssueKey,
    head: &str,
    reason: &str,
) -> Result<bool> {
    let Some(mut state) = lock_gate_task(uow, issue).await? else {
        return Ok(false);
    };
    if state.stage != Stage::Gates {
        return Ok(false);
    }
    let changed = sqlx::query(
        "UPDATE tasks.pr_ready_intents SET status='failed',error=$3 \
         WHERE task_id=$1 AND head=$2 AND status='sent'",
    )
    .bind(state.id.0)
    .bind(head)
    .bind(reason)
    .execute(uow.connection())
    .await?;
    if changed.rows_affected() == 0 {
        return Ok(false);
    }
    state.retry_stage = Some(state.stage);
    state.stage = Stage::Blocked;
    state.stage_reason = Some(format!("mark-ready refused by GitHub: {reason}"));
    state.revision += 1;
    sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
        .bind(state.id.0)
        .bind(i64::try_from(state.revision)?)
        .bind(Json(&state))
        .execute(uow.connection())
        .await?;
    append_task_snapshot(uow, outbox, &state).await?;
    Ok(true)
}

/// Lock the issue decision row before any gate or ready state change.
async fn lock_gate_task(uow: &mut UnitOfWork<'_>, issue: IssueKey) -> Result<Option<TaskState>> {
    sqlx::query(
        "SELECT 1 FROM tasks.issue_controls WHERE repository_id=$1 AND issue_number=$2 FOR UPDATE",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_optional(uow.connection())
    .await?;
    let state: Option<Json<TaskState>> = sqlx::query_scalar(
        "SELECT state FROM tasks.task_state WHERE repository_id=$1 AND issue_number=$2 FOR UPDATE",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_optional(uow.connection())
    .await?;
    Ok(state.map(|Json(state)| state))
}
