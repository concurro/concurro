//! Request ordering and replay against a real PostgreSQL transaction boundary.

use std::collections::BTreeMap;
use std::io::Write;

use base64::{Engine, engine::general_purpose::STANDARD};
use chrono::{DateTime, Utc};
use concurro_contracts::{TaskReport, TurnFinished, decode};
use concurro_db::Database;
use concurro_events_postgres::{Outbox, migrate as migrate_events};
use concurro_exchange_v1::{TurnObservation, TurnSubmission};
use concurro_tasks_core::{
    Actor, Admission, AttemptSpec, Authority, CommentRevision, ConsentAction, ConsentFact,
    InitializationSnapshot, IssueKey, LaterCommand, PinnedInputs, Provider, Request, RequestKind,
    RequestStatus, Requirements, SourceOrder, SponsorSlot, Stage, python_digest,
};
use concurro_tasks_postgres::{
    CommandEvidence, EffectKind, RetainedEligibility, acknowledge_retirement, admit_intake,
    apply_request, apply_request_with_evidence, apply_request_with_evidence_mode, baseline_page,
    browser_action, browser_retry_feedback, cancel_attempt, claim_dispatch, claim_effect,
    complete_withdrawal_step, confirm_current_sponsorship_sources, consent_projection,
    create_baseline, due_attempt_deadlines, due_withdrawal_steps, expire_attempt,
    fail_pr_review_action, initialize_task, latest_intake_correction, list_tasks,
    mark_pr_review_action_sent, migrate, next_pending, pending_effects, record_browser_action,
    record_comment_revision, record_observation, record_poll_failure, record_request,
    record_sponsorship_transition, record_submit_failure, record_withdraw_transition,
    recover_retirements, request_record, resolve_pending_write, retained_eligibility,
    runner_session, settle_submit, settle_submit_refusal, task, task_detail, transcript_page,
    turn_artifact,
};
use concurro_testkit::PostgresContainer;
use flate2::{Compression, write::GzEncoder};
use serde_json::{Value, json};
use sqlx::types::Json as SqlJson;
use tar::{Builder, Header};
use uuid::Uuid;

/// A paused writer start spends one revision without erasing prior model-call spend.
#[tokio::test]
async fn blocked_start_reassesses_only_with_revision_capacity() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("task schema");
    migrate_events(&database).await.expect("event schema");
    let outbox = Outbox::new(
        1024 * 1024,
        vec!["request.>".to_owned(), "task.>".to_owned()],
    );
    let authority = Authority {
        writer: true,
        issue_open: true,
    };
    let first = request(10, RequestKind::Start);
    let mut work = database.begin().await.expect("record first start");
    record_request(&mut work, &first).await.expect("record");
    work.commit().await.expect("record commit");
    let mut work = database.begin().await.expect("apply first start");
    let applied = apply_request(&mut work, &outbox, first.id, first.issue, authority)
        .await
        .expect("decision")
        .expect("pending")
        .1;
    work.commit().await.expect("start commit");
    let mut state = applied.task.expect("started task");
    let pins = PinnedInputs {
        config_sha: "a".repeat(40),
        base_sha: "b".repeat(40),
        max_model_calls: 10,
        max_fix_rounds: 2,
        max_plan_revisions: 1,
    };
    let mut work = database.begin().await.expect("initialize");
    state = initialize_task(
        &mut work,
        &outbox,
        first.issue,
        state.revision,
        &snapshot(pins.clone()),
    )
    .await
    .expect("initialization")
    .expect("current task");
    work.commit().await.expect("initialize commit");
    state.stage = Stage::Blocked;
    state.used_model_calls = 3;
    state.revision += 1;
    sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
        .bind(state.id.0)
        .bind(i64::try_from(state.revision).expect("revision"))
        .bind(SqlJson(&state))
        .execute(database.pool())
        .await
        .expect("pause task");

    let reassess = request(20, RequestKind::Start);
    let mut work = database.begin().await.expect("record reassessment");
    record_request(&mut work, &reassess).await.expect("record");
    work.commit().await.expect("record commit");
    let mut work = database.begin().await.expect("apply reassessment");
    let applied = apply_request(&mut work, &outbox, reassess.id, reassess.issue, authority)
        .await
        .expect("decision")
        .expect("pending")
        .1;
    work.commit().await.expect("reassessment commit");
    assert_eq!(applied.status, RequestStatus::Applied);
    let resumed = applied.task.expect("reassessing task");
    assert_eq!(resumed.stage, Stage::Initializing);
    assert_eq!(resumed.id, state.id);
    assert_eq!(resumed.used_model_calls, 3);
    let spent: i64 = sqlx::query_scalar(
        "SELECT spent_revisions FROM tasks.initialization_inputs WHERE task_id=$1",
    )
    .bind(state.id.0)
    .fetch_one(database.pool())
    .await
    .expect("revision spend");
    assert_eq!(spent, 1);

    state = resumed;
    state.stage = Stage::Blocked;
    state.pins = Some(pins);
    state.revision += 1;
    sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
        .bind(state.id.0)
        .bind(i64::try_from(state.revision).expect("revision"))
        .bind(SqlJson(&state))
        .execute(database.pool())
        .await
        .expect("pause again");
    let exhausted = request(30, RequestKind::Start);
    let mut work = database.begin().await.expect("record exhausted start");
    record_request(&mut work, &exhausted).await.expect("record");
    work.commit().await.expect("record commit");
    let mut work = database.begin().await.expect("apply exhausted start");
    let refused = apply_request(&mut work, &outbox, exhausted.id, exhausted.issue, authority)
        .await
        .expect("decision")
        .expect("pending")
        .1;
    work.commit().await.expect("refusal commit");
    assert_eq!(refused.status, RequestStatus::Refused);
    assert_eq!(refused.reason, Some("plan_revision_exhausted"));
    assert_eq!(
        task(&database, exhausted.issue)
            .await
            .expect("task")
            .expect("task")
            .stage,
        Stage::Blocked
    );
    let mut waiting = task(&database, exhausted.issue)
        .await
        .expect("task")
        .expect("waiting task");
    waiting.stage = Stage::Escalated;
    waiting.stage_reason =
        Some("Plan revision budget exhausted: design reviewers require a revision".to_owned());
    waiting.revision += 1;
    sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
        .bind(waiting.id.0)
        .bind(i64::try_from(waiting.revision).expect("revision"))
        .bind(SqlJson(&waiting))
        .execute(database.pool())
        .await
        .expect("waiting revision");
    let grant = request(
        40,
        RequestKind::Later(LaterCommand::Retry {
            plan_revisions: Some(2),
        }),
    );
    let mut work = database.begin().await.expect("record grant");
    record_request(&mut work, &grant).await.expect("record");
    work.commit().await.expect("record commit");
    let mut work = database.begin().await.expect("apply grant");
    let granted = apply_request_with_evidence(
        &mut work,
        &outbox,
        grant.id,
        grant.issue,
        authority,
        Some(&CommandEvidence {
            snapshot: snapshot(waiting.pins.clone().expect("pins")),
            pr_unchanged: true,
            implementer_skill_update: None,
        }),
    )
    .await
    .expect("decision")
    .expect("pending")
    .1;
    work.commit().await.expect("grant commit");
    assert_eq!(granted.status, RequestStatus::Applied);
    let resumed = granted.task.expect("resumed plan");
    assert_eq!(resumed.stage, Stage::Plan);
    assert_eq!(resumed.used_model_calls, 3);
    let budget: (i64, i64) = sqlx::query_as(
        "SELECT spent_revisions,extra_plan_revisions FROM tasks.initialization_inputs WHERE task_id=$1",
    )
    .bind(resumed.id.0)
    .fetch_one(database.pool())
    .await
    .expect("granted budget");
    assert_eq!(budget, (2, 2));
    let invalidate = request(50, RequestKind::Later(LaterCommand::Invalidate));
    let mut changed_source = snapshot(resumed.pins.clone().expect("pins"));
    changed_source.requirements.body = Some("Revised description".to_owned());
    let mut work = database.begin().await.expect("record invalidation");
    record_request(&mut work, &invalidate)
        .await
        .expect("record");
    work.commit().await.expect("record commit");
    let mut work = database.begin().await.expect("apply invalidation");
    let invalidated = apply_request_with_evidence(
        &mut work,
        &outbox,
        invalidate.id,
        invalidate.issue,
        authority,
        Some(&CommandEvidence {
            snapshot: changed_source.clone(),
            pr_unchanged: true,
            implementer_skill_update: None,
        }),
    )
    .await
    .expect("decision")
    .expect("pending")
    .1;
    work.commit().await.expect("invalidation commit");
    assert_eq!(invalidated.status, RequestStatus::Applied);
    let restarted = invalidated.task.expect("reassessment task");
    assert_eq!(restarted.stage, Stage::Intake);
    assert_eq!(restarted.used_model_calls, 3);
    let saved_body: String = sqlx::query_scalar(
        "SELECT snapshot->'requirements'->>'body' FROM tasks.initialization_inputs WHERE task_id=$1",
    )
    .bind(restarted.id.0)
    .fetch_one(database.pool())
    .await
    .expect("revised requirements");
    assert_eq!(saved_body, "Revised description");
    let invalidation_actor: String = sqlx::query_scalar(
        "SELECT actor->>'login' FROM tasks.task_invalidations WHERE request_id=$1",
    )
    .bind(invalidate.id)
    .fetch_one(database.pool())
    .await
    .expect("invalidation audit");
    assert_eq!(invalidation_actor, "writer");
    let mut gated = restarted;
    gated.stage = Stage::Gates;
    gated.revision += 1;
    sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
        .bind(gated.id.0)
        .bind(i64::try_from(gated.revision).expect("revision"))
        .bind(SqlJson(&gated))
        .execute(database.pool())
        .await
        .expect("gate fixture");
    let retry = request(
        60,
        RequestKind::Later(LaterCommand::Retry {
            plan_revisions: None,
        }),
    );
    let mut work = database.begin().await.expect("record retry");
    record_request(&mut work, &retry).await.expect("record");
    work.commit().await.expect("record commit");
    let mut work = database.begin().await.expect("apply retry");
    let retried = apply_request_with_evidence(
        &mut work,
        &outbox,
        retry.id,
        retry.issue,
        authority,
        Some(&CommandEvidence {
            snapshot: changed_source.clone(),
            pr_unchanged: true,
            implementer_skill_update: None,
        }),
    )
    .await
    .expect("decision")
    .expect("pending")
    .1;
    work.commit().await.expect("retry commit");
    assert_eq!(retried.status, RequestStatus::Applied);
    let mut paused = retried.task.expect("gates task");
    assert_eq!(paused.stage, Stage::Gates);
    paused.stage = Stage::Blocked;
    paused.retry_stage = Some(Stage::Plan);
    paused.stage_reason = Some("Planner output repair allowance exhausted".to_owned());
    paused.revision += 1;
    sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
        .bind(paused.id.0)
        .bind(i64::try_from(paused.revision).expect("revision"))
        .bind(SqlJson(&paused))
        .execute(database.pool())
        .await
        .expect("pause planner");
    let repair = request(
        70,
        RequestKind::Later(LaterCommand::Retry {
            plan_revisions: None,
        }),
    );
    let mut work = database.begin().await.expect("record planner retry");
    record_request(&mut work, &repair).await.expect("record");
    work.commit().await.expect("record commit");
    let mut work = database.begin().await.expect("apply planner retry");
    let repaired = apply_request_with_evidence(
        &mut work,
        &outbox,
        repair.id,
        repair.issue,
        authority,
        Some(&CommandEvidence {
            snapshot: changed_source.clone(),
            pr_unchanged: true,
            implementer_skill_update: None,
        }),
    )
    .await
    .expect("decision")
    .expect("pending")
    .1;
    work.commit().await.expect("planner retry commit");
    assert_eq!(repaired.status, RequestStatus::Applied);
    let resumed_plan = repaired.task.expect("planner task");
    assert_eq!(resumed_plan.stage, Stage::Plan);
    assert_eq!(resumed_plan.retry_stage, None);
    assert_eq!(resumed_plan.used_model_calls, 3);

    let planner_turn = Uuid::now_v7();
    sqlx::query(
        "INSERT INTO tasks.attempts (id,task_id,operation,expected_revision,staffing_group, \
         fork,config_sha,submit_body,initial_deadline,unbounded_running,status) \
         VALUES ($1,$2,'plan',$3,'planner','acme/fork',$4,'{}',now(),false,'finished')",
    )
    .bind(planner_turn)
    .bind(resumed_plan.id.0)
    .bind(i64::try_from(resumed_plan.revision).expect("revision"))
    .bind(&resumed_plan.pins.as_ref().expect("pins").config_sha)
    .execute(database.pool())
    .await
    .expect("planner turn");
    sqlx::query(
        "INSERT INTO tasks.plan_reports (attempt_id,task_id,initialization_revision, \
         raw_report,raw_report_bytes,accepted,plan_id) VALUES ($1,$2,1,'{}','{}',true,'plan')",
    )
    .bind(planner_turn)
    .bind(resumed_plan.id.0)
    .execute(database.pool())
    .await
    .expect("reviewed plan");
    let batch = Uuid::now_v7();
    sqlx::query(
        "INSERT INTO tasks.plan_review_batches \
         (id,task_id,plan_id,generation,phase,binding,roster,status) \
         VALUES ($1,$2,'plan',0,'cooperative','binding','[]','superseded')",
    )
    .bind(batch)
    .bind(resumed_plan.id.0)
    .execute(database.pool())
    .await
    .expect("review batch");
    for (reviewer, status) in [("failed", "failed"), ("accepted", "accepted")] {
        sqlx::query(
            "INSERT INTO tasks.plan_review_seats \
             (batch_id,reviewer_id,identity,status,failure_count) \
             VALUES ($1,$2,'{}',$3,2)",
        )
        .bind(batch)
        .bind(reviewer)
        .bind(status)
        .execute(database.pool())
        .await
        .expect("review seat");
    }
    let mut paused_review = resumed_plan;
    paused_review.stage = Stage::Blocked;
    paused_review.retry_stage = Some(Stage::PlanQc);
    paused_review.revision += 1;
    sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
        .bind(paused_review.id.0)
        .bind(i64::try_from(paused_review.revision).expect("revision"))
        .bind(SqlJson(&paused_review))
        .execute(database.pool())
        .await
        .expect("pause design review");
    let retry_review = request(
        80,
        RequestKind::Later(LaterCommand::Retry {
            plan_revisions: None,
        }),
    );
    let mut work = database.begin().await.expect("record design retry");
    record_request(&mut work, &retry_review)
        .await
        .expect("record");
    work.commit().await.expect("record commit");
    let mut work = database.begin().await.expect("apply design retry");
    let reviewed = apply_request_with_evidence(
        &mut work,
        &outbox,
        retry_review.id,
        retry_review.issue,
        authority,
        Some(&CommandEvidence {
            snapshot: changed_source.clone(),
            pr_unchanged: true,
            implementer_skill_update: None,
        }),
    )
    .await
    .expect("decision")
    .expect("pending")
    .1;
    work.commit().await.expect("design retry commit");
    assert_eq!(reviewed.status, RequestStatus::Applied);
    let mut paused_pr = reviewed.task.expect("design task");
    assert_eq!(paused_pr.stage, Stage::PlanQc);
    let seats: Vec<(String, String, i32)> = sqlx::query_as(
        "SELECT reviewer_id,status,failure_count FROM tasks.plan_review_seats \
         WHERE batch_id=$1 ORDER BY reviewer_id",
    )
    .bind(batch)
    .fetch_all(database.pool())
    .await
    .expect("review seats");
    assert_eq!(
        seats,
        vec![
            ("accepted".to_owned(), "accepted".to_owned(), 2),
            ("failed".to_owned(), "retrying".to_owned(), 0),
        ]
    );

    let pr_batch = Uuid::now_v7();
    let pr_turn = Uuid::now_v7();
    sqlx::query(
        "INSERT INTO tasks.attempts (id,task_id,operation,expected_revision,staffing_group, \
         fork,config_sha,submit_body,initial_deadline,unbounded_running,status) \
         VALUES ($1,$2,'review',$3,'reviewer','acme/fork',$4,'{}',now(),false,'finished')",
    )
    .bind(pr_turn)
    .bind(paused_pr.id.0)
    .bind(i64::try_from(paused_pr.revision).expect("revision"))
    .bind(&paused_pr.pins.as_ref().expect("pins").config_sha)
    .execute(database.pool())
    .await
    .expect("PR review turn");
    sqlx::query(
        "INSERT INTO tasks.pr_review_batches (id,task_id,publication_attempt,pr_number, \
         head,branch,plan_id,round,changed_paths,roster,status) \
         VALUES ($1,$2,$3,19,$4,'concurro/task','plan',0,'[]','[]','superseded')",
    )
    .bind(pr_batch)
    .bind(paused_pr.id.0)
    .bind(planner_turn)
    .bind("e".repeat(40))
    .execute(database.pool())
    .await
    .expect("PR review batch");
    for (reviewer, status) in [("failed", "failed"), ("accepted", "accepted")] {
        sqlx::query(
            "INSERT INTO tasks.pr_review_seats \
             (batch_id,reviewer_id,identity,binding,status,failure_count) \
             VALUES ($1,$2,'{}','binding',$3,2)",
        )
        .bind(pr_batch)
        .bind(reviewer)
        .bind(status)
        .execute(database.pool())
        .await
        .expect("PR review seat");
    }
    sqlx::query(
        "INSERT INTO tasks.pr_review_publications \
         (turn_id,task_id,batch_id,pr_number,head,marker,body,status,error) \
         VALUES ($1,$2,$3,19,$4,'marker','marker body','failed','HTTP 403')",
    )
    .bind(pr_turn)
    .bind(paused_pr.id.0)
    .bind(pr_batch)
    .bind("e".repeat(40))
    .execute(database.pool())
    .await
    .expect("refused native review");
    sqlx::query(
        "INSERT INTO tasks.pr_review_actions \
         (turn_id,sequence,kind,marker,body,status) \
         VALUES ($1,0,'create','marker','marker','sent')",
    )
    .bind(pr_turn)
    .execute(database.pool())
    .await
    .expect("sent native action");
    let mut work = database.begin().await.expect("definite refusal");
    assert!(
        fail_pr_review_action(&mut work, pr_turn, 0)
            .await
            .expect("mark failed action")
    );
    work.commit().await.expect("refusal commit");
    paused_pr.stage = Stage::Blocked;
    paused_pr.retry_stage = Some(Stage::Review);
    paused_pr.revision += 1;
    sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
        .bind(paused_pr.id.0)
        .bind(i64::try_from(paused_pr.revision).expect("revision"))
        .bind(SqlJson(&paused_pr))
        .execute(database.pool())
        .await
        .expect("pause PR review");
    let retry_pr = request(
        90,
        RequestKind::Later(LaterCommand::Retry {
            plan_revisions: None,
        }),
    );
    let mut work = database.begin().await.expect("record PR retry");
    record_request(&mut work, &retry_pr).await.expect("record");
    work.commit().await.expect("record commit");
    let mut work = database.begin().await.expect("apply PR retry");
    let pr_retried = apply_request_with_evidence(
        &mut work,
        &outbox,
        retry_pr.id,
        retry_pr.issue,
        authority,
        Some(&CommandEvidence {
            snapshot: changed_source.clone(),
            pr_unchanged: true,
            implementer_skill_update: None,
        }),
    )
    .await
    .expect("decision")
    .expect("pending")
    .1;
    work.commit().await.expect("PR retry commit");
    assert_eq!(pr_retried.status, RequestStatus::Applied);
    let mut paused_implementation = pr_retried.task.expect("PR review task");
    assert_eq!(paused_implementation.stage, Stage::Review);
    let pr_seats: Vec<(String, String, i32)> = sqlx::query_as(
        "SELECT reviewer_id,status,failure_count FROM tasks.pr_review_seats \
         WHERE batch_id=$1 ORDER BY reviewer_id",
    )
    .bind(pr_batch)
    .fetch_all(database.pool())
    .await
    .expect("PR review seats");
    assert_eq!(
        pr_seats,
        vec![
            ("accepted".to_owned(), "accepted".to_owned(), 2),
            ("failed".to_owned(), "retrying".to_owned(), 0),
        ]
    );
    let publication_status: String =
        sqlx::query_scalar("SELECT status FROM tasks.pr_review_publications WHERE turn_id=$1")
            .bind(pr_turn)
            .fetch_one(database.pool())
            .await
            .expect("native review status");
    assert_eq!(publication_status, "recorded");
    let action_status: String = sqlx::query_scalar(
        "SELECT status FROM tasks.pr_review_actions WHERE turn_id=$1 AND sequence=0",
    )
    .bind(pr_turn)
    .fetch_one(database.pool())
    .await
    .expect("reopened action");
    assert_eq!(action_status, "recorded");

    let sibling_turn = Uuid::now_v7();
    sqlx::query(
        "INSERT INTO tasks.attempts (id,task_id,operation,expected_revision,staffing_group, \
         fork,config_sha,submit_body,initial_deadline,unbounded_running,status) \
         VALUES ($1,$2,'review',$3,'reviewer','acme/fork',$4,'{}',now(),false,'finished')",
    )
    .bind(sibling_turn)
    .bind(paused_implementation.id.0)
    .bind(i64::try_from(paused_implementation.revision).expect("revision"))
    .bind(
        &paused_implementation
            .pins
            .as_ref()
            .expect("pins")
            .config_sha,
    )
    .execute(database.pool())
    .await
    .expect("sibling turn");
    sqlx::query(
        "UPDATE tasks.pr_review_publications SET status='sent',bot_user_id=99 WHERE turn_id=$1",
    )
    .bind(pr_turn)
    .execute(database.pool())
    .await
    .expect("first pending review");
    sqlx::query(
        "UPDATE tasks.pr_review_actions SET status='confirmed' WHERE turn_id=$1 AND sequence=0",
    )
    .bind(pr_turn)
    .execute(database.pool())
    .await
    .expect("first create confirmed");
    sqlx::query(
        "INSERT INTO tasks.pr_review_actions \
         (turn_id,sequence,kind,marker,body,status) \
         VALUES ($1,1,'submit','marker','body','recorded')",
    )
    .bind(pr_turn)
    .execute(database.pool())
    .await
    .expect("first submit pending");
    sqlx::query(
        "INSERT INTO tasks.pr_review_publications \
         (turn_id,task_id,batch_id,pr_number,head,marker,body,status,bot_user_id) \
         VALUES ($1,$2,$3,19,$4,'sibling','sibling body','sent',99)",
    )
    .bind(sibling_turn)
    .bind(paused_implementation.id.0)
    .bind(pr_batch)
    .bind("e".repeat(40))
    .execute(database.pool())
    .await
    .expect("sibling pending review");
    for (sequence, kind) in [(0, "create"), (1, "submit")] {
        sqlx::query(
            "INSERT INTO tasks.pr_review_actions (turn_id,sequence,kind,marker,body) \
             VALUES ($1,$2,$3,'sibling','sibling body')",
        )
        .bind(sibling_turn)
        .bind(sequence)
        .bind(kind)
        .execute(database.pool())
        .await
        .expect("sibling action");
    }
    let mut work = database.begin().await.expect("sibling create fence");
    assert!(
        !mark_pr_review_action_sent(&mut work, sibling_turn, 0)
            .await
            .expect("first review still pending")
    );
    work.commit().await.expect("pending fence commit");
    sqlx::query(
        "UPDATE tasks.pr_review_actions SET status='confirmed' \
         WHERE turn_id=$1 AND kind='submit'",
    )
    .bind(pr_turn)
    .execute(database.pool())
    .await
    .expect("first submit confirmed");
    let mut work = database.begin().await.expect("sibling create after submit");
    assert!(
        mark_pr_review_action_sent(&mut work, sibling_turn, 0)
            .await
            .expect("sibling create unblocked")
    );
    work.commit().await.expect("sibling fence commit");
    let mut work = database.begin().await.expect("resolve uncertain action");
    assert!(
        resolve_pending_write(
            &mut work,
            &outbox,
            "action",
            sibling_turn,
            Some(0),
            "host-user",
            true,
        )
        .await
        .expect("operator reopen")
    );
    work.commit().await.expect("operator resolution commit");
    let reopened: String = sqlx::query_scalar(
        "SELECT status FROM tasks.pr_review_actions WHERE turn_id=$1 AND sequence=0",
    )
    .bind(sibling_turn)
    .fetch_one(database.pool())
    .await
    .expect("reopened sibling action");
    assert_eq!(reopened, "recorded");
    let operator_action: (String, String) = sqlx::query_as(
        "SELECT actor,outcome FROM tasks.operator_actions WHERE target=$1 ORDER BY id DESC LIMIT 1",
    )
    .bind(format!("{sibling_turn}:0"))
    .fetch_one(database.pool())
    .await
    .expect("audited operator resolution");
    assert_eq!(operator_action, ("host-user".into(), "recorded".into()));
    sqlx::query("DELETE FROM tasks.pr_review_actions WHERE turn_id IN ($1,$2)")
        .bind(pr_turn)
        .bind(sibling_turn)
        .execute(database.pool())
        .await
        .expect("remove fence fixture actions");
    sqlx::query("DELETE FROM tasks.pr_review_publications WHERE turn_id=$1")
        .bind(sibling_turn)
        .execute(database.pool())
        .await
        .expect("remove sibling publication");
    sqlx::query("DELETE FROM tasks.attempts WHERE id=$1")
        .bind(sibling_turn)
        .execute(database.pool())
        .await
        .expect("remove sibling turn");

    sqlx::query(
        "UPDATE tasks.pr_review_publications SET status='confirmed',review_id=27 \
         WHERE turn_id=$1",
    )
    .bind(pr_turn)
    .execute(database.pool())
    .await
    .expect("settle native review fixture");
    paused_implementation.stage = Stage::Blocked;
    paused_implementation.retry_stage = Some(Stage::Implement);
    paused_implementation.revision += 1;
    sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
        .bind(paused_implementation.id.0)
        .bind(i64::try_from(paused_implementation.revision).expect("revision"))
        .bind(SqlJson(&paused_implementation))
        .execute(database.pool())
        .await
        .expect("pause implementation");
    let mut fresh = changed_source;
    fresh.pins.config_sha = "c".repeat(40);
    fresh.pins.base_sha = "d".repeat(40);
    fresh.pins.max_model_calls = 15;
    fresh.bundle_id = "new-bundle".to_owned();
    fresh.configuration = json!({
        "planner":"codex","implementer":"codex","qc":"claude",
        "reviewers":["codex"],"models":{"codex":"gpt","claude":"sonnet"}
    });
    fresh.requirements.body = Some("Unapproved new issue text".to_owned());
    let resume = request(
        100,
        RequestKind::Later(LaterCommand::Resume {
            description_only: false,
        }),
    );
    let mut work = database.begin().await.expect("record resume");
    record_request(&mut work, &resume).await.expect("record");
    work.commit().await.expect("record commit");
    let mut work = database.begin().await.expect("apply resume");
    let resumed = apply_request_with_evidence(
        &mut work,
        &outbox,
        resume.id,
        resume.issue,
        authority,
        Some(&CommandEvidence {
            snapshot: fresh,
            pr_unchanged: true,
            implementer_skill_update: None,
        }),
    )
    .await
    .expect("decision")
    .expect("pending")
    .1;
    work.commit().await.expect("resume commit");
    assert_eq!(resumed.status, RequestStatus::Applied);
    let continued = resumed.task.expect("implementation task");
    assert_eq!(continued.stage, Stage::Implement);
    assert_eq!(continued.used_model_calls, 3);
    assert_eq!(
        continued.pins.as_ref().expect("pins").base_sha,
        "b".repeat(40)
    );
    assert_eq!(continued.pins.as_ref().expect("pins").max_model_calls, 15);
    let adopted: SqlJson<InitializationSnapshot> =
        sqlx::query_scalar("SELECT snapshot FROM tasks.initialization_inputs WHERE task_id=$1")
            .bind(continued.id.0)
            .fetch_one(database.pool())
            .await
            .expect("adopted snapshot");
    assert_eq!(adopted.0.bundle_id, "new-bundle");
    assert_eq!(adopted.0.pins.base_sha, "b".repeat(40));
    assert_eq!(
        adopted.0.requirements.body.as_deref(),
        Some("Revised description")
    );
    let receipt: (String, String) = sqlx::query_as(
        "SELECT prior_snapshot->>'bundle_id', \
         adopted_snapshot->>'bundle_id' FROM tasks.task_resumes WHERE request_id=$1",
    )
    .bind(resume.id)
    .fetch_one(database.pool())
    .await
    .expect("resume receipt");
    assert_eq!(
        receipt,
        ("bundle-identity".to_owned(), "new-bundle".to_owned())
    );

    let mut reviewing = continued;
    reviewing.stage = Stage::PlanQc;
    reviewing.revision += 1;
    sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
        .bind(reviewing.id.0)
        .bind(i64::try_from(reviewing.revision).expect("revision"))
        .bind(SqlJson(&reviewing))
        .execute(database.pool())
        .await
        .expect("active design review");
    let mut third = adopted.0;
    third.bundle_id = "third-bundle".to_owned();
    let resume_review = request(
        110,
        RequestKind::Later(LaterCommand::Resume {
            description_only: false,
        }),
    );
    let mut work = database.begin().await.expect("record review resume");
    record_request(&mut work, &resume_review)
        .await
        .expect("record");
    work.commit().await.expect("record commit");
    let mut work = database.begin().await.expect("apply review resume");
    let review_resumed = apply_request_with_evidence(
        &mut work,
        &outbox,
        resume_review.id,
        resume_review.issue,
        authority,
        Some(&CommandEvidence {
            snapshot: third,
            pr_unchanged: true,
            implementer_skill_update: None,
        }),
    )
    .await
    .expect("decision")
    .expect("pending")
    .1;
    work.commit().await.expect("review resume commit");
    assert_eq!(review_resumed.status, RequestStatus::Applied);
    assert_eq!(
        review_resumed.task.expect("reviewing task").stage,
        Stage::PlanQc
    );
    let fresh_batch: (String, String) = sqlx::query_as(
        "SELECT status,snapshot->>'bundle_id' FROM tasks.plan_review_batches \
         WHERE task_id=$1 ORDER BY generation DESC,id DESC LIMIT 1",
    )
    .bind(reviewing.id.0)
    .fetch_one(database.pool())
    .await
    .expect("new review batch");
    assert_eq!(
        fresh_batch,
        ("recorded".to_owned(), "third-bundle".to_owned())
    );
    let prior_batch_status: String =
        sqlx::query_scalar("SELECT status FROM tasks.plan_review_batches WHERE id=$1")
            .bind(batch)
            .fetch_one(database.pool())
            .await
            .expect("prior review batch");
    assert_eq!(prior_batch_status, "superseded");

    let mut publication_task = task(&database, first.issue)
        .await
        .expect("task read")
        .expect("task");
    publication_task.stage = Stage::Publish;
    publication_task.revision += 1;
    sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
        .bind(publication_task.id.0)
        .bind(i64::try_from(publication_task.revision).expect("revision"))
        .bind(SqlJson(&publication_task))
        .execute(database.pool())
        .await
        .expect("publish recovery fixture");
    let intent = json!({
        "branch": "concurro/task", "parent": "a".repeat(40),
        "commit_date": "2026-11-20T10:00:00Z", "branch_created": true,
        "commit_sha": "b".repeat(40), "ref_advanced": true,
        "pr_title": "Task", "pr_body": "Revised body",
        "pr_create_sent": true, "pr_update_sent": true, "pr_number": 19
    });
    sqlx::query(
        "INSERT INTO tasks.git_publications (task_id,attempt_id,intent) \
         VALUES ($1,$2,$3)",
    )
    .bind(publication_task.id.0)
    .bind(pr_turn)
    .bind(SqlJson(&intent))
    .execute(database.pool())
    .await
    .expect("uncertain PR description");
    let mut work = database.begin().await.expect("reopen description");
    assert!(
        resolve_pending_write(&mut work, &outbox, "git", pr_turn, None, "host-user", true)
            .await
            .expect("operator resend")
    );
    work.commit().await.expect("reopen commit");
    let reopened: SqlJson<Value> =
        sqlx::query_scalar("SELECT intent FROM tasks.git_publications WHERE attempt_id=$1")
            .bind(pr_turn)
            .fetch_one(database.pool())
            .await
            .expect("reopened description intent");
    assert_eq!(reopened.0["pr_update_sent"], false);
    assert_eq!(reopened.0["pr_create_sent"], true);
    sqlx::query(
        "UPDATE tasks.git_publications SET intent=jsonb_set(intent,'{pr_update_sent}','true'::jsonb) \
         WHERE attempt_id=$1",
    )
    .bind(pr_turn)
    .execute(database.pool())
    .await
    .expect("simulate later uncertain update");
    let mut work = database.begin().await.expect("block uncertain description");
    assert!(
        resolve_pending_write(&mut work, &outbox, "git", pr_turn, None, "host-user", false)
            .await
            .expect("operator block")
    );
    work.commit().await.expect("block commit");
    let blocked = task(&database, first.issue)
        .await
        .expect("task read")
        .expect("task");
    assert_eq!(blocked.stage, Stage::Blocked);
    assert_eq!(blocked.retry_stage, Some(Stage::Publish));
}

/// Baseline pages retain one source snapshot while later decisions keep changing.
#[tokio::test]
async fn baseline_pages_keep_one_consistent_request_history() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("task schema");
    let empty = create_baseline(&database).await.expect("empty baseline");
    assert!(
        baseline_page(&database, empty, 0, 10)
            .await
            .expect("empty page")
            .expect("empty snapshot")
            .is_empty()
    );
    let first = request(10, RequestKind::Start);
    let second = request(20, RequestKind::Stop);
    let mut work = database.begin().await.expect("request transaction");
    record_request(&mut work, &first)
        .await
        .expect("first request");
    record_request(&mut work, &second)
        .await
        .expect("second request");
    work.commit().await.expect("request commit");
    let snapshot = create_baseline(&database).await.expect("baseline");
    let first_page = baseline_page(&database, snapshot, 0, 1)
        .await
        .expect("page one")
        .expect("snapshot");
    assert_eq!(first_page.len(), 1);
    assert_eq!(first_page[0].body["status"], "pending");
    sqlx::query("UPDATE tasks.requests SET status='refused',reason='changed' WHERE id=$1")
        .bind(second.id)
        .execute(database.pool())
        .await
        .expect("later decision");
    let third = request(30, RequestKind::Start);
    let mut work = database.begin().await.expect("later request transaction");
    record_request(&mut work, &third)
        .await
        .expect("later request");
    work.commit().await.expect("later request commit");
    let rest = baseline_page(&database, snapshot, first_page[0].ordinal, 10)
        .await
        .expect("page two")
        .expect("snapshot");
    assert_eq!(rest.len(), 1);
    assert_eq!(rest[0].body["status"], "pending");
    let refreshed = create_baseline(&database)
        .await
        .expect("refreshed baseline");
    let current = baseline_page(&database, refreshed, 0, 10)
        .await
        .expect("current page")
        .expect("snapshot");
    assert_eq!(current.len(), 3);
    assert!(current.iter().any(|row| row.body["status"] == "refused"));
}

/// Make one source-ordered comment request for the same issue.
fn request(at: i64, kind: RequestKind) -> Request {
    Request {
        id: Uuid::now_v7(),
        channel: "comment".to_owned(),
        reply: format!("issue_comment:{at}"),
        comment_id: Some(at),
        issue: IssueKey {
            repository_id: 7,
            number: 12,
        },
        issue_author: Actor {
            id: 7,
            login: "author".to_owned(),
        },
        actor: Actor {
            id: 42,
            login: "writer".to_owned(),
        },
        kind,
        order: SourceOrder {
            at: DateTime::<Utc>::from_timestamp(at, 0).expect("time"),
            source_id: at,
            source: format!("comment:{at}"),
            index: 0,
        },
    }
}

#[tokio::test]
async fn browser_action_replays_exact_contents_across_restarts() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("task schema");
    migrate_events(&database).await.expect("event schema");
    let mut command = request(
        20,
        RequestKind::Later(LaterCommand::Retry {
            plan_revisions: None,
        }),
    );
    command.channel = "browser".to_owned();
    command.comment_id = None;
    command.reply.clear();
    command.order.source = format!("browser:{}", command.id);
    command.order.source_id = 0;
    let content = json!({
        "issue": command.issue, "kind": "retry", "argument": {},
        "detail": {"feedback": "please repair"}, "source_at": command.order.at,
    });
    let mut work = database.begin().await.expect("capture");
    let first = record_browser_action(&mut work, command.id, &content, &command)
        .await
        .expect("first action");
    work.commit().await.expect("commit");
    assert_eq!(first.status, "pending");
    let reopened = Database::connect(postgres.url(), 2).await.expect("reopen");
    let repeat = browser_action(&reopened, command.id, command.actor.id, &content)
        .await
        .expect("replay")
        .expect("action");
    assert_eq!(repeat.request_id, first.request_id);
    assert_eq!(
        browser_retry_feedback(&reopened, command.id)
            .await
            .expect("feedback"),
        Some("please repair".to_owned())
    );
    assert!(
        browser_action(
            &reopened,
            command.id,
            command.actor.id,
            &json!({"changed": true})
        )
        .await
        .is_err()
    );
    assert!(
        browser_action(&reopened, command.id, 99, &content)
            .await
            .is_err()
    );
    let outbox = Outbox::new(1024 * 1024, vec!["request.>".to_owned()]);
    let mut decision = reopened.begin().await.expect("decision");
    apply_request(
        &mut decision,
        &outbox,
        command.id,
        command.issue,
        Authority {
            writer: true,
            issue_open: true,
        },
    )
    .await
    .expect("apply")
    .expect("pending");
    decision.commit().await.expect("decision commit");
    let settled = request_record(&reopened, command.id)
        .await
        .expect("status")
        .expect("request");
    assert_eq!(settled.status, "refused");
    assert!(settled.applied_at.is_some());
}

#[tokio::test]
async fn pre_enrollment_consent_keeps_source_order_and_self_withdrawal() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("task schema");
    migrate_events(&database).await.expect("event schema");
    let outbox = Outbox::new(1024 * 1024, vec!["request.>".to_owned()]);
    let writer = Authority {
        writer: true,
        issue_open: true,
    };
    let former_writer = Authority {
        writer: false,
        issue_open: true,
    };
    let mut latest = request(
        25,
        RequestKind::Later(LaterCommand::Sponsor {
            provider: Some("openai".to_owned()),
        }),
    );
    latest.actor = Actor {
        id: 25,
        login: "new-sponsor".to_owned(),
    };
    let issue = latest.issue;
    let mut earlier = request(
        10,
        RequestKind::Later(LaterCommand::Sponsor {
            provider: Some("anthropic".to_owned()),
        }),
    );
    earlier.actor = Actor {
        id: 10,
        login: "former-sponsor".to_owned(),
    };
    let mut withdrawal = request(
        30,
        RequestKind::Later(LaterCommand::Withdraw {
            provider: "openai".to_owned(),
        }),
    );
    withdrawal.actor = earlier.actor.clone();
    for (command, authority) in [
        (&latest, writer),
        (&earlier, writer),
        (&withdrawal, former_writer),
    ] {
        let mut work = database.begin().await.expect("record command");
        record_request(&mut work, command).await.expect("record");
        sqlx::query(
            "INSERT INTO tasks.comment_revisions \
             (repository_id,issue_number,comment_id,source,source_at) VALUES ($1,$2,$3,$4,$5)",
        )
        .bind(issue.repository_id)
        .bind(issue.number)
        .bind(command.comment_id.expect("comment"))
        .bind(&command.order.source)
        .bind(command.order.at)
        .execute(work.connection())
        .await
        .expect("revision");
        work.commit().await.expect("record commit");
        let mut work = database.begin().await.expect("apply command");
        let decision = apply_request(&mut work, &outbox, command.id, issue, authority)
            .await
            .expect("decision")
            .expect("pending")
            .1;
        work.commit().await.expect("apply commit");
        assert_eq!(decision.status, RequestStatus::Applied);
    }
    let boundary = request(40, RequestKind::Start).order;
    let visible = [
        (earlier.order.clone(), earlier.actor.clone()),
        (latest.order.clone(), latest.actor.clone()),
        (withdrawal.order.clone(), withdrawal.actor.clone()),
    ];
    let mut read = database.begin().await.expect("source read");
    assert!(
        confirm_current_sponsorship_sources(&mut read, issue, &boundary, &visible)
            .await
            .expect("confirmed source")
    );
    assert!(
        !confirm_current_sponsorship_sources(&mut read, issue, &boundary, &visible[1..])
            .await
            .expect("missing source")
    );
    read.commit().await.expect("source read commit");
    assert!(task(&database, issue).await.expect("task read").is_none());
    let projected = consent_projection(&database, issue)
        .await
        .expect("projection");
    assert!(matches!(
        &projected.providers[&Provider::Openai].slot,
        SponsorSlot::Active { sponsor, .. } if sponsor.id == latest.actor.id
    ));
    assert!(matches!(
        &projected.providers[&Provider::Anthropic].slot,
        SponsorSlot::Active { sponsor, .. } if sponsor.id == earlier.actor.id
    ));
    assert!(
        projected.providers[&Provider::Openai]
            .tombstones
            .contains_key(&earlier.actor.id)
    );

    let removed = CommentRevision {
        issue,
        comment_id: earlier.comment_id.expect("comment"),
        order: SourceOrder {
            at: DateTime::<Utc>::from_timestamp(35, 0).expect("time"),
            source_id: earlier.comment_id.expect("comment"),
            source: "comment:10:deleted".to_owned(),
            index: 0,
        },
        deleted: true,
        request: None,
    };
    let mut work = database.begin().await.expect("remove source");
    record_comment_revision(&mut work, &outbox, &removed)
        .await
        .expect("comment removal");
    work.commit().await.expect("removal commit");
    let projected = consent_projection(&database, issue)
        .await
        .expect("projection");
    assert!(matches!(
        &projected.providers[&Provider::Openai].slot,
        SponsorSlot::Active { sponsor, .. } if sponsor.id == latest.actor.id
    ));
    assert!(matches!(
        &projected.providers[&Provider::Anthropic].slot,
        SponsorSlot::Withdrawn { .. }
    ));
    let revision: i64 = sqlx::query_scalar(
        "SELECT consent_revision FROM tasks.issue_controls WHERE repository_id=$1 AND issue_number=$2",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_one(database.pool())
    .await
    .expect("consent revision");
    assert_eq!(revision, 4);

    let mut pending = request(
        40,
        RequestKind::Later(LaterCommand::Sponsor {
            provider: Some("anthropic".to_owned()),
        }),
    );
    pending.issue.number += 1;
    let received = CommentRevision {
        issue: pending.issue,
        comment_id: pending.comment_id.expect("comment"),
        order: pending.order.clone(),
        deleted: false,
        request: Some(pending.clone()),
    };
    let mut work = database.begin().await.expect("receive pending consent");
    record_comment_revision(&mut work, &outbox, &received)
        .await
        .expect("record source");
    work.commit().await.expect("source commit");
    let pending_revision: i64 = sqlx::query_scalar(
        "SELECT consent_revision FROM tasks.issue_controls WHERE repository_id=$1 AND issue_number=$2",
    )
    .bind(pending.issue.repository_id)
    .bind(pending.issue.number)
    .fetch_one(database.pool())
    .await
    .expect("pending revision");
    assert_eq!(
        pending_revision, 1,
        "a receipt invalidates an outstanding fill"
    );
    let mut work = database.begin().await.expect("redeliver source");
    record_comment_revision(&mut work, &outbox, &received)
        .await
        .expect("replay source");
    work.commit().await.expect("replay commit");
    let replay_revision: i64 = sqlx::query_scalar(
        "SELECT consent_revision FROM tasks.issue_controls WHERE repository_id=$1 AND issue_number=$2",
    )
    .bind(pending.issue.repository_id)
    .bind(pending.issue.number)
    .fetch_one(database.pool())
    .await
    .expect("replay revision");
    assert_eq!(replay_revision, pending_revision);

    let mut bare = request(
        50,
        RequestKind::Later(LaterCommand::Sponsor { provider: None }),
    );
    bare.issue.number += 2;
    let mut work = database.begin().await.expect("bare sponsor");
    record_request(&mut work, &bare)
        .await
        .expect("record bare sponsor");
    let decision = apply_request(&mut work, &outbox, bare.id, bare.issue, writer)
        .await
        .expect("bare sponsor decision")
        .expect("pending bare sponsor")
        .1;
    work.commit().await.expect("bare sponsor commit");
    assert_eq!(decision.status, RequestStatus::Refused);
    assert_eq!(
        decision.reason,
        Some("usage: /concurro sponsor <openai|anthropic>")
    );
    let facts: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM tasks.sponsorship_facts WHERE repository_id=$1 AND issue_number=$2",
    )
    .bind(bare.issue.repository_id)
    .bind(bare.issue.number)
    .fetch_one(database.pool())
    .await
    .expect("bare sponsor fact count");
    assert_eq!(facts, 0);
}

/// Removing an older source cannot revoke a newer slot held by the same actor.
#[tokio::test]
async fn superseded_sponsor_source_removal_keeps_newer_grant() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("task schema");
    migrate_events(&database).await.expect("event schema");
    let outbox = Outbox::new(
        1024 * 1024,
        vec!["request.>".to_owned(), "task.>".to_owned()],
    );
    let start = request(30, RequestKind::Start);
    let mut work = database.begin().await.expect("start");
    record_request(&mut work, &start)
        .await
        .expect("record start");
    let state = apply_request(
        &mut work,
        &outbox,
        start.id,
        start.issue,
        Authority {
            writer: true,
            issue_open: true,
        },
    )
    .await
    .expect("start decision")
    .expect("pending start")
    .1
    .task
    .expect("started task");
    for (source, epoch) in [(10, 1), (20, 2)] {
        let mut transition = json!({
            "repository":"acme/demo", "issue":start.issue.number,
            "provider":"openai", "generation":1,
            "expected_epoch":epoch - 1, "reason":"sponsor",
            "slot":{
                "status":"active", "sponsor_id":start.actor.id.to_string(),
                "epoch":epoch, "grant_id":format!("grant-{epoch}"),
                "funding":"contributed", "source_id":source,
                "source_order":["2026-11-20T00:00:00Z",source,0]
            }
        });
        transition["transition"] = json!(python_digest(&transition).expect("digest"));
        record_sponsorship_transition(&mut work, state.id.0, &transition)
            .await
            .expect("active slot");
    }
    let removal = ConsentFact {
        provider: Provider::Openai,
        actor: start.actor.clone(),
        order: request(40, RequestKind::Stop).order,
        grant_id: "removed-old-source".to_owned(),
        action: ConsentAction::Withdraw {
            removed_source: Some(10),
        },
    };
    record_withdraw_transition(&mut work, state.id.0, &removal)
        .await
        .expect("obsolete source removal");
    let count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM tasks.sponsorship_transitions WHERE task_id=$1")
            .bind(state.id.0)
            .fetch_one(work.connection())
            .await
            .expect("transition count");
    assert_eq!(count, 2);
    record_withdraw_transition(
        &mut work,
        state.id.0,
        &ConsentFact {
            action: ConsentAction::Withdraw {
                removed_source: None,
            },
            ..removal
        },
    )
    .await
    .expect("self withdrawal");
    let count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM tasks.sponsorship_transitions WHERE task_id=$1")
            .bind(state.id.0)
            .fetch_one(work.connection())
            .await
            .expect("transition count");
    assert_eq!(count, 3);
    work.commit().await.expect("commit");
}

/// A committed cutoff survives restart but cannot report stopped before its release settles.
#[tokio::test]
async fn withdrawal_reports_blocked_then_stopped_after_acknowledged_stream_bound() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("task schema");
    migrate_events(&database).await.expect("event schema");
    let outbox = Outbox::new(
        1024 * 1024,
        vec!["request.>".to_owned(), "task.>".to_owned()],
    );
    let authority = Authority {
        writer: true,
        issue_open: true,
    };
    let start = request(10, RequestKind::Start);
    let mut work = database.begin().await.expect("start");
    record_request(&mut work, &start)
        .await
        .expect("record start");
    let state = apply_request(&mut work, &outbox, start.id, start.issue, authority)
        .await
        .expect("start decision")
        .expect("pending start")
        .1
        .task
        .expect("task");
    let mut active = json!({
        "repository":"acme/demo", "issue":start.issue.number,
        "provider":"openai", "generation":1, "expected_epoch":0, "reason":"sponsor",
        "slot": {
            "status":"active", "sponsor_id":start.actor.id.to_string(),
            "epoch":1, "grant_id":"grant-1", "funding":"contributed",
            "source_id":20, "source_order":["2026-11-20T00:00:00Z",20,0]
        }
    });
    active["transition"] = json!(python_digest(&active).expect("digest"));
    record_sponsorship_transition(&mut work, state.id.0, &active)
        .await
        .expect("active slot");
    work.commit().await.expect("start commit");

    let withdraw = request(
        30,
        RequestKind::Later(LaterCommand::Withdraw {
            provider: "openai".to_owned(),
        }),
    );
    let mut work = database.begin().await.expect("withdraw");
    record_request(&mut work, &withdraw)
        .await
        .expect("record withdrawal");
    let decision = apply_request_with_evidence_mode(
        &mut work,
        &outbox,
        withdraw.id,
        withdraw.issue,
        authority,
        None,
        true,
    )
    .await
    .expect("withdrawal decision")
    .expect("pending withdrawal")
    .1;
    assert_eq!(decision.status, RequestStatus::Applied);
    work.commit().await.expect("withdrawal commit");
    let transitions: Vec<String> = sqlx::query_scalar(
        "SELECT unnest(transitions) FROM tasks.withdrawal_steps WHERE request_id=$1",
    )
    .bind(withdraw.id)
    .fetch_all(database.pool())
    .await
    .expect("withdrawal transitions");
    assert_eq!(transitions.len(), 1);
    let blocked: i64 =
        sqlx::query_scalar("SELECT count(*) FROM events.outbox WHERE type='request.tasks.blocked'")
            .fetch_one(database.pool())
            .await
            .expect("blocked fact");
    assert_eq!(blocked, 1);
    let again = request(
        31,
        RequestKind::Later(LaterCommand::Withdraw {
            provider: "openai".to_owned(),
        }),
    );
    let mut work = database.begin().await.expect("repeat withdrawal");
    record_request(&mut work, &again)
        .await
        .expect("record repeat");
    assert_eq!(
        apply_request_with_evidence_mode(
            &mut work,
            &outbox,
            again.id,
            again.issue,
            authority,
            None,
            true,
        )
        .await
        .expect("repeat decision")
        .expect("pending repeat")
        .1
        .status,
        RequestStatus::Applied
    );
    work.commit().await.expect("repeat commit");
    let repeated: Vec<String> = sqlx::query_scalar(
        "SELECT unnest(transitions) FROM tasks.withdrawal_steps WHERE request_id=$1",
    )
    .bind(again.id)
    .fetch_all(database.pool())
    .await
    .expect("repeat obligations");
    assert_eq!(repeated, transitions);
    let blocked: i64 =
        sqlx::query_scalar("SELECT count(*) FROM events.outbox WHERE type='request.tasks.blocked'")
            .fetch_one(database.pool())
            .await
            .expect("both blocked facts");
    assert_eq!(blocked, 2);
    sqlx::query("UPDATE tasks.withdrawal_steps SET blocked_at=now()-interval '41 seconds' WHERE request_id IN ($1,$2)")
        .bind(withdraw.id).bind(again.id).execute(database.pool()).await.expect("age cutoff");
    assert!(
        due_withdrawal_steps(&database)
            .await
            .expect("pending scan")
            .is_empty()
    );
    drop(database);

    let restarted = Database::connect(postgres.url(), 2).await.expect("reopen");
    assert!(
        due_withdrawal_steps(&restarted)
            .await
            .expect("restart pending scan")
            .is_empty()
    );
    sqlx::query(
        "UPDATE tasks.sponsorship_transitions SET status='acknowledged', \
         acknowledged_at=now() WHERE transition=$1",
    )
    .bind(&transitions[0])
    .execute(restarted.pool())
    .await
    .expect("acknowledge revoke");
    assert!(
        due_withdrawal_steps(&restarted)
            .await
            .expect("stream bound scan")
            .is_empty()
    );
    sqlx::query(
        "UPDATE tasks.sponsorship_transitions SET acknowledged_at=now()-interval '41 seconds' \
         WHERE transition=$1",
    )
    .bind(&transitions[0])
    .execute(restarted.pool())
    .await
    .expect("age acknowledgement");
    assert_eq!(
        due_withdrawal_steps(&restarted).await.expect("ready scan"),
        vec![withdraw.id, again.id]
    );
    let mut work = restarted.begin().await.expect("complete");
    assert!(
        complete_withdrawal_step(&mut work, &outbox, withdraw.id)
            .await
            .expect("stopped")
    );
    work.commit().await.expect("stopped commit");
    let mut work = restarted.begin().await.expect("complete repeat");
    assert!(
        complete_withdrawal_step(&mut work, &outbox, again.id)
            .await
            .expect("repeat stopped")
    );
    work.commit().await.expect("repeat stopped commit");
    let mut replay = restarted.begin().await.expect("replay");
    assert!(
        !complete_withdrawal_step(&mut replay, &outbox, withdraw.id)
            .await
            .expect("idempotent")
    );
    replay.commit().await.expect("replay commit");
    let stopped: i64 =
        sqlx::query_scalar("SELECT count(*) FROM events.outbox WHERE type='request.tasks.stopped'")
            .fetch_one(restarted.pool())
            .await
            .expect("stopped fact");
    assert_eq!(stopped, 2);
    let steps: Vec<String> = sqlx::query_scalar(
        "SELECT body FROM tasks.publication_comments WHERE marker LIKE '<!-- concurro:withdrawal:%' ORDER BY body",
    ).fetch_all(restarted.pool()).await.expect("step replies");
    assert_eq!(steps.len(), 4);
    assert!(steps.iter().any(|body| body.contains("new grants blocked")));
    assert!(steps.iter().any(|body| body.contains("spending stopped")));
}

/// Python-mode withdrawals preserve their original single applied outcome.
#[tokio::test]
async fn python_withdrawal_does_not_stage_rust_exchange_steps() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("task schema");
    migrate_events(&database).await.expect("event schema");
    let outbox = Outbox::new(
        1024 * 1024,
        vec!["request.>".to_owned(), "task.>".to_owned()],
    );
    let authority = Authority {
        writer: true,
        issue_open: true,
    };
    let start = request(10, RequestKind::Start);
    let mut work = database.begin().await.expect("start");
    record_request(&mut work, &start)
        .await
        .expect("record start");
    apply_request(&mut work, &outbox, start.id, start.issue, authority)
        .await
        .expect("start decision")
        .expect("pending start");
    work.commit().await.expect("start commit");
    let withdraw = request(
        30,
        RequestKind::Later(LaterCommand::Withdraw {
            provider: "openai".to_owned(),
        }),
    );
    let mut work = database.begin().await.expect("withdraw");
    record_request(&mut work, &withdraw)
        .await
        .expect("record withdraw");
    apply_request(&mut work, &outbox, withdraw.id, withdraw.issue, authority)
        .await
        .expect("withdraw decision")
        .expect("pending withdraw");
    work.commit().await.expect("withdraw commit");
    let staged: i64 = sqlx::query_scalar("SELECT count(*) FROM tasks.withdrawal_steps")
        .fetch_one(database.pool())
        .await
        .expect("step count");
    assert_eq!(staged, 0);
    let blocked: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM events.outbox WHERE type IN ('request.tasks.blocked','request.tasks.stopped')",
    ).fetch_one(database.pool()).await.expect("later facts");
    assert_eq!(blocked, 0);
}

/// Supply exact inputs while exercising initialization transaction boundaries.
fn snapshot(pins: PinnedInputs) -> InitializationSnapshot {
    InitializationSnapshot {
        pins,
        configuration: json!({"repository": "acme/fork"}),
        requirements: Requirements {
            title: "Task".to_owned(),
            body: Some("Description".to_owned()),
            discussion: vec![],
        },
        charter: "{\"version\":1}".to_owned(),
        instruction_texts: BTreeMap::new(),
        policy_documents: BTreeMap::new(),
        base_files: vec!["src/lib.rs".to_owned()],
        bundle_id: "bundle-identity".to_owned(),
    }
}

#[tokio::test]
async fn stop_cutoff_and_deduplication_survive_restart() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let first = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&first).await.expect("tasks schema");
    migrate_events(&first).await.expect("events schema");
    let outbox = Outbox::new(
        1024 * 1024,
        vec![
            "request.>".to_owned(),
            "task.>".to_owned(),
            "turn.>".to_owned(),
        ],
    );
    let old_start = request(10, RequestKind::Start);
    let stop = request(20, RequestKind::Stop);
    let new_start = request(30, RequestKind::Start);
    let mut capture = first.begin().await.expect("capture");
    record_request(&mut capture, &old_start)
        .await
        .expect("record");
    let duplicate = Request {
        id: Uuid::now_v7(),
        ..old_start.clone()
    };
    assert_eq!(
        record_request(&mut capture, &duplicate)
            .await
            .expect("redelivery"),
        old_start.id
    );
    capture.commit().await.expect("commit capture");
    assert_eq!(
        next_pending(&first, stop.issue)
            .await
            .expect("pending")
            .unwrap()
            .id,
        old_start.id
    );

    let mut control = first.begin().await.expect("capture stop");
    record_request(&mut control, &stop)
        .await
        .expect("record stop");
    control.commit().await.expect("commit stop source");

    let authority = Authority {
        writer: true,
        issue_open: true,
    };
    let mut stale = first.begin().await.expect("stale authorization");
    assert!(
        apply_request(&mut stale, &outbox, old_start.id, stop.issue, authority)
            .await
            .expect("stale decision")
            .is_none()
    );
    stale.rollback().await.expect("discard stale decision");
    assert_eq!(
        next_pending(&first, stop.issue)
            .await
            .expect("pending")
            .unwrap()
            .id,
        stop.id
    );
    let mut apply = first.begin().await.expect("apply stop");
    let (selected, stopped) = apply_request(&mut apply, &outbox, stop.id, stop.issue, authority)
        .await
        .expect("decision")
        .expect("pending");
    assert_eq!(selected.id, stop.id);
    assert_eq!(stopped.status, RequestStatus::Applied);
    apply.commit().await.expect("commit stop");
    drop(first);

    let restarted = Database::connect(postgres.url(), 2).await.expect("reopen");
    let mut delayed = restarted.begin().await.expect("apply old start");
    let (selected, superseded) =
        apply_request(&mut delayed, &outbox, old_start.id, stop.issue, authority)
            .await
            .expect("decision")
            .expect("pending");
    assert_eq!(selected.id, old_start.id);
    assert_eq!(superseded.status, RequestStatus::Superseded);
    delayed.commit().await.expect("commit old start");
    assert!(task(&restarted, stop.issue).await.expect("state").is_none());

    let mut fresh = restarted.begin().await.expect("record new start");
    record_request(&mut fresh, &new_start)
        .await
        .expect("record");
    fresh.commit().await.expect("commit new start");
    let mut apply = restarted.begin().await.expect("apply new start");
    let (selected, started) =
        apply_request(&mut apply, &outbox, new_start.id, stop.issue, authority)
            .await
            .expect("decision")
            .expect("pending");
    assert_eq!(selected.id, new_start.id);
    assert_eq!(started.status, RequestStatus::Applied);
    apply.commit().await.expect("commit start");
    let state = task(&restarted, stop.issue)
        .await
        .expect("state")
        .expect("task");
    assert_eq!((state.stage, state.revision), (Stage::Initializing, 1));
    let implementing = concurro_tasks_core::TaskState {
        stage: Stage::Implement,
        revision: state.revision + 1,
        ..state.clone()
    };
    sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
        .bind(state.id.0)
        .bind(i64::try_from(implementing.revision).expect("revision"))
        .bind(sqlx::types::Json(&implementing))
        .execute(restarted.pool())
        .await
        .expect("seed implementation stage");
    let outcomes: Vec<String> = sqlx::query_scalar(
        "SELECT type FROM events.outbox WHERE type LIKE 'request.%' ORDER BY sequence",
    )
    .fetch_all(restarted.pool())
    .await
    .expect("outcomes");
    assert_eq!(
        outcomes,
        [
            "request.tasks.applied",
            "request.tasks.superseded",
            "request.tasks.applied"
        ]
    );

    let delayed_stop = request(25, RequestKind::Stop);
    let mut delayed_work = restarted.begin().await.expect("delayed stop");
    record_request(&mut delayed_work, &delayed_stop)
        .await
        .expect("record delayed stop");
    let (_, decision) = apply_request(
        &mut delayed_work,
        &outbox,
        delayed_stop.id,
        delayed_stop.issue,
        authority,
    )
    .await
    .expect("apply delayed stop")
    .expect("pending delayed stop");
    assert_eq!(
        (decision.status, decision.reason),
        (
            RequestStatus::Superseded,
            Some("superseded_by_newer_command")
        )
    );
    delayed_work.commit().await.expect("delayed stop commit");
    assert_eq!(
        task(&restarted, stop.issue)
            .await
            .expect("task")
            .expect("current")
            .stage,
        Stage::Implement
    );

    let later_stop = request(40, RequestKind::Stop);
    let mut stop_work = restarted.begin().await.expect("later stop");
    record_request(&mut stop_work, &later_stop)
        .await
        .expect("record later stop");
    apply_request(
        &mut stop_work,
        &outbox,
        later_stop.id,
        later_stop.issue,
        authority,
    )
    .await
    .expect("apply later stop");
    stop_work.commit().await.expect("later stop commit");
    let stopped_revision = task(&restarted, stop.issue)
        .await
        .expect("task")
        .expect("stopped")
        .revision;
    let edited_start = Request {
        reply: new_start.reply.clone(),
        ..request(50, RequestKind::Start)
    };
    let mut edit_work = restarted.begin().await.expect("edited start");
    record_request(&mut edit_work, &edited_start)
        .await
        .expect("record edited start");
    let (_, decision) = apply_request(
        &mut edit_work,
        &outbox,
        edited_start.id,
        edited_start.issue,
        authority,
    )
    .await
    .expect("apply edited start")
    .expect("pending edit");
    assert_eq!(
        (decision.status, decision.reason),
        (RequestStatus::Refused, Some("already_applied"))
    );
    edit_work.commit().await.expect("edited start commit");
    assert_eq!(
        task(&restarted, stop.issue)
            .await
            .expect("task")
            .expect("stopped")
            .revision,
        stopped_revision
    );
    let initialized: bool = sqlx::query_scalar(
        "SELECT (message->'data'->>'initialized')::boolean FROM events.outbox \
         WHERE type LIKE 'task.%' ORDER BY sequence DESC LIMIT 1",
    )
    .fetch_one(restarted.pool())
    .await
    .expect("stopped snapshot");
    assert!(!initialized);
    let unsupported = request(
        60,
        RequestKind::Later(LaterCommand::Retry {
            plan_revisions: Some(3),
        }),
    );
    let mut unsupported_work = restarted.begin().await.expect("unsupported request");
    record_request(&mut unsupported_work, &unsupported)
        .await
        .expect("record unsupported");
    let (_, decision) = apply_request(
        &mut unsupported_work,
        &outbox,
        unsupported.id,
        unsupported.issue,
        authority,
    )
    .await
    .expect("apply unsupported")
    .expect("pending unsupported");
    assert_eq!(
        (decision.status, decision.reason),
        (RequestStatus::Refused, Some("plan_revision_not_waiting"))
    );
    unsupported_work.commit().await.expect("unsupported commit");
    let argument: sqlx::types::Json<serde_json::Value> = sqlx::query_scalar(
        "SELECT message->'data'->'argument' FROM events.outbox WHERE type='request.tasks.refused' ORDER BY sequence DESC LIMIT 1",
    )
    .fetch_one(restarted.pool())
    .await
    .expect("structured argument");
    assert_eq!(argument.0, serde_json::json!({"plan_revisions": 3}));
    let restart_implement = request(70, RequestKind::Start);
    let mut restart_work = restarted.begin().await.expect("restart implement");
    record_request(&mut restart_work, &restart_implement)
        .await
        .expect("record implement restart");
    let (_, decision) = apply_request(
        &mut restart_work,
        &outbox,
        restart_implement.id,
        restart_implement.issue,
        authority,
    )
    .await
    .expect("apply implement restart")
    .expect("pending implement restart");
    assert_eq!(decision.status, RequestStatus::Applied);
    restart_work
        .commit()
        .await
        .expect("implement restart commit");
    let restarted_implement = task(&restarted, restart_implement.issue)
        .await
        .expect("task")
        .expect("restarted implement");
    assert_eq!(restarted_implement.stage, Stage::Initializing);
    assert_eq!(restarted_implement.id, state.id);
}

#[tokio::test]
async fn label_removal_supersedes_old_enrollment_without_stopping_manual_task() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("tasks schema");
    migrate_events(&database).await.expect("events schema");
    let outbox = Outbox::new(
        1024 * 1024,
        vec!["request.>".to_owned(), "task.>".to_owned()],
    );
    let authority = Authority {
        writer: true,
        issue_open: true,
    };
    let mut removed = request(20, RequestKind::Later(LaterCommand::Unenroll));
    removed.channel = "label".to_owned();
    removed.reply = "issue_label:20".to_owned();
    removed.order.source = removed.reply.clone();
    let mut work = database.begin().await.expect("record removal");
    record_request(&mut work, &removed)
        .await
        .expect("record removal");
    work.commit().await.expect("commit removal");
    let mut work = database.begin().await.expect("apply removal");
    let (_, decision) = apply_request(&mut work, &outbox, removed.id, removed.issue, authority)
        .await
        .expect("decision")
        .expect("pending removal");
    assert_eq!(decision.status, RequestStatus::Applied);
    work.commit().await.expect("commit removal decision");

    let mut old = request(10, RequestKind::Start);
    old.channel = "label".to_owned();
    old.reply = "issue_label:10".to_owned();
    old.order.source = old.reply.clone();
    let mut work = database.begin().await.expect("record old label");
    record_request(&mut work, &old)
        .await
        .expect("record old label");
    work.commit().await.expect("commit old label");
    let mut work = database.begin().await.expect("apply old label");
    let (_, decision) = apply_request(&mut work, &outbox, old.id, old.issue, authority)
        .await
        .expect("decision")
        .expect("pending old label");
    assert_eq!(decision.status, RequestStatus::Superseded);
    work.commit().await.expect("commit old label decision");
    assert!(task(&database, old.issue).await.expect("task").is_none());

    let manual = request(15, RequestKind::Start);
    let mut work = database.begin().await.expect("record delayed manual start");
    record_request(&mut work, &manual)
        .await
        .expect("record delayed manual start");
    work.commit().await.expect("commit delayed manual start");
    let mut work = database.begin().await.expect("apply delayed manual start");
    let (_, decision) = apply_request(&mut work, &outbox, manual.id, manual.issue, authority)
        .await
        .expect("decision")
        .expect("pending manual start");
    assert_eq!(decision.status, RequestStatus::Applied);
    work.commit()
        .await
        .expect("commit delayed manual start decision");

    let latest = request(30, RequestKind::Start);
    let mut work = database.begin().await.expect("record latest label");
    record_request(&mut work, &latest)
        .await
        .expect("record latest label");
    work.commit().await.expect("commit latest label");
    let mut work = database.begin().await.expect("apply latest label");
    let (_, decision) = apply_request(&mut work, &outbox, latest.id, latest.issue, authority)
        .await
        .expect("decision")
        .expect("pending latest label");
    assert_eq!(decision.status, RequestStatus::Refused);
    assert_eq!(decision.reason, Some("already_active"));
    work.commit().await.expect("commit latest label decision");
    let active = task(&database, latest.issue)
        .await
        .expect("task")
        .expect("active task");

    let mut removed_again = request(40, RequestKind::Later(LaterCommand::Unenroll));
    removed_again.channel = "label".to_owned();
    removed_again.reply = "issue_label:40".to_owned();
    removed_again.order.source = removed_again.reply.clone();
    let mut work = database.begin().await.expect("record new removal");
    record_request(&mut work, &removed_again)
        .await
        .expect("record new removal");
    work.commit().await.expect("commit new removal");
    let mut work = database.begin().await.expect("apply new removal");
    let (_, decision) = apply_request(
        &mut work,
        &outbox,
        removed_again.id,
        latest.issue,
        authority,
    )
    .await
    .expect("decision")
    .expect("pending new removal");
    assert_eq!(decision.status, RequestStatus::Applied);
    work.commit().await.expect("commit new removal decision");
    assert_eq!(
        task(&database, latest.issue).await.expect("task"),
        Some(active)
    );
}

#[tokio::test]
async fn attempt_charge_submit_and_stop_are_atomic() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("tasks schema");
    migrate_events(&database).await.expect("events schema");
    let outbox = Outbox::new(
        1024 * 1024,
        vec![
            "request.>".to_owned(),
            "task.>".to_owned(),
            "turn.>".to_owned(),
        ],
    );
    let authority = Authority {
        writer: true,
        issue_open: true,
    };
    let start = request(10, RequestKind::Start);
    let mut work = database.begin().await.expect("start");
    record_request(&mut work, &start).await.expect("record");
    apply_request(&mut work, &outbox, start.id, start.issue, authority)
        .await
        .expect("apply");
    work.commit().await.expect("start commit");
    let state = task(&database, start.issue)
        .await
        .expect("state")
        .expect("task");
    let pins = PinnedInputs {
        config_sha: "a".repeat(40),
        base_sha: "b".repeat(40),
        max_model_calls: 2,
        max_fix_rounds: 2,
        max_plan_revisions: 2,
    };
    let mut work = database.begin().await.expect("initialize");
    initialize_task(
        &mut work,
        &outbox,
        start.issue,
        state.revision,
        &snapshot(pins),
    )
    .await
    .expect("initialize")
    .expect("revision current");
    work.commit().await.expect("initialize commit");

    let first = AttemptSpec {
        id: Uuid::now_v7(),
        task: state.id,
        expected_revision: 2,
        staffing_group: "group".to_owned(),
        fork: "acme/fork".to_owned(),
        config_sha: "a".repeat(40),
        submit_body: b"exact body bytes".to_vec(),
        prompt_body: None,
        deadline: DateTime::<Utc>::from_timestamp(1000, 0).expect("deadline"),
        unbounded_running: false,
    };
    let mut work = database.begin().await.expect("admit");
    assert_eq!(
        admit_intake(&mut work, &outbox, start.issue, &first)
            .await
            .expect("admit"),
        Admission::Admitted
    );
    work.commit().await.expect("admit commit");
    let charged = task(&database, start.issue)
        .await
        .expect("state")
        .expect("task");
    assert_eq!(
        (charged.used_model_calls, charged.current_attempt),
        (1, Some(first.id))
    );
    let mut replay = database.begin().await.expect("replay admission");
    assert_eq!(
        admit_intake(&mut replay, &outbox, start.issue, &first)
            .await
            .expect("same attempt"),
        Admission::Existing
    );
    replay.commit().await.expect("replay commit");
    let mut changed = first.clone();
    changed.submit_body.push(b'!');
    let mut replay = database.begin().await.expect("changed replay");
    assert!(
        admit_intake(&mut replay, &outbox, start.issue, &changed)
            .await
            .is_err()
    );
    replay.rollback().await.expect("changed replay rollback");
    let body: Vec<u8> = sqlx::query_scalar("SELECT submit_body FROM tasks.attempts WHERE id=$1")
        .bind(first.id)
        .fetch_one(database.pool())
        .await
        .expect("stored exact body");
    assert_eq!(body, first.submit_body);
    let listed = list_tasks(&database).await.expect("operator task list");
    assert_eq!(listed.len(), 1);
    assert_eq!(listed[0]["stage"], "Intake");
    let detail = task_detail(&database, start.issue)
        .await
        .expect("operator task detail")
        .expect("task exists");
    assert_eq!(detail["state"]["current_attempt"], first.id.to_string());
    assert_eq!(detail["attempts"][0]["status"], "ready");
    assert_eq!(detail["requests"][0]["actor"]["login"], "writer");
    let pending = pending_effects(&database).await.expect("pending effects");
    assert_eq!(pending.len(), 4);
    assert!(pending.iter().any(|effect| effect["kind"] == "submit"));
    assert!(pending.iter().any(|effect| effect["kind"] == "reply"));
    assert!(pending.iter().any(|effect| effect["kind"] == "dashboard"));
    assert!(pending.iter().any(|effect| effect["kind"] == "notice"));
    let notice_marker: String = sqlx::query_scalar(
        "SELECT marker FROM tasks.publication_comments WHERE task_id=$1 AND kind='notice' LIMIT 1",
    )
    .bind(state.id.0)
    .fetch_one(database.pool())
    .await
    .expect("welcome marker");
    assert!(notice_marker.starts_with("<!-- concurro:event:v1:"));
    assert_eq!(
        notice_marker.len(),
        "<!-- concurro:event:v1:".len() + 64 + " -->".len()
    );
    assert!(!detail.to_string().contains("exact body bytes"));

    let stop = request(20, RequestKind::Stop);
    let mut work = database.begin().await.expect("stop");
    record_request(&mut work, &stop).await.expect("record stop");
    apply_request(&mut work, &outbox, stop.id, stop.issue, authority)
        .await
        .expect("apply stop");
    work.commit().await.expect("stop commit");
    let submit: (String, String) = sqlx::query_as(
        "SELECT a.status, e.status FROM tasks.attempts a JOIN tasks.effects e ON e.turn_id=a.id WHERE a.id=$1 AND e.kind='submit'",
    )
    .bind(first.id)
    .fetch_one(database.pool())
    .await
    .expect("cancelled unsent attempt");
    assert_eq!(submit, ("cancelled".to_owned(), "settled".to_owned()));
    let group: Option<Uuid> = sqlx::query_scalar(
        "SELECT turn_id FROM tasks.staffing_groups WHERE staffing_group='group'",
    )
    .fetch_optional(database.pool())
    .await
    .expect("released reservation");
    assert!(group.is_none());
    let pending = pending_effects(&database).await.expect("pending replies");
    assert_eq!(pending.len(), 4);
    assert_eq!(
        pending
            .iter()
            .filter(|effect| effect["kind"] == "reply")
            .count(),
        2
    );
    assert!(pending.iter().any(|effect| effect["kind"] == "dashboard"));
    assert!(pending.iter().any(|effect| effect["kind"] == "notice"));

    let restart = request(30, RequestKind::Start);
    let mut work = database.begin().await.expect("restart");
    record_request(&mut work, &restart)
        .await
        .expect("record restart");
    apply_request(&mut work, &outbox, restart.id, restart.issue, authority)
        .await
        .expect("apply restart");
    work.commit().await.expect("restart commit");
    let restarted = task(&database, restart.issue)
        .await
        .expect("state")
        .expect("task");
    let mut work = database.begin().await.expect("reinitialize");
    initialize_task(
        &mut work,
        &outbox,
        restart.issue,
        restarted.revision,
        &snapshot(PinnedInputs {
            config_sha: "c".repeat(40),
            base_sha: "d".repeat(40),
            max_model_calls: 2,
            max_fix_rounds: 2,
            max_plan_revisions: 2,
        }),
    )
    .await
    .expect("reinitialize")
    .expect("current");
    work.commit().await.expect("reinitialize commit");
    let stale = AttemptSpec {
        id: Uuid::now_v7(),
        ..first.clone()
    };
    let mut old_build = database.begin().await.expect("stale admission");
    assert_eq!(
        admit_intake(&mut old_build, &outbox, restart.issue, &stale)
            .await
            .expect("stale decision"),
        Admission::Stale
    );
    old_build.commit().await.expect("stale admission commit");
    let second = AttemptSpec {
        id: Uuid::now_v7(),
        expected_revision: restarted.revision + 1,
        ..first.clone()
    };
    let mut work = database.begin().await.expect("second admit");
    assert_eq!(
        admit_intake(&mut work, &outbox, restart.issue, &second)
            .await
            .expect("admit"),
        Admission::Admitted
    );
    work.commit().await.expect("second commit");
    let submit_effect: i64 =
        sqlx::query_scalar("SELECT id FROM tasks.effects WHERE turn_id=$1 AND kind='submit'")
            .bind(second.id)
            .fetch_one(database.pool())
            .await
            .expect("submit effect");
    let mut lease = database.begin().await.expect("claim submit");
    let first_claim = claim_effect(&mut lease, submit_effect)
        .await
        .expect("claim")
        .expect("submit");
    assert_eq!(
        (first_claim.kind, first_claim.submit_body.as_deref()),
        (EffectKind::Submit, Some(second.submit_body.as_slice()))
    );
    lease.commit().await.expect("sending commit");
    let mut early = database.begin().await.expect("early replay");
    assert!(
        claim_effect(&mut early, submit_effect)
            .await
            .expect("deferred claim")
            .is_none()
    );
    early.commit().await.expect("deferred commit");
    sqlx::query("UPDATE tasks.effects SET updated_at=now()-interval '16 seconds' WHERE id=$1")
        .bind(submit_effect)
        .execute(database.pool())
        .await
        .expect("replay due");
    let mut replay = database.begin().await.expect("replay submit");
    let replay_claim = claim_effect(&mut replay, submit_effect)
        .await
        .expect("claim")
        .expect("same submit");
    assert_eq!(
        (replay_claim.turn, replay_claim.submit_body.as_deref()),
        (second.id, Some(second.submit_body.as_slice()))
    );
    assert_ne!(first_claim.claim_token, replay_claim.claim_token);
    replay.commit().await.expect("replay claim commit");
    let mut stale_reply = database.begin().await.expect("late reply");
    assert!(
        !settle_submit(
            &mut stale_reply,
            &outbox,
            &first_claim,
            serde_json::json!({"status":"queued","response":{"rationale":"model\0text"}}),
            None,
        )
        .await
        .expect("fenced")
    );
    stale_reply.commit().await.expect("late reply commit");
    let stop = request(40, RequestKind::Stop);
    let mut work = database.begin().await.expect("stop in-flight");
    record_request(&mut work, &stop).await.expect("record stop");
    apply_request(&mut work, &outbox, stop.id, stop.issue, authority)
        .await
        .expect("apply stop");
    work.commit().await.expect("stop commit");
    let effects: Vec<(String, String)> =
        sqlx::query_as("SELECT kind,status FROM tasks.effects WHERE turn_id=$1 ORDER BY kind")
            .bind(second.id)
            .fetch_all(database.pool())
            .await
            .expect("effects");
    let old_retirements: i64 =
        sqlx::query_scalar("SELECT count(*) FROM tasks.effects WHERE turn_id=$1 AND kind='retire'")
            .bind(first.id)
            .fetch_one(database.pool())
            .await
            .expect("unsent turn effects");
    assert_eq!(old_retirements, 0);
    assert_eq!(
        effects,
        [
            ("retire".to_owned(), "recorded".to_owned()),
            ("submit".to_owned(), "sending".to_owned())
        ]
    );
    let owner: Uuid = sqlx::query_scalar(
        "SELECT turn_id FROM tasks.staffing_groups WHERE staffing_group='group'",
    )
    .fetch_one(database.pool())
    .await
    .expect("held until retirement");
    assert_eq!(owner, second.id);
    let submit_raw = br#" { "status": "queued", "response": { "rationale": "model\u0000text" } } "#;
    let mut late_reply = database.begin().await.expect("submit response after stop");
    assert!(
        settle_submit(
            &mut late_reply,
            &outbox,
            &replay_claim,
            serde_json::from_slice(submit_raw).expect("submit reply"),
            Some(submit_raw),
        )
        .await
        .expect("settle")
    );
    late_reply.commit().await.expect("late reply commit");
    let (projected, raw): (Value, Vec<u8>) = sqlx::query_as(
        "SELECT response,response_raw FROM tasks.effects WHERE turn_id=$1 AND kind='submit'",
    )
    .bind(second.id)
    .fetch_one(database.pool())
    .await
    .expect("safe submit reply");
    assert_eq!(projected["response"]["rationale"], "model\\u0000text");
    assert_eq!(raw, submit_raw);
    let session = Uuid::now_v7();
    let observation: TurnObservation = serde_json::from_value(serde_json::json!({
        "turn": second.id,
        "status": "done",
        "created": 1.0,
        "finished": 2.0,
        "dispatch": session,
        "result": {"outcome": "proceed"}
    }))
    .expect("late poll observation");
    let mut late_poll = database.begin().await.expect("late poll");
    assert!(
        record_observation(&mut late_poll, &outbox, &observation, None)
            .await
            .expect("record late result")
    );
    late_poll.commit().await.expect("late poll commit");
    let status: String = sqlx::query_scalar("SELECT status FROM tasks.attempts WHERE id=$1")
        .bind(second.id)
        .fetch_one(database.pool())
        .await
        .expect("attempt status");
    assert_eq!(status, "cancelled");
    let mut retirement = database.begin().await.expect("claim retirement");
    let retire_effect: i64 =
        sqlx::query_scalar("SELECT id FROM tasks.effects WHERE turn_id=$1 AND kind='retire'")
            .bind(second.id)
            .fetch_one(database.pool())
            .await
            .expect("retire effect");
    let retirement_job = claim_effect(&mut retirement, retire_effect)
        .await
        .expect("claim")
        .expect("retire");
    assert_eq!(retirement_job.kind, EffectKind::Retire);
    assert_eq!(retirement_job.turn, second.id);
    retirement.commit().await.expect("retirement claim commit");
    let mut ack = database.begin().await.expect("retirement ack");
    assert!(
        acknowledge_retirement(&mut ack, &retirement_job, &[session])
            .await
            .expect("ack")
    );
    ack.commit().await.expect("retirement commit");
    let late_session = Uuid::now_v7();
    let late_queued: TurnObservation = serde_json::from_value(json!({
        "turn": second.id, "status": "queued", "created": 1.0, "dispatch": late_session
    }))
    .expect("queued reply delayed past retirement");
    let mut late_poll = database.begin().await.expect("late queued poll");
    record_observation(&mut late_poll, &outbox, &late_queued, None)
        .await
        .expect("retain late session evidence");
    late_poll.commit().await.expect("late queued poll commit");
    let late_intent = runner_session(&database, late_session)
        .await
        .expect("late intent")
        .expect("session evidence");
    assert!(!late_intent.desired_present);
    assert_eq!(late_intent.dispatch_status, "skipped");
    let mut claim = database.begin().await.expect("late dispatch claim");
    assert!(
        !claim_dispatch(&mut claim, late_session)
            .await
            .expect("fenced dispatch")
    );
    claim.rollback().await.expect("claim rollback");
    let held: Option<Uuid> = sqlx::query_scalar(
        "SELECT turn_id FROM tasks.staffing_groups WHERE staffing_group='group'",
    )
    .fetch_optional(database.pool())
    .await
    .expect("reservation released");
    assert!(held.is_none(), "held {held:?} after retiring {}", second.id);
    let retired: Vec<Uuid> =
        sqlx::query_scalar("SELECT session_id FROM tasks.attempt_sessions WHERE turn_id=$1")
            .bind(second.id)
            .fetch_all(database.pool())
            .await
            .expect("retired sessions");
    assert_eq!(retired.len(), 2);
    assert!(retired.contains(&session));
    assert!(retired.contains(&late_session));
    let snapshots: Vec<(i64, sqlx::types::Json<serde_json::Value>)> = sqlx::query_as(
        "SELECT (message->>'revision')::bigint, message FROM events.outbox WHERE type LIKE 'task.%.state' ORDER BY sequence",
    )
    .fetch_all(database.pool())
    .await
    .expect("task snapshots");
    assert_eq!(
        snapshots
            .iter()
            .map(|(revision, _)| *revision)
            .collect::<Vec<_>>(),
        (1..=8).collect::<Vec<_>>()
    );
    assert_eq!(snapshots[2].1["data"]["budgets"]["model_calls"]["used"], 1);
    assert_eq!(snapshots[7].1["data"]["stage"], "stopped");
}

/// A cleared retry claim must not erase evidence that the exchange may run the turn.
#[tokio::test]
async fn stop_after_lost_submit_reply_retires_sent_turn() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("tasks schema");
    migrate_events(&database).await.expect("events schema");
    let outbox = Outbox::new(
        1024 * 1024,
        vec![
            "task.>".to_owned(),
            "request.>".to_owned(),
            "turn.>".to_owned(),
        ],
    );
    let start = request(30, RequestKind::Start);
    let authority = Authority {
        writer: true,
        issue_open: true,
    };
    let mut decision = database.begin().await.expect("start");
    record_request(&mut decision, &start)
        .await
        .expect("record start");
    apply_request(&mut decision, &outbox, start.id, start.issue, authority)
        .await
        .expect("apply start");
    decision.commit().await.expect("start commit");
    let started = task(&database, start.issue)
        .await
        .expect("task")
        .expect("started");
    let mut initialize = database.begin().await.expect("initialize");
    let initialized = initialize_task(
        &mut initialize,
        &outbox,
        start.issue,
        started.revision,
        &snapshot(PinnedInputs {
            config_sha: "a".repeat(40),
            base_sha: "b".repeat(40),
            max_model_calls: 2,
            max_fix_rounds: 0,
            max_plan_revisions: 0,
        }),
    )
    .await
    .expect("initialize")
    .expect("initialized");
    initialize.commit().await.expect("initialize commit");
    let turn = Uuid::now_v7();
    let spec = AttemptSpec {
        id: turn,
        task: initialized.id,
        expected_revision: initialized.revision,
        staffing_group: "triage".to_owned(),
        fork: "acme/fork".to_owned(),
        config_sha: "a".repeat(40),
        submit_body: b"exact body".to_vec(),
        prompt_body: None,
        deadline: Utc::now() + chrono::Duration::minutes(30),
        unbounded_running: false,
    };
    let mut admission = database.begin().await.expect("admission");
    assert_eq!(
        admit_intake(&mut admission, &outbox, start.issue, &spec)
            .await
            .expect("admit"),
        Admission::Admitted
    );
    admission.commit().await.expect("admission commit");
    let effect: i64 =
        sqlx::query_scalar("SELECT id FROM tasks.effects WHERE turn_id=$1 AND kind='submit'")
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("submit effect");
    let mut send = database.begin().await.expect("send claim");
    let claimed = claim_effect(&mut send, effect)
        .await
        .expect("claim")
        .expect("submit");
    send.commit().await.expect("send claim commit");
    let mut failed = database.begin().await.expect("lost reply");
    assert!(
        record_submit_failure(&mut failed, &outbox, &claimed)
            .await
            .expect("schedule retry")
    );
    failed.commit().await.expect("failure commit");
    let claim: (Option<Uuid>, i32) =
        sqlx::query_as("SELECT claim_token,submit_attempts FROM tasks.effects WHERE id=$1")
            .bind(effect)
            .fetch_one(database.pool())
            .await
            .expect("sent evidence");
    assert_eq!(claim, (None, 1));
    let stop = request(40, RequestKind::Stop);
    let mut control = database.begin().await.expect("stop");
    record_request(&mut control, &stop)
        .await
        .expect("record stop");
    apply_request(&mut control, &outbox, stop.id, stop.issue, authority)
        .await
        .expect("apply stop");
    control.commit().await.expect("stop commit");
    let retire: i64 =
        sqlx::query_scalar("SELECT id FROM tasks.effects WHERE turn_id=$1 AND kind='retire'")
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("retirement queued");
    let owner: Uuid = sqlx::query_scalar(
        "SELECT turn_id FROM tasks.staffing_groups WHERE staffing_group='triage'",
    )
    .fetch_one(database.pool())
    .await
    .expect("group retained");
    assert_eq!(owner, turn);
    let detail = task_detail(&database, start.issue)
        .await
        .expect("detail")
        .expect("task");
    assert_eq!(detail["attempts"][0]["may_be_live"], true);
    let mut retirement = database.begin().await.expect("retirement claim");
    let retirement_job = claim_effect(&mut retirement, retire)
        .await
        .expect("retirement claim")
        .expect("retirement");
    retirement.commit().await.expect("retirement claim commit");
    let mut ack = database.begin().await.expect("retirement ack");
    acknowledge_retirement(&mut ack, &retirement_job, &[])
        .await
        .expect("retired");
    ack.commit().await.expect("retirement ack commit");
    let owner: Option<Uuid> = sqlx::query_scalar(
        "SELECT turn_id FROM tasks.staffing_groups WHERE staffing_group='triage'",
    )
    .fetch_optional(database.pool())
    .await
    .expect("group released");
    assert!(owner.is_none());
}

#[tokio::test]
async fn operator_cancellation_records_actor_and_retires_only_the_selected_turn() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("tasks schema");
    migrate_events(&database).await.expect("events schema");
    let outbox = Outbox::new(
        1024 * 1024,
        vec![
            "task.>".to_owned(),
            "request.>".to_owned(),
            "turn.>".to_owned(),
        ],
    );
    let start = request(10, RequestKind::Start);
    let mut work = database.begin().await.expect("start");
    record_request(&mut work, &start)
        .await
        .expect("record start");
    apply_request(
        &mut work,
        &outbox,
        start.id,
        start.issue,
        Authority {
            writer: true,
            issue_open: true,
        },
    )
    .await
    .expect("apply start");
    work.commit().await.expect("start commit");
    let started = task(&database, start.issue)
        .await
        .expect("task")
        .expect("started");
    let mut work = database.begin().await.expect("initialize");
    initialize_task(
        &mut work,
        &outbox,
        start.issue,
        started.revision,
        &snapshot(PinnedInputs {
            config_sha: "a".repeat(40),
            base_sha: "b".repeat(40),
            max_model_calls: 2,
            max_fix_rounds: 2,
            max_plan_revisions: 2,
        }),
    )
    .await
    .expect("initialize");
    work.commit().await.expect("initialization commit");
    let attempt = AttemptSpec {
        id: Uuid::now_v7(),
        task: started.id,
        expected_revision: 2,
        staffing_group: "operator-group".to_owned(),
        fork: "acme/fork".to_owned(),
        config_sha: "a".repeat(40),
        submit_body: b"body".to_vec(),
        prompt_body: None,
        deadline: DateTime::<Utc>::from_timestamp(1000, 0).expect("deadline"),
        unbounded_running: false,
    };
    let mut work = database.begin().await.expect("admit");
    assert_eq!(
        admit_intake(&mut work, &outbox, start.issue, &attempt)
            .await
            .expect("admit"),
        Admission::Admitted
    );
    work.commit().await.expect("admit commit");
    let submit_id: i64 =
        sqlx::query_scalar("SELECT id FROM tasks.effects WHERE turn_id=$1 AND kind='submit'")
            .bind(attempt.id)
            .fetch_one(database.pool())
            .await
            .expect("submit effect");
    let mut work = database.begin().await.expect("claim submit");
    claim_effect(&mut work, submit_id)
        .await
        .expect("claim")
        .expect("submit effect");
    work.commit().await.expect("claim commit");
    let mut work = database.begin().await.expect("operator cancel");
    assert!(
        cancel_attempt(&mut work, &outbox, attempt.id, "host-user")
            .await
            .expect("cancel")
    );
    work.commit().await.expect("cancel commit");
    let current = task(&database, start.issue)
        .await
        .expect("task")
        .expect("cancelled task");
    assert_eq!(current.stage, Stage::Blocked);
    let grant_status: String =
        sqlx::query_scalar("SELECT status FROM tasks.funding_grants WHERE attempt_id=$1")
            .bind(attempt.id)
            .fetch_one(database.pool())
            .await
            .expect("cancelled grant");
    assert_eq!(grant_status, "revoked");
    let effects = pending_effects(&database).await.expect("pending effects");
    assert_eq!(effects.len(), 6);
    assert!(effects.iter().any(|effect| effect["kind"] == "retire"));
    assert!(effects.iter().any(|effect| effect["kind"] == "reply"));
    assert!(effects.iter().any(|effect| effect["kind"] == "dashboard"));
    assert!(effects.iter().any(|effect| effect["kind"] == "notice"));
    let actions: Vec<(String, String)> =
        sqlx::query_as("SELECT actor,outcome FROM tasks.operator_actions ORDER BY id")
            .fetch_all(database.pool())
            .await
            .expect("operator actions");
    assert_eq!(actions, [("host-user".to_owned(), "cancelled".to_owned())]);
    let mut replay = database.begin().await.expect("repeat cancel");
    assert!(
        !cancel_attempt(&mut replay, &outbox, attempt.id, "host-user")
            .await
            .expect("repeat cancel")
    );
    replay.commit().await.expect("repeat commit");
    let actions: Vec<String> =
        sqlx::query_scalar("SELECT outcome FROM tasks.operator_actions ORDER BY id")
            .fetch_all(database.pool())
            .await
            .expect("recorded decisions");
    assert_eq!(actions, ["cancelled", "noop"]);
}

/// A pre-send personal cancellation frees capacity without reusing its call ordinal.
#[tokio::test]
async fn cancelled_unsent_personal_attempt_releases_its_call() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("tasks schema");
    migrate_events(&database).await.expect("events schema");
    let outbox = Outbox::new(
        1024 * 1024,
        vec![
            "task.>".to_owned(),
            "request.>".to_owned(),
            "turn.>".to_owned(),
        ],
    );
    let start = request(10, RequestKind::Start);
    let mut work = database.begin().await.expect("start");
    record_request(&mut work, &start)
        .await
        .expect("record start");
    apply_request(
        &mut work,
        &outbox,
        start.id,
        start.issue,
        Authority {
            writer: true,
            issue_open: true,
        },
    )
    .await
    .expect("apply start");
    work.commit().await.expect("start commit");
    let started = task(&database, start.issue)
        .await
        .expect("task")
        .expect("started");
    let mut work = database.begin().await.expect("initialize");
    initialize_task(
        &mut work,
        &outbox,
        start.issue,
        started.revision,
        &snapshot(PinnedInputs {
            config_sha: "a".repeat(40),
            base_sha: "b".repeat(40),
            max_model_calls: 1,
            max_fix_rounds: 1,
            max_plan_revisions: 1,
        }),
    )
    .await
    .expect("initialize");
    work.commit().await.expect("initialization commit");
    let first = AttemptSpec {
        id: Uuid::now_v7(),
        task: started.id,
        expected_revision: 2,
        staffing_group: "first-personal".to_owned(),
        fork: "acme/fork".to_owned(),
        config_sha: "a".repeat(40),
        submit_body: b"first".to_vec(),
        prompt_body: None,
        deadline: DateTime::<Utc>::from_timestamp(1000, 0).expect("deadline"),
        unbounded_running: false,
    };
    let mut work = database.begin().await.expect("admit first");
    assert_eq!(
        admit_intake(&mut work, &outbox, start.issue, &first)
            .await
            .expect("first admission"),
        Admission::Admitted
    );
    sqlx::query(
        "UPDATE tasks.funding_grants SET purpose='task_contributed',payer_id=2, \
         provider='openai',slot_epoch=1,slot_grant_id='slot-1', \
         credential_record_id='record-1',credential_version=1,route='openai-api-key' \
         WHERE attempt_id=$1",
    )
    .bind(first.id)
    .execute(work.connection())
    .await
    .expect("personal grant fixture");
    work.commit().await.expect("first admission commit");
    let mut work = database.begin().await.expect("cancel before send");
    assert!(
        cancel_attempt(&mut work, &outbox, first.id, "host-user")
            .await
            .expect("cancel")
    );
    work.commit().await.expect("cancel commit");
    let reservation: (String, String) = sqlx::query_as(
        "SELECT status,reservation_status FROM tasks.funding_grants WHERE attempt_id=$1",
    )
    .bind(first.id)
    .fetch_one(database.pool())
    .await
    .expect("released reservation");
    assert_eq!(reservation, ("revoked".to_owned(), "released".to_owned()));
    let dashboard: String =
        sqlx::query_scalar("SELECT desired_body FROM tasks.dashboard_surfaces WHERE task_id=$1")
            .bind(started.id.0)
            .fetch_one(database.pool())
            .await
            .expect("dashboard");
    assert!(dashboard.contains("Model calls: 0 / 1"));
    let mut reset = task(&database, start.issue)
        .await
        .expect("task")
        .expect("cancelled task");
    assert_eq!(reset.used_model_calls, 1);
    reset.stage = Stage::Intake;
    reset.retry_stage = None;
    reset.stage_reason = None;
    reset.current_attempt = None;
    reset.revision += 1;
    sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
        .bind(reset.id.0)
        .bind(i64::try_from(reset.revision).expect("revision"))
        .bind(SqlJson(&reset))
        .execute(database.pool())
        .await
        .expect("resume fixture");
    let second = AttemptSpec {
        id: Uuid::now_v7(),
        expected_revision: reset.revision,
        staffing_group: "second-attempt".to_owned(),
        submit_body: b"second".to_vec(),
        ..first
    };
    let mut work = database.begin().await.expect("admit replacement");
    assert_eq!(
        admit_intake(&mut work, &outbox, start.issue, &second)
            .await
            .expect("replacement admission"),
        Admission::Admitted
    );
    work.commit().await.expect("replacement commit");
    let resumed = task(&database, start.issue)
        .await
        .expect("task")
        .expect("resumed task");
    assert_eq!(resumed.used_model_calls, 2);
}

#[tokio::test]
async fn invalid_intake_reports_schedule_two_paid_repairs_then_escalate() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("tasks schema");
    migrate_events(&database).await.expect("events schema");
    let outbox = Outbox::new(
        1024 * 1024,
        vec![
            "task.>".to_owned(),
            "request.>".to_owned(),
            "turn.>".to_owned(),
        ],
    );
    let start = request(10, RequestKind::Start);
    let mut work = database.begin().await.expect("start");
    record_request(&mut work, &start)
        .await
        .expect("record start");
    apply_request(
        &mut work,
        &outbox,
        start.id,
        start.issue,
        Authority {
            writer: true,
            issue_open: true,
        },
    )
    .await
    .expect("apply start");
    work.commit().await.expect("start commit");
    let started = task(&database, start.issue)
        .await
        .expect("task")
        .expect("started");
    let mut work = database.begin().await.expect("initialize");
    initialize_task(
        &mut work,
        &outbox,
        start.issue,
        started.revision,
        &snapshot(PinnedInputs {
            config_sha: "a".repeat(40),
            base_sha: "b".repeat(40),
            max_model_calls: 4,
            max_fix_rounds: 0,
            max_plan_revisions: 0,
        }),
    )
    .await
    .expect("initialize");
    work.commit().await.expect("initialize commit");
    let line = format!(
        "{{\"type\":\"response_item\",\"payload\":{{\"name\":\"shell\",\"arguments\":\"searchable alpha\"}}}}\n{{\"type\":\"bad\\u0000kind\",\"message\":\"fallback\"}}\n{{\"type\":\"x\",\"text\":\"{} boundaryneedle\"}}\n",
        "a".repeat(99_980)
    );
    let mut tar = Builder::new(Vec::new());
    let mut header = Header::new_gnu();
    header.set_size(line.len() as u64);
    header.set_mtime(20);
    header.set_mode(0o644);
    header.set_cksum();
    tar.append_data(&mut header, "codex/turn.jsonl", line.as_bytes())
        .expect("transcript member");
    let journal = br#"{"scope":"scope-1","turn":0,"status":"complete",
        "request":"digest-0","identifier":"11111111-1111-4111-8111-111111111111",
        "prompt":{"instructions":"pinned","response_schema":{}},"response":{"ok":true}}"#;
    let mut journal_header = Header::new_gnu();
    journal_header.set_size(journal.len() as u64);
    journal_header.set_mtime(20);
    journal_header.set_mode(0o644);
    journal_header.set_cksum();
    tar.append_data(&mut journal_header, "turn.json", journal.as_slice())
        .expect("journal member");
    let tar_bytes = tar.into_inner().expect("tar bytes");
    let mut gzip = GzEncoder::new(Vec::new(), Compression::default());
    gzip.write_all(&tar_bytes).expect("gzip write");
    let archive = STANDARD.encode(gzip.finish().expect("gzip bytes"));
    let source_session = Uuid::now_v7();
    let mut turns = Vec::new();
    for index in 0..3 {
        let current = task(&database, start.issue)
            .await
            .expect("task")
            .expect("current");
        assert_eq!(current.stage, Stage::Intake);
        let turn = Uuid::now_v7();
        turns.push(turn);
        let (submit_body, prompt_body) = if index == 1 {
            let sha: String =
                sqlx::query_scalar("SELECT gzip_sha256 FROM tasks.turn_archives WHERE turn_id=$1")
                    .bind(turns[0])
                    .fetch_one(database.pool())
                    .await
                    .expect("predecessor archive digest");
            let prompt = json!({"instructions":"pinned","response_schema":{}});
            let submission = TurnSubmission {
                turn,
                expected: json!({}),
                models: vec!["test".to_owned()],
                budget: 1,
                lease: 1,
                payload: json!({
                    "request":{
                        "session":{
                            "scope":"scope-1","turn":1,"role":"intake:test:test",
                            "predecessor_digest":"digest-0","gzip_sha256":sha,
                        },
                        "max_retained_bytes":(line.len() + journal.len()) as u64,
                    },
                    "retained":archive,
                }),
                pin: None,
                binding: json!({}),
            };
            (
                serde_json::to_vec(&submission).expect("retained submission"),
                Some(serde_json::to_vec(&prompt).expect("retained prompt")),
            )
        } else {
            (b"body".to_vec(), None)
        };
        let mut work = database.begin().await.expect("admit");
        assert_eq!(
            admit_intake(
                &mut work,
                &outbox,
                start.issue,
                &AttemptSpec {
                    id: turn,
                    task: current.id,
                    expected_revision: current.revision,
                    staffing_group: format!("group-{index}"),
                    fork: "acme/fork".to_owned(),
                    config_sha: "a".repeat(40),
                    submit_body,
                    prompt_body,
                    deadline: DateTime::<Utc>::from_timestamp(1000, 0).expect("deadline"),
                    unbounded_running: false,
                }
            )
            .await
            .expect("admit"),
            Admission::Admitted
        );
        work.commit().await.expect("admit commit");
        let observation: TurnObservation = serde_json::from_value(json!({
            "turn": turn, "status": "done", "created": 1.0, "finished": 2.0,
            "dispatch": (index == 0).then_some(source_session),
            "result": {"outcome": "ok", "response": {"decision": "proceed",
                "rationale": (index == 0).then_some("model\0text")},
                "retained": (index == 0).then(|| archive.clone())}
        }))
        .expect("observation");
        let mut work = database.begin().await.expect("result");
        record_observation(&mut work, &outbox, &observation, None)
            .await
            .expect("record result");
        work.commit().await.expect("result commit");
        if index == 0 {
            let (raw, projected, observation): (Vec<u8>, Value, Vec<u8>) = sqlx::query_as(
                "SELECT r.raw_report_bytes,r.raw_report,a.observation_raw \
                 FROM tasks.intake_reports r JOIN tasks.attempts a ON a.id=r.attempt_id \
                 WHERE r.attempt_id=$1",
            )
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("raw and queryable report");
            assert_eq!(
                serde_json::from_slice::<Value>(&raw).expect("exact report")["rationale"],
                "model\0text"
            );
            assert_eq!(projected["rationale"], "model\\u0000text");
            assert_eq!(
                serde_json::from_slice::<Value>(&observation).expect("original observation")["result"]
                    ["response"]["rationale"],
                "model\0text"
            );
            assert_eq!(
                latest_intake_correction(&database, started.id.0)
                    .await
                    .expect("correction")
                    .expect("invalid report")
                    .report["rationale"],
                "model\0text"
            );
            let matches: i64 = sqlx::query_scalar(
                "SELECT count(*) FROM tasks.transcript_text_parts p \
                 JOIN tasks.turn_archives a ON a.tar_sha256=p.tar_sha256 \
                 WHERE a.turn_id=$1 AND p.search_vector @@ plainto_tsquery('simple','searchable alpha')",
            )
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("transcript search");
            assert_eq!(matches, 1);
            let boundary_matches: i64 = sqlx::query_scalar(
                "SELECT count(*) FROM tasks.transcript_text_parts p \
                 JOIN tasks.turn_archives a ON a.tar_sha256=p.tar_sha256 \
                 WHERE a.turn_id=$1 AND p.search_vector @@ plainto_tsquery('simple','boundaryneedle')",
            )
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("boundary word search");
            assert_eq!(boundary_matches, 1);
            let (kind, fallback): (String, Vec<u8>) = sqlx::query_as(
                "SELECT e.event_kind,e.fallback_bytes FROM tasks.transcript_events e \
                 JOIN tasks.turn_archives a ON a.tar_sha256=e.tar_sha256 \
                 WHERE a.turn_id=$1 AND e.line_number=2",
            )
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("fallback metadata");
            assert_eq!(kind, "bad\\u0000kind");
            assert_eq!(
                fallback,
                b"{\"type\":\"bad\\u0000kind\",\"message\":\"fallback\"}\n"
            );
            let first_page = transcript_page(&database, turn, 0, 1)
                .await
                .expect("transcript page")
                .expect("turn archive");
            assert_eq!(first_page.len(), 1);
            let rest = transcript_page(&database, turn, first_page[0].position, 10)
                .await
                .expect("transcript continuation")
                .expect("turn archive");
            assert!(
                rest.iter().any(|line| line.fallback_base64.as_deref()
                    == Some(STANDARD.encode(&fallback).as_str()))
            );
            let digest = "c".repeat(64);
            sqlx::query(
                "INSERT INTO tasks.turn_artifacts (sha256,body,media_type) \
                 VALUES ($1,$2,'text/plain')",
            )
            .bind(&digest)
            .bind(b"linked artifact".as_slice())
            .execute(database.pool())
            .await
            .expect("artifact");
            sqlx::query("INSERT INTO tasks.turn_artifact_links (turn_id,sha256) VALUES ($1,$2)")
                .bind(turn)
                .bind(&digest)
                .execute(database.pool())
                .await
                .expect("artifact link");
            assert_eq!(
                turn_artifact(&database, turn, &digest)
                    .await
                    .expect("linked artifact read"),
                Some(("text/plain".to_owned(), b"linked artifact".to_vec()))
            );
            assert!(
                turn_artifact(&database, Uuid::now_v7(), &digest)
                    .await
                    .expect("unlinked artifact read")
                    .is_none()
            );
            let (upload, stored_result, retained_file_bytes): (Vec<u8>, Value, i64) = sqlx::query_as(
                "SELECT u.gzip_bytes,a.result,l.retained_file_bytes FROM tasks.archive_uploads u \
                 JOIN tasks.turn_archives l ON l.gzip_sha256=u.gzip_sha256 \
                 JOIN tasks.attempts a ON a.id=l.turn_id WHERE l.turn_id=$1",
            )
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("original archive and sanitized result");
            assert_eq!(STANDARD.encode(upload), archive);
            assert_eq!(retained_file_bytes, (line.len() + journal.len()) as i64);
            let source: (i64, i64, Option<String>, Option<i64>) = sqlx::query_as(
                "SELECT repository_id,issue_number,session_scope,session_turn \
                 FROM tasks.turn_archives WHERE turn_id=$1",
            )
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("archive source");
            assert_eq!(
                source,
                (
                    start.issue.repository_id,
                    start.issue.number,
                    Some("scope-1".to_owned()),
                    Some(0)
                )
            );
            let linked: i64 = sqlx::query_scalar(
                "SELECT count(*) FROM tasks.transcript_events e \
                 JOIN tasks.turn_archives a ON a.tar_sha256=e.tar_sha256 \
                 JOIN tasks.attempt_sessions s ON s.turn_id=a.turn_id \
                 WHERE a.turn_id=$1 AND s.session_id=$2",
            )
            .bind(turn)
            .bind(source_session)
            .fetch_one(database.pool())
            .await
            .expect("transcript source links");
            assert_eq!(linked, 3);
            assert!(stored_result.get("retained").is_none());
            let current = task(&database, start.issue)
                .await
                .expect("task")
                .expect("current task");
            let mut eligibility = database.begin().await.expect("eligibility transaction");
            let prompt = json!({"instructions":"pinned","response_schema":{}});
            assert_eq!(
                retained_eligibility(
                    &mut eligibility,
                    current.id.0,
                    "scope-1",
                    1,
                    "digest-1",
                    Some("digest-0"),
                    None,
                    &prompt
                )
                .await
                .expect("continuation eligibility"),
                RetainedEligibility::Continue {
                    gzip_sha256: sqlx::query_scalar(
                        "SELECT gzip_sha256 FROM tasks.turn_archives WHERE turn_id=$1"
                    )
                    .bind(turn)
                    .fetch_one(database.pool())
                    .await
                    .expect("digest")
                }
            );
            assert_eq!(
                retained_eligibility(
                    &mut eligibility,
                    current.id.0,
                    "scope-1",
                    0,
                    "digest-0",
                    None,
                    None,
                    &prompt
                )
                .await
                .expect("replay eligibility"),
                RetainedEligibility::Replay {
                    response: json!({"ok":true})
                }
            );
            let retained_size = (line.len() + journal.len()) as u64;
            for (ordinal, digest, predecessor) in
                [(0, "digest-0", None), (1, "digest-1", Some("digest-0"))]
            {
                assert_eq!(
                    retained_eligibility(
                        &mut eligibility,
                        current.id.0,
                        "scope-1",
                        ordinal,
                        digest,
                        predecessor,
                        Some(retained_size - 1),
                        &prompt
                    )
                    .await
                    .expect("oversized retained revision"),
                    RetainedEligibility::Unavailable("retained_size_exceeded")
                );
            }
            std::assert_matches!(
                retained_eligibility(
                    &mut eligibility,
                    current.id.0,
                    "scope-1",
                    1,
                    "digest-1",
                    Some("digest-0"),
                    Some(retained_size),
                    &prompt
                )
                .await
                .expect("exact retained limit"),
                RetainedEligibility::Continue { .. }
            );
            std::assert_matches!(
                retained_eligibility(
                    &mut eligibility,
                    current.id.0,
                    "scope-1",
                    0,
                    "digest-0",
                    None,
                    Some(retained_size),
                    &prompt
                )
                .await
                .expect("exact replay limit"),
                RetainedEligibility::Replay { .. }
            );
            sqlx::query(
                "UPDATE tasks.turn_archives SET \
                 journal=jsonb_set(journal,'{response}','null'::jsonb) WHERE turn_id=$1",
            )
            .bind(turn)
            .execute(eligibility.connection())
            .await
            .expect("simulate null cached response");
            assert_eq!(
                retained_eligibility(
                    &mut eligibility,
                    current.id.0,
                    "scope-1",
                    0,
                    "digest-0",
                    None,
                    None,
                    &prompt
                )
                .await
                .expect("null cached replay"),
                RetainedEligibility::Unavailable("retained_replay_missing_response")
            );
            sqlx::query(
                "UPDATE tasks.turn_archives SET \
                 journal=jsonb_set(journal,'{response}','{\"ok\":true}'::jsonb) WHERE turn_id=$1",
            )
            .bind(turn)
            .execute(eligibility.connection())
            .await
            .expect("restore cached response for other cases");
            assert_eq!(
                retained_eligibility(
                    &mut eligibility,
                    current.id.0,
                    "scope-1",
                    0,
                    "changed",
                    None,
                    None,
                    &prompt
                )
                .await
                .expect("changed request"),
                RetainedEligibility::Unavailable("retained_replay_mismatch")
            );
            assert_eq!(
                retained_eligibility(
                    &mut eligibility,
                    current.id.0,
                    "scope-1",
                    1,
                    "digest-1",
                    Some("wrong-predecessor"),
                    None,
                    &prompt
                )
                .await
                .expect("changed predecessor"),
                RetainedEligibility::Unavailable("retained_predecessor_mismatch")
            );
            assert_eq!(
                retained_eligibility(
                    &mut eligibility,
                    current.id.0,
                    "scope-1",
                    1,
                    "digest-1",
                    Some("digest-0"),
                    None,
                    &json!({"instructions":"changed","response_schema":{}})
                )
                .await
                .expect("changed instructions"),
                RetainedEligibility::Unavailable("retained_instruction_mismatch")
            );
            assert_eq!(
                retained_eligibility(
                    &mut eligibility,
                    current.id.0,
                    "other-scope",
                    1,
                    "digest-1",
                    Some("digest-0"),
                    None,
                    &prompt
                )
                .await
                .expect("other scope"),
                RetainedEligibility::Unavailable("retained_revision_missing")
            );
            assert_eq!(
                retained_eligibility(
                    &mut eligibility,
                    current.id.0,
                    "scope-1",
                    2,
                    "digest-2",
                    Some("digest-1"),
                    None,
                    &prompt
                )
                .await
                .expect("skipped ordinal"),
                RetainedEligibility::Unavailable("retained_revision_missing")
            );
            sqlx::query(
                "UPDATE tasks.turn_archives SET archive_error='invalid_tar' WHERE turn_id=$1",
            )
            .bind(turn)
            .execute(eligibility.connection())
            .await
            .expect("simulate corrupt revision");
            assert_eq!(
                retained_eligibility(
                    &mut eligibility,
                    current.id.0,
                    "scope-1",
                    1,
                    "digest-1",
                    Some("digest-0"),
                    None,
                    &prompt
                )
                .await
                .expect("corrupt revision"),
                RetainedEligibility::Unavailable("retained_revision_unrestorable")
            );
            sqlx::query(
                "UPDATE tasks.turn_archives SET archive_error=NULL, \
                 journal=jsonb_set(journal,'{status}','\"pending\"'::jsonb) WHERE turn_id=$1",
            )
            .bind(turn)
            .execute(eligibility.connection())
            .await
            .expect("simulate pending journal");
            assert_eq!(
                retained_eligibility(
                    &mut eligibility,
                    current.id.0,
                    "scope-1",
                    1,
                    "digest-1",
                    Some("digest-0"),
                    None,
                    &prompt
                )
                .await
                .expect("pending journal"),
                RetainedEligibility::Unavailable("retained_journal_mismatch")
            );
            eligibility.rollback().await.expect("read-only eligibility");
            let detail = task_detail(&database, start.issue)
                .await
                .expect("task detail")
                .expect("task");
            assert_eq!(detail["archives"][0]["turn"], turn.to_string());
            assert_eq!(detail["archives"][0]["upload_status"], "pending");
            let pending = pending_effects(&database).await.expect("pending effects");
            assert!(
                pending
                    .iter()
                    .any(|effect| { effect["kind"] == "archive" && effect["status"] == "pending" })
            );
        }
        let advanced = task(&database, start.issue)
            .await
            .expect("task")
            .expect("advanced");
        assert_eq!(
            advanced.stage,
            if index == 2 {
                Stage::Escalated
            } else {
                Stage::Intake
            }
        );
        assert_eq!(advanced.used_model_calls, index + 1);
        assert!(advanced.current_attempt.is_none());
        if index == 0 {
            let queued_before: i64 =
                sqlx::query_scalar("SELECT count(*) FROM graphile_worker._private_jobs")
                    .fetch_one(database.pool())
                    .await
                    .expect("queued jobs before retirement");
            let retirement_id: i64 = sqlx::query_scalar(
                "SELECT id FROM tasks.effects WHERE turn_id=$1 AND kind='retire'",
            )
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("retirement effect");
            let mut claim = database.begin().await.expect("claim retirement");
            let retirement = claim_effect(&mut claim, retirement_id)
                .await
                .expect("claim")
                .expect("retirement");
            claim.commit().await.expect("claim commit");
            let mut acknowledge = database.begin().await.expect("acknowledge retirement");
            assert!(
                acknowledge_retirement(&mut acknowledge, &retirement, &[])
                    .await
                    .expect("acknowledge")
            );
            acknowledge.commit().await.expect("acknowledgement commit");
            let queued_after: i64 =
                sqlx::query_scalar("SELECT count(*) FROM graphile_worker._private_jobs")
                    .fetch_one(database.pool())
                    .await
                    .expect("queued jobs after retirement");
            assert_eq!(queued_after, queued_before + 1);
        }
    }
    let invalid_count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM tasks.intake_reports WHERE NOT accepted")
            .fetch_one(database.pool())
            .await
            .expect("invalid reports");
    assert_eq!(invalid_count, 3);
    let replay: TurnObservation = serde_json::from_value(json!({
        "turn": turns[0], "status": "done", "created": 1.0, "finished": 2.0,
        "result": {"outcome": "ok", "response": {"decision": "changed"}}
    }))
    .expect("replay");
    let mut work = database.begin().await.expect("replay");
    record_observation(&mut work, &outbox, &replay, None)
        .await
        .expect("record replay");
    work.commit().await.expect("replay commit");
    let original: Value = sqlx::query_scalar("SELECT result FROM tasks.attempts WHERE id=$1")
        .bind(turns[0])
        .fetch_one(database.pool())
        .await
        .expect("original result");
    assert_eq!(original["response"]["decision"], "proceed");
    assert_eq!(
        task(&database, start.issue)
            .await
            .expect("task")
            .expect("task")
            .stage,
        Stage::Escalated
    );
}

#[tokio::test]
async fn exhausted_poll_retries_block_intake_without_retiring_a_live_turn() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("tasks schema");
    migrate_events(&database).await.expect("events schema");
    let outbox = Outbox::new(
        1024 * 1024,
        vec![
            "task.>".to_owned(),
            "request.>".to_owned(),
            "turn.>".to_owned(),
        ],
    );
    let start = request(11, RequestKind::Start);
    let mut work = database.begin().await.expect("start");
    record_request(&mut work, &start)
        .await
        .expect("record start");
    apply_request(
        &mut work,
        &outbox,
        start.id,
        start.issue,
        Authority {
            writer: true,
            issue_open: true,
        },
    )
    .await
    .expect("apply start");
    work.commit().await.expect("start commit");
    let started = task(&database, start.issue)
        .await
        .expect("task")
        .expect("started");
    let mut work = database.begin().await.expect("initialize");
    initialize_task(
        &mut work,
        &outbox,
        start.issue,
        started.revision,
        &snapshot(PinnedInputs {
            config_sha: "a".repeat(40),
            base_sha: "b".repeat(40),
            max_model_calls: 2,
            max_fix_rounds: 0,
            max_plan_revisions: 0,
        }),
    )
    .await
    .expect("initialize");
    work.commit().await.expect("initialize commit");
    let initialized = task(&database, start.issue)
        .await
        .expect("task")
        .expect("initialized");
    let turn = Uuid::now_v7();
    let mut work = database.begin().await.expect("admit");
    assert_eq!(
        admit_intake(
            &mut work,
            &outbox,
            start.issue,
            &AttemptSpec {
                id: turn,
                task: initialized.id,
                expected_revision: initialized.revision,
                staffing_group: "triage".to_owned(),
                fork: "acme/fork".to_owned(),
                config_sha: "a".repeat(40),
                submit_body: b"body".to_vec(),
                prompt_body: None,
                deadline: DateTime::<Utc>::from_timestamp(1000, 0).expect("deadline"),
                unbounded_running: false,
            }
        )
        .await
        .expect("admit"),
        Admission::Admitted
    );
    work.commit().await.expect("admit commit");
    assert!(
        due_attempt_deadlines(&database)
            .await
            .expect("due ready attempts")
            .contains(&(turn, 0))
    );
    let queued: TurnObservation = serde_json::from_value(json!({
        "turn": turn, "status": "queued", "created": 1.0,
    }))
    .expect("queued");
    let mut work = database.begin().await.expect("queued result");
    record_observation(&mut work, &outbox, &queued, None)
        .await
        .expect("record queued");
    work.commit().await.expect("queued commit");
    for failure in 1..=21 {
        let mut work = database.begin().await.expect("poll failure");
        let exhausted = record_poll_failure(&mut work, &outbox, turn, i64::from(failure), None)
            .await
            .expect("count failure");
        assert_eq!(exhausted, Some(failure > 20));
        work.commit().await.expect("failure commit");
        if failure == 1 {
            let delay: f64 = sqlx::query_scalar(
                "SELECT EXTRACT(EPOCH FROM run_at-now())::double precision \
                 FROM graphile_worker.jobs WHERE key=$1",
            )
            .bind(format!("exchange-poll:{turn}:2"))
            .fetch_one(database.pool())
            .await
            .expect("scheduled poll retry");
            assert!((10.0..=16.0).contains(&delay));
        }
    }
    let blocked = task(&database, start.issue)
        .await
        .expect("task")
        .expect("blocked");
    assert_eq!(blocked.stage, Stage::Blocked);
    assert!(blocked.current_attempt.is_none());
    let status: String = sqlx::query_scalar("SELECT status FROM tasks.attempts WHERE id=$1")
        .bind(turn)
        .fetch_one(database.pool())
        .await
        .expect("attempt status");
    assert_eq!(status, "unknown");
    let retire_count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM tasks.effects WHERE turn_id=$1 AND kind='retire'")
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("retirement count");
    assert_eq!(retire_count, 0);
    let delayed_session = Uuid::now_v7();
    let delayed: TurnObservation = serde_json::from_value(json!({
        "turn":turn,"status":"queued","created":1.0,"dispatch":delayed_session
    }))
    .expect("delayed queued observation");
    let mut work = database.begin().await.expect("late poll");
    record_observation(&mut work, &outbox, &delayed, None)
        .await
        .expect("retain live session");
    work.commit().await.expect("late poll commit");
    let session = runner_session(&database, delayed_session)
        .await
        .expect("runner")
        .expect("session");
    assert!(session.desired_present);
    let group_count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM tasks.staffing_groups WHERE turn_id=$1")
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("held staffing group");
    assert_eq!(group_count, 1);
    let finished_count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM events.outbox WHERE type=$1")
            .bind(format!("turn.{turn}.finished"))
            .fetch_one(database.pool())
            .await
            .expect("finished facts");
    assert_eq!(finished_count, 0);
    let stop = request(20, RequestKind::Stop);
    let mut work = database.begin().await.expect("stop live unknown");
    record_request(&mut work, &stop).await.expect("record stop");
    apply_request(
        &mut work,
        &outbox,
        stop.id,
        stop.issue,
        Authority {
            writer: true,
            issue_open: true,
        },
    )
    .await
    .expect("apply stop");
    work.commit().await.expect("stop commit");
    let stopped: String = sqlx::query_scalar("SELECT status FROM tasks.attempts WHERE id=$1")
        .bind(turn)
        .fetch_one(database.pool())
        .await
        .expect("stopped live attempt");
    assert_eq!(stopped, "cancelled");
    assert!(
        !runner_session(&database, delayed_session)
            .await
            .expect("runner")
            .expect("session")
            .desired_present
    );
}

/// A definitive missing turn retires its session before the second admission.
#[tokio::test]
async fn poll_404_retires_then_releases_the_next_intake() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("tasks schema");
    migrate_events(&database).await.expect("events schema");
    let outbox = Outbox::new(
        1024 * 1024,
        vec![
            "task.>".to_owned(),
            "request.>".to_owned(),
            "turn.>".to_owned(),
        ],
    );
    let start = request(11, RequestKind::Start);
    let mut work = database.begin().await.expect("start");
    record_request(&mut work, &start)
        .await
        .expect("record start");
    apply_request(
        &mut work,
        &outbox,
        start.id,
        start.issue,
        Authority {
            writer: true,
            issue_open: true,
        },
    )
    .await
    .expect("apply start");
    work.commit().await.expect("start commit");
    let started = task(&database, start.issue)
        .await
        .expect("task")
        .expect("started");
    let mut work = database.begin().await.expect("initialize");
    initialize_task(
        &mut work,
        &outbox,
        start.issue,
        started.revision,
        &snapshot(PinnedInputs {
            config_sha: "a".repeat(40),
            base_sha: "b".repeat(40),
            max_model_calls: 3,
            max_fix_rounds: 0,
            max_plan_revisions: 0,
        }),
    )
    .await
    .expect("initialize");
    work.commit().await.expect("initialize commit");
    let initialized = task(&database, start.issue)
        .await
        .expect("task")
        .expect("initialized");
    let turn = Uuid::now_v7();
    let mut work = database.begin().await.expect("admit");
    assert_eq!(
        admit_intake(
            &mut work,
            &outbox,
            start.issue,
            &AttemptSpec {
                id: turn,
                task: initialized.id,
                expected_revision: initialized.revision,
                staffing_group: "triage".to_owned(),
                fork: "acme/fork".to_owned(),
                config_sha: "a".repeat(40),
                submit_body: b"body".to_vec(),
                prompt_body: None,
                deadline: DateTime::<Utc>::from_timestamp(1000, 0).expect("deadline"),
                unbounded_running: false,
            },
        )
        .await
        .expect("admit"),
        Admission::Admitted
    );
    work.commit().await.expect("admit commit");
    let session = Uuid::now_v7();
    let queued: TurnObservation = serde_json::from_value(json!({
        "turn":turn,"status":"queued","created":1.0,"dispatch":session
    }))
    .expect("queued");
    let mut work = database.begin().await.expect("queued result");
    record_observation(&mut work, &outbox, &queued, None)
        .await
        .expect("record queued");
    work.commit().await.expect("queued commit");
    let mut work = database.begin().await.expect("poll refusal");
    assert_eq!(
        record_poll_failure(&mut work, &outbox, turn, 1, Some(404))
            .await
            .expect("missing turn"),
        Some(true)
    );
    work.commit().await.expect("refusal commit");
    let retrying = task(&database, start.issue)
        .await
        .expect("task")
        .expect("retrying");
    assert_eq!(retrying.stage, Stage::Intake);
    assert!(retrying.current_attempt.is_none());
    assert!(
        !runner_session(&database, session)
            .await
            .expect("runner")
            .expect("session")
            .desired_present
    );
    let retirement: i64 =
        sqlx::query_scalar("SELECT id FROM tasks.effects WHERE turn_id=$1 AND kind='retire'")
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("queued retirement");
    let mut work = database.begin().await.expect("claim retirement");
    let job = claim_effect(&mut work, retirement)
        .await
        .expect("claim")
        .expect("retirement");
    work.commit().await.expect("claim commit");
    let mut work = database.begin().await.expect("ack retirement");
    acknowledge_retirement(&mut work, &job, &[session])
        .await
        .expect("acknowledge");
    work.commit().await.expect("ack commit");
    let second = Uuid::now_v7();
    let mut work = database.begin().await.expect("second admission");
    assert_eq!(
        admit_intake(
            &mut work,
            &outbox,
            start.issue,
            &AttemptSpec {
                id: second,
                task: retrying.id,
                expected_revision: retrying.revision,
                staffing_group: "triage".to_owned(),
                fork: "acme/fork".to_owned(),
                config_sha: "a".repeat(40),
                submit_body: b"body".to_vec(),
                prompt_body: None,
                deadline: DateTime::<Utc>::from_timestamp(1000, 0).expect("deadline"),
                unbounded_running: false,
            },
        )
        .await
        .expect("second admission"),
        Admission::Admitted
    );
    work.commit().await.expect("second commit");
    let fact: Vec<u8> = sqlx::query_scalar("SELECT wire_bytes FROM events.outbox WHERE type=$1")
        .bind(format!("turn.{turn}.finished"))
        .fetch_one(database.pool())
        .await
        .expect("finished fact");
    let fact = decode::<TurnFinished>(&fact).expect("contract");
    assert_eq!(fact.data.outcome, "unknown");
    assert_eq!(fact.data.reason.as_deref(), Some("exchange_poll_http_404"));
    let prior_snapshots: i64 =
        sqlx::query_scalar("SELECT count(*) FROM events.outbox WHERE type=$1")
            .bind(format!("turn.{turn}.status"))
            .fetch_one(database.pool())
            .await
            .expect("prior status snapshot count");
    let stop = request(20, RequestKind::Stop);
    let mut work = database.begin().await.expect("stop after ended unknown");
    record_request(&mut work, &stop).await.expect("record stop");
    apply_request(
        &mut work,
        &outbox,
        stop.id,
        stop.issue,
        Authority {
            writer: true,
            issue_open: true,
        },
    )
    .await
    .expect("apply stop");
    work.commit().await.expect("stop commit");
    let ended_status: String = sqlx::query_scalar("SELECT status FROM tasks.attempts WHERE id=$1")
        .bind(turn)
        .fetch_one(database.pool())
        .await
        .expect("ended attempt status");
    assert_eq!(ended_status, "unknown");
    let snapshots: i64 = sqlx::query_scalar("SELECT count(*) FROM events.outbox WHERE type=$1")
        .bind(format!("turn.{turn}.status"))
        .fetch_one(database.pool())
        .await
        .expect("status snapshot count");
    assert_eq!(snapshots, prior_snapshots);
}

#[tokio::test]
async fn expired_intake_deadline_retries_once_then_blocks() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("tasks schema");
    migrate_events(&database).await.expect("events schema");
    let outbox = Outbox::new(
        1024 * 1024,
        vec![
            "task.>".to_owned(),
            "request.>".to_owned(),
            "turn.>".to_owned(),
        ],
    );
    let start = request(11, RequestKind::Start);
    let mut work = database.begin().await.expect("start");
    record_request(&mut work, &start)
        .await
        .expect("record start");
    apply_request(
        &mut work,
        &outbox,
        start.id,
        start.issue,
        Authority {
            writer: true,
            issue_open: true,
        },
    )
    .await
    .expect("apply start");
    work.commit().await.expect("start commit");
    let started = task(&database, start.issue)
        .await
        .expect("task")
        .expect("started");
    let mut work = database.begin().await.expect("initialize");
    initialize_task(
        &mut work,
        &outbox,
        start.issue,
        started.revision,
        &snapshot(PinnedInputs {
            config_sha: "a".repeat(40),
            base_sha: "b".repeat(40),
            max_model_calls: 2,
            max_fix_rounds: 0,
            max_plan_revisions: 0,
        }),
    )
    .await
    .expect("initialize");
    work.commit().await.expect("initialize commit");
    let initialized = task(&database, start.issue)
        .await
        .expect("task")
        .expect("initialized");
    let turn = Uuid::now_v7();
    let mut work = database.begin().await.expect("admit");
    assert_eq!(
        admit_intake(
            &mut work,
            &outbox,
            start.issue,
            &AttemptSpec {
                id: turn,
                task: initialized.id,
                expected_revision: initialized.revision,
                staffing_group: "triage".to_owned(),
                fork: "acme/fork".to_owned(),
                config_sha: "a".repeat(40),
                submit_body: b"body".to_vec(),
                prompt_body: None,
                deadline: DateTime::<Utc>::from_timestamp(1000, 0).expect("deadline"),
                unbounded_running: false,
            }
        )
        .await
        .expect("admit"),
        Admission::Admitted
    );
    work.commit().await.expect("admit commit");
    let queued: TurnObservation = serde_json::from_value(json!({
        "turn": turn, "status": "queued", "created": 1.0,
    }))
    .expect("queued");
    let mut work = database.begin().await.expect("queued result");
    record_observation(&mut work, &outbox, &queued, None)
        .await
        .expect("record queued");
    work.commit().await.expect("queued commit");
    let mut work = database.begin().await.expect("expiry");
    assert!(
        expire_attempt(&mut work, &outbox, turn, 1)
            .await
            .expect("expire")
    );
    work.commit().await.expect("expiry commit");
    let retrying = task(&database, start.issue)
        .await
        .expect("task")
        .expect("retrying");
    assert_eq!(retrying.stage, Stage::Intake);
    assert!(retrying.current_attempt.is_none());
    let status: String = sqlx::query_scalar("SELECT status FROM tasks.attempts WHERE id=$1")
        .bind(turn)
        .fetch_one(database.pool())
        .await
        .expect("attempt status");
    assert_eq!(status, "unknown");
    let fact: Vec<u8> = sqlx::query_scalar("SELECT wire_bytes FROM events.outbox WHERE type=$1")
        .bind(format!("turn.{turn}.finished"))
        .fetch_one(database.pool())
        .await
        .expect("deadline fact");
    let fact = decode::<TurnFinished>(&fact).expect("deadline contract");
    assert_eq!(fact.data.outcome, "failed");
    assert_eq!(fact.data.reason.as_deref(), Some("queue_expired"));
    let retire_count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM tasks.effects WHERE turn_id=$1 AND kind='retire'")
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("retirement count");
    assert_eq!(retire_count, 1);
    sqlx::query("UPDATE tasks.effects SET updated_at=now()-interval '6 minutes' WHERE turn_id=$1 AND kind='retire'")
        .bind(turn)
        .execute(database.pool())
        .await
        .expect("simulate exhausted retirement job");
    assert_eq!(
        recover_retirements(&database)
            .await
            .expect("recover retirement"),
        1
    );
    assert_eq!(
        recover_retirements(&database)
            .await
            .expect("stable recovery"),
        0
    );
    let retirement_id: i64 =
        sqlx::query_scalar("SELECT id FROM tasks.effects WHERE turn_id=$1 AND kind='retire'")
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("retirement");
    let mut work = database.begin().await.expect("claim retirement");
    let job = claim_effect(&mut work, retirement_id)
        .await
        .expect("claim")
        .expect("job");
    work.commit().await.expect("claim commit");
    let mut work = database.begin().await.expect("ack retirement");
    acknowledge_retirement(&mut work, &job, &[])
        .await
        .expect("acknowledge");
    work.commit().await.expect("ack commit");
    let second = Uuid::now_v7();
    let mut work = database.begin().await.expect("second admission");
    assert_eq!(
        admit_intake(
            &mut work,
            &outbox,
            start.issue,
            &AttemptSpec {
                id: second,
                task: retrying.id,
                expected_revision: retrying.revision,
                staffing_group: "triage".to_owned(),
                fork: "acme/fork".to_owned(),
                config_sha: "a".repeat(40),
                submit_body: b"body".to_vec(),
                prompt_body: None,
                deadline: DateTime::<Utc>::from_timestamp(1000, 0).expect("deadline"),
                unbounded_running: false,
            }
        )
        .await
        .expect("second admission"),
        Admission::Admitted
    );
    work.commit().await.expect("second admission commit");
    let mut work = database.begin().await.expect("second expiry");
    assert!(
        expire_attempt(&mut work, &outbox, second, 0)
            .await
            .expect("second expiry")
    );
    work.commit().await.expect("second expiry commit");
    assert_eq!(
        task(&database, start.issue)
            .await
            .expect("task")
            .expect("blocked")
            .stage,
        Stage::Blocked
    );
}

#[tokio::test]
async fn provider_failure_blocks_intake_before_a_terminal_exchange_status() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("tasks schema");
    migrate_events(&database).await.expect("events schema");
    let outbox = Outbox::new(
        1024 * 1024,
        vec![
            "task.>".to_owned(),
            "request.>".to_owned(),
            "turn.>".to_owned(),
        ],
    );
    let start = request(11, RequestKind::Start);
    let mut work = database.begin().await.expect("start");
    record_request(&mut work, &start)
        .await
        .expect("record start");
    apply_request(
        &mut work,
        &outbox,
        start.id,
        start.issue,
        Authority {
            writer: true,
            issue_open: true,
        },
    )
    .await
    .expect("apply start");
    work.commit().await.expect("start commit");
    let started = task(&database, start.issue)
        .await
        .expect("task")
        .expect("started");
    let mut work = database.begin().await.expect("initialize");
    initialize_task(
        &mut work,
        &outbox,
        start.issue,
        started.revision,
        &snapshot(PinnedInputs {
            config_sha: "a".repeat(40),
            base_sha: "b".repeat(40),
            max_model_calls: 2,
            max_fix_rounds: 0,
            max_plan_revisions: 0,
        }),
    )
    .await
    .expect("initialize");
    work.commit().await.expect("initialize commit");
    let initialized = task(&database, start.issue)
        .await
        .expect("task")
        .expect("initialized");
    let turn = Uuid::now_v7();
    let mut work = database.begin().await.expect("admit");
    assert_eq!(
        admit_intake(
            &mut work,
            &outbox,
            start.issue,
            &AttemptSpec {
                id: turn,
                task: initialized.id,
                expected_revision: initialized.revision,
                staffing_group: "triage".to_owned(),
                fork: "acme/fork".to_owned(),
                config_sha: "a".repeat(40),
                submit_body: b"body".to_vec(),
                prompt_body: None,
                deadline: Utc::now() + chrono::Duration::minutes(30),
                unbounded_running: false,
            }
        )
        .await
        .expect("admit"),
        Admission::Admitted
    );
    work.commit().await.expect("admit commit");
    let refused: TurnObservation = serde_json::from_value(json!({
        "turn": turn, "status": "queued", "created": 1.0,
        "provider_failure": {"category": "budget_denied"},
        "result": {"outcome": "ok", "response": {"decision": "proceed"}},
    }))
    .expect("failure");
    let mut work = database.begin().await.expect("failure observation");
    record_observation(&mut work, &outbox, &refused, None)
        .await
        .expect("record failure");
    work.commit().await.expect("failure commit");
    let blocked = task(&database, start.issue)
        .await
        .expect("task")
        .expect("blocked");
    assert_eq!(blocked.stage, Stage::Blocked);
    assert_eq!(
        blocked.stage_reason.as_deref(),
        Some("provider_failure:budget_denied")
    );
    let (status, result): (String, Option<Value>) =
        sqlx::query_as("SELECT status,result FROM tasks.attempts WHERE id=$1")
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("attempt");
    assert_eq!(status, "finished");
    assert!(result.is_none());
    let retire_count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM tasks.effects WHERE turn_id=$1 AND kind='retire'")
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("retirement count");
    assert_eq!(retire_count, 1);
}

#[tokio::test]
async fn stop_waits_for_admission_and_preserves_its_charge() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 3)
        .await
        .expect("database");
    migrate(&database).await.expect("tasks schema");
    migrate_events(&database).await.expect("events schema");
    let outbox = Outbox::new(
        1024 * 1024,
        vec![
            "request.>".to_owned(),
            "task.>".to_owned(),
            "turn.>".to_owned(),
        ],
    );
    let authority = Authority {
        writer: true,
        issue_open: true,
    };
    let start = request(10, RequestKind::Start);
    let issue = start.issue;
    let mut work = database.begin().await.expect("start");
    record_request(&mut work, &start)
        .await
        .expect("record start");
    apply_request(&mut work, &outbox, start.id, start.issue, authority)
        .await
        .expect("apply start");
    work.commit().await.expect("start commit");
    let current = task(&database, start.issue)
        .await
        .expect("state")
        .expect("task");
    let mut work = database.begin().await.expect("initialize");
    initialize_task(
        &mut work,
        &outbox,
        start.issue,
        current.revision,
        &snapshot(PinnedInputs {
            config_sha: "a".repeat(40),
            base_sha: "b".repeat(40),
            max_model_calls: 2,
            max_fix_rounds: 1,
            max_plan_revisions: 1,
        }),
    )
    .await
    .expect("initialize");
    work.commit().await.expect("initialize commit");
    let stop = request(20, RequestKind::Stop);
    let mut work = database.begin().await.expect("stop source");
    record_request(&mut work, &stop).await.expect("record stop");
    work.commit().await.expect("stop source commit");
    let attempt = AttemptSpec {
        id: Uuid::now_v7(),
        task: current.id,
        expected_revision: current.revision + 1,
        staffing_group: "intake".to_owned(),
        fork: "acme/fork".to_owned(),
        config_sha: "a".repeat(40),
        submit_body: b"stored body".to_vec(),
        prompt_body: None,
        deadline: Utc::now() + chrono::Duration::minutes(30),
        unbounded_running: false,
    };
    let mut admission = database.begin().await.expect("admission");
    assert_eq!(
        admit_intake(&mut admission, &outbox, start.issue, &attempt)
            .await
            .expect("admit"),
        Admission::Admitted
    );
    let stop_database = database.clone();
    let stop_outbox = outbox.clone();
    let (started, entering) = tokio::sync::oneshot::channel();
    let mut stopping = tokio::spawn(async move {
        let mut transaction = stop_database.begin().await.expect("stop transaction");
        started.send(()).expect("stop observer");
        let result = apply_request(
            &mut transaction,
            &stop_outbox,
            stop.id,
            stop.issue,
            authority,
        )
        .await
        .expect("apply stop");
        transaction.commit().await.expect("stop commit");
        result.expect("stop selected")
    });
    entering.await.expect("stop began");
    assert!(
        tokio::time::timeout(std::time::Duration::from_millis(100), &mut stopping)
            .await
            .is_err(),
        "stop must wait for the admission lock"
    );
    admission.commit().await.expect("admission commit");
    let (_, stopped) = stopping.await.expect("stop join");
    assert_eq!(stopped.status, RequestStatus::Applied);
    let final_state = task(&database, start.issue)
        .await
        .expect("state")
        .expect("task");
    assert_eq!(
        (
            final_state.stage,
            final_state.revision,
            final_state.used_model_calls
        ),
        (Stage::Stopped, 4, 1)
    );
    let status: String = sqlx::query_scalar("SELECT status FROM tasks.attempts WHERE id=$1")
        .bind(attempt.id)
        .fetch_one(database.pool())
        .await
        .expect("attempt");
    assert_eq!(status, "cancelled");

    let restart = request(30, RequestKind::Start);
    let mut work = database.begin().await.expect("restart");
    record_request(&mut work, &restart)
        .await
        .expect("record restart");
    apply_request(&mut work, &outbox, restart.id, issue, authority)
        .await
        .expect("apply restart");
    work.commit().await.expect("restart commit");
    let current = task(&database, issue).await.expect("task").expect("state");
    let mut work = database.begin().await.expect("reinitialize");
    initialize_task(
        &mut work,
        &outbox,
        issue,
        current.revision,
        &snapshot(PinnedInputs {
            config_sha: "c".repeat(40),
            base_sha: "d".repeat(40),
            max_model_calls: 2,
            max_fix_rounds: 1,
            max_plan_revisions: 1,
        }),
    )
    .await
    .expect("reinitialize");
    work.commit().await.expect("reinitialize commit");
    let second = AttemptSpec {
        id: Uuid::now_v7(),
        expected_revision: current.revision + 1,
        ..attempt
    };
    let mut work = database.begin().await.expect("second admission");
    assert_eq!(
        admit_intake(&mut work, &outbox, issue, &second)
            .await
            .expect("admit second"),
        Admission::Admitted
    );
    work.commit().await.expect("second admission commit");
    let stop = request(40, RequestKind::Stop);
    let mut work = database.begin().await.expect("second stop source");
    record_request(&mut work, &stop).await.expect("record stop");
    work.commit().await.expect("second stop source commit");
    let effect_id: i64 =
        sqlx::query_scalar("SELECT id FROM tasks.effects WHERE turn_id=$1 AND kind='submit'")
            .bind(second.id)
            .fetch_one(database.pool())
            .await
            .expect("submit effect");
    let mut claimed = database.begin().await.expect("claim effect");
    let submit_job = claim_effect(&mut claimed, effect_id)
        .await
        .expect("claim")
        .expect("submit");
    let stop_database = database.clone();
    let stop_outbox = outbox.clone();
    let (started, entering) = tokio::sync::oneshot::channel();
    let mut stopping = tokio::spawn(async move {
        let mut transaction = stop_database.begin().await.expect("stop transaction");
        started.send(()).expect("stop observer");
        let result = apply_request(&mut transaction, &stop_outbox, stop.id, issue, authority)
            .await
            .expect("apply stop");
        transaction.commit().await.expect("stop commit");
        result.expect("stop selected")
    });
    entering.await.expect("stop began");
    assert!(
        tokio::time::timeout(std::time::Duration::from_millis(100), &mut stopping)
            .await
            .is_err(),
        "stop must wait for the effect claim"
    );
    claimed.commit().await.expect("effect claim commit");
    let (_, stopped) = stopping.await.expect("stop join");
    assert_eq!(stopped.status, RequestStatus::Applied);
    let attempt_status: String =
        sqlx::query_scalar("SELECT status FROM tasks.attempts WHERE id=$1")
            .bind(second.id)
            .fetch_one(database.pool())
            .await
            .expect("second attempt");
    assert_eq!(attempt_status, "cancelled");
    let retirement_count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM tasks.effects WHERE turn_id=$1 AND kind='retire'")
            .bind(second.id)
            .fetch_one(database.pool())
            .await
            .expect("retirement");
    assert_eq!(retirement_count, 1);

    let retire_id: i64 =
        sqlx::query_scalar("SELECT id FROM tasks.effects WHERE turn_id=$1 AND kind='retire'")
            .bind(second.id)
            .fetch_one(database.pool())
            .await
            .expect("retirement effect");
    let mut work = database.begin().await.expect("claim retirement");
    let retire_job = claim_effect(&mut work, retire_id)
        .await
        .expect("retire claim")
        .expect("retirement");
    work.commit().await.expect("retirement claim commit");
    let mut refusal = database.begin().await.expect("late refusal");
    assert!(
        settle_submit_refusal(&mut refusal, &outbox, &submit_job, 409, "turn_conflict")
            .await
            .expect("late submit settlement")
    );
    let retire_database = database.clone();
    let mut retiring = tokio::spawn(async move {
        let mut work = retire_database.begin().await.expect("retire transaction");
        let settled = acknowledge_retirement(&mut work, &retire_job, &[])
            .await
            .expect("retire settlement");
        work.commit().await.expect("retire commit");
        settled
    });
    assert!(
        tokio::time::timeout(std::time::Duration::from_millis(100), &mut retiring)
            .await
            .is_err(),
        "retirement must wait for the submit settlement's attempt lock"
    );
    refusal.commit().await.expect("refusal commit");
    assert!(retiring.await.expect("retirement join"));
}

/// Accepted reports keep unrepresentable JSON on the wire and safe query projections.
#[tokio::test]
async fn accepted_intake_unrepresentable_json_keeps_raw_reports_and_safe_views() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 3)
        .await
        .expect("database");
    migrate(&database).await.expect("tasks schema");
    migrate_events(&database).await.expect("event schema");
    let outbox = Outbox::new(
        1024 * 1024,
        vec![
            "task.>".to_owned(),
            "request.>".to_owned(),
            "turn.>".to_owned(),
        ],
    );
    for (index, decision, risk, questions, concerns, expected) in [
        (
            0,
            "clarify",
            "low",
            vec!["Need\0answer"],
            vec![],
            Stage::Clarify,
        ),
        (1, "escalate", "high", vec![], vec![], Stage::Escalated),
        (
            2,
            "proceed",
            "low",
            vec![],
            vec!["Check\0concern"],
            Stage::Plan,
        ),
        (3, "proceed", "low", vec![], vec![], Stage::Plan),
    ] {
        let mut start = request(100 + index, RequestKind::Start);
        start.issue.number += index;
        let mut work = database.begin().await.expect("start");
        record_request(&mut work, &start)
            .await
            .expect("record start");
        apply_request(
            &mut work,
            &outbox,
            start.id,
            start.issue,
            Authority {
                writer: true,
                issue_open: true,
            },
        )
        .await
        .expect("apply start");
        work.commit().await.expect("start commit");
        let started = task(&database, start.issue)
            .await
            .expect("task")
            .expect("started");
        let mut source = snapshot(PinnedInputs {
            config_sha: "a".repeat(40),
            base_sha: "b".repeat(40),
            max_model_calls: 3,
            max_fix_rounds: 0,
            max_plan_revisions: 0,
        });
        source.charter = r#"{"version":1,"goals":{"G-1":"Goal"}}"#.to_owned();
        let mut work = database.begin().await.expect("initialize");
        initialize_task(&mut work, &outbox, start.issue, started.revision, &source)
            .await
            .expect("initialize");
        work.commit().await.expect("initialize commit");
        let current = task(&database, start.issue)
            .await
            .expect("task")
            .expect("intake");
        let turn = Uuid::now_v7();
        let mut work = database.begin().await.expect("admit");
        assert_eq!(
            admit_intake(
                &mut work,
                &outbox,
                start.issue,
                &AttemptSpec {
                    id: turn,
                    task: current.id,
                    expected_revision: current.revision,
                    staffing_group: format!("safe-nul-{index}"),
                    fork: "acme/fork".to_owned(),
                    config_sha: "a".repeat(40),
                    submit_body: b"body".to_vec(),
                    prompt_body: None,
                    deadline: DateTime::<Utc>::from_timestamp(1000, 0).expect("deadline"),
                    unbounded_running: false,
                },
            )
            .await
            .expect("admit"),
            Admission::Admitted
        );
        work.commit().await.expect("admit commit");
        let giant: Value = serde_json::from_str("1e999999").expect("large JSON number");
        let rationale = if index == 3 {
            "model text"
        } else {
            "model\0text"
        };
        let fit = if index == 3 {
            "Fits scope"
        } else {
            "Fits\0scope"
        };
        let observation: TurnObservation = serde_json::from_value(json!({
            "turn":turn,"status":"done","created":1.0,"finished":2.0,
            "result":{"outcome":"ok","response":{
                "decision":decision,"rationale":rationale,"risk":risk,
                "questions":questions,"concerns":concerns,
                "scope":{"goal_ids":["G-1"],"fit":fit,"conflicts":[]},
                "classification":{"n":giant}
            }}
        }))
        .expect("observation");
        let mut work = database.begin().await.expect("result");
        record_observation(&mut work, &outbox, &observation, None)
            .await
            .expect("accepted NUL report");
        work.commit().await.expect("result commit");
        let settled = task(&database, start.issue)
            .await
            .expect("task")
            .expect("settled");
        assert_eq!(settled.stage, expected);
        if expected != Stage::Plan && index != 3 {
            assert!(
                settled
                    .stage_reason
                    .as_deref()
                    .unwrap_or_default()
                    .contains("\\u0000")
            );
        }
        let (raw, projected): (Vec<u8>, Value) = sqlx::query_as(
            "SELECT raw_report_bytes,raw_report FROM tasks.intake_reports WHERE attempt_id=$1",
        )
        .bind(turn)
        .fetch_one(database.pool())
        .await
        .expect("stored report");
        assert_eq!(
            serde_json::from_slice::<Value>(&raw).expect("raw")["rationale"],
            rationale
        );
        assert_eq!(
            projected["rationale"],
            if index == 3 {
                "model text"
            } else {
                "model\\u0000text"
            }
        );
        assert_eq!(
            serde_json::from_slice::<Value>(&raw).expect("raw report")["classification"]["n"],
            giant
        );
        assert!(projected["classification"]["n"].is_string());
        let wire: Vec<u8> =
            sqlx::query_scalar("SELECT wire_bytes FROM events.outbox WHERE type=$1")
                .bind(format!("task.{}.report", settled.id.0))
                .fetch_one(database.pool())
                .await
                .expect("report event");
        let report = decode::<TaskReport>(&wire).expect("report contract");
        assert_eq!(report.data.body["rationale"], rationale);
        assert_eq!(report.data.body["classification"]["n"], giant);
        let message: Option<Value> =
            sqlx::query_scalar("SELECT message FROM events.outbox WHERE type=$1")
                .bind(format!("task.{}.report", settled.id.0))
                .fetch_one(database.pool())
                .await
                .expect("wire-only report projection");
        assert!(message.is_none());
        let notice: String = sqlx::query_scalar(
            "SELECT body FROM tasks.publication_comments WHERE task_id=$1 AND kind='notice' ORDER BY updated_at DESC LIMIT 1",
        )
        .bind(settled.id.0)
        .fetch_one(database.pool())
        .await
        .expect("notice");
        if index != 3 {
            assert!(notice.contains("\\u0000"));
        }
    }
}
