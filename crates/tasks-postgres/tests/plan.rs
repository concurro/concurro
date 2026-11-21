//! Planner admission and report settlement share the intake transaction boundary.

use std::collections::BTreeMap;

use chrono::{Duration, Utc};
use concurro_contracts::{TaskId, TaskSnapshot, TurnFinished, decode};
use concurro_db::Database;
use concurro_events_postgres::{Outbox, migrate as migrate_events};
use concurro_exchange_v1::{TurnObservation, TurnSubmission};
use concurro_tasks_core::{
    Actor, Admission, AttemptSpec, Authority, DesignIssue, DesignIssueKind, DesignReviewer,
    InitializationSnapshot, IssueKey, LaterCommand, PinnedInputs, Request, RequestKind,
    Requirements, SourceOrder, Stage, TaskState, design_binding, design_roster, plan_identity,
    python_digest, validate_design_review,
};
use concurro_tasks_postgres::{
    CommandEvidence, ImplementationAssessment, admit_design_batch, admit_design_correction,
    admit_implementation, admit_plan, apply_design_result, apply_implementation_result,
    apply_request, apply_request_with_evidence, continue_planner_answer, design_correction,
    finish_unapplied_result, git_publication_input, implementation_result,
    latest_implementation_correction, latest_plan_correction, latest_plan_revision, migrate,
    pending_effects, record_observation, record_request, reserve_git_publication,
    save_git_publication, task, task_detail,
};
use concurro_testkit::PostgresContainer;
use serde_json::{Value, json};
use sqlx::types::Json;
use uuid::Uuid;

/// A valid planner result advances once and binds Python-compatible plan identity.
#[tokio::test]
async fn planner_result_is_atomic_with_plan_report_and_task_snapshot() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 3)
        .await
        .expect("database");
    migrate(&database).await.expect("task schema");
    migrate_events(&database).await.expect("event schema");
    let outbox = Outbox::new(
        1024 * 1024,
        vec![
            "task.>".to_owned(),
            "turn.>".to_owned(),
            "request.>".to_owned(),
        ],
    );
    let issue = IssueKey {
        repository_id: 8,
        number: 3,
    };
    let task_id = TaskId(Uuid::now_v7());
    let turn = Uuid::now_v7();
    let pins = PinnedInputs {
        config_sha: "a".repeat(40),
        base_sha: "b".repeat(40),
        max_model_calls: 12,
        max_fix_rounds: 2,
        max_plan_revisions: 1,
    };
    let state = TaskState {
        id: task_id,
        issue,
        author: Actor {
            id: 7,
            login: "writer".to_owned(),
        },
        stage: Stage::Plan,
        revision: 1,
        stage_reason: None,
        retry_stage: None,
        pins: Some(pins.clone()),
        used_model_calls: 1,
        current_attempt: None,
    };
    let requirements = Requirements {
        title: "Task".to_owned(),
        body: Some("Description".to_owned()),
        discussion: vec![],
    };
    let snapshot = InitializationSnapshot {
        pins: pins.clone(),
        configuration: json!({
            "planner":"codex","implementer":"codex","qc":"claude",
            "reviewers":["codex","claude"],"qc_model":"",
            "models":{"codex":"gpt","claude":"sonnet"},
            "max_review_repairs":1,
            "max_design_reopens":1,
            "policy_documents": ["README.md"],
            "paths": {
                "protected": [], "protected_extensions": [], "protected_names": [],
                "protected_name_prefixes": [], "protected_prefixes": [".concurro/"],
                "protected_files": [], "generated": []
            }
        }),
        requirements: requirements.clone(),
        charter: r#"{"goals":{"G1":"Correctness"}}"#.to_owned(),
        instruction_texts: BTreeMap::new(),
        policy_documents: BTreeMap::new(),
        base_files: vec!["src/lib.rs".to_owned()],
        bundle_id: "bundle".to_owned(),
    };
    sqlx::query("INSERT INTO tasks.issue_controls (repository_id,issue_number) VALUES (8,3)")
        .execute(database.pool())
        .await
        .expect("issue");
    sqlx::query(
        "INSERT INTO tasks.task_state (repository_id,issue_number,task_id,revision,state) \
         VALUES (8,3,$1,1,$2)",
    )
    .bind(task_id.0)
    .bind(Json(&state))
    .execute(database.pool())
    .await
    .expect("task");
    sqlx::query(
        "INSERT INTO tasks.initialization_inputs (task_id,task_revision,snapshot) VALUES ($1,1,$2)",
    )
    .bind(task_id.0)
    .bind(Json(&snapshot))
    .execute(database.pool())
    .await
    .expect("snapshot");
    let failed_turn = Uuid::now_v7();
    let failed_submission = TurnSubmission {
        turn: failed_turn,
        expected: json!({}),
        models: vec!["gpt".to_owned()],
        budget: 1,
        lease: 1,
        payload: json!({"request":{"session":{}}}),
        pin: None,
        binding: json!({}),
    };
    let mut work = database.begin().await.expect("admit failed planner");
    assert_eq!(
        admit_plan(
            &mut work,
            &outbox,
            issue,
            &AttemptSpec {
                id: failed_turn,
                task: task_id,
                expected_revision: 1,
                staffing_group: "planner-failed".to_owned(),
                fork: "acme/demo".to_owned(),
                config_sha: "a".repeat(40),
                submit_body: serde_json::to_vec(&failed_submission).expect("submission"),
                prompt_body: None,
                deadline: Utc::now() + Duration::hours(1),
                unbounded_running: false,
            }
        )
        .await
        .expect("failed planner admission"),
        Admission::Admitted
    );
    work.commit()
        .await
        .expect("failed planner admission commit");
    let failure: TurnObservation = serde_json::from_value(json!({
        "turn":failed_turn,"status":"done","created":1.0,"finished":2.0,
        "result":{"outcome":"failed","response":null}
    }))
    .expect("failed planner result");
    let mut work = database.begin().await.expect("failed planner settlement");
    record_observation(&mut work, &outbox, &failure, None)
        .await
        .expect("retry planner session");
    work.commit().await.expect("failure commit");
    assert_eq!(
        task(&database, issue)
            .await
            .expect("task")
            .expect("retrying planner")
            .stage,
        Stage::Plan
    );
    let (epoch, failures): (i32, i32) = sqlx::query_as(
        "SELECT planner_session_epoch,planner_failure_count \
         FROM tasks.initialization_inputs WHERE task_id=$1",
    )
    .bind(task_id.0)
    .fetch_one(database.pool())
    .await
    .expect("planner session rotation");
    assert_eq!((epoch, failures), (1, 1));
    let second_failure_turn = Uuid::now_v7();
    let retrying = task(&database, issue)
        .await
        .expect("task")
        .expect("retrying planner");
    let retry_submission = TurnSubmission {
        turn: second_failure_turn,
        expected: json!({}),
        models: vec!["gpt".to_owned()],
        budget: 1,
        lease: 1,
        payload: json!({"request":{"session":{
            "scope":"planner-retry-epoch-1","turn":0,"role":"plan:codex:gpt"
        }}}),
        pin: None,
        binding: json!({}),
    };
    let mut exhausted = database.begin().await.expect("second failure probe");
    assert_eq!(
        admit_plan(
            &mut exhausted,
            &outbox,
            issue,
            &AttemptSpec {
                id: second_failure_turn,
                task: task_id,
                expected_revision: retrying.revision,
                staffing_group: "planner-second-failure".to_owned(),
                fork: "acme/demo".to_owned(),
                config_sha: "a".repeat(40),
                submit_body: serde_json::to_vec(&retry_submission).expect("retry submission"),
                prompt_body: Some(
                    serde_json::to_vec(&json!({"instructions":"retry","response_schema":{}}))
                        .expect("retry prompt"),
                ),
                deadline: Utc::now() + Duration::hours(1),
                unbounded_running: false,
            }
        )
        .await
        .expect("retained planner admission"),
        Admission::Admitted
    );
    let second_failure: TurnObservation = serde_json::from_value(json!({
        "turn":second_failure_turn,"status":"done","created":1.0,"finished":2.0,
        "result":{"outcome":"failed","response":null}
    }))
    .expect("second planner failure");
    record_observation(&mut exhausted, &outbox, &second_failure, None)
        .await
        .expect("exhaust planner retry");
    let (blocked, epoch, failures): (Json<TaskState>, i32, i32) = sqlx::query_as(
        "SELECT t.state,i.planner_session_epoch,i.planner_failure_count \
         FROM tasks.task_state t JOIN tasks.initialization_inputs i ON i.task_id=t.task_id \
         WHERE t.task_id=$1",
    )
    .bind(task_id.0)
    .fetch_one(exhausted.connection())
    .await
    .expect("planner retry exhaustion");
    assert_eq!(blocked.0.stage, Stage::Blocked);
    assert_eq!((epoch, failures), (2, 2));
    exhausted.rollback().await.expect("restore planner retry");
    for correction in 1..=2 {
        let current = task(&database, issue)
            .await
            .expect("task")
            .expect("planning");
        let invalid_turn = Uuid::now_v7();
        let mut work = database.begin().await.expect("admit invalid plan");
        assert_eq!(
            admit_plan(
                &mut work,
                &outbox,
                issue,
                &AttemptSpec {
                    id: invalid_turn,
                    task: task_id,
                    expected_revision: current.revision,
                    staffing_group: format!("planner-invalid-{correction}"),
                    fork: "acme/demo".to_owned(),
                    config_sha: "a".repeat(40),
                    submit_body: b"body".to_vec(),
                    prompt_body: None,
                    deadline: Utc::now() + Duration::hours(1),
                    unbounded_running: false,
                }
            )
            .await
            .expect("invalid plan admission"),
            Admission::Admitted
        );
        work.commit().await.expect("invalid admission commit");
        let observation: TurnObservation = serde_json::from_value(json!({
            "turn":invalid_turn,"status":"done","created":1.0,"finished":2.0,
            "result":{"outcome":"ok","response":{"summary":"Incomplete"}}
        }))
        .expect("invalid planner response");
        let mut work = database.begin().await.expect("invalid result");
        record_observation(&mut work, &outbox, &observation, None)
            .await
            .expect("reject invalid plan");
        work.commit().await.expect("invalid result commit");
        let current = task(&database, issue)
            .await
            .expect("task")
            .expect("repair plan");
        assert_eq!(current.stage, Stage::Plan);
        assert_eq!(
            latest_plan_correction(&database, task_id.0)
                .await
                .expect("correction")
                .expect("rejected plan")
                .attempt,
            correction
        );
    }
    let planning = task(&database, issue)
        .await
        .expect("task")
        .expect("planning");
    let spec = AttemptSpec {
        id: turn,
        task: task_id,
        expected_revision: planning.revision,
        staffing_group: "planner".to_owned(),
        fork: "acme/demo".to_owned(),
        config_sha: pins.config_sha,
        submit_body: b"body".to_vec(),
        prompt_body: None,
        deadline: Utc::now() + Duration::hours(1),
        unbounded_running: false,
    };
    let mut work = database.begin().await.expect("admit");
    assert_eq!(
        admit_plan(&mut work, &outbox, issue, &spec)
            .await
            .expect("planner admission"),
        Admission::Admitted
    );
    work.commit().await.expect("admit commit");
    let plan = json!({
        "summary":"Implement the change","questions":[],"files":["src/lib.rs"],
        "steps":["Edit code"],"tests":["Run suite"],"responses":[],
        "publication":{"changes":["Change behavior"],"review_focus":["Correctness"]},
        "scope":{"paths":["src/"]}
    });
    let observation: TurnObservation = serde_json::from_value(json!({
        "turn":turn,"status":"done","created":1.0,"finished":2.0,
        "result":{"outcome":"ok","response":plan}
    }))
    .expect("planner observation");
    let mut work = database.begin().await.expect("result");
    record_observation(&mut work, &outbox, &observation, None)
        .await
        .expect("settle plan");
    work.commit().await.expect("result commit");
    let settled = task(&database, issue)
        .await
        .expect("task")
        .expect("settled");
    assert_eq!(settled.stage, Stage::PlanQc);
    assert_eq!(settled.used_model_calls, 5);
    let (stored_id, raw): (String, Vec<u8>) = sqlx::query_as(
        "SELECT plan_id,raw_report_bytes FROM tasks.plan_reports WHERE attempt_id=$1 AND accepted",
    )
    .bind(turn)
    .fetch_one(database.pool())
    .await
    .expect("accepted plan");
    let expected_id = python_digest(&json!({
        "plan":plan,"base":"b".repeat(40),
        "requirements":{"title":requirements.title,"body":requirements.body,"discussion":[]}
    }))
    .expect("plan identity");
    assert_eq!(stored_id, expected_id);
    let (batch_plan, phase, generation, seats): (String, String, i64, i64) = sqlx::query_as(
        "SELECT b.plan_id,b.phase,b.generation,count(s.reviewer_id) \
         FROM tasks.plan_review_batches b JOIN tasks.plan_review_seats s ON s.batch_id=b.id \
         WHERE b.task_id=$1 GROUP BY b.id",
    )
    .bind(task_id.0)
    .fetch_one(database.pool())
    .await
    .expect("pinned review batch");
    assert_eq!(batch_plan, expected_id);
    assert_eq!(phase, "cooperative");
    assert_eq!(generation, 0);
    assert_eq!(seats, 2);
    assert_eq!(
        serde_json::from_slice::<Value>(&raw).expect("raw plan"),
        plan
    );
    let wire: Vec<u8> = sqlx::query_scalar(
        "SELECT wire_bytes FROM events.outbox WHERE type=$1 ORDER BY sequence DESC LIMIT 1",
    )
    .bind(format!("task.{}.state", task_id.0))
    .fetch_one(database.pool())
    .await
    .expect("task snapshot");
    let wire_snapshot = decode::<TaskSnapshot>(&wire).expect("task contract");
    assert_eq!(wire_snapshot.data.plan.expect("plan")["id"], stored_id);
    let finished: Vec<u8> =
        sqlx::query_scalar("SELECT wire_bytes FROM events.outbox WHERE type=$1")
            .bind(format!("turn.{turn}.finished"))
            .fetch_one(database.pool())
            .await
            .expect("finished fact");
    assert_eq!(
        decode::<TurnFinished>(&finished)
            .expect("finished contract")
            .data
            .outcome,
        "succeeded"
    );
    let (batch_id, binding, Json(roster)): (Uuid, String, Json<Vec<DesignReviewer>>) =
        sqlx::query_as("SELECT id,binding,roster FROM tasks.plan_review_batches WHERE task_id=$1")
            .bind(task_id.0)
            .fetch_one(database.pool())
            .await
            .expect("review batch");
    let specs = roster
        .iter()
        .map(|reviewer| {
            let turn = Uuid::now_v7();
            let submission = TurnSubmission {
                turn,
                expected: json!({}),
                models: vec![reviewer.model.clone()],
                budget: 1,
                lease: 1,
                payload: json!({"request":{"context":{
                    "design_binding":binding,
                    "reviewer_identity":{"id":reviewer.id}
                }}}),
                pin: None,
                binding: json!({}),
            };
            (
                reviewer.id.clone(),
                AttemptSpec {
                    id: turn,
                    task: task_id,
                    expected_revision: settled.revision,
                    staffing_group: reviewer.id.clone(),
                    fork: "acme/demo".to_owned(),
                    config_sha: "a".repeat(40),
                    submit_body: serde_json::to_vec(&submission).expect("submission"),
                    prompt_body: None,
                    deadline: Utc::now() + Duration::hours(1),
                    unbounded_running: false,
                },
            )
        })
        .collect::<Vec<_>>();
    let mut work = database.begin().await.expect("review admission");
    assert_eq!(
        admit_design_batch(
            &mut work,
            &outbox,
            issue,
            batch_id,
            settled.revision,
            &specs
        )
        .await
        .expect("review roster"),
        Admission::Admitted
    );
    work.commit().await.expect("review commit");
    let admitted = task(&database, issue)
        .await
        .expect("task")
        .expect("admitted");
    assert_eq!(admitted.used_model_calls, 7);
    let seats: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM tasks.plan_review_seats WHERE batch_id=$1 AND status='admitted'",
    )
    .bind(batch_id)
    .fetch_one(database.pool())
    .await
    .expect("admitted seats");
    assert_eq!(seats, 2);
    let review = json!({
        "decision":"approve","rationale":"The plan covers the request.\0", "risk":"low",
        "questions":[],"concerns":[],
        "scope":{"goal_ids":["G1"],"fit":"Preserves correctness.","conflicts":[]},
        "sources":[{"id":"S1","path":"src/lib.rs","line":1,"quote":"fn example()"}],
        "assessments":[
            {"criterion":"R1","status":"satisfied","sources":["S1"],
             "reason":"The title is covered.","counterexample":"None found."},
            {"criterion":"R2","status":"satisfied","sources":["S1"],
             "reason":"The body is covered.","counterexample":"None found."}
        ],
        "objection_updates":[]
    });
    let files = BTreeMap::from([("src/lib.rs".to_owned(), "fn example() {}\n".to_owned())]);
    let invalid: TurnObservation = serde_json::from_value(json!({
        "turn":specs[0].1.id,"status":"done","created":1.0,"finished":2.0,
        "result":{"outcome":"ok","response":{"decision":"approve"}}
    }))
    .expect("invalid review observation");
    let mut work = database.begin().await.expect("retain invalid review");
    record_observation(&mut work, &outbox, &invalid, None)
        .await
        .expect("retain invalid review");
    work.commit().await.expect("retain invalid commit");
    let premature: i64 = sqlx::query_scalar("SELECT count(*) FROM events.outbox WHERE type=$1")
        .bind(format!("turn.{}.finished", specs[0].1.id))
        .fetch_one(database.pool())
        .await
        .expect("deferred review fact");
    assert_eq!(premature, 0);
    let mut obsolete = database.begin().await.expect("superseded review probe");
    sqlx::query("UPDATE tasks.plan_reports SET current=false WHERE task_id=$1")
        .bind(task_id.0)
        .execute(obsolete.connection())
        .await
        .expect("supersede plan");
    assert!(
        !apply_design_result(
            &mut obsolete,
            &outbox,
            specs[0].1.id,
            Err("stale review".to_owned()),
        )
        .await
        .expect("ignore superseded review")
    );
    assert!(
        finish_unapplied_result(&mut obsolete, &outbox, specs[0].1.id)
            .await
            .expect("finish obsolete review")
    );
    let obsolete_finished: Vec<u8> =
        sqlx::query_scalar("SELECT wire_bytes FROM events.outbox WHERE type=$1")
            .bind(format!("turn.{}.finished", specs[0].1.id))
            .fetch_one(obsolete.connection())
            .await
            .expect("obsolete review fact");
    let obsolete_finished = decode::<TurnFinished>(&obsolete_finished).expect("finished contract");
    assert_eq!(obsolete_finished.data.outcome, "unknown");
    assert_eq!(
        obsolete_finished.data.reason.as_deref(),
        Some("superseded_before_validation")
    );
    obsolete.rollback().await.expect("restore review");
    let mut work = database.begin().await.expect("reject invalid review");
    assert!(
        apply_design_result(
            &mut work,
            &outbox,
            specs[0].1.id,
            validate_design_review(
                json!({"decision":"approve"}),
                &requirements,
                &snapshot.charter,
                &files,
                &[],
            ),
        )
        .await
        .expect("reject review")
    );
    work.commit().await.expect("reject commit");
    let invalid_finished: Vec<u8> =
        sqlx::query_scalar("SELECT wire_bytes FROM events.outbox WHERE type=$1")
            .bind(format!("turn.{}.finished", specs[0].1.id))
            .fetch_one(database.pool())
            .await
            .expect("invalid review fact");
    let invalid_finished = decode::<TurnFinished>(&invalid_finished).expect("finished contract");
    assert_eq!(invalid_finished.data.outcome, "failed");
    assert_eq!(
        invalid_finished.data.reason.as_deref(),
        Some("invalid_output")
    );
    let correction = design_correction(&database, batch_id, &specs[0].0, 1)
        .await
        .expect("correction read")
        .expect("queued correction");
    assert_eq!(correction.reviewer.id, specs[0].0);
    assert_eq!(correction.report, json!({"decision":"approve"}));
    let current = task(&database, issue)
        .await
        .expect("task")
        .expect("correcting");
    let correction_turn = Uuid::now_v7();
    let correction_submission = TurnSubmission {
        turn: correction_turn,
        expected: json!({}),
        models: vec![correction.reviewer.model.clone()],
        budget: 1,
        lease: 1,
        payload: json!({"request":{"context":{
            "design_binding":binding,
            "reviewer_identity":{"id":correction.reviewer.id},
            "review_validation_feedback":{"detail":correction.reason}
        }}}),
        pin: None,
        binding: json!({}),
    };
    let correction_spec = AttemptSpec {
        id: correction_turn,
        task: task_id,
        expected_revision: current.revision,
        staffing_group: "correction".to_owned(),
        fork: "acme/demo".to_owned(),
        config_sha: "a".repeat(40),
        submit_body: serde_json::to_vec(&correction_submission).expect("submission"),
        prompt_body: None,
        deadline: Utc::now() + Duration::hours(1),
        unbounded_running: false,
    };
    let mut work = database.begin().await.expect("correction admission");
    assert_eq!(
        admit_design_correction(
            &mut work,
            &outbox,
            issue,
            batch_id,
            &specs[0].0,
            1,
            &correction_spec,
        )
        .await
        .expect("correction admission"),
        Admission::Admitted
    );
    work.commit().await.expect("correction commit");
    drop(database);
    let database = Database::connect(postgres.url(), 3)
        .await
        .expect("restarted storage connection");
    let mut work = database.begin().await.expect("late original vote");
    assert!(
        !apply_design_result(
            &mut work,
            &outbox,
            specs[0].1.id,
            validate_design_review(
                review.clone(),
                &requirements,
                &snapshot.charter,
                &files,
                &[],
            ),
        )
        .await
        .expect("fence original vote")
    );
    work.commit().await.expect("late vote commit");
    for spec in [&correction_spec, &specs[1].1] {
        let observation: TurnObservation = serde_json::from_value(json!({
            "turn":spec.id,"status":"done","created":1.0,"finished":2.0,
            "result":{"outcome":"ok","response":review}
        }))
        .expect("review observation");
        let mut work = database.begin().await.expect("retain review");
        record_observation(&mut work, &outbox, &observation, None)
            .await
            .expect("retain independent result");
        work.commit().await.expect("retain commit");
        let validated = validate_design_review(
            review.clone(),
            &requirements,
            &snapshot.charter,
            &files,
            &[],
        );
        let mut work = database.begin().await.expect("apply review");
        assert!(
            apply_design_result(&mut work, &outbox, spec.id, validated)
                .await
                .expect("apply vote")
        );
        work.commit().await.expect("vote commit");
    }
    let reviewed = task(&database, issue)
        .await
        .expect("task")
        .expect("reviewed");
    assert_eq!(reviewed.stage, Stage::Planned);
    assert_eq!(reviewed.used_model_calls, 8);
    let fresh_batches: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM tasks.plan_review_batches WHERE task_id=$1 AND phase='fresh'",
    )
    .bind(task_id.0)
    .fetch_one(database.pool())
    .await
    .expect("fresh batch count");
    assert_eq!(fresh_batches, 0);
    let mut changed_snapshot = snapshot.clone();
    changed_snapshot.pins.base_sha = "c".repeat(40);
    sqlx::query("UPDATE tasks.initialization_inputs SET snapshot=$2 WHERE task_id=$1")
        .bind(task_id.0)
        .bind(Json(&changed_snapshot))
        .execute(database.pool())
        .await
        .expect("changed source");
    let stale_request = Request {
        id: Uuid::now_v7(),
        channel: "comment".to_owned(),
        reply: "issue_comment:899".to_owned(),
        comment_id: Some(899),
        issue,
        issue_author: state.author.clone(),
        actor: state.author.clone(),
        kind: RequestKind::Later(LaterCommand::Approve {
            plan_id: stored_id.clone(),
        }),
        order: SourceOrder {
            at: Utc::now(),
            source_id: 899,
            source: "approve:899".to_owned(),
            index: 0,
        },
    };
    let mut work = database.begin().await.expect("stale approval request");
    record_request(&mut work, &stale_request)
        .await
        .expect("record stale approval");
    work.commit().await.expect("stale request commit");
    let mut work = database.begin().await.expect("stale approval decision");
    let (_, stale) = apply_request(
        &mut work,
        &outbox,
        stale_request.id,
        issue,
        Authority {
            writer: true,
            issue_open: true,
        },
    )
    .await
    .expect("stale approval decision")
    .expect("stale decision");
    assert_eq!(stale.status, concurro_tasks_core::RequestStatus::Refused);
    work.commit().await.expect("stale decision commit");
    assert_eq!(
        task(&database, issue)
            .await
            .expect("task")
            .expect("planned")
            .stage,
        Stage::Planned
    );
    sqlx::query("UPDATE tasks.initialization_inputs SET snapshot=$2 WHERE task_id=$1")
        .bind(task_id.0)
        .bind(Json(&snapshot))
        .execute(database.pool())
        .await
        .expect("restore pinned source");
    let approver = Actor {
        id: 9,
        login: "second-writer".to_owned(),
    };
    let request = Request {
        id: Uuid::now_v7(),
        channel: "comment".to_owned(),
        reply: "issue_comment:900".to_owned(),
        comment_id: Some(900),
        issue,
        issue_author: state.author,
        actor: approver.clone(),
        kind: RequestKind::Later(LaterCommand::Approve {
            plan_id: stored_id.clone(),
        }),
        order: SourceOrder {
            at: Utc::now(),
            source_id: 900,
            source: "approve:900".to_owned(),
            index: 0,
        },
    };
    let mut work = database.begin().await.expect("approve request");
    record_request(&mut work, &request)
        .await
        .expect("record approve");
    work.commit().await.expect("request commit");
    let mut work = database.begin().await.expect("approve decision");
    apply_request(
        &mut work,
        &outbox,
        request.id,
        issue,
        Authority {
            writer: true,
            issue_open: true,
        },
    )
    .await
    .expect("approve decision");
    work.commit().await.expect("approve commit");
    let approved = task(&database, issue)
        .await
        .expect("task")
        .expect("approved");
    assert_eq!(approved.stage, Stage::Implement);
    let (approved_plan, Json(approved_actor)): (String, Json<Actor>) =
        sqlx::query_as("SELECT plan_id,actor FROM tasks.plan_approvals WHERE request_id=$1")
            .bind(request.id)
            .fetch_one(database.pool())
            .await
            .expect("approval binding");
    assert_eq!(approved_plan, stored_id);
    assert_eq!(approved_actor, approver);
    let detail = task_detail(&database, issue)
        .await
        .expect("operator detail")
        .expect("task detail");
    assert_eq!(detail["plans"].as_array().expect("plan history").len(), 3);
    assert_eq!(
        detail["design"].as_array().expect("review batches").len(),
        1
    );
    assert_eq!(detail["approvals"][0]["actor"]["id"], 9);
    assert!(
        detail["design"][0]["seats"]
            .as_array()
            .expect("cooperative seats")
            .iter()
            .any(|seat| seat["correction_count"] == 1)
    );
    let mut skill_probe = database.begin().await.expect("skill-only retry probe");
    let mut paused = approved.clone();
    paused.stage = Stage::Blocked;
    paused.retry_stage = Some(Stage::Implement);
    paused.stage_reason = Some("Implementation instructions changed".to_owned());
    sqlx::query("UPDATE tasks.task_state SET state=$2 WHERE task_id=$1")
        .bind(task_id.0)
        .bind(Json(&paused))
        .execute(skill_probe.connection())
        .await
        .expect("pause implementer");
    let skill_request = Request {
        id: Uuid::now_v7(),
        channel: "comment".to_owned(),
        reply: "issue_comment:901".to_owned(),
        comment_id: Some(901),
        issue,
        issue_author: approver.clone(),
        actor: approver.clone(),
        kind: RequestKind::Later(LaterCommand::Retry {
            plan_revisions: None,
        }),
        order: SourceOrder {
            at: Utc::now(),
            source_id: 901,
            source: "skill-retry:901".to_owned(),
            index: 0,
        },
    };
    record_request(&mut skill_probe, &skill_request)
        .await
        .expect("record skill retry");
    let mut updated_snapshot = snapshot.clone();
    updated_snapshot.pins.config_sha = "f".repeat(40);
    updated_snapshot.bundle_id = "updated-implementer-only-bundle".to_owned();
    let (_, skill_decision) = apply_request_with_evidence(
        &mut skill_probe,
        &outbox,
        skill_request.id,
        issue,
        Authority {
            writer: true,
            issue_open: true,
        },
        Some(&CommandEvidence {
            snapshot: updated_snapshot.clone(),
            pr_unchanged: true,
            implementer_skill_update: Some(("1".repeat(64), "2".repeat(64))),
        }),
    )
    .await
    .expect("skill retry decision")
    .expect("pending skill retry");
    assert_eq!(
        skill_decision.status,
        concurro_tasks_core::RequestStatus::Applied
    );
    assert_eq!(
        skill_decision.task.expect("retried task").stage,
        Stage::Implement
    );
    let (Json(adopted), epoch): (Json<InitializationSnapshot>, i32) = sqlx::query_as(
        "SELECT snapshot,implementation_session_epoch FROM tasks.initialization_inputs WHERE task_id=$1",
    )
    .bind(task_id.0)
    .fetch_one(skill_probe.connection())
    .await
    .expect("adopted skill snapshot");
    assert_eq!(adopted.bundle_id, updated_snapshot.bundle_id);
    assert_eq!(adopted.pins, snapshot.pins);
    assert_eq!(epoch, 1);
    let receipt: (String, String, String) = sqlx::query_as(
        "SELECT kind,old_skill_sha,new_skill_sha FROM tasks.task_resumes WHERE request_id=$1",
    )
    .bind(skill_request.id)
    .fetch_one(skill_probe.connection())
    .await
    .expect("skill receipt");
    assert_eq!(
        receipt,
        (
            "implementer_skill_retry".to_owned(),
            "1".repeat(64),
            "2".repeat(64)
        )
    );
    skill_probe.rollback().await.expect("isolate skill retry");
    let develop_turn = Uuid::now_v7();
    let mut work = database.begin().await.expect("admit implementer");
    assert_eq!(
        admit_implementation(
            &mut work,
            &outbox,
            issue,
            &AttemptSpec {
                id: develop_turn,
                task: task_id,
                expected_revision: approved.revision,
                staffing_group: "implementer".to_owned(),
                fork: "acme/demo".to_owned(),
                config_sha: "a".repeat(40),
                submit_body: b"body".to_vec(),
                prompt_body: None,
                deadline: Utc::now() + Duration::hours(1),
                unbounded_running: false,
            }
        )
        .await
        .expect("implementation admission"),
        Admission::Admitted
    );
    work.commit().await.expect("admit commit");
    let result: TurnObservation = serde_json::from_value(json!({
        "turn":develop_turn,"status":"done","created":1.0,"finished":2.0,
        "result":{"outcome":"ok","response":{
            "summary":"Implement change","validation":"Tests pass",
            "design_issue":null,"description":{},"commit":{}
        },"workspace":{"edits":[{"path":"src/lib.rs","content":"changed"}],"removed":[]}}
    }))
    .expect("implementer result");
    let mut work = database.begin().await.expect("retain implementation");
    record_observation(&mut work, &outbox, &result, None)
        .await
        .expect("retain result");
    work.commit().await.expect("result commit");
    let premature: i64 = sqlx::query_scalar("SELECT count(*) FROM events.outbox WHERE type=$1")
        .bind(format!("turn.{develop_turn}.finished"))
        .fetch_one(database.pool())
        .await
        .expect("deferred implementer fact");
    assert_eq!(premature, 0);
    let pending = implementation_result(&database, develop_turn)
        .await
        .expect("pending implementation")
        .expect("retained result");
    assert_eq!(
        pending.result["workspace"]["edits"][0]["content"],
        "changed"
    );
    let mut work = database.begin().await.expect("invalid candidate");
    assert!(
        apply_implementation_result(
            &mut work,
            &outbox,
            develop_turn,
            ImplementationAssessment::Invalid(
                "candidate path is not a regular text file".to_owned()
            ),
        )
        .await
        .expect("record rejected candidate")
    );
    work.commit().await.expect("invalid candidate commit");
    let invalid_finished: Vec<u8> =
        sqlx::query_scalar("SELECT wire_bytes FROM events.outbox WHERE type=$1")
            .bind(format!("turn.{develop_turn}.finished"))
            .fetch_one(database.pool())
            .await
            .expect("invalid develop fact");
    let invalid_finished = decode::<TurnFinished>(&invalid_finished).expect("finished contract");
    assert_eq!(invalid_finished.data.outcome, "failed");
    assert_eq!(
        invalid_finished.data.reason.as_deref(),
        Some("invalid_output")
    );
    let correction = latest_implementation_correction(&database, task_id.0)
        .await
        .expect("implementation feedback")
        .expect("rejected result");
    assert_eq!(correction.attempt, 1);
    assert!(correction.reason.contains("regular text file"));
    let correcting = task(&database, issue)
        .await
        .expect("task")
        .expect("correcting");
    assert_eq!(correcting.stage, Stage::Implement);
    let repair_turn = Uuid::now_v7();
    let mut work = database.begin().await.expect("repair admission");
    assert_eq!(
        admit_implementation(
            &mut work,
            &outbox,
            issue,
            &AttemptSpec {
                id: repair_turn,
                task: task_id,
                expected_revision: correcting.revision,
                staffing_group: "implementer-repair".to_owned(),
                fork: "acme/demo".to_owned(),
                config_sha: "a".repeat(40),
                submit_body: b"body".to_vec(),
                prompt_body: None,
                deadline: Utc::now() + Duration::hours(1),
                unbounded_running: false,
            }
        )
        .await
        .expect("repair admission"),
        Admission::Admitted
    );
    work.commit().await.expect("repair admission commit");
    let repair_result: TurnObservation = serde_json::from_value(json!({
        "turn":repair_turn,"status":"done","created":1.0,"finished":2.0,
        "result":{"outcome":"ok","response":{
            "summary":"Implement change","validation":"Tests pass",
            "design_issue":null,"description":{},"commit":{}
        },"workspace":{"edits":[{"path":"src/lib.rs","content":"changed"}],"removed":[]}}
    }))
    .expect("repaired result");
    let mut work = database.begin().await.expect("retain repair");
    record_observation(&mut work, &outbox, &repair_result, None)
        .await
        .expect("retain repaired result");
    work.commit().await.expect("repair result commit");
    let binding: String =
        sqlx::query_scalar("SELECT binding FROM tasks.plan_approvals WHERE request_id=$1")
            .bind(request.id)
            .fetch_one(database.pool())
            .await
            .expect("approval binding");
    let mut work = database.begin().await.expect("candidate decision");
    assert!(
        apply_implementation_result(
            &mut work,
            &outbox,
            repair_turn,
            ImplementationAssessment::Candidate(json!({
                "plan_id":stored_id,"binding":binding,"parent":"b".repeat(40),
                "edits":[{"path":"src/lib.rs","content":"changed"}]
            }))
        )
        .await
        .expect("candidate acceptance")
    );
    work.commit().await.expect("candidate commit");
    let reopened_turn = Uuid::now_v7();
    let mut probe = database.begin().await.expect("reopen probe");
    sqlx::query(
        "INSERT INTO tasks.pr_review_batches \
         (id,task_id,publication_attempt,pr_number,head,branch,plan_id,round, \
          changed_paths,roster,status) \
         VALUES ($1,$2,$3,17,$4,'concurro/issue-3-test',$5,0,$6,$7,'settled')",
    )
    .bind(Uuid::now_v7())
    .bind(task_id.0)
    .bind(repair_turn)
    .bind("c".repeat(40))
    .bind(&stored_id)
    .bind(Json(json!(["src/lib.rs"])))
    .bind(Json(json!([])))
    .execute(probe.connection())
    .await
    .expect("published PR head for reopen");
    let mut reopened_state = task(&database, issue)
        .await
        .expect("task")
        .expect("published candidate");
    reopened_state.stage = Stage::Implement;
    reopened_state.current_attempt = Some(reopened_turn);
    sqlx::query("UPDATE tasks.task_state SET state=$2 WHERE task_id=$1")
        .bind(task_id.0)
        .bind(Json(&reopened_state))
        .execute(probe.connection())
        .await
        .expect("simulate retained implementer result");
    sqlx::query(
        "INSERT INTO tasks.attempts (id,task_id,operation,expected_revision,staffing_group, \
         fork,config_sha,submit_body,initial_deadline,unbounded_running,status,observation) \
         VALUES ($1,$2,'develop',$3,$4,'acme/demo',$5,$6,now(),false,'finished','{}'::jsonb)",
    )
    .bind(reopened_turn)
    .bind(task_id.0)
    .bind(i64::try_from(reopened_state.revision).expect("revision"))
    .bind(format!("fixture:{reopened_turn}"))
    .bind("a".repeat(40))
    .bind(b"reopen fixture".as_slice())
    .execute(probe.connection())
    .await
    .expect("reopen attempt");
    sqlx::query(
        "INSERT INTO tasks.implementation_reports (attempt_id,task_id,raw_result) VALUES ($1,$2,$3)",
    )
    .bind(reopened_turn)
    .bind(task_id.0)
    .bind(b"{}".as_slice())
    .execute(probe.connection())
    .await
    .expect("pending design issue");
    assert!(
        apply_implementation_result(
            &mut probe,
            &outbox,
            reopened_turn,
            ImplementationAssessment::Reopen(DesignIssue {
                kind: DesignIssueKind::Plan,
                explanation: "Approved scope omitted a required path".to_owned(),
                evidence: vec!["The implementation needs src/other.rs".to_owned()],
            }),
        )
        .await
        .expect("record design reopening")
    );
    let (reopened_stage, reopen_status): (String, String) = sqlx::query_as(
        "SELECT t.state->>'stage',r.status FROM tasks.task_state t \
         JOIN tasks.implementation_reports r ON r.task_id=t.task_id \
         WHERE t.task_id=$1 AND r.attempt_id=$2",
    )
    .bind(task_id.0)
    .bind(reopened_turn)
    .fetch_one(probe.connection())
    .await
    .expect("reopen receipt");
    assert_eq!(
        (reopened_stage.as_str(), reopen_status.as_str()),
        ("Plan", "reopened")
    );
    let description_request = Request {
        id: Uuid::now_v7(),
        channel: "comment".to_owned(),
        reply: "issue_comment:899".to_owned(),
        comment_id: Some(899),
        issue,
        issue_author: approver.clone(),
        actor: approver.clone(),
        kind: RequestKind::Later(LaterCommand::Resume {
            description_only: true,
        }),
        order: SourceOrder {
            at: Utc::now(),
            source_id: 899,
            source: "description-repair:899".to_owned(),
            index: 0,
        },
    };
    record_request(&mut probe, &description_request)
        .await
        .expect("record description repair");
    let (_, repaired) = apply_request_with_evidence(
        &mut probe,
        &outbox,
        description_request.id,
        issue,
        Authority {
            writer: true,
            issue_open: true,
        },
        Some(&CommandEvidence {
            snapshot: snapshot.clone(),
            pr_unchanged: true,
            implementer_skill_update: None,
        }),
    )
    .await
    .expect("description recovery")
    .expect("pending recovery");
    assert_eq!(repaired.status, concurro_tasks_core::RequestStatus::Applied);
    assert_eq!(
        repaired.task.expect("recovered task").stage,
        Stage::Implement
    );
    let (current_plan, recovered): (bool, bool) = sqlx::query_as(
        "SELECT p.current,r.recovered_request=$2 FROM tasks.plan_reports p \
         JOIN tasks.implementation_reopens r ON r.task_id=p.task_id \
         WHERE p.task_id=$1 AND p.plan_id=$3",
    )
    .bind(task_id.0)
    .bind(description_request.id)
    .bind(&stored_id)
    .fetch_one(probe.connection())
    .await
    .expect("restored approval receipt");
    assert!(current_plan && recovered);
    probe
        .rollback()
        .await
        .expect("keep publication fixture intact");
    assert_eq!(
        task(&database, issue)
            .await
            .expect("task")
            .expect("candidate")
            .stage,
        Stage::Publish
    );
    let publication = git_publication_input(&database, issue)
        .await
        .expect("publication input")
        .expect("accepted candidate");
    assert_eq!(publication.turn, repair_turn);
    assert_eq!(publication.candidate["plan_id"], stored_id);
    let mut simulated = database.begin().await.expect("publication boundary probe");
    assert!(
        reserve_git_publication(
            &mut simulated,
            repair_turn,
            "concurro/issue-3-test",
            &"b".repeat(40),
        )
        .await
        .expect("reserve candidate")
    );
    let (revision, Json(mut intent)): (i64, Json<concurro_tasks_postgres::GitPublicationIntent>) =
        sqlx::query_as("SELECT revision,intent FROM tasks.git_publications WHERE task_id=$1")
            .bind(task_id.0)
            .fetch_one(simulated.connection())
            .await
            .expect("publication intent");
    intent.branch_created = true;
    intent.commit_sha = Some("c".repeat(40));
    intent.ref_advanced = true;
    intent.pr_create_sent = true;
    assert!(
        save_git_publication(&mut simulated, repair_turn, revision, &intent)
            .await
            .expect("save publication readback")
    );
    simulated
        .commit()
        .await
        .expect("persist uncertain PR create");
    assert!(
        implementation_result(&database, repair_turn)
            .await
            .expect("settled result")
            .is_none()
    );
    let detail = task_detail(&database, issue)
        .await
        .expect("operator candidate")
        .expect("task detail");
    assert_eq!(
        detail["implementation"].as_array().expect("reports").len(),
        2
    );
    assert_eq!(detail["implementation"][0]["status"], "rejected");
    assert_eq!(detail["implementation"][1]["status"], "accepted");
    for (source_id, kind) in [(901, RequestKind::Stop), (902, RequestKind::Start)] {
        let request = Request {
            id: Uuid::now_v7(),
            channel: "comment".to_owned(),
            reply: format!("issue_comment:{source_id}"),
            comment_id: Some(source_id),
            issue,
            issue_author: Actor {
                id: 7,
                login: "writer".to_owned(),
            },
            actor: approver.clone(),
            kind,
            order: SourceOrder {
                at: Utc::now(),
                source_id,
                source: format!("lifecycle:{source_id}"),
                index: 0,
            },
        };
        let mut work = database.begin().await.expect("lifecycle request");
        record_request(&mut work, &request)
            .await
            .expect("record lifecycle");
        work.commit().await.expect("lifecycle request commit");
        let mut work = database.begin().await.expect("lifecycle decision");
        let (_, decision) = apply_request(
            &mut work,
            &outbox,
            request.id,
            issue,
            Authority {
                writer: true,
                issue_open: true,
            },
        )
        .await
        .expect("lifecycle decision")
        .expect("decision");
        assert_eq!(
            (decision.status, decision.reason),
            if source_id == 901 {
                (concurro_tasks_core::RequestStatus::Applied, None)
            } else {
                (
                    concurro_tasks_core::RequestStatus::Refused,
                    Some("publication_prepared"),
                )
            }
        );
        work.commit().await.expect("lifecycle decision commit");
    }
    let stopped = task(&database, issue)
        .await
        .expect("task")
        .expect("stopped publication");
    assert_eq!(stopped.stage, Stage::Stopped);
    let retained: (Uuid, Json<concurro_tasks_postgres::GitPublicationIntent>) =
        sqlx::query_as("SELECT attempt_id,intent FROM tasks.git_publications WHERE task_id=$1")
            .bind(task_id.0)
            .fetch_one(database.pool())
            .await
            .expect("uncertain intent retained");
    assert_eq!(retained.0, repair_turn);
    assert!(retained.1.0.pr_create_sent);
    assert!(retained.1.0.pr_number.is_none());
    let pending = pending_effects(&database).await.expect("pending effects");
    assert!(pending.iter().any(|effect| {
        effect["kind"] == "git_publication"
            && effect["task_id"] == task_id.0.to_string()
            && effect["resolution"] == "readback_or_operator"
    }));
}

/// A non-approval opens a reviewer-owned objection that the next plan must answer.
#[tokio::test]
async fn design_objection_requires_an_exact_plan_revision_response() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 3)
        .await
        .expect("database");
    migrate(&database).await.expect("task schema");
    migrate_events(&database).await.expect("event schema");
    let outbox = Outbox::new(1024 * 1024, vec!["task.>".to_owned(), "turn.>".to_owned()]);
    let issue = IssueKey {
        repository_id: 28,
        number: 4,
    };
    let task_id = TaskId(Uuid::now_v7());
    let pins = PinnedInputs {
        config_sha: "a".repeat(40),
        base_sha: "b".repeat(40),
        max_model_calls: 7,
        max_fix_rounds: 1,
        max_plan_revisions: 1,
    };
    let snapshot = InitializationSnapshot {
        pins: pins.clone(),
        configuration: json!({
            "planner":"codex","implementer":"codex","qc":"claude",
            "reviewers":["codex","claude"],"models":{"codex":"gpt","claude":"sonnet"},
            "policy_documents":["README.md"],
            "paths":{"protected":[],"protected_extensions":[],"protected_names":[],
                "protected_name_prefixes":[],"protected_prefixes":[],
                "protected_files":[],"generated":[]}
        }),
        requirements: Requirements {
            title: "Task".to_owned(),
            body: Some("Cover the edge case".to_owned()),
            discussion: vec![],
        },
        charter: r#"{"goals":{"G1":"Correctness"}}"#.to_owned(),
        instruction_texts: BTreeMap::new(),
        policy_documents: BTreeMap::new(),
        base_files: vec!["src/lib.rs".to_owned()],
        bundle_id: "bundle".to_owned(),
    };
    let state = TaskState {
        id: task_id,
        issue,
        author: Actor {
            id: 7,
            login: "writer".to_owned(),
        },
        stage: Stage::PlanQc,
        revision: 1,
        stage_reason: None,
        retry_stage: None,
        pins: Some(pins),
        used_model_calls: 1,
        current_attempt: None,
    };
    sqlx::query("INSERT INTO tasks.issue_controls (repository_id,issue_number) VALUES (28,4)")
        .execute(database.pool())
        .await
        .expect("issue");
    sqlx::query("INSERT INTO tasks.task_state (repository_id,issue_number,task_id,revision,state) VALUES (28,4,$1,1,$2)")
        .bind(task_id.0).bind(Json(&state)).execute(database.pool()).await.expect("task");
    sqlx::query(
        "INSERT INTO tasks.initialization_inputs (task_id,task_revision,snapshot) VALUES ($1,1,$2)",
    )
    .bind(task_id.0)
    .bind(Json(&snapshot))
    .execute(database.pool())
    .await
    .expect("snapshot");
    let prior = json!({
        "summary":"Implement task","questions":[],"files":["src/lib.rs"],
        "steps":["Edit"],"tests":["Run"],"responses":[],
        "publication":{"changes":["Change"],"review_focus":["Edge"]},
        "scope":{"paths":["src/"]}
    });
    let plan_id = plan_identity(&prior, &snapshot).expect("plan identity");
    let dummy = Uuid::now_v7();
    sqlx::query("INSERT INTO tasks.attempts \
        (id,task_id,operation,expected_revision,staffing_group,fork,config_sha,submit_body,initial_deadline,unbounded_running,status) \
        VALUES ($1,$2,'plan',1,'old','acme/demo',$3,$4,now(),false,'finished')")
        .bind(dummy).bind(task_id.0).bind("a".repeat(40)).bind(b"old".to_vec())
        .execute(database.pool()).await.expect("prior planner");
    sqlx::query(
        "INSERT INTO tasks.plan_reports \
        (attempt_id,task_id,initialization_revision,raw_report,raw_report_bytes,accepted,plan_id) \
        VALUES ($1,$2,1,$3,$4,true,$5)",
    )
    .bind(dummy)
    .bind(task_id.0)
    .bind(Json(&prior))
    .bind(serde_json::to_vec(&prior).expect("plan bytes"))
    .bind(&plan_id)
    .execute(database.pool())
    .await
    .expect("prior plan");
    let roster = design_roster(&snapshot.configuration).expect("roster");
    let binding = design_binding(
        &prior,
        &plan_id,
        &snapshot,
        &roster,
        0,
        "cooperative",
        None,
        &[],
    )
    .expect("design binding");
    let batch_id = Uuid::now_v7();
    sqlx::query(
        "INSERT INTO tasks.plan_review_batches \
        (id,task_id,plan_id,generation,phase,binding,roster) \
        VALUES ($1,$2,$3,0,'cooperative',$4,$5)",
    )
    .bind(batch_id)
    .bind(task_id.0)
    .bind(&plan_id)
    .bind(&binding)
    .bind(Json(&roster))
    .execute(database.pool())
    .await
    .expect("batch");
    for reviewer in &roster {
        sqlx::query(
            "INSERT INTO tasks.plan_review_seats (batch_id,reviewer_id,identity) VALUES ($1,$2,$3)",
        )
        .bind(batch_id)
        .bind(&reviewer.id)
        .bind(Json(reviewer))
        .execute(database.pool())
        .await
        .expect("seat");
    }
    let specs = roster
        .iter()
        .map(|reviewer| {
            let turn = Uuid::now_v7();
            let submission = TurnSubmission {
                turn,
                expected: json!({}),
                models: vec![reviewer.model.clone()],
                budget: 1,
                lease: 1,
                payload: json!({"request":{
                    "session":{"scope":format!("design-initial:{}",reviewer.id),
                               "turn":0,"role":"plan_qc"},
                    "context":{
                        "design_binding":binding,"reviewer_identity":{"id":reviewer.id}
                    }
                }}),
                pin: None,
                binding: json!({}),
            };
            (
                reviewer.id.clone(),
                AttemptSpec {
                    id: turn,
                    task: task_id,
                    expected_revision: 1,
                    staffing_group: reviewer.id.clone(),
                    fork: "acme/demo".to_owned(),
                    config_sha: "a".repeat(40),
                    submit_body: serde_json::to_vec(&submission).expect("submission"),
                    prompt_body: Some(
                        serde_json::to_vec(&json!({
                            "instructions":"review initial plan","response_schema":{}
                        }))
                        .expect("review prompt"),
                    ),
                    deadline: Utc::now() + Duration::hours(1),
                    unbounded_running: false,
                },
            )
        })
        .collect::<Vec<_>>();
    let mut work = database.begin().await.expect("admit review");
    assert_eq!(
        admit_design_batch(&mut work, &outbox, issue, batch_id, 1, &specs)
            .await
            .expect("admit roster"),
        Admission::Admitted
    );
    work.commit().await.expect("admit commit");
    let files = BTreeMap::from([("src/lib.rs".to_owned(), "fn example() {}\n".to_owned())]);
    let mut review = json!({
        "decision":"approve","rationale":"Plan is sound.","risk":"low",
        "questions":[],"concerns":[],
        "scope":{"goal_ids":["G1"],"fit":"Fits.","conflicts":[]},
        "sources":[{"id":"S1","path":"src/lib.rs","line":1,"quote":"fn example()"}],
        "assessments":[
            {"criterion":"R1","status":"satisfied","sources":["S1"],"reason":"Covered.","counterexample":"None."},
            {"criterion":"R2","status":"satisfied","sources":["S1"],"reason":"Covered.","counterexample":"None."}
        ],
        "objection_updates":[]
    });
    let failed: TurnObservation = serde_json::from_value(json!({
        "turn":specs[0].1.id,"status":"done","created":1.0,"finished":2.0,
        "result":{"outcome":"failed","response":null}
    }))
    .expect("failed reviewer observation");
    let mut non_session = database.begin().await.expect("non-session failure probe");
    sqlx::query("UPDATE tasks.attempts SET submit_body=$2 WHERE id=$1")
        .bind(specs[0].1.id)
        .bind(
            serde_json::to_vec(&json!({
                "turn":specs[0].1.id,"payload":{"request":{}}
            }))
            .expect("unscoped submission"),
        )
        .execute(non_session.connection())
        .await
        .expect("remove session from probe");
    record_observation(&mut non_session, &outbox, &failed, None)
        .await
        .expect("non-session failure");
    let blocked: Json<TaskState> =
        sqlx::query_scalar("SELECT state FROM tasks.task_state WHERE task_id=$1")
            .bind(task_id.0)
            .fetch_one(non_session.connection())
            .await
            .expect("blocked reviewer state");
    assert_eq!(blocked.0.stage, Stage::Blocked);
    let epochs: i64 =
        sqlx::query_scalar("SELECT count(*) FROM tasks.plan_review_sessions WHERE task_id=$1")
            .bind(task_id.0)
            .fetch_one(non_session.connection())
            .await
            .expect("no session rotation");
    assert_eq!(epochs, 0);
    non_session.rollback().await.expect("restore reviewer turn");
    let mut work = database.begin().await.expect("retain failure");
    record_observation(&mut work, &outbox, &failed, None)
        .await
        .expect("record reviewer failure");
    work.commit().await.expect("failure commit");
    drop(database);
    let database = Database::connect(postgres.url(), 3)
        .await
        .expect("restarted storage connection");
    let retry = design_correction(&database, batch_id, &specs[0].0, 1)
        .await
        .expect("retry read")
        .expect("queued reviewer retry");
    assert!(retry.retry);
    assert_eq!(retry.report, Value::Null);
    let sibling: String = sqlx::query_scalar(
        "SELECT status FROM tasks.plan_review_seats WHERE batch_id=$1 AND reviewer_id=$2",
    )
    .bind(batch_id)
    .bind(&specs[1].0)
    .fetch_one(database.pool())
    .await
    .expect("sibling seat");
    assert_eq!(sibling, "admitted");
    let retry_turn = Uuid::now_v7();
    let retry_submission = TurnSubmission {
        turn: retry_turn,
        expected: json!({}),
        models: vec![retry.reviewer.model.clone()],
        budget: 1,
        lease: 1,
        payload: json!({"request":{
            "session":{"scope":format!("design-retry:{}:epoch:1",retry.reviewer.id),
                       "turn":0,"role":"plan_qc"},
            "context":{
                "design_binding":binding,"reviewer_identity":{"id":retry.reviewer.id}
            }
        }}),
        pin: None,
        binding: json!({}),
    };
    let retry_spec = AttemptSpec {
        id: retry_turn,
        task: task_id,
        expected_revision: task(&database, issue)
            .await
            .expect("task")
            .expect("reviewing")
            .revision,
        staffing_group: "retry-reviewer".to_owned(),
        fork: "acme/demo".to_owned(),
        config_sha: "a".repeat(40),
        submit_body: serde_json::to_vec(&retry_submission).expect("retry submission"),
        prompt_body: Some(
            serde_json::to_vec(&json!({
                "instructions":"retry design review","response_schema":{}
            }))
            .expect("retry prompt"),
        ),
        deadline: Utc::now() + Duration::hours(1),
        unbounded_running: false,
    };
    let mut work = database.begin().await.expect("retry admission");
    assert_eq!(
        admit_design_correction(
            &mut work,
            &outbox,
            issue,
            batch_id,
            &specs[0].0,
            1,
            &retry_spec
        )
        .await
        .expect("admit reviewer retry"),
        Admission::Admitted
    );
    work.commit().await.expect("retry commit");
    let second_failure: TurnObservation = serde_json::from_value(json!({
        "turn":retry_turn,"status":"done","created":1.0,"finished":2.0,
        "result":{"outcome":"failed","response":null}
    }))
    .expect("second reviewer failure");
    let mut exhausted = database
        .begin()
        .await
        .expect("second reviewer failure probe");
    record_observation(&mut exhausted, &outbox, &second_failure, None)
        .await
        .expect("block exhausted reviewer");
    let blocked: Json<TaskState> =
        sqlx::query_scalar("SELECT state FROM tasks.task_state WHERE task_id=$1")
            .bind(task_id.0)
            .fetch_one(exhausted.connection())
            .await
            .expect("exhausted reviewer state");
    assert_eq!(blocked.0.stage, Stage::Blocked);
    let epoch: i32 = sqlx::query_scalar(
        "SELECT session_epoch FROM tasks.plan_review_sessions \
         WHERE task_id=$1 AND reviewer_id=$2",
    )
    .bind(task_id.0)
    .bind(&specs[0].0)
    .fetch_one(exhausted.connection())
    .await
    .expect("second reviewer epoch");
    assert_eq!(epoch, 2);
    exhausted.rollback().await.expect("restore reviewer retry");
    for (index, spec) in [&retry_spec, &specs[1].1].into_iter().enumerate() {
        if index == 0 {
            review["decision"] = json!("revise");
            review["concerns"] = json!(["Missing edge case"]);
            review["assessments"][1]["status"] = json!("defect");
            review["objection_updates"] = json!([{
                "id":"","status":"open","detail":"Missing edge case",
                "counterexample":"Input x fails","closure":"Add an edge test","evidence":"R2"
            }]);
        } else {
            review["decision"] = json!("approve");
            review["concerns"] = json!([]);
            review["assessments"][1]["status"] = json!("satisfied");
            review["objection_updates"] = json!([]);
        }
        let observation: TurnObservation = serde_json::from_value(json!({
            "turn":spec.id,"status":"done","created":1.0,"finished":2.0,
            "result":{"outcome":"ok","response":review}
        }))
        .expect("review result");
        let mut work = database.begin().await.expect("retain review");
        record_observation(&mut work, &outbox, &observation, None)
            .await
            .expect("retain");
        work.commit().await.expect("retain commit");
        let mut work = database.begin().await.expect("apply review");
        assert!(
            apply_design_result(
                &mut work,
                &outbox,
                spec.id,
                validate_design_review(
                    review.clone(),
                    &snapshot.requirements,
                    &snapshot.charter,
                    &files,
                    &[]
                )
            )
            .await
            .expect("apply vote")
        );
        work.commit().await.expect("vote commit");
    }
    let planning = task(&database, issue)
        .await
        .expect("task")
        .expect("planning");
    assert_eq!(planning.stage, Stage::Plan);
    let objection: String = sqlx::query_scalar(
        "SELECT id FROM tasks.plan_objections WHERE task_id=$1 AND NOT resolved",
    )
    .bind(task_id.0)
    .fetch_one(database.pool())
    .await
    .expect("open objection");
    let revision = latest_plan_revision(&database, task_id.0)
        .await
        .expect("feedback")
        .expect("prior plan");
    assert_eq!(revision.plan, prior);
    assert_eq!(revision.design_feedback.len(), 1);
    let planner_turn = Uuid::now_v7();
    let mut work = database.begin().await.expect("admit revision");
    assert_eq!(
        admit_plan(
            &mut work,
            &outbox,
            issue,
            &AttemptSpec {
                id: planner_turn,
                task: task_id,
                expected_revision: planning.revision,
                staffing_group: "new-planner".to_owned(),
                fork: "acme/demo".to_owned(),
                config_sha: "a".repeat(40),
                submit_body: b"body".to_vec(),
                prompt_body: None,
                deadline: Utc::now() + Duration::hours(1),
                unbounded_running: false,
            }
        )
        .await
        .expect("admit revision"),
        Admission::Admitted
    );
    work.commit().await.expect("admit revision commit");
    let mut revised = prior.clone();
    revised["summary"] = json!("Implement edge case with a test");
    revised["responses"] = json!([{"id":objection,"response":"Added edge case coverage"}]);
    let observation: TurnObservation = serde_json::from_value(json!({
        "turn":planner_turn,"status":"done","created":1.0,"finished":2.0,
        "result":{"outcome":"ok","response":revised}
    }))
    .expect("revised plan");
    let mut work = database.begin().await.expect("revision result");
    record_observation(&mut work, &outbox, &observation, None)
        .await
        .expect("settle revision");
    work.commit().await.expect("revision commit");
    let validation_error: Option<String> =
        sqlx::query_scalar("SELECT validation_error FROM tasks.plan_reports WHERE attempt_id=$1")
            .bind(planner_turn)
            .fetch_one(database.pool())
            .await
            .expect("revision report");
    assert_eq!(validation_error, None);
    assert_eq!(
        task(&database, issue)
            .await
            .expect("task")
            .expect("review again")
            .stage,
        Stage::PlanQc
    );
    let generation: i64 = sqlx::query_scalar(
        "SELECT generation FROM tasks.plan_review_batches WHERE task_id=$1 AND generation=1",
    )
    .bind(task_id.0)
    .fetch_one(database.pool())
    .await
    .expect("new generation");
    assert_eq!(generation, 1);
    let next_epochs: Vec<(String, i32)> = sqlx::query_as(
        "SELECT s.reviewer_id,s.session_epoch FROM tasks.plan_review_seats s \
         JOIN tasks.plan_review_batches b ON b.id=s.batch_id \
         WHERE b.task_id=$1 AND b.generation=1 ORDER BY s.reviewer_id",
    )
    .bind(task_id.0)
    .fetch_all(database.pool())
    .await
    .expect("next reviewer sessions");
    assert_eq!(next_epochs.len(), 2);
    assert_eq!(
        next_epochs
            .iter()
            .find(|(reviewer, _)| reviewer == &specs[0].0)
            .expect("retried reviewer")
            .1,
        1
    );
    assert_eq!(
        next_epochs
            .iter()
            .find(|(reviewer, _)| reviewer == &specs[1].0)
            .expect("sibling reviewer")
            .1,
        0
    );
    let (next_batch, next_binding, Json(next_roster)): (Uuid, String, Json<Vec<DesignReviewer>>) =
        sqlx::query_as(
            "SELECT id,binding,roster FROM tasks.plan_review_batches \
         WHERE task_id=$1 AND generation=1",
        )
        .bind(task_id.0)
        .fetch_one(database.pool())
        .await
        .expect("next batch");
    let next_state = task(&database, issue)
        .await
        .expect("task")
        .expect("next review");
    let next_specs = next_roster
        .iter()
        .map(|reviewer| {
            let epoch = next_epochs
                .iter()
                .find(|(id, _)| id == &reviewer.id)
                .expect("reviewer epoch")
                .1;
            let turn = Uuid::now_v7();
            let scope = format!("design:{}:epoch:{epoch}", reviewer.id);
            let submission = TurnSubmission {
                turn,
                expected: json!({}),
                models: vec![reviewer.model.clone()],
                budget: 1,
                lease: 1,
                payload: json!({"request":{
                    "session":{"scope":scope,"turn":0,"role":"plan_qc"},
                    "context":{"design_binding":next_binding,
                               "reviewer_identity":{"id":reviewer.id}}
                }}),
                pin: None,
                binding: json!({}),
            };
            (
                reviewer.id.clone(),
                scope,
                AttemptSpec {
                    id: turn,
                    task: task_id,
                    expected_revision: next_state.revision,
                    staffing_group: format!("review-generation-1:{}", reviewer.id),
                    fork: "acme/demo".to_owned(),
                    config_sha: "a".repeat(40),
                    submit_body: serde_json::to_vec(&submission).expect("review submission"),
                    prompt_body: Some(
                        serde_json::to_vec(&json!({
                            "instructions":"review revised plan","response_schema":{}
                        }))
                        .expect("review prompt"),
                    ),
                    deadline: Utc::now() + Duration::hours(1),
                    unbounded_running: false,
                },
            )
        })
        .collect::<Vec<_>>();
    let admission_specs = next_specs
        .iter()
        .map(|(id, _, spec)| (id.clone(), spec.clone()))
        .collect::<Vec<_>>();
    let mut work = database.begin().await.expect("retained review admission");
    assert_eq!(
        admit_design_batch(
            &mut work,
            &outbox,
            issue,
            next_batch,
            next_state.revision,
            &admission_specs,
        )
        .await
        .expect("admit revised review"),
        Admission::Admitted
    );
    work.commit().await.expect("revised review commit");
    for (_, scope, spec) in next_specs {
        let stored_scope: String =
            sqlx::query_scalar("SELECT retained_scope FROM tasks.attempts WHERE id=$1")
                .bind(spec.id)
                .fetch_one(database.pool())
                .await
                .expect("retained review scope");
        assert_eq!(stored_scope, scope);
    }
}

/// A writer's later start carries one answer into the same unfinished plan.
#[tokio::test]
async fn planner_clarification_continues_with_one_source_ordered_answer() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 3)
        .await
        .expect("database");
    migrate(&database).await.expect("task schema");
    migrate_events(&database).await.expect("event schema");
    let outbox = Outbox::new(
        1024 * 1024,
        vec![
            "task.>".to_owned(),
            "turn.>".to_owned(),
            "request.>".to_owned(),
        ],
    );
    let issue = IssueKey {
        repository_id: 18,
        number: 9,
    };
    let task_id = TaskId(Uuid::now_v7());
    let author = Actor {
        id: 7,
        login: "writer".to_owned(),
    };
    let pins = PinnedInputs {
        config_sha: "a".repeat(40),
        base_sha: "b".repeat(40),
        max_model_calls: 5,
        max_fix_rounds: 1,
        max_plan_revisions: 1,
    };
    let state = TaskState {
        id: task_id,
        issue,
        author: author.clone(),
        stage: Stage::Plan,
        revision: 1,
        stage_reason: None,
        retry_stage: None,
        pins: Some(pins.clone()),
        used_model_calls: 0,
        current_attempt: None,
    };
    let snapshot = InitializationSnapshot {
        pins: pins.clone(),
        configuration: json!({
            "planner":"codex","implementer":"codex","qc":"claude",
            "reviewers":["codex","claude"],"models":{"codex":"gpt","claude":"sonnet"},
            "paths":{"protected":[],"protected_extensions":[],"protected_names":[],
                "protected_name_prefixes":[],"protected_prefixes":[],
                "protected_files":[],"generated":[]}
        }),
        requirements: Requirements {
            title: "Task".to_owned(),
            body: Some("Description".to_owned()),
            discussion: vec![],
        },
        charter: r#"{"goals":{"G1":"Correctness"}}"#.to_owned(),
        instruction_texts: BTreeMap::new(),
        policy_documents: BTreeMap::new(),
        base_files: vec!["src/lib.rs".to_owned()],
        bundle_id: "bundle".to_owned(),
    };
    sqlx::query("INSERT INTO tasks.issue_controls (repository_id,issue_number) VALUES (18,9)")
        .execute(database.pool())
        .await
        .expect("issue control");
    sqlx::query("INSERT INTO tasks.task_state (repository_id,issue_number,task_id,revision,state) VALUES (18,9,$1,1,$2)")
        .bind(task_id.0).bind(Json(&state)).execute(database.pool()).await.expect("task");
    sqlx::query(
        "INSERT INTO tasks.initialization_inputs (task_id,task_revision,snapshot) VALUES ($1,1,$2)",
    )
    .bind(task_id.0)
    .bind(Json(&snapshot))
    .execute(database.pool())
    .await
    .expect("snapshot");
    let turn = Uuid::now_v7();
    let mut work = database.begin().await.expect("admit");
    assert_eq!(
        admit_plan(
            &mut work,
            &outbox,
            issue,
            &AttemptSpec {
                id: turn,
                task: task_id,
                expected_revision: 1,
                staffing_group: "planner".to_owned(),
                fork: "acme/demo".to_owned(),
                config_sha: pins.config_sha.clone(),
                submit_body: b"body".to_vec(),
                prompt_body: None,
                deadline: Utc::now() + Duration::hours(1),
                unbounded_running: false,
            }
        )
        .await
        .expect("planner admission"),
        Admission::Admitted
    );
    work.commit().await.expect("admit commit");
    let plan = json!({
        "summary":"Need the API choice","questions":["Which API?"],
        "files":[],"steps":[],"tests":[],"responses":[],
        "publication":{"changes":["Choose an API"],"review_focus":["Intent"]}
    });
    let observation: TurnObservation = serde_json::from_value(json!({
        "turn":turn,"status":"done","created":1.0,"finished":2.0,
        "result":{"outcome":"ok","response":plan}
    }))
    .expect("planner observation");
    let mut work = database.begin().await.expect("result");
    record_observation(&mut work, &outbox, &observation, None)
        .await
        .expect("settle plan");
    work.commit().await.expect("result commit");
    assert_eq!(
        task(&database, issue)
            .await
            .expect("task")
            .expect("clarify")
            .stage,
        Stage::Clarify
    );
    sqlx::query(
        "UPDATE tasks.publication_comments SET status='written',comment_id=918 \
         WHERE task_id=$1 AND kind='notice' AND comment_id IS NULL",
    )
    .bind(task_id.0)
    .execute(database.pool())
    .await
    .expect("publish question notice");
    sqlx::query(
        "INSERT INTO tasks.plan_review_batches \
         (id,task_id,plan_id,generation,phase,binding,roster,status) \
         VALUES ($1,$2,'historical-plan',0,'cooperative','historical-binding','[]'::jsonb,'settled')",
    )
    .bind(Uuid::now_v7())
    .bind(task_id.0)
    .execute(database.pool())
    .await
    .expect("prior review history");
    let request = Request {
        id: Uuid::now_v7(),
        channel: "comment".to_owned(),
        reply: "issue_comment:920".to_owned(),
        comment_id: Some(920),
        issue,
        issue_author: author.clone(),
        actor: author.clone(),
        kind: RequestKind::Start,
        order: SourceOrder {
            at: Utc::now(),
            source_id: 920,
            source: "start:920".to_owned(),
            index: 0,
        },
    };
    let mut work = database.begin().await.expect("request");
    record_request(&mut work, &request)
        .await
        .expect("record start");
    work.commit().await.expect("request commit");
    let mut work = database.begin().await.expect("decision");
    let (_, decision) = apply_request(
        &mut work,
        &outbox,
        request.id,
        issue,
        Authority {
            writer: true,
            issue_open: true,
        },
    )
    .await
    .expect("apply answer start")
    .expect("decision");
    assert_eq!(decision.status, concurro_tasks_core::RequestStatus::Applied);
    work.commit().await.expect("decision commit");
    let checking = task(&database, issue)
        .await
        .expect("task")
        .expect("checking");
    assert_eq!(checking.stage, Stage::Clarify);
    let job: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM graphile_worker.jobs WHERE task_identifier='task_clarification'",
    )
    .fetch_one(database.pool())
    .await
    .expect("clarification job");
    assert_eq!(job, 1);
    drop(database);
    let database = Database::connect(postgres.url(), 3)
        .await
        .expect("restarted storage connection");
    let mut answered = snapshot.clone();
    answered
        .requirements
        .discussion
        .push(concurro_tasks_core::RequirementComment {
            author,
            body: "Use the stable API".to_owned(),
        });
    let mut work = database
        .begin()
        .await
        .expect("answer before question probe");
    assert!(
        continue_planner_answer(&mut work, &outbox, issue, checking.revision, &answered, 917)
            .await
            .expect("pre-notice answer reassessment")
    );
    let premature_stage: String =
        sqlx::query_scalar("SELECT state->>'stage' FROM tasks.task_state WHERE task_id=$1")
            .bind(task_id.0)
            .fetch_one(work.connection())
            .await
            .expect("probe stage");
    assert_eq!(premature_stage, "Intake");
    work.rollback().await.expect("discard ordering probe");
    let mut work = database.begin().await.expect("answer");
    assert!(
        continue_planner_answer(&mut work, &outbox, issue, checking.revision, &answered, 919)
            .await
            .expect("continue plan")
    );
    work.commit().await.expect("answer commit");
    let resumed = task(&database, issue)
        .await
        .expect("task")
        .expect("resumed");
    assert_eq!(resumed.stage, Stage::Plan);
    assert_eq!(resumed.used_model_calls, 1);
    assert_eq!(
        latest_plan_revision(&database, task_id.0)
            .await
            .expect("prior plan")
            .expect("prior plan")
            .plan,
        plan
    );
    let Json(saved): Json<InitializationSnapshot> =
        sqlx::query_scalar("SELECT snapshot FROM tasks.initialization_inputs WHERE task_id=$1")
            .bind(task_id.0)
            .fetch_one(database.pool())
            .await
            .expect("saved answer");
    assert_eq!(saved.requirements, answered.requirements);
    let second_turn = Uuid::now_v7();
    let mut work = database.begin().await.expect("admit next question");
    assert_eq!(
        admit_plan(
            &mut work,
            &outbox,
            issue,
            &AttemptSpec {
                id: second_turn,
                task: task_id,
                expected_revision: resumed.revision,
                staffing_group: "planner-second".to_owned(),
                fork: "acme/demo".to_owned(),
                config_sha: pins.config_sha.clone(),
                submit_body: b"body".to_vec(),
                prompt_body: None,
                deadline: Utc::now() + Duration::hours(1),
                unbounded_running: false,
            }
        )
        .await
        .expect("admit next question"),
        Admission::Admitted
    );
    work.commit().await.expect("next question commit");
    let next_question: TurnObservation = serde_json::from_value(json!({
        "turn":second_turn,"status":"done","created":1.0,"finished":2.0,
        "result":{"outcome":"ok","response":plan}
    }))
    .expect("next question");
    let mut work = database.begin().await.expect("settle next question");
    record_observation(&mut work, &outbox, &next_question, None)
        .await
        .expect("settle next question");
    work.commit().await.expect("next question commit");
    sqlx::query(
        "UPDATE tasks.publication_comments SET status='written',comment_id=919 \
         WHERE task_id=$1 AND kind='notice' AND source_revision=$2",
    )
    .bind(task_id.0)
    .bind(
        i64::try_from(
            task(&database, issue)
                .await
                .expect("task")
                .expect("clarify")
                .revision,
        )
        .expect("revision"),
    )
    .execute(database.pool())
    .await
    .expect("publish next question");
    let changed_start = Request {
        id: Uuid::now_v7(),
        channel: "comment".to_owned(),
        reply: "issue_comment:921".to_owned(),
        comment_id: Some(921),
        issue,
        issue_author: request.issue_author,
        actor: request.actor,
        kind: RequestKind::Start,
        order: SourceOrder {
            at: Utc::now(),
            source_id: 921,
            source: "start:921".to_owned(),
            index: 0,
        },
    };
    let mut work = database.begin().await.expect("changed start");
    record_request(&mut work, &changed_start)
        .await
        .expect("record changed start");
    work.commit().await.expect("changed start commit");
    let mut work = database.begin().await.expect("changed decision");
    let (_, decision) = apply_request(
        &mut work,
        &outbox,
        changed_start.id,
        issue,
        Authority {
            writer: true,
            issue_open: true,
        },
    )
    .await
    .expect("changed decision")
    .expect("changed request");
    assert_eq!(decision.status, concurro_tasks_core::RequestStatus::Applied);
    work.commit().await.expect("changed decision commit");
    let checking = task(&database, issue)
        .await
        .expect("task")
        .expect("checking source");
    let mut changed = answered.clone();
    changed.requirements.title = "Changed issue title".to_owned();
    let mut work = database.begin().await.expect("shared revision probe");
    sqlx::query("UPDATE tasks.initialization_inputs SET spent_revisions=1 WHERE task_id=$1")
        .bind(task_id.0)
        .execute(work.connection())
        .await
        .expect("consume design revision");
    assert!(
        continue_planner_answer(&mut work, &outbox, issue, checking.revision, &changed, 920)
            .await
            .expect("exhausted reassessment")
    );
    let exhausted_stage: String =
        sqlx::query_scalar("SELECT state->>'stage' FROM tasks.task_state WHERE task_id=$1")
            .bind(task_id.0)
            .fetch_one(work.connection())
            .await
            .expect("exhausted stage");
    assert_eq!(exhausted_stage, "Escalated");
    work.rollback().await.expect("discard budget probe");
    let mut work = database.begin().await.expect("reassess");
    assert!(
        continue_planner_answer(&mut work, &outbox, issue, checking.revision, &changed, 920)
            .await
            .expect("changed source reassessment")
    );
    work.commit().await.expect("reassess commit");
    let reassessed = task(&database, issue)
        .await
        .expect("task")
        .expect("reassessed");
    assert_eq!(reassessed.stage, Stage::Intake);
    assert_eq!(reassessed.used_model_calls, 2);
    assert!(
        latest_plan_revision(&database, task_id.0)
            .await
            .expect("current plan")
            .is_none()
    );
    let (spent_revisions, current_plans): (i64, i64) = sqlx::query_as(
        "SELECT i.spent_revisions,(SELECT count(*) FROM tasks.plan_reports p \
         WHERE p.task_id=i.task_id AND p.current) FROM tasks.initialization_inputs i \
         WHERE i.task_id=$1",
    )
    .bind(task_id.0)
    .fetch_one(database.pool())
    .await
    .expect("reassessment evidence");
    assert_eq!((spent_revisions, current_plans), (1, 0));
}

/// A definite retained implementer failure rotates its journal once, then blocks with a fresh epoch.
#[tokio::test]
async fn implementer_session_failure_retries_once_and_rotates_after_second_failure() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("task schema");
    migrate_events(&database).await.expect("event schema");
    let outbox = Outbox::new(1024 * 1024, vec!["task.>".to_owned(), "turn.>".to_owned()]);
    let task_id = TaskId(Uuid::now_v7());
    let issue = IssueKey {
        repository_id: 8,
        number: 9,
    };
    let pins = PinnedInputs {
        config_sha: "a".repeat(40),
        base_sha: "b".repeat(40),
        max_model_calls: 8,
        max_fix_rounds: 2,
        max_plan_revisions: 1,
    };
    let first = Uuid::now_v7();
    let state = TaskState {
        id: task_id,
        issue,
        author: Actor {
            id: 7,
            login: "writer".to_owned(),
        },
        stage: Stage::Implement,
        revision: 1,
        stage_reason: None,
        retry_stage: None,
        pins: Some(pins.clone()),
        used_model_calls: 1,
        current_attempt: Some(first),
    };
    let snapshot = InitializationSnapshot {
        pins,
        configuration: json!({"repository":"acme/demo"}),
        requirements: Requirements {
            title: "Task".to_owned(),
            body: None,
            discussion: vec![],
        },
        charter: "{}".to_owned(),
        instruction_texts: BTreeMap::new(),
        policy_documents: BTreeMap::new(),
        base_files: vec![],
        bundle_id: "bundle".to_owned(),
    };
    sqlx::query("INSERT INTO tasks.issue_controls (repository_id,issue_number) VALUES (8,9)")
        .execute(database.pool())
        .await
        .expect("issue");
    sqlx::query("INSERT INTO tasks.task_state (repository_id,issue_number,task_id,revision,state) VALUES (8,9,$1,1,$2)")
        .bind(task_id.0).bind(Json(&state)).execute(database.pool()).await.expect("task");
    sqlx::query(
        "INSERT INTO tasks.initialization_inputs (task_id,task_revision,snapshot) VALUES ($1,1,$2)",
    )
    .bind(task_id.0)
    .bind(Json(&snapshot))
    .execute(database.pool())
    .await
    .expect("snapshot");
    for (number, turn) in [(1, first), (2, Uuid::now_v7())] {
        if number == 2 {
            let retrying = task(&database, issue)
                .await
                .expect("task")
                .expect("retry stage");
            assert_eq!(retrying.stage, Stage::Implement);
            assert_eq!(retrying.current_attempt, None);
            let active = TaskState {
                revision: retrying.revision + 1,
                current_attempt: Some(turn),
                ..retrying
            };
            sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
                .bind(task_id.0)
                .bind(i64::try_from(active.revision).expect("revision"))
                .bind(Json(&active))
                .execute(database.pool())
                .await
                .expect("second attempt stage");
        }
        let submission = TurnSubmission {
            turn,
            expected: json!({}),
            models: vec!["gpt".to_owned()],
            budget: 1,
            lease: 1,
            payload: json!({"request":{"session":{"scope":format!("develop-epoch-{number}")}}}),
            pin: None,
            binding: json!({}),
        };
        sqlx::query("INSERT INTO tasks.attempts (id,task_id,operation,expected_revision,staffing_group,fork,config_sha,submit_body,initial_deadline,deadline,unbounded_running,status) \
             VALUES ($1,$2,'develop',$3,$4,'acme/demo',$5,$6,now()+interval '1 hour',now()+interval '1 hour',false,'running')")
            .bind(turn).bind(task_id.0).bind(i64::from(number))
            .bind(format!("develop-{number}")).bind("a".repeat(40))
            .bind(serde_json::to_vec(&submission).expect("submission"))
            .execute(database.pool()).await.expect("attempt");
        let failed: TurnObservation = serde_json::from_value(json!({
            "turn":turn,"status":"done","created":1.0,"finished":2.0,
            "result":{"outcome":"failed","response":null}
        }))
        .expect("failed turn");
        let mut work = database.begin().await.expect("settle");
        record_observation(&mut work, &outbox, &failed, None)
            .await
            .expect("record failure");
        work.commit().await.expect("failure commit");
        let (epoch, failures): (i32, i32) = sqlx::query_as(
            "SELECT implementation_session_epoch,implementation_failure_count FROM tasks.initialization_inputs WHERE task_id=$1",
        ).bind(task_id.0).fetch_one(database.pool()).await.expect("session state");
        assert_eq!((epoch, failures), (number, number));
    }
    let blocked = task(&database, issue)
        .await
        .expect("task")
        .expect("blocked");
    assert_eq!(blocked.stage, Stage::Blocked);
    assert!(
        blocked
            .stage_reason
            .as_deref()
            .is_some_and(|reason| reason.contains("failed twice"))
    );
    assert_eq!(blocked.current_attempt, None);
}
