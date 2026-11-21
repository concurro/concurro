//! Host operator task views and audited recovery actions.

use anyhow::{Result, ensure};
use concurro_db::{Database, UnitOfWork};
use concurro_events_postgres::Outbox;
use concurro_tasks_core::{IssueKey, Stage, TaskState};
use serde_json::Value;
use sqlx::types::Json;
use uuid::Uuid;

use crate::{
    cancel_attempts, effects::settle_attempt, git_publication::GitPublicationIntent,
    lock_issue_for_turn, pr_review, pr_review_actions,
};

/// Reopen or visibly stop a host-reviewed uncertain publication write.
pub async fn resolve_pending_write(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    kind: &str,
    id: Uuid,
    sequence: Option<i32>,
    actor: &str,
    resend: bool,
) -> Result<bool> {
    ensure!(!actor.trim().is_empty(), "operator actor is empty");
    let resolved = match kind {
        "git" if sequence.is_none() => {
            let Some(state) = lock_issue_for_turn(uow, id).await? else {
                return Ok(false);
            };
            if state.stage != Stage::Publish {
                return Ok(false);
            }
            let row: Option<(i64, Json<GitPublicationIntent>)> = sqlx::query_as(
                "SELECT revision,intent FROM tasks.git_publications \
                 WHERE task_id=$1 AND attempt_id=$2 FOR UPDATE",
            )
            .bind(state.id.0)
            .bind(id)
            .fetch_optional(uow.connection())
            .await?;
            let Some((revision, Json(mut intent))) = row else {
                return Ok(false);
            };
            let field = if intent.pr_update_sent {
                &mut intent.pr_update_sent
            } else if intent.pr_create_sent && intent.pr_number.is_none() {
                &mut intent.pr_create_sent
            } else {
                return Ok(false);
            };
            if resend {
                *field = false;
                if !crate::save_git_publication(uow, id, revision, &intent).await? {
                    return Ok(false);
                }
                crate::enqueue_git_publication(uow, &state).await?;
                true
            } else {
                crate::block_git_publication(
                    uow,
                    outbox,
                    id,
                    "operator marked uncertain GitHub publication failed",
                )
                .await?
            }
        }
        "review" | "action" => {
            let Some(state) = lock_issue_for_turn(uow, id).await? else {
                return Ok(false);
            };
            if state.stage != Stage::Review {
                return Ok(false);
            }
            let row: Option<(Uuid, String)> = sqlx::query_as(
                "SELECT batch_id,status FROM tasks.pr_review_publications \
                 WHERE turn_id=$1 AND task_id=$2 FOR UPDATE",
            )
            .bind(id)
            .bind(state.id.0)
            .fetch_optional(uow.connection())
            .await?;
            let Some((batch, status)) = row else {
                return Ok(false);
            };
            if status != "sent" {
                return Ok(false);
            }
            if kind == "action" {
                let Some(sequence) = sequence else {
                    return Ok(false);
                };
                let changed = if resend {
                    pr_review_actions::retry_pr_review_action(uow, id, sequence).await?
                } else {
                    pr_review_actions::fail_pr_review_action(uow, id, sequence).await?
                };
                if !changed {
                    return Ok(false);
                }
            } else {
                if sequence.is_some() {
                    return Ok(false);
                }
                let unresolved_action: bool = sqlx::query_scalar(
                    "SELECT EXISTS(SELECT 1 FROM tasks.pr_review_actions \
                     WHERE turn_id=$1 AND status IN ('sent','failed'))",
                )
                .bind(id)
                .fetch_one(uow.connection())
                .await?;
                if unresolved_action {
                    return Ok(false);
                }
            }
            if resend {
                if kind == "review" {
                    pr_review::retry_pr_review_publication(uow, id).await?;
                }
                pr_review::enqueue_pr_review_publish(uow, state.id.0, id).await?;
            } else {
                pr_review::block_pr_review_batch(
                    uow,
                    outbox,
                    state.issue,
                    batch,
                    "operator marked uncertain GitHub review failed",
                )
                .await?;
                sqlx::query(
                    "UPDATE tasks.pr_review_publications SET status='failed',error='operator_failed' \
                     WHERE turn_id=$1 AND status='sent'",
                )
                .bind(id)
                .execute(uow.connection())
                .await?;
            }
            true
        }
        "standalone_review" if sequence.is_none() => {
            let status: Option<String> = sqlx::query_scalar(
                "SELECT status FROM tasks.standalone_review_publications \
                 WHERE turn_id=$1 FOR UPDATE",
            )
            .bind(id)
            .fetch_optional(uow.connection())
            .await?;
            if status.as_deref() != Some("sent") {
                return Ok(false);
            }
            if resend {
                if !crate::standalone_publication::retry_standalone_review_publication(uow, id)
                    .await?
                {
                    return Ok(false);
                }
                crate::standalone::enqueue_standalone_review_publish(uow, id).await?;
                true
            } else {
                crate::standalone_publication::fail_standalone_review_publication(
                    uow,
                    id,
                    "operator_failed",
                )
                .await?
            }
        }
        "ready" if sequence.is_none() => {
            let row: Option<(i64, i64, String, String)> = sqlx::query_as(
                "SELECT t.repository_id,t.issue_number,r.head,r.status \
                 FROM tasks.pr_ready_intents r JOIN tasks.task_state t ON t.task_id=r.task_id \
                 WHERE r.task_id=$1",
            )
            .bind(id)
            .fetch_optional(uow.connection())
            .await?;
            let Some((repository_id, number, head, status)) = row else {
                return Ok(false);
            };
            if status != "sent" {
                return Ok(false);
            }
            let issue = IssueKey {
                repository_id,
                number,
            };
            if resend {
                if !crate::retry_ready_intent(uow, issue, &head).await? {
                    return Ok(false);
                }
                let state: Option<Json<TaskState>> = sqlx::query_scalar(
                    "SELECT state FROM tasks.task_state WHERE repository_id=$1 AND issue_number=$2",
                )
                .bind(repository_id)
                .bind(number)
                .fetch_optional(uow.connection())
                .await?;
                if let Some(Json(state)) = state {
                    crate::enqueue_gates(uow, &state).await?;
                }
                true
            } else {
                crate::fail_ready_intent(uow, outbox, issue, &head, "operator_failed").await?
            }
        }
        _ => return Ok(false),
    };
    if resolved {
        sqlx::query(
            "INSERT INTO tasks.operator_actions (actor,action,target,outcome) \
             VALUES ($1,$2,$3,$4)",
        )
        .bind(actor)
        .bind(format!("writes.resolve.{kind}"))
        .bind(match sequence {
            Some(sequence) => format!("{id}:{sequence}"),
            None => id.to_string(),
        })
        .bind(if resend { "recorded" } else { "failed" })
        .execute(uow.connection())
        .await?;
    }
    Ok(resolved)
}

/// List current tasks in a stable repository and issue order.
pub async fn list_tasks(database: &Database) -> Result<Vec<Value>, sqlx::Error> {
    let rows: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object( \
         'kind','contribution','repository_id',repository_id,'issue_number',issue_number,'task_id',task_id, \
         'revision',revision,'stage',state->>'stage','stage_reason',state->>'stage_reason', \
         'used_model_calls',state->'used_model_calls') \
         FROM tasks.task_state ORDER BY repository_id,issue_number",
    )
    .fetch_all(database.pool())
    .await?;
    let standalone: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object( \
         'kind','standalone_pr_review','repository_id',repository_id, \
         'issue_number',pr_number,'task_id',id,'round',round,'stage',stage, \
         'used_model_calls',used_model_calls,'upstream',upstream) \
         FROM tasks.standalone_review_tasks ORDER BY repository_id,pr_number",
    )
    .fetch_all(database.pool())
    .await?;
    let mut listed = rows
        .into_iter()
        .chain(standalone)
        .map(|row| row.0.0)
        .collect::<Vec<_>>();
    listed.sort_by_key(|item| {
        (
            item["repository_id"].as_i64().unwrap_or_default(),
            item["issue_number"].as_i64().unwrap_or_default(),
        )
    });
    Ok(listed)
}

/// Show the independent PR review owner, source rounds, findings and write receipts.
pub async fn standalone_review_detail(
    database: &Database,
    source: IssueKey,
) -> Result<Option<Value>, sqlx::Error> {
    let task: Option<(Uuid, Json<Value>)> = sqlx::query_as(
        "SELECT id,jsonb_build_object( \
         'kind','standalone_pr_review','task_id',id,'repository_id',repository_id, \
         'upstream',upstream,'pr_number',pr_number,'reviewer_id',reviewer_id, \
         'stage',stage,'round',round,'used_model_calls',used_model_calls, \
         'closed_from_stage',closed_from_stage,'enrolled_at',enrolled_at) \
         FROM tasks.standalone_review_tasks \
         WHERE repository_id=$1 AND pr_number=$2",
    )
    .bind(source.repository_id)
    .bind(source.number)
    .fetch_optional(database.pool())
    .await?;
    let Some((task_id, Json(mut detail))) = task else {
        return Ok(None);
    };
    let rounds: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object( \
         'number',number,'request_id',request_id,'status',status,'snapshot',snapshot, \
         'cancellation_request_id',cancellation_request_id,'cancelled_by',cancelled_by, \
         'opened_at',opened_at,'completed_at',completed_at) \
         FROM tasks.standalone_review_rounds WHERE task_id=$1 ORDER BY number",
    )
    .bind(task_id)
    .fetch_all(database.pool())
    .await?;
    let requests: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object( \
         'id',id,'source_id',source_id,'source_at',source_at,'actor',actor, \
         'action',action,'status',status,'reason',reason,'round',round) \
         FROM tasks.standalone_review_requests \
         WHERE repository_id=$1 AND pr_number=$2 \
         ORDER BY source_at,source_id",
    )
    .bind(source.repository_id)
    .bind(source.number)
    .fetch_all(database.pool())
    .await?;
    let commands: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object( \
         'id',id,'comment_id',comment_id,'source',source,'source_at',source_at, \
         'actor',actor,'kind',kind,'status',status,'reason',reason,'decided_at',decided_at) \
         FROM tasks.standalone_review_commands \
         WHERE repository_id=$1 AND pr_number=$2 ORDER BY source_at,comment_id",
    )
    .bind(source.repository_id)
    .bind(source.number)
    .fetch_all(database.pool())
    .await?;
    let adoptions: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object( \
         'command_id',command_id,'round',round, \
         'previous_config_sha',previous_snapshot->>'config_sha', \
         'adopted_config_sha',adopted_snapshot->>'config_sha', \
         'adopted_at',adopted_at) \
         FROM tasks.standalone_review_adoptions \
         WHERE task_id=$1 ORDER BY adopted_at,command_id",
    )
    .bind(task_id)
    .fetch_all(database.pool())
    .await?;
    let attempts: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object( \
         'id',a.id,'round',s.round,'reviewer_id',s.reviewer_id, \
         'attempt_number',s.attempt_number,'status',s.status, \
         'turn_status',a.status,'report',s.report,'validated_report',s.validated_report, \
         'failure_reason',s.failure_reason,'funding',f.purpose, \
         'payer_id',f.payer_id,'credential_record_id',f.credential_record_id, \
         'credential_version',f.credential_version, \
         'effects',COALESCE((SELECT jsonb_agg(jsonb_build_object( \
             'id',e.id,'kind',e.kind,'status',e.status,'updated_at',e.updated_at) \
             ORDER BY e.id) FROM tasks.effects e WHERE e.turn_id=a.id),'[]'::jsonb)) \
         FROM tasks.standalone_review_attempts s \
         JOIN tasks.attempts a ON a.id=s.attempt_id \
         LEFT JOIN tasks.standalone_review_funding_grants f ON f.attempt_id=a.id \
         WHERE s.task_id=$1 ORDER BY s.round,s.reviewer_id,s.attempt_number",
    )
    .bind(task_id)
    .fetch_all(database.pool())
    .await?;
    let findings: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object( \
         'id',id,'reviewer_id',reviewer_id,'status',status,'head',head, \
         'finding',finding,'disposition',disposition,'thread_node_id',thread_node_id, \
         'root_comment_id',root_comment_id) \
         FROM tasks.standalone_review_findings WHERE task_id=$1 ORDER BY sequence",
    )
    .bind(task_id)
    .fetch_all(database.pool())
    .await?;
    let dispositions: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object( \
         'turn_id',turn_id,'finding_id',finding_id,'reply_status',reply_status, \
         'reply_id',reply_id,'resolve_status',resolve_status,'error',error) \
         FROM tasks.standalone_review_dispositions WHERE task_id=$1 \
         ORDER BY turn_id,finding_id",
    )
    .bind(task_id)
    .fetch_all(database.pool())
    .await?;
    let reports: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object( \
         'turn_id',report_id,'name',name,'status',status, \
         'artifact_number',artifact_number,'error',error) \
         FROM tasks.report_artifact_writes WHERE standalone_task_id=$1 \
         ORDER BY recorded_at,report_id",
    )
    .bind(task_id)
    .fetch_all(database.pool())
    .await?;
    let checks: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object( \
         'round',round,'attempt',attempt,'head',head,'external_id',external_id, \
         'run_id',run_id,'create_state',create_state,'create_misses',create_misses, \
         'recheck_until',recheck_until,'completed',completed,'error',error) \
         FROM tasks.standalone_review_checks WHERE task_id=$1 ORDER BY round,attempt",
    )
    .bind(task_id)
    .fetch_all(database.pool())
    .await?;
    let publications: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object( \
         'turn_id',turn_id,'round',round,'reviewer_id',reviewer_id, \
         'status',status,'review_id',review_id,'error',error,'recorded_at',recorded_at, \
         'comments',comments,'unplaced_comments',unplaced_comments) \
         FROM tasks.standalone_review_publications WHERE task_id=$1 \
         ORDER BY recorded_at,turn_id",
    )
    .bind(task_id)
    .fetch_all(database.pool())
    .await?;
    let object = detail
        .as_object_mut()
        .expect("standalone detail is an object");
    for (name, entries) in [
        ("rounds", rounds),
        ("requests", requests),
        ("commands", commands),
        ("adoptions", adoptions),
        ("attempts", attempts),
        ("findings", findings),
        ("dispositions", dispositions),
        ("report_artifacts", reports),
        ("checks", checks),
        ("review_publications", publications),
    ] {
        object.insert(
            name.to_owned(),
            Value::Array(entries.into_iter().map(|row| row.0.0).collect()),
        );
    }
    Ok(Some(detail))
}

/// Read one task with its ordered requests, turns, reports and publication writes.
pub async fn task_detail(
    database: &Database,
    issue: IssueKey,
) -> Result<Option<Value>, sqlx::Error> {
    let state: Option<(Json<Value>,)> = sqlx::query_as(
        "SELECT state FROM tasks.task_state WHERE repository_id=$1 AND issue_number=$2",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_optional(database.pool())
    .await?;
    let dashboard: Option<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('revision',s.revision,'comment_id',c.comment_id, \
         'watch',s.watch,'last_error',s.last_error,'retry_at',s.retry_at, \
         'writes',COALESCE((SELECT jsonb_agg(jsonb_build_object( \
             'id',w.id,'revision',w.revision,'status',w.status, \
             'failure',w.failure,'updated_at',w.updated_at) ORDER BY w.id) \
             FROM tasks.dashboard_writes w WHERE w.task_id=s.task_id),'[]'::jsonb)) \
         FROM tasks.dashboard_surfaces s \
         JOIN tasks.publication_comments c ON c.id=s.create_id \
         WHERE s.repository_id=$1 AND s.issue_number=$2",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_optional(database.pool())
    .await?;
    let Some((Json(state),)) = state else {
        return standalone_review_detail(database, issue).await;
    };
    let requests: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('id',id,'channel',channel,'source',source, \
         'source_at',source_at,'actor',request->'actor','kind',request->'kind', \
         'status',status,'reason',reason) \
         FROM tasks.requests WHERE repository_id=$1 AND issue_number=$2 \
         ORDER BY source_at,source_id,source COLLATE \"C\",source_index",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_all(database.pool())
    .await?;
    let attempts: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('id',a.id,'operation',a.operation, \
         'expected_revision',a.expected_revision,'status',a.status, \
         'outcome_reason',a.outcome_reason,'admitted_at',a.admitted_at, \
         'deadline',a.deadline,'poll_failures',a.poll_failures, \
         'status_revision',a.status_revision,'finished_event_emitted',a.finished_event_emitted, \
         'may_be_live',EXISTS(SELECT 1 FROM tasks.effects s \
             WHERE s.turn_id=a.id AND s.kind='submit' AND s.submit_attempts>0) \
             AND NOT EXISTS(SELECT 1 FROM tasks.effects r \
             WHERE r.turn_id=a.id AND r.kind='retire' AND r.status='settled'), \
         'observation',a.observation,'result',a.result, \
         'artifacts',COALESCE((SELECT jsonb_agg(jsonb_build_object( \
             'sha256',b.sha256,'bytes',octet_length(b.body),'media_type',b.media_type) \
             ORDER BY b.sha256) FROM tasks.turn_artifact_links l \
             JOIN tasks.turn_artifacts b ON b.sha256=l.sha256 \
             WHERE l.turn_id=a.id),'[]'::jsonb), \
         'sessions',COALESCE((SELECT jsonb_agg(jsonb_build_object( \
             'session_id',s.session_id,'retired_at',s.retired_at) \
             ORDER BY s.session_id) FROM tasks.attempt_sessions s WHERE s.turn_id=a.id),'[]'::jsonb), \
         'report',(SELECT jsonb_build_object('accepted',r.accepted, \
             'raw_report',r.raw_report,'validation_error',r.validation_error) \
             FROM tasks.intake_reports r WHERE r.attempt_id=a.id), \
         'effects',COALESCE((SELECT jsonb_agg(jsonb_build_object( \
             'id',e.id,'kind',e.kind,'status',e.status,'updated_at',e.updated_at) \
             ORDER BY e.id) FROM tasks.effects e WHERE e.turn_id=a.id),'[]'::jsonb)) \
         FROM tasks.attempts a JOIN tasks.task_state t ON t.task_id=a.task_id \
         WHERE t.repository_id=$1 AND t.issue_number=$2 ORDER BY a.admitted_at,a.id",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_all(database.pool())
    .await?;
    let publication: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('id',id,'request_id',request_id,'kind',kind, \
         'status',status,'comment_id',comment_id,'failure',failure,'updated_at',updated_at) \
         FROM tasks.publication_comments WHERE repository_id=$1 AND issue_number=$2 \
         ORDER BY updated_at,id",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_all(database.pool())
    .await?;
    let archives: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('turn',a.turn_id,'gzip_sha256',a.gzip_sha256, \
         'tar_sha256',a.tar_sha256,'archive_error',a.archive_error, \
         'upload_status',u.status,'byte_count',u.byte_count,'last_error',u.last_error) \
         FROM tasks.turn_archives a JOIN tasks.attempts turn ON turn.id=a.turn_id \
         JOIN tasks.task_state t ON t.task_id=turn.task_id \
         LEFT JOIN tasks.archive_uploads u ON u.gzip_sha256=a.gzip_sha256 \
         WHERE t.repository_id=$1 AND t.issue_number=$2 ORDER BY a.recorded_at,a.turn_id",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_all(database.pool())
    .await?;
    let stage_label: Option<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('revision',s.revision,'desired',s.desired, \
         'watch',s.watch,'last_error',s.last_error,'retry_at',s.retry_at, \
         'writes',COALESCE((SELECT jsonb_agg(jsonb_build_object( \
             'id',w.id,'revision',w.revision,'action',w.action,'label',w.label, \
             'status',w.status,'failure',w.failure,'updated_at',w.updated_at) ORDER BY w.id) \
             FROM tasks.stage_label_writes w WHERE w.task_id=s.task_id),'[]'::jsonb)) \
         FROM tasks.stage_label_surfaces s JOIN tasks.task_state t ON t.task_id=s.task_id \
         WHERE t.repository_id=$1 AND t.issue_number=$2",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_optional(database.pool())
    .await?;
    let runners: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('session_id',r.session_id,'turn_id',r.turn_id, \
         'fork',r.fork,'config_sha',r.config_sha,'desired_present',r.desired_present, \
         'ref_revision',r.ref_revision,'ref_watch',r.ref_watch,'ref_error',r.ref_error, \
         'dispatch_status',r.dispatch_status,'dispatch_error',r.dispatch_error, \
         'run_id',r.run_id,'writes',COALESCE((SELECT jsonb_agg(jsonb_build_object( \
             'id',w.id,'revision',w.revision,'action',w.action,'status',w.status, \
             'failure',w.failure,'updated_at',w.updated_at) ORDER BY w.id) \
             FROM tasks.runner_ref_writes w WHERE w.session_id=r.session_id),'[]'::jsonb)) \
         FROM tasks.runner_sessions r JOIN tasks.attempts a ON a.id=r.turn_id \
         JOIN tasks.task_state t ON t.task_id=a.task_id \
         WHERE t.repository_id=$1 AND t.issue_number=$2 ORDER BY r.updated_at,r.session_id",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_all(database.pool())
    .await?;
    let design: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object( \
         'id',b.id,'plan_id',b.plan_id,'generation',b.generation,'phase',b.phase, \
         'binding',b.binding,'status',b.status,'roster',b.roster, \
         'seats',COALESCE((SELECT jsonb_agg(jsonb_build_object( \
             'reviewer_id',s.reviewer_id,'identity',s.identity,'attempt_id',s.attempt_id, \
             'status',s.status,'first_pass_eligible',s.first_pass_eligible, \
             'correction_count',s.correction_count,'failure_count',s.failure_count, \
             'session_epoch',s.session_epoch,'report',s.report, \
             'validation_error',s.validation_error) ORDER BY s.reviewer_id) \
             FROM tasks.plan_review_seats s WHERE s.batch_id=b.id),'[]'::jsonb)) \
         FROM tasks.plan_review_batches b JOIN tasks.task_state t ON t.task_id=b.task_id \
         WHERE t.repository_id=$1 AND t.issue_number=$2 ORDER BY b.generation,b.phase,b.id",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_all(database.pool())
    .await?;
    let plans: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('attempt_id',p.attempt_id, \
         'initialization_revision',p.initialization_revision,'plan_id',p.plan_id, \
         'accepted',p.accepted,'current',p.current,'report',p.raw_report, \
         'validation_error',p.validation_error,'recorded_at',p.recorded_at) \
         FROM tasks.plan_reports p JOIN tasks.task_state t ON t.task_id=p.task_id \
         WHERE t.repository_id=$1 AND t.issue_number=$2 \
         ORDER BY p.recorded_at,p.attempt_id",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_all(database.pool())
    .await?;
    let objections: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('sequence',o.sequence,'id',o.id, \
         'reviewer_id',o.reviewer_id,'detail',o.detail, \
         'counterexample',o.counterexample,'closure',o.closure, \
         'evidence',o.evidence,'resolved',o.resolved, \
         'plan_id',o.plan_id,'phase',o.phase) \
         FROM tasks.plan_objections o JOIN tasks.task_state t ON t.task_id=o.task_id \
         WHERE t.repository_id=$1 AND t.issue_number=$2 ORDER BY o.sequence",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_all(database.pool())
    .await?;
    let approvals: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('request_id',a.request_id, \
         'plan_id',a.plan_id,'binding',a.binding,'actor',a.actor, \
         'recorded_at',a.recorded_at) \
         FROM tasks.plan_approvals a JOIN tasks.task_state t ON t.task_id=a.task_id \
         WHERE t.repository_id=$1 AND t.issue_number=$2 ORDER BY a.recorded_at,a.request_id",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_all(database.pool())
    .await?;
    let implementation: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('attempt_id',r.attempt_id,'status',r.status, \
         'validation_error',r.validation_error,'candidate',r.candidate, \
         'recorded_at',r.recorded_at) \
         FROM tasks.implementation_reports r JOIN tasks.task_state t ON t.task_id=r.task_id \
         WHERE t.repository_id=$1 AND t.issue_number=$2 ORDER BY r.recorded_at,r.attempt_id",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_all(database.pool())
    .await?;
    let git_publication: Option<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('attempt_id',p.attempt_id,'revision',p.revision, \
         'intent',p.intent,'recorded_at',p.recorded_at) \
         FROM tasks.git_publications p JOIN tasks.task_state t ON t.task_id=p.task_id \
         WHERE t.repository_id=$1 AND t.issue_number=$2 \
         ORDER BY p.recorded_at DESC,p.attempt_id DESC LIMIT 1",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_optional(database.pool())
    .await?;
    let pr_reviews: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('id',b.id,'pr_number',b.pr_number, \
         'head',b.head,'branch',b.branch,'plan_id',b.plan_id,'round',b.round, \
         'status',b.status,'changed_paths',b.changed_paths,'roster',b.roster, \
         'seats',COALESCE((SELECT jsonb_agg(jsonb_build_object( \
             'reviewer_id',s.reviewer_id,'attempt_id',s.attempt_id,'status',s.status, \
             'correction_count',s.correction_count,'failure_count',s.failure_count, \
             'validation_error',s.validation_error,'report',s.report) ORDER BY s.reviewer_id) \
             FROM tasks.pr_review_seats s WHERE s.batch_id=b.id),'[]'::jsonb)) \
         FROM tasks.pr_review_batches b JOIN tasks.task_state t ON t.task_id=b.task_id \
         WHERE t.repository_id=$1 AND t.issue_number=$2 ORDER BY b.round,b.id",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_all(database.pool())
    .await?;
    let review_findings: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('sequence',f.sequence,'id',f.id, \
         'reviewer_id',f.reviewer_id,'status',f.status,'head',f.head, \
         'finding',f.finding,'disposition',f.disposition, \
         'thread_node_id',f.thread_node_id) \
         FROM tasks.pr_review_findings f JOIN tasks.task_state t ON t.task_id=f.task_id \
         WHERE t.repository_id=$1 AND t.issue_number=$2 ORDER BY f.sequence",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_all(database.pool())
    .await?;
    let review_actions: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('turn_id',a.turn_id,'sequence',a.sequence, \
         'kind',a.kind,'finding_id',a.finding_id,'path',a.path, \
         'placement',a.placement,'status',a.status, \
         'result_node_id',a.result_node_id,'error',a.error) \
         FROM tasks.pr_review_actions a JOIN tasks.pr_review_publications p \
         ON p.turn_id=a.turn_id JOIN tasks.task_state t ON t.task_id=p.task_id \
         WHERE t.repository_id=$1 AND t.issue_number=$2 \
         ORDER BY p.recorded_at,a.turn_id,a.sequence",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_all(database.pool())
    .await?;
    let review_publications: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('turn_id',p.turn_id,'batch_id',p.batch_id, \
         'pr_number',p.pr_number,'head',p.head,'marker',p.marker, \
         'status',p.status,'review_id',p.review_id, \
         'review_node_id',p.review_node_id,'error',p.error, \
         'resolution',CASE WHEN p.status='recorded' THEN 'send' \
             WHEN p.status='sent' THEN 'readback_or_operator' \
             WHEN p.status='failed' THEN 'operator_review' ELSE 'settled' END) \
         FROM tasks.pr_review_publications p JOIN tasks.task_state t ON t.task_id=p.task_id \
         WHERE t.repository_id=$1 AND t.issue_number=$2 ORDER BY p.recorded_at,p.turn_id",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_all(database.pool())
    .await?;
    let ready_intent: Option<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('pr_number',r.pr_number,'head',r.head, \
         'base_sha',r.base_sha,'merge_sha',r.merge_sha,'status',r.status, \
         'ready_seen',r.ready_seen,'error',r.error, \
         'resolution',CASE WHEN r.status='recorded' THEN 'check_gates' \
             WHEN r.status='sent' THEN 'readback_or_operator' \
             WHEN r.status='failed' THEN 'operator_review' ELSE 'settled' END) \
         FROM tasks.pr_ready_intents r JOIN tasks.task_state t ON t.task_id=r.task_id \
         WHERE t.repository_id=$1 AND t.issue_number=$2",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_optional(database.pool())
    .await?;
    let invalidations: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('request_id',i.request_id,'actor',i.actor, \
         'prior_base_sha',i.prior_base_sha,'prior_bundle_id',i.prior_bundle_id, \
         'prior_plan_id',i.prior_plan_id,'prior_head',i.prior_head, \
         'next_base_sha',i.next_base_sha,'next_bundle_id',i.next_bundle_id, \
         'recorded_at',i.recorded_at) \
         FROM tasks.task_invalidations i JOIN tasks.task_state t ON t.task_id=i.task_id \
         WHERE t.repository_id=$1 AND t.issue_number=$2 ORDER BY i.recorded_at,i.request_id",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_all(database.pool())
    .await?;
    let resumes: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('request_id',r.request_id,'actor',r.actor, \
         'description_only',r.description_only,'prior_snapshot',r.prior_snapshot, \
         'adopted_snapshot',r.adopted_snapshot,'prior_stage',r.prior_stage, \
         'next_stage',r.next_stage,'approval_binding',r.approval_binding, \
         'kind',r.kind,'old_skill_sha',r.old_skill_sha,'new_skill_sha',r.new_skill_sha, \
         'recorded_at',r.recorded_at) \
         FROM tasks.task_resumes r JOIN tasks.task_state t ON t.task_id=r.task_id \
         WHERE t.repository_id=$1 AND t.issue_number=$2 ORDER BY r.recorded_at,r.request_id",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_all(database.pool())
    .await?;
    let implementation_reopens: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('turn_id',r.turn_id,'plan_id',r.plan_id, \
         'binding',r.binding,'prior_head',r.prior_head,'design_issue',r.design_issue, \
         'findings_snapshot',r.findings_snapshot,'recovered_request',r.recovered_request, \
         'recorded_at',r.recorded_at) \
         FROM tasks.implementation_reopens r JOIN tasks.task_state t ON t.task_id=r.task_id \
         WHERE t.repository_id=$1 AND t.issue_number=$2 ORDER BY r.recorded_at,r.turn_id",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_all(database.pool())
    .await?;
    let report_artifacts: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('report_id',r.report_id,'name',r.name, \
         'status',r.status,'artifact_number',r.artifact_number,'error',r.error, \
         'recorded_at',r.recorded_at) \
         FROM tasks.report_artifact_writes r JOIN tasks.task_state t ON t.task_id=r.task_id \
         WHERE t.repository_id=$1 AND t.issue_number=$2 ORDER BY r.recorded_at,r.report_id",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_all(database.pool())
    .await?;
    let terminal: Option<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('pr_number',p.pr_number,'published_head',p.published_head, \
         'observed_head',p.observed_head,'merged',p.merged, \
         'merge_commit_sha',p.merge_commit_sha,'gates_verified',p.gates_verified, \
         'recorded_at',p.recorded_at) \
         FROM tasks.pr_terminal_observations p JOIN tasks.task_state t ON t.task_id=p.task_id \
         WHERE t.repository_id=$1 AND t.issue_number=$2",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_optional(database.pool())
    .await?;
    Ok(Some(serde_json::json!({
        "state": state,
        "requests": requests.into_iter().map(|row| row.0.0).collect::<Vec<_>>(),
        "attempts": attempts.into_iter().map(|row| row.0.0).collect::<Vec<_>>(),
        "publication": publication.into_iter().map(|row| row.0.0).collect::<Vec<_>>(),
        "archives": archives.into_iter().map(|row| row.0.0).collect::<Vec<_>>(),
        "stage_label": stage_label.map(|row| row.0.0),
        "dashboard": dashboard.map(|row| row.0.0),
        "runners": runners.into_iter().map(|row| row.0.0).collect::<Vec<_>>(),
        "plans": plans.into_iter().map(|row| row.0.0).collect::<Vec<_>>(),
        "design": design.into_iter().map(|row| row.0.0).collect::<Vec<_>>(),
        "objections": objections.into_iter().map(|row| row.0.0).collect::<Vec<_>>(),
        "approvals": approvals.into_iter().map(|row| row.0.0).collect::<Vec<_>>(),
        "implementation": implementation.into_iter().map(|row| row.0.0).collect::<Vec<_>>(),
        "git_publication": git_publication.map(|row| row.0.0),
        "pr_reviews": pr_reviews.into_iter().map(|row| row.0.0).collect::<Vec<_>>(),
        "review_findings": review_findings.into_iter().map(|row| row.0.0).collect::<Vec<_>>(),
        "review_publications": review_publications.into_iter().map(|row| row.0.0).collect::<Vec<_>>(),
        "review_actions": review_actions.into_iter().map(|row| row.0.0).collect::<Vec<_>>(),
        "ready_intent": ready_intent.map(|row| row.0.0),
        "invalidations": invalidations.into_iter().map(|row| row.0.0).collect::<Vec<_>>(),
        "resumes": resumes.into_iter().map(|row| row.0.0).collect::<Vec<_>>(),
        "implementation_reopens": implementation_reopens.into_iter().map(|row| row.0.0).collect::<Vec<_>>(),
        "report_artifacts": report_artifacts.into_iter().map(|row| row.0.0).collect::<Vec<_>>(),
        "terminal_pr": terminal.map(|row| row.0.0),
    })))
}

/// List unsent and uncertain exchange and publication effects without credentials.
pub async fn pending_effects(database: &Database) -> Result<Vec<Value>, sqlx::Error> {
    let rows: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('id',e.id,'turn',e.turn_id,'kind',e.kind, \
         'status',e.status,'updated_at',e.updated_at, \
         'age_seconds',EXTRACT(EPOCH FROM now()-e.updated_at), \
         'attempt_status',a.status,'outcome_reason',a.outcome_reason, \
         'repository_id',COALESCE(t.repository_id,s.repository_id), \
         'issue_number',COALESCE(t.issue_number,s.pr_number), \
         'owner_kind',CASE WHEN s.id IS NULL THEN 'contribution' ELSE 'standalone_pr_review' END) \
         FROM tasks.effects e JOIN tasks.attempts a ON a.id=e.turn_id \
         LEFT JOIN tasks.task_state t ON t.task_id=a.task_id \
         LEFT JOIN tasks.standalone_review_tasks s ON s.id=a.standalone_task_id \
         WHERE e.status<>'settled' ORDER BY e.updated_at,e.id",
    )
    .fetch_all(database.pool())
    .await?;
    let mut effects = rows.into_iter().map(|row| row.0.0).collect::<Vec<_>>();
    let comments: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('id',id,'request_id',request_id,'kind',kind, \
         'status',status,'repository_id',repository_id,'issue_number',issue_number, \
         'updated_at',updated_at,'age_seconds',EXTRACT(EPOCH FROM now()-updated_at), \
         'resolution',CASE WHEN status='sending' THEN 'readback_or_operator' \
             ELSE 'send' END) \
         FROM tasks.publication_comments WHERE status IN ('recorded','sending') \
         ORDER BY updated_at,id",
    )
    .fetch_all(database.pool())
    .await?;
    effects.extend(comments.into_iter().map(|row| row.0.0));
    let sponsorship: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('kind','sponsorship_transition', \
         'transition',s.transition,'task_id',s.task_id,'provider',s.provider, \
         'expected_epoch',s.expected_epoch,'epoch',s.epoch,'status',s.status, \
         'failure',s.failure,'repository_id',t.repository_id, \
         'issue_number',t.issue_number,'age_seconds', \
         EXTRACT(EPOCH FROM now()-s.recorded_at), \
         'resolution',CASE WHEN s.status='failed' THEN 'operator_review' \
             ELSE 'retry_exact' END) \
         FROM tasks.sponsorship_transitions s \
         JOIN tasks.task_state t ON t.task_id=s.task_id \
         WHERE s.status<>'acknowledged' ORDER BY s.recorded_at,s.transition",
    )
    .fetch_all(database.pool())
    .await?;
    effects.extend(sponsorship.into_iter().map(|row| row.0.0));
    let publications: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('kind','git_publication','task_id',p.task_id, \
         'attempt_id',p.attempt_id,'repository_id',t.repository_id, \
         'issue_number',t.issue_number,'branch',p.intent->>'branch', \
         'commit_sha',p.intent->>'commit_sha','pr_create_sent',p.intent->'pr_create_sent', \
         'pr_update_sent',p.intent->'pr_update_sent', \
         'pr_number',p.intent->'pr_number','stage',t.state->>'stage', \
         'resolution',CASE WHEN p.intent->'pr_update_sent'='true'::jsonb \
             OR (p.intent->'pr_create_sent'='true'::jsonb \
             AND p.intent->'pr_number'='null'::jsonb) THEN 'readback_or_operator' \
             WHEN lower(t.state->>'stage')<>'publish' THEN 'operator_review' \
             ELSE 'continue' END) \
         FROM tasks.git_publications p JOIN tasks.task_state t ON t.task_id=p.task_id \
         WHERE lower(t.state->>'stage')='publish' OR p.intent->'pr_number'='null'::jsonb \
         ORDER BY p.recorded_at",
    )
    .fetch_all(database.pool())
    .await?;
    effects.extend(publications.into_iter().map(|row| row.0.0));
    let review_publications: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('kind','pr_review_publication', \
         'turn_id',p.turn_id,'batch_id',p.batch_id,'pr_number',p.pr_number, \
         'head',p.head,'marker',p.marker,'status',p.status,'error',p.error, \
         'next_action',(SELECT jsonb_build_object('sequence',a.sequence, \
             'kind',a.kind,'finding_id',a.finding_id,'status',a.status, \
             'placement',a.placement,'error',a.error) \
             FROM tasks.pr_review_actions a WHERE a.turn_id=p.turn_id \
             AND a.status<>'confirmed' ORDER BY a.sequence LIMIT 1), \
         'repository_id',t.repository_id,'issue_number',t.issue_number, \
         'resolution',CASE WHEN p.status='recorded' THEN 'send' \
             WHEN p.status='sent' THEN 'readback_or_operator' \
             ELSE 'operator_review' END) \
         FROM tasks.pr_review_publications p JOIN tasks.task_state t ON t.task_id=p.task_id \
         WHERE p.status IN ('recorded','sent','failed') ORDER BY p.recorded_at,p.turn_id",
    )
    .fetch_all(database.pool())
    .await?;
    effects.extend(review_publications.into_iter().map(|row| row.0.0));
    let standalone_publications: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('kind','standalone_review_publication', \
         'turn_id',p.turn_id,'task_id',p.task_id,'round',p.round, \
         'pr_number',p.pr_number,'head',p.head,'marker',p.marker, \
         'status',p.status,'error',p.error,'reviewer_user_id',p.reviewer_user_id, \
         'repository_id',t.repository_id,'upstream',t.upstream, \
         'resolution',CASE WHEN p.status='recorded' THEN 'send' \
             WHEN p.status='sent' THEN 'readback_or_operator' \
             ELSE 'operator_review' END) \
         FROM tasks.standalone_review_publications p \
         JOIN tasks.standalone_review_tasks t ON t.id=p.task_id \
         WHERE p.status IN ('recorded','sent','failed') \
         ORDER BY p.recorded_at,p.turn_id",
    )
    .fetch_all(database.pool())
    .await?;
    effects.extend(standalone_publications.into_iter().map(|row| row.0.0));
    let ready_intents: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('kind','pr_ready_intent','task_id',r.task_id, \
         'pr_number',r.pr_number,'head',r.head,'base_sha',r.base_sha, \
         'merge_sha',r.merge_sha,'status',r.status,'ready_seen',r.ready_seen, \
         'error',r.error,'repository_id',t.repository_id,'issue_number',t.issue_number, \
         'resolution',CASE WHEN r.status='recorded' THEN 'check_gates' \
             WHEN r.status='sent' THEN 'readback_or_operator' \
             ELSE 'operator_review' END) \
         FROM tasks.pr_ready_intents r JOIN tasks.task_state t ON t.task_id=r.task_id \
         WHERE r.status IN ('recorded','sent','failed') ORDER BY r.recorded_at,r.task_id",
    )
    .fetch_all(database.pool())
    .await?;
    effects.extend(ready_intents.into_iter().map(|row| row.0.0));
    let report_artifacts: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('kind','report_artifact','report_id',r.report_id, \
         'name',r.name,'status',r.status,'error',r.error, \
         'repository_id',r.repository_id,'issue_number',r.issue_number, \
         'resolution',CASE WHEN r.status='recorded' THEN 'send' \
             WHEN r.status='sent' THEN 'readback_or_operator' \
             ELSE 'operator_review' END) \
         FROM tasks.report_artifact_writes r WHERE r.status<>'confirmed' \
         ORDER BY r.recorded_at,r.report_id",
    )
    .fetch_all(database.pool())
    .await?;
    effects.extend(report_artifacts.into_iter().map(|row| row.0.0));
    let archives: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('kind','archive','gzip_sha256',gzip_sha256, \
         'status',status,'byte_count',byte_count,'last_error',last_error, \
         'upload_attempts',upload_attempts,'updated_at',updated_at, \
         'age_seconds',EXTRACT(EPOCH FROM now()-updated_at)) \
         FROM tasks.archive_uploads WHERE status<>'verified' ORDER BY updated_at,gzip_sha256",
    )
    .fetch_all(database.pool())
    .await?;
    effects.extend(archives.into_iter().map(|row| row.0.0));
    let labels: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('id',w.id,'kind','stage_label', \
         'task_id',w.task_id,'revision',w.revision,'action',w.action,'label',w.label, \
         'status',w.status,'updated_at',w.updated_at, \
         'age_seconds',EXTRACT(EPOCH FROM now()-w.updated_at), \
         'desired',s.desired,'desired_revision',s.revision,'watch',s.watch) \
         FROM tasks.stage_label_writes w JOIN tasks.stage_label_surfaces s ON s.task_id=w.task_id \
         WHERE w.status IN ('sending','obsolete_uncertain') ORDER BY w.updated_at,w.id",
    )
    .fetch_all(database.pool())
    .await?;
    effects.extend(labels.into_iter().map(|row| row.0.0));
    let dashboards: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('kind','dashboard_edit','task_id',s.task_id, \
         'repository_id',s.repository_id,'issue_number',s.issue_number, \
         'revision',s.revision,'comment_id',c.comment_id,'watch',s.watch, \
         'last_error',s.last_error,'retry_at',s.retry_at,'updated_at',s.updated_at, \
         'age_seconds',EXTRACT(EPOCH FROM now()-s.updated_at)) \
         FROM tasks.dashboard_surfaces s \
         JOIN tasks.publication_comments c ON c.id=s.create_id \
         WHERE s.watch AND c.comment_id IS NOT NULL ORDER BY s.updated_at,s.task_id",
    )
    .fetch_all(database.pool())
    .await?;
    effects.extend(dashboards.into_iter().map(|row| row.0.0));
    let runners: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('kind','runner','session_id',session_id,'turn_id',turn_id, \
         'fork',fork,'config_sha',config_sha,'desired_present',desired_present, \
         'ref_revision',ref_revision,'ref_watch',ref_watch,'ref_error',ref_error, \
         'dispatch_status',dispatch_status,'dispatch_error',dispatch_error, \
         'run_id',run_id,'updated_at',updated_at, \
         'age_seconds',EXTRACT(EPOCH FROM now()-updated_at)) \
         FROM tasks.runner_sessions WHERE ref_watch OR ref_error IS NOT NULL \
         OR dispatch_status IN ('recorded','sending','obsolete_uncertain','sent','failed') \
         ORDER BY updated_at,session_id",
    )
    .fetch_all(database.pool())
    .await?;
    effects.extend(runners.into_iter().map(|row| row.0.0));
    let label_wakes: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('kind','label_wake','delivery_id',delivery_id, \
         'repository_id',repository_id,'issue_number',issue_number,'action',action, \
         'actor_id',actor_id,'updated_at',updated_at,'scan_attempts',scan_attempts, \
         'retry_at',retry_at,'last_error',last_error,'exhausted_at',exhausted_at, \
         'resolution',CASE WHEN exhausted_at IS NULL THEN 'retry' \
             ELSE 'reconciliation_required' END) \
         FROM tasks.label_wakes WHERE observed_at IS NULL ORDER BY retry_at,delivery_id",
    )
    .fetch_all(database.pool())
    .await?;
    effects.extend(label_wakes.into_iter().map(|row| row.0.0));
    let review_wakes: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('kind','standalone_review_wake', \
         'delivery_id',delivery_id,'repository_id',repository_id,'upstream',upstream, \
         'pr_number',pr_number,'action',action,'actor_id',actor_id, \
         'reviewer_id',reviewer_id,'updated_at',updated_at, \
         'scan_attempts',scan_attempts,'retry_at',retry_at,'last_error',last_error, \
         'exhausted_at',exhausted_at, \
         'resolution',CASE WHEN exhausted_at IS NULL THEN 'retry' \
             ELSE 'reconciliation_required' END) \
         FROM tasks.standalone_review_wakes WHERE observed_at IS NULL \
         ORDER BY retry_at,delivery_id",
    )
    .fetch_all(database.pool())
    .await?;
    effects.extend(review_wakes.into_iter().map(|row| row.0.0));
    Ok(effects)
}

/// Record a host operator's attempt cancellation and queue exchange retirement.
pub async fn cancel_attempt(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    turn: Uuid,
    actor: &str,
) -> anyhow::Result<bool> {
    ensure!(!actor.trim().is_empty(), "operator actor is empty");
    let changed = if let Some(state) = lock_issue_for_turn(uow, turn).await? {
        let changed = cancel_attempts(uow, outbox, state.id.0, Some(turn)).await? > 0;
        if changed {
            settle_attempt(uow, outbox, &state, turn).await?;
        }
        changed
    } else {
        false
    };
    sqlx::query(
        "INSERT INTO tasks.operator_actions (actor,action,target,outcome) \
         VALUES ($1,'attempts.cancel',$2,$3)",
    )
    .bind(actor)
    .bind(turn.to_string())
    .bind(if changed { "cancelled" } else { "noop" })
    .execute(uow.connection())
    .await?;
    Ok(changed)
}
