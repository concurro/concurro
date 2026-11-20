//! Request ordering and replay against a real PostgreSQL transaction boundary.

use std::collections::BTreeMap;
use std::io::Write;

use base64::{Engine, engine::general_purpose::STANDARD};
use chrono::{DateTime, Utc};
use concurro_contracts::{TaskReport, TurnFinished, decode};
use concurro_db::Database;
use concurro_events_postgres::{Outbox, migrate as migrate_events};
use concurro_exchange_v1::TurnObservation;
use concurro_tasks_core::{
    Actor, Admission, AttemptSpec, Authority, InitializationSnapshot, IssueKey, LaterCommand,
    PinnedInputs, Request, RequestKind, RequestStatus, Requirements, SourceOrder, Stage,
};
use concurro_tasks_postgres::{
    EffectKind, RetainedEligibility, acknowledge_retirement, admit_intake, apply_request,
    cancel_attempt, claim_dispatch, claim_effect, due_attempt_deadlines, expire_attempt,
    initialize_task, latest_intake_correction, list_tasks, migrate, next_pending, pending_effects,
    record_observation, record_poll_failure, record_request, record_submit_failure,
    recover_retirements, retained_eligibility, runner_session, settle_submit,
    settle_submit_refusal, task, task_detail,
};
use concurro_testkit::PostgresContainer;
use flate2::{Compression, write::GzEncoder};
use serde_json::{Value, json};
use tar::{Builder, Header};
use uuid::Uuid;

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
        Stage::Initializing
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
        (RequestStatus::Refused, Some("not_supported"))
    );
    unsupported_work.commit().await.expect("unsupported commit");
    let argument: sqlx::types::Json<serde_json::Value> = sqlx::query_scalar(
        "SELECT message->'data'->'argument' FROM events.outbox WHERE type='request.tasks.refused' ORDER BY sequence DESC LIMIT 1",
    )
    .fetch_one(restarted.pool())
    .await
    .expect("structured argument");
    assert_eq!(argument.0, serde_json::json!({"plan_revisions": 3}));
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
