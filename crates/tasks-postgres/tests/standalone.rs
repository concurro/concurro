//! Standalone PR request ordering and ownership at the database boundary.

use concurro_tasks_postgres::StandaloneActionSource;
use concurro_tasks_postgres::StandaloneReviewAuthority;
use std::collections::BTreeMap;

use chrono::{Duration, Utc};
use concurro_contracts::{ReadModelRecord, decode};
use concurro_db::Database;
use concurro_events_postgres::{Outbox, migrate as migrate_events};
use concurro_exchange_v1::{TurnObservation, TurnSubmission};
use concurro_tasks_core::{
    Actor, ConsentAction, LaterCommand, Provider, RequestKind, SourceOrder, SponsorSlot,
    initialize_consent, project_consent, python_digest, validate_standalone_pr_review,
};
use concurro_tasks_postgres::{
    StandaloneReviewCommand, StandaloneReviewRequest, apply_standalone_review_request,
    apply_standalone_review_result, await_standalone_review_credentials, browser_standalone_action,
    comment_scope, complete_withdrawal_step, confirm_standalone_review_publication,
    due_withdrawal_steps, excluded_credential_versions, migrate,
    observe_standalone_review_pr_state, record_browser_standalone_action, record_observation,
    record_runner_session, record_sponsorship_transition, record_standalone_funding_revision,
    record_standalone_review_request, record_standalone_withdrawal_steps,
    resume_standalone_review_credentials, standalone_consent_sources,
    standalone_review_outstanding_findings, standalone_review_result,
    standalone_review_source_is_current, standalone_review_task,
};
use concurro_testkit::PostgresContainer;
use serde_json::{Value, json};
use uuid::Uuid;

/// Browser and PR-comment withdrawals share durable later-step reporting.
#[tokio::test]
async fn standalone_withdrawal_steps_use_existing_request_facts_and_replies() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("task schema");
    migrate_events(&database).await.expect("event schema");
    let outbox = Outbox::new(1024 * 1024, vec!["request.>".to_owned()]);
    let actor = Actor {
        id: 7,
        login: "writer".to_owned(),
    };
    let task = Uuid::now_v7();
    let mut work = database.begin().await.expect("seed task");
    sqlx::query(
        "INSERT INTO tasks.standalone_review_tasks \
         (id,repository_id,runner_repository_id,upstream,pr_number,reviewer_id,stage) \
         VALUES ($1,8,8,'acme/demo',4,99,'review')",
    )
    .bind(task)
    .execute(work.connection())
    .await
    .expect("task");
    let mut withdrawal = json!({
        "repository":"acme/demo", "issue":4, "provider":"openai",
        "generation":1, "expected_epoch":1, "reason":"withdraw", "actor":"7",
        "cutoff":1, "slot":{"status":"withdrawn","sponsor_id":null,"epoch":2,
            "grant_id":"grant","funding":"contributed","source_id":70,
            "source_order":["2026-11-20T00:00:00Z",70,0]}
    });
    withdrawal["transition"] = json!(python_digest(&withdrawal).expect("digest"));
    let transition = record_sponsorship_transition(&mut work, task, &withdrawal)
        .await
        .expect("pending revoke");
    work.commit().await.expect("seed commit");
    let mut ids = Vec::new();
    for (source, comment_id) in [("browser:action", -7), ("comment:action", 70)] {
        let command = StandaloneReviewCommand {
            id: Uuid::now_v7(),
            repository_id: 8,
            upstream: "acme/demo".to_owned(),
            pr_number: 4,
            comment_id,
            source: source.to_owned(),
            source_at: Utc::now(),
            actor: actor.clone(),
            kind: Some(RequestKind::Later(LaterCommand::Withdraw {
                provider: "openai".to_owned(),
            })),
            body: "/concurro withdraw openai".to_owned(),
            deleted: false,
        };
        let mut work = database.begin().await.expect("source transaction");
        sqlx::query(
            "INSERT INTO tasks.standalone_review_commands \
             (id,repository_id,upstream,pr_number,comment_id,source,source_at,actor, \
              comment_author_id,kind,body,deleted,status) \
             VALUES ($1,$2,$3,$4,$5,$6,$7,$8,7,$9,$10,false,'applied')",
        )
        .bind(command.id)
        .bind(command.repository_id)
        .bind(&command.upstream)
        .bind(command.pr_number)
        .bind(command.comment_id)
        .bind(&command.source)
        .bind(command.source_at)
        .bind(sqlx::types::Json(&command.actor))
        .bind(command.kind.as_ref().map(sqlx::types::Json))
        .bind(&command.body)
        .execute(work.connection())
        .await
        .expect("command source");
        record_standalone_withdrawal_steps(&mut work, &outbox, &command)
            .await
            .expect("later steps");
        work.commit().await.expect("source commit");
        ids.push(command.id);
    }
    let blocked: i64 =
        sqlx::query_scalar("SELECT count(*) FROM events.outbox WHERE type='request.tasks.blocked'")
            .fetch_one(database.pool())
            .await
            .expect("blocked facts");
    assert_eq!(blocked, 2);
    for id in &ids {
        let obligations: Vec<String> = sqlx::query_scalar(
            "SELECT unnest(transitions) FROM tasks.withdrawal_steps WHERE source_id=$1",
        )
        .bind(id)
        .fetch_all(database.pool())
        .await
        .expect("pending obligations");
        assert_eq!(obligations, vec![transition.clone()]);
    }
    let replies: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM tasks.publication_comments WHERE marker LIKE '<!-- concurro:withdrawal:%'",
    ).fetch_one(database.pool()).await.expect("comment replies");
    assert_eq!(replies, 1);
    let notice: Uuid = sqlx::query_scalar(
        "SELECT id FROM tasks.publication_comments WHERE standalone_step_command_id IS NOT NULL",
    )
    .fetch_one(database.pool())
    .await
    .expect("standalone notice");
    assert_eq!(
        comment_scope(&database, notice)
            .await
            .expect("publication route"),
        Some((8, "recorded".to_owned(), true))
    );
    sqlx::query("UPDATE tasks.withdrawal_steps SET blocked_at=now()-interval '41 seconds'")
        .execute(database.pool())
        .await
        .expect("age bound");
    assert!(
        due_withdrawal_steps(&database)
            .await
            .expect("pending revoke")
            .is_empty()
    );
    sqlx::query(
        "UPDATE tasks.sponsorship_transitions SET status='acknowledged', \
        acknowledged_at=now()-interval '41 seconds' WHERE transition=$1",
    )
    .bind(transition)
    .execute(database.pool())
    .await
    .expect("acknowledge revoke");
    assert_eq!(due_withdrawal_steps(&database).await.expect("due"), ids);
    for id in ids {
        let mut work = database.begin().await.expect("completion");
        assert!(
            complete_withdrawal_step(&mut work, &outbox, id)
                .await
                .expect("stopped")
        );
        work.commit().await.expect("completion commit");
    }
    let stopped: i64 =
        sqlx::query_scalar("SELECT count(*) FROM events.outbox WHERE type='request.tasks.stopped'")
            .fetch_one(database.pool())
            .await
            .expect("stopped facts");
    assert_eq!(stopped, 2);
}

/// Separate signed actions on one PR do not share a synthetic comment identity.
#[tokio::test]
async fn browser_standalone_actions_replay_without_folding_each_other() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("task schema");
    let issue = concurro_tasks_core::IssueKey {
        repository_id: 8,
        number: 42,
    };
    let actor = Actor {
        id: 7,
        login: "writer".to_owned(),
    };
    let content = json!({"kind":"stop","issue":{"repository_id":8,"number":42}});
    let actions = [Uuid::now_v7(), Uuid::now_v7()];
    for id in actions {
        let mut work = database.begin().await.expect("action transaction");
        record_browser_standalone_action(
            &mut work,
            id,
            &content,
            issue,
            StandaloneActionSource {
                upstream: "acme/demo",
                actor: &actor,
                kind: RequestKind::Stop,
                source_at: Utc::now(),
            },
        )
        .await
        .expect("browser action");
        work.commit().await.expect("action commit");
        assert_eq!(
            browser_standalone_action(&database, id, actor.id, &content)
                .await
                .expect("replay")
                .expect("record")
                .status,
            "pending"
        );
        assert!(
            browser_standalone_action(&database, id, actor.id, &json!({"kind":"retry"}))
                .await
                .is_err()
        );
    }
    let comment_ids: Vec<i64> =
        sqlx::query_scalar("SELECT comment_id FROM tasks.standalone_review_commands ORDER BY id")
            .fetch_all(database.pool())
            .await
            .expect("synthetic comments");
    assert_eq!(comment_ids.len(), 2);
    assert!(comment_ids.iter().all(|id| *id < 0));
    assert_ne!(comment_ids[0], comment_ids[1]);
}

/// A standalone result retains its exact report while the separate round awaits validation.
#[tokio::test]
async fn standalone_observation_retains_report_on_owning_round() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
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
    let request = StandaloneReviewRequest {
        id: Uuid::now_v7(),
        repository_id: 8,
        upstream: "acme/demo".to_owned(),
        pr_number: 42,
        source_id: 10,
        source_at: Utc::now(),
        actor: Actor {
            id: 7,
            login: "writer".to_owned(),
        },
        action: "review_requested".to_owned(),
    };
    let mut work = database.begin().await.expect("request transaction");
    record_standalone_review_request(&mut work, &request)
        .await
        .expect("request");
    apply_standalone_review_request(
&mut work,
request.id,
StandaloneReviewAuthority { upstream: &request.upstream, pr_number: request.pr_number, reviewer_id: 99, runner_repository_id: 8, actor_writer: true, pr_open: true, reviewer_requested: true, source_defect: None, snapshot: Some(&json!({"head":"a".repeat(40),"panel":[{"id":"reviewer"}],"configuration":{"max_review_repairs":1}})) },
)
    .await
    .expect("round");
    work.commit().await.expect("request commit");
    let task: Uuid = sqlx::query_scalar(
        "SELECT id FROM tasks.standalone_review_tasks WHERE upstream=$1 AND pr_number=$2",
    )
    .bind(&request.upstream)
    .bind(request.pr_number)
    .fetch_one(database.pool())
    .await
    .expect("task");
    let view_events: Vec<Vec<u8>> = sqlx::query_scalar(
        "SELECT wire_bytes FROM events.outbox WHERE subject='view.record' ORDER BY sequence",
    )
    .fetch_all(database.pool())
    .await
    .expect("standalone view events");
    assert!(!view_events.is_empty());
    let latest = decode::<ReadModelRecord>(view_events.last().expect("latest view event"))
        .expect("typed view event");
    assert_eq!(latest.data.entity, "standalone_review");
    assert_eq!(latest.data.entity_id, task.to_string());
    assert_eq!(latest.data.body["snapshot"]["head"], "a".repeat(40));
    let turn = Uuid::now_v7();
    sqlx::query(
        "INSERT INTO tasks.attempts \
         (id,standalone_task_id,operation,expected_revision,staffing_group,fork,config_sha, \
          submit_body,initial_deadline,deadline,unbounded_running,status) \
         VALUES ($1,$2,'standalone_review',1,'standalone-test','acme/demo',$3, \
                 $4,now()+interval '1 hour',now()+interval '1 hour',false,'running')",
    )
    .bind(turn)
    .bind(task)
    .bind("a".repeat(40))
    .bind(b"{}".as_slice())
    .execute(database.pool())
    .await
    .expect("attempt");
    sqlx::query(
        "INSERT INTO tasks.standalone_review_attempts (attempt_id,task_id,round,reviewer_id) VALUES ($1,$2,1,'reviewer')",
    )
    .bind(turn)
    .bind(task)
    .execute(database.pool())
    .await
    .expect("round attempt");
    let report = json!({"verdict":"comment","summary":"A\u{0000}B"});
    let observation: TurnObservation = serde_json::from_value(json!({
        "turn":turn,"status":"done","created":1.0,"finished":2.0,
        "result":{"outcome":"ok","response":report}
    }))
    .expect("observation");
    let mut work = database.begin().await.expect("observation transaction");
    assert!(
        record_observation(&mut work, &outbox, &observation, None)
            .await
            .expect("record observation")
    );
    work.commit().await.expect("observation commit");
    let (status, projected, raw): (String, serde_json::Value, Vec<u8>) = sqlx::query_as(
        "SELECT status,report,report_raw FROM tasks.standalone_review_attempts WHERE attempt_id=$1",
    )
    .bind(turn)
    .fetch_one(database.pool())
    .await
    .expect("retained report");
    assert_eq!(status, "completed");
    assert_eq!(projected["summary"], "A\\u0000B");
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(&raw).expect("raw report"),
        report
    );
    sqlx::query("UPDATE tasks.standalone_review_tasks SET stage='review' WHERE id=$1")
        .bind(task)
        .execute(database.pool())
        .await
        .expect("review stage");
    let mut work = database.begin().await.expect("invalid report transaction");
    assert!(
        apply_standalone_review_result(&mut work, turn, Err("invalid finding".to_owned()))
            .await
            .expect("queue correction")
    );
    work.commit().await.expect("invalid report commit");
    let (seat, signature): (String, String) = sqlx::query_as(
        "SELECT status,repair_signature FROM tasks.standalone_review_attempts WHERE attempt_id=$1",
    )
    .bind(turn)
    .fetch_one(database.pool())
    .await
    .expect("correction seat");
    assert_eq!(seat, "correcting");
    assert_eq!(signature.len(), 64);
    let mut work = database.begin().await.expect("grant wait");
    assert!(
        await_standalone_review_credentials(&mut work, task, 1)
            .await
            .expect("pause next seat")
    );
    assert!(
        resume_standalone_review_credentials(&mut work, task, 1)
            .await
            .expect("resume next seat")
    );
    work.commit().await.expect("grant repair commit");
    let stage: String =
        sqlx::query_scalar("SELECT stage FROM tasks.standalone_review_tasks WHERE id=$1")
            .bind(task)
            .fetch_one(database.pool())
            .await
            .expect("continued review");
    assert_eq!(stage, "review");
    let body: String = sqlx::query_scalar(
        "SELECT body FROM tasks.report_artifact_writes WHERE report_id=$1 AND standalone_task_id=$2",
    )
    .bind(turn)
    .bind(task)
    .fetch_one(database.pool())
    .await
    .expect("complete report artifact");
    assert!(body.contains("invalid finding"));
    assert!(body.contains("A\\u0000B"));
    let report_event: Vec<u8> = sqlx::query_scalar(
        "SELECT wire_bytes FROM events.outbox WHERE subject='view.record' \
         AND message->'data'->>'entity'='report' ORDER BY sequence DESC LIMIT 1",
    )
    .fetch_one(database.pool())
    .await
    .expect("typed report view event");
    let report_event = decode::<ReadModelRecord>(&report_event).expect("report contract");
    assert_eq!(report_event.data.entity_id, turn.to_string());
    assert_eq!(report_event.data.body["status"], "recorded");
}

/// A trusted standalone provider refusal excludes its exact version and retries the seat.
#[tokio::test]
async fn standalone_provider_failure_retries_with_a_new_credential() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
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
    let request = StandaloneReviewRequest {
        id: Uuid::now_v7(),
        repository_id: 8,
        upstream: "acme/demo".to_owned(),
        pr_number: 42,
        source_id: 10,
        source_at: Utc::now(),
        actor: Actor {
            id: 7,
            login: "writer".to_owned(),
        },
        action: "review_requested".to_owned(),
    };
    let mut work = database.begin().await.expect("request transaction");
    record_standalone_review_request(&mut work, &request)
        .await
        .expect("request");
    apply_standalone_review_request(
        &mut work,
        request.id,
        StandaloneReviewAuthority {
            upstream: &request.upstream,
            pr_number: request.pr_number,
            reviewer_id: 99,
            runner_repository_id: 8,
            actor_writer: true,
            pr_open: true,
            reviewer_requested: true,
            source_defect: None,
            snapshot: Some(&json!({"head":"a".repeat(40),"configuration":{"max_model_calls":3}})),
        },
    )
    .await
    .expect("round");
    work.commit().await.expect("request commit");
    let task: Uuid = sqlx::query_scalar(
        "SELECT id FROM tasks.standalone_review_tasks WHERE upstream='acme/demo' AND pr_number=42",
    )
    .fetch_one(database.pool())
    .await
    .expect("task");
    sqlx::query("UPDATE tasks.standalone_review_tasks SET stage='review' WHERE id=$1")
        .bind(task)
        .execute(database.pool())
        .await
        .expect("review stage");
    let turn = Uuid::now_v7();
    let mut submission: Value = serde_json::from_str(include_str!(
        "../../../contracts/fixtures/exchange.http.v1/operator_submit.json"
    ))
    .expect("v2 fixture");
    submission["attempt_id"] = json!(turn);
    submission["task_id"] = json!(task);
    submission["grant"]["grant_id"] = json!(turn.to_string());
    submission["grant"]["purpose"] = json!("task_contributed");
    submission["grant"]["payer"] = json!({"id":7,"login":"writer"});
    submission["grant"]["credential"] = json!({"route":"openai-api-key","entry_id":"entry",
        "record_id":"record","version":1,"custody":"secret_store"});
    submission["models"] = json!(["gpt-test"]);
    submission["grant"]["models"] = json!(["gpt-test"]);
    submission["payload"]["request"]["model"] = json!("gpt-test");
    submission["payload"]["request"]["admission"] = json!({"grant_id":"grant"});
    sqlx::query(
        "INSERT INTO tasks.attempts \
        (id,standalone_task_id,operation,expected_revision,staffing_group,fork,config_sha, \
         submit_body,initial_deadline,deadline,unbounded_running,status) \
        VALUES ($1,$2,'standalone_review',1,'reviewer','acme/demo',$3,$4, \
          now()+interval '1 hour',now()+interval '1 hour',false,'running')",
    )
    .bind(turn)
    .bind(task)
    .bind("a".repeat(40))
    .bind(serde_json::to_vec(&submission).expect("submission"))
    .execute(database.pool())
    .await
    .expect("attempt");
    sqlx::query(
        "INSERT INTO tasks.standalone_review_attempts \
        (attempt_id,task_id,round,reviewer_id) VALUES ($1,$2,1,'reviewer')",
    )
    .bind(turn)
    .bind(task)
    .execute(database.pool())
    .await
    .expect("seat");
    sqlx::query(
        "INSERT INTO tasks.standalone_review_funding_grants \
        (attempt_id,task_id,purpose,payer_id,provider,slot_epoch,slot_grant_id, \
         credential_record_id,credential_version,route) \
        VALUES ($1,$2,'contributed',7,'openai',1,'grant','record',1,'openai-api-key')",
    )
    .bind(turn)
    .bind(task)
    .execute(database.pool())
    .await
    .expect("grant");
    let observation: TurnObservation = serde_json::from_value(json!({
        "turn":turn,"status":"running","created":1.0,
        "provider_failure":{"grant_id":turn.to_string(),"record_id":"record","version":1,
            "model":"gpt-test","category":"auth","request_id":Uuid::now_v7(),"retry_after":null}
    }))
    .expect("trusted failure");
    let mut work = database.begin().await.expect("observation transaction");
    assert!(
        record_observation(&mut work, &outbox, &observation, None)
            .await
            .expect("record failure")
    );
    work.commit().await.expect("observation commit");
    let seat: String = sqlx::query_scalar(
        "SELECT status FROM tasks.standalone_review_attempts WHERE attempt_id=$1",
    )
    .bind(turn)
    .fetch_one(database.pool())
    .await
    .expect("seat status");
    assert_eq!(seat, "retrying");
    let exclusions =
        excluded_credential_versions(&database, task, 7, Provider::Openai, "gpt-test", Utc::now())
            .await
            .expect("excluded versions");
    assert_eq!(exclusions, vec![("record".to_owned(), 1)]);
}

/// An edit after enrollment withdraws the old sponsor without erasing the boundary snapshot.
#[tokio::test]
async fn standalone_funding_edit_preserves_historical_boundary() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("task schema");
    let boundary_at = Utc::now();
    let actor = Actor {
        id: 7,
        login: "writer".to_owned(),
    };
    let prior = StandaloneReviewCommand {
        id: Uuid::now_v7(),
        repository_id: 8,
        upstream: "acme/demo".to_owned(),
        pr_number: 4,
        comment_id: 120,
        source: "120:old".to_owned(),
        source_at: boundary_at - Duration::minutes(1),
        actor: actor.clone(),
        kind: Some(RequestKind::Later(LaterCommand::Sponsor {
            provider: Some("openai".to_owned()),
        })),
        body: "/concurro sponsor openai".to_owned(),
        deleted: false,
    };
    let edited = StandaloneReviewCommand {
        id: Uuid::now_v7(),
        repository_id: 8,
        upstream: "acme/demo".to_owned(),
        pr_number: 4,
        comment_id: 120,
        source: "120:removed".to_owned(),
        source_at: boundary_at + Duration::minutes(1),
        actor: actor.clone(),
        kind: None,
        body: "removed".to_owned(),
        deleted: false,
    };
    for command in [&prior, &edited] {
        sqlx::query(
            "INSERT INTO tasks.standalone_review_commands \
            (id,repository_id,upstream,pr_number,comment_id,source,source_at,actor, \
             comment_author_id,kind,body,deleted,status) \
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,7,$9,$10,false,'applied')",
        )
        .bind(command.id)
        .bind(command.repository_id)
        .bind(&command.upstream)
        .bind(command.pr_number)
        .bind(command.comment_id)
        .bind(&command.source)
        .bind(command.source_at)
        .bind(sqlx::types::Json(&command.actor))
        .bind(command.kind.as_ref().map(sqlx::types::Json))
        .bind(&command.body)
        .execute(database.pool())
        .await
        .expect("command source");
        let mut work = database.begin().await.expect("funding transaction");
        record_standalone_funding_revision(&mut work, command, command.id == prior.id)
            .await
            .expect("funding revision");
        work.commit().await.expect("funding commit");
    }
    let boundary = SourceOrder {
        at: boundary_at,
        source_id: 123,
        source: "123:review_requested".to_owned(),
        index: 0,
    };
    let mut read = database.begin().await.expect("boundary read");
    let (_, facts) = standalone_consent_sources(&mut read, 8, 4, &boundary)
        .await
        .expect("historical facts");
    read.commit().await.expect("boundary commit");
    assert_eq!(facts.len(), 2);
    assert!(
        facts
            .iter()
            .any(|fact| matches!(fact.action, ConsentAction::Sponsor))
    );
    assert!(
        facts
            .iter()
            .any(|fact| matches!(fact.action, ConsentAction::Withdraw { .. }))
    );
    let available = BTreeMap::from([
        (Provider::Openai, Some(false)),
        (Provider::Anthropic, Some(false)),
    ]);
    let initial = initialize_consent(
        &facts,
        &boundary,
        &Actor {
            id: 9,
            login: "requester".to_owned(),
        },
        &available,
    )
    .expect("boundary consent");
    std::assert_matches!(initial.projection.providers[&Provider::Openai].slot,
        SponsorSlot::Active {ref sponsor,..} if sponsor.id==7);
    std::assert_matches!(
        project_consent(&facts).providers[&Provider::Openai].slot,
        SponsorSlot::Withdrawn { .. }
    );
}

/// A removal during an in-flight review closes only after the exact native write settles.
#[tokio::test]
async fn standalone_publication_finishes_cancelled_round_after_readback() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("task schema");
    let requested = StandaloneReviewRequest {
        id: Uuid::now_v7(),
        repository_id: 8,
        upstream: "acme/demo".to_owned(),
        pr_number: 42,
        source_id: 10,
        source_at: Utc::now(),
        actor: Actor {
            id: 7,
            login: "writer".to_owned(),
        },
        action: "review_requested".to_owned(),
    };
    let mut work = database.begin().await.expect("request transaction");
    record_standalone_review_request(&mut work, &requested)
        .await
        .expect("request");
    apply_standalone_review_request(
        &mut work,
        requested.id,
        StandaloneReviewAuthority {
            upstream: &requested.upstream,
            pr_number: requested.pr_number,
            reviewer_id: 99,
            runner_repository_id: 8,
            actor_writer: true,
            pr_open: true,
            reviewer_requested: true,
            source_defect: None,
            snapshot: Some(&json!({"head":"a".repeat(40),"panel":[{"id":"reviewer"}]})),
        },
    )
    .await
    .expect("round");
    work.commit().await.expect("request commit");
    let task: Uuid = sqlx::query_scalar(
        "SELECT id FROM tasks.standalone_review_tasks WHERE upstream=$1 AND pr_number=$2",
    )
    .bind(&requested.upstream)
    .bind(requested.pr_number)
    .fetch_one(database.pool())
    .await
    .expect("task");
    let turn = Uuid::now_v7();
    sqlx::query(
        "INSERT INTO tasks.attempts \
         (id,standalone_task_id,operation,expected_revision,staffing_group,fork,config_sha, \
          submit_body,initial_deadline,deadline,unbounded_running,status) \
         VALUES ($1,$2,'standalone_review',1,'standalone-test','acme/demo',$3, \
                 $4,now()+interval '1 hour',now()+interval '1 hour',false,'finished')",
    )
    .bind(turn)
    .bind(task)
    .bind("a".repeat(40))
    .bind(b"{}".as_slice())
    .execute(database.pool())
    .await
    .expect("attempt");
    sqlx::query(
        "INSERT INTO tasks.standalone_review_attempts \
         (attempt_id,task_id,round,reviewer_id,status) \
         VALUES ($1,$2,1,'reviewer','validated')",
    )
    .bind(turn)
    .bind(task)
    .execute(database.pool())
    .await
    .expect("review seat");
    sqlx::query("UPDATE tasks.standalone_review_tasks SET stage='review' WHERE id=$1")
        .bind(task)
        .execute(database.pool())
        .await
        .expect("active round");
    sqlx::query(
        "INSERT INTO tasks.standalone_review_publications \
         (turn_id,task_id,round,reviewer_id,pr_number,head,marker,body,status,reviewer_user_id) \
         VALUES ($1,$2,1,'reviewer',42,$3,'marker','marker body','sent',99)",
    )
    .bind(turn)
    .bind(task)
    .bind("a".repeat(40))
    .execute(database.pool())
    .await
    .expect("publication");
    let removed = StandaloneReviewRequest {
        id: Uuid::now_v7(),
        source_id: 11,
        source_at: requested.source_at + Duration::seconds(1),
        action: "review_request_removed".to_owned(),
        ..requested
    };
    let mut work = database.begin().await.expect("remove transaction");
    record_standalone_review_request(&mut work, &removed)
        .await
        .expect("remove");
    apply_standalone_review_request(
        &mut work,
        removed.id,
        StandaloneReviewAuthority {
            upstream: &removed.upstream,
            pr_number: removed.pr_number,
            reviewer_id: 99,
            runner_repository_id: 8,
            actor_writer: true,
            pr_open: true,
            reviewer_requested: false,
            source_defect: None,
            snapshot: None,
        },
    )
    .await
    .expect("cancel");
    work.commit().await.expect("remove commit");
    let mut work = database.begin().await.expect("confirmation transaction");
    assert!(
        confirm_standalone_review_publication(&mut work, turn, 300, &[])
            .await
            .expect("confirm")
    );
    work.commit().await.expect("confirmation commit");
    let state = standalone_review_task(&database, "acme/demo", 42)
        .await
        .expect("task state")
        .expect("task");
    assert_eq!(state["stage"], "cancelled");
    let (seat, publication, round): (String, String, String) = sqlx::query_as(
        "SELECT a.status,p.status,r.status \
         FROM tasks.standalone_review_attempts a \
         JOIN tasks.standalone_review_publications p ON p.turn_id=a.attempt_id \
         JOIN tasks.standalone_review_rounds r ON r.task_id=a.task_id AND r.number=a.round \
         WHERE a.attempt_id=$1",
    )
    .bind(turn)
    .fetch_one(database.pool())
    .await
    .expect("settled states");
    assert_eq!(
        (seat.as_str(), publication.as_str(), round.as_str()),
        ("published", "confirmed", "cancelled")
    );
}

/// Review requests join an unfinished round, reopen a completed one, and respect PR ownership.
#[tokio::test]
async fn standalone_review_sources_open_join_and_cancel_owned_rounds() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("task schema");
    let source_at = Utc::now() - Duration::minutes(5);
    let first = StandaloneReviewRequest {
        id: Uuid::now_v7(),
        repository_id: 8,
        upstream: "acme/demo".to_owned(),
        pr_number: 42,
        source_id: 10,
        source_at,
        actor: Actor {
            id: 7,
            login: "writer".to_owned(),
        },
        action: "review_requested".to_owned(),
    };
    let mut work = database.begin().await.expect("record request");
    record_standalone_review_request(&mut work, &first)
        .await
        .expect("record first source");
    record_standalone_review_request(&mut work, &first)
        .await
        .expect("duplicate source");
    work.commit().await.expect("source commit");
    let snapshot = json!({"head":"a".repeat(40),"base":"b".repeat(40),"panel":[{"id":"reviewer","provider":"openai","model":"test","roles":["pr_review"]}]});
    let mut work = database.begin().await.expect("open round");
    assert!(
        apply_standalone_review_request(
            &mut work,
            first.id,
            StandaloneReviewAuthority {
                upstream: &first.upstream,
                pr_number: 42,
                reviewer_id: 99,
                runner_repository_id: 8,
                actor_writer: true,
                pr_open: true,
                reviewer_requested: true,
                source_defect: None,
                snapshot: Some(&snapshot)
            },
        )
        .await
        .expect("apply request")
    );
    work.commit().await.expect("round commit");
    let current = standalone_review_task(&database, &first.upstream, 42)
        .await
        .expect("task")
        .expect("standalone task");
    assert_eq!(current["stage"], "queued");
    assert_eq!(current["round"], 1);
    let task_id = current["id"].as_str().expect("task ID");
    let owner: (String, Uuid) = sqlx::query_as(
        "SELECT owner_kind,owner_id FROM tasks.pr_ownership WHERE upstream=$1 AND pr_number=42",
    )
    .bind(&first.upstream)
    .fetch_one(database.pool())
    .await
    .expect("PR ownership");
    assert_eq!(owner.0, "standalone_pr_review");
    assert_eq!(owner.1.to_string(), task_id);
    let turn = Uuid::now_v7();
    let session = Uuid::now_v7();
    sqlx::query(
        "INSERT INTO tasks.attempts \
         (id,standalone_task_id,operation,expected_revision,staffing_group,fork,config_sha, \
          submit_body,initial_deadline,deadline,unbounded_running,status) \
         VALUES ($1,$2,'standalone_review',1,'standalone-test','acme/demo',$3, \
                 $4,now()+interval '1 hour',now()+interval '1 hour',false,'queued')",
    )
    .bind(turn)
    .bind(owner.1)
    .bind("a".repeat(40))
    .bind(b"{}".as_slice())
    .execute(database.pool())
    .await
    .expect("standalone attempt owner");
    let mut work = database.begin().await.expect("runner session");
    record_runner_session(&mut work, turn, session)
        .await
        .expect("runner target");
    work.commit().await.expect("runner commit");
    let runner_repository_id: i64 =
        sqlx::query_scalar("SELECT repository_id FROM tasks.runner_sessions WHERE session_id=$1")
            .bind(session)
            .fetch_one(database.pool())
            .await
            .expect("runner repository");
    assert_eq!(runner_repository_id, 8);

    let joined = StandaloneReviewRequest {
        id: Uuid::now_v7(),
        source_id: 15,
        source_at: source_at + Duration::seconds(1),
        ..first.clone()
    };
    let mut work = database.begin().await.expect("record join");
    record_standalone_review_request(&mut work, &joined)
        .await
        .expect("record join");
    work.commit().await.expect("join source commit");
    let mut work = database.begin().await.expect("join round");
    apply_standalone_review_request(
        &mut work,
        joined.id,
        StandaloneReviewAuthority {
            upstream: &joined.upstream,
            pr_number: 42,
            reviewer_id: 99,
            runner_repository_id: 8,
            actor_writer: true,
            pr_open: true,
            reviewer_requested: true,
            source_defect: None,
            snapshot: None,
        },
    )
    .await
    .expect("join request");
    work.commit().await.expect("join commit");
    let joined_round: i32 =
        sqlx::query_scalar("SELECT round FROM tasks.standalone_review_requests WHERE id=$1")
            .bind(joined.id)
            .fetch_one(database.pool())
            .await
            .expect("joined round");
    assert_eq!(joined_round, 1);

    sqlx::query("UPDATE tasks.standalone_review_tasks SET stage='complete' WHERE id=$1")
        .bind(owner.1)
        .execute(database.pool())
        .await
        .expect("finish round");
    let again = StandaloneReviewRequest {
        id: Uuid::now_v7(),
        source_id: 20,
        source_at: source_at + Duration::seconds(2),
        ..first.clone()
    };
    let mut work = database.begin().await.expect("record new round");
    record_standalone_review_request(&mut work, &again)
        .await
        .expect("record next request");
    work.commit().await.expect("new round source commit");
    let mut work = database.begin().await.expect("open new round");
    apply_standalone_review_request(
        &mut work,
        again.id,
        StandaloneReviewAuthority {
            upstream: &again.upstream,
            pr_number: 42,
            reviewer_id: 99,
            runner_repository_id: 8,
            actor_writer: true,
            pr_open: true,
            reviewer_requested: true,
            source_defect: None,
            snapshot: Some(&snapshot),
        },
    )
    .await
    .expect("new round");
    work.commit().await.expect("new round commit");
    let current = standalone_review_task(&database, &first.upstream, 42)
        .await
        .expect("task")
        .expect("standalone task");
    assert_eq!(current["round"], 2);
    assert_eq!(current["id"], task_id);

    let mut work = database.begin().await.expect("close PR");
    assert!(
        observe_standalone_review_pr_state(&mut work, owner.1, false)
            .await
            .expect("pause closed PR")
    );
    work.commit().await.expect("close commit");

    let removed = StandaloneReviewRequest {
        id: Uuid::now_v7(),
        source_id: 30,
        source_at: source_at + Duration::seconds(3),
        action: "review_request_removed".to_owned(),
        ..first.clone()
    };
    let mut work = database.begin().await.expect("record removal");
    record_standalone_review_request(&mut work, &removed)
        .await
        .expect("record removal");
    work.commit().await.expect("removal source commit");
    let mut work = database.begin().await.expect("cancel round");
    apply_standalone_review_request(
        &mut work,
        removed.id,
        StandaloneReviewAuthority {
            upstream: &removed.upstream,
            pr_number: 42,
            reviewer_id: 99,
            runner_repository_id: 8,
            actor_writer: false,
            pr_open: true,
            reviewer_requested: false,
            source_defect: None,
            snapshot: None,
        },
    )
    .await
    .expect("remove request");
    work.commit().await.expect("cancel commit");
    assert_eq!(
        standalone_review_task(&database, &first.upstream, 42)
            .await
            .expect("task")
            .expect("task")["stage"],
        "closed"
    );
    let cancellation: (Uuid, String) = sqlx::query_as(
        "SELECT cancellation_request_id,status FROM tasks.standalone_review_rounds \
         WHERE task_id=$1 AND number=2",
    )
    .bind(owner.1)
    .fetch_one(database.pool())
    .await
    .expect("round cancellation");
    assert_eq!(cancellation, (removed.id, "closed".to_owned()));
    let mut work = database.begin().await.expect("reopen PR");
    assert!(
        observe_standalone_review_pr_state(&mut work, owner.1, true)
            .await
            .expect("reopen cancelled PR")
    );
    work.commit().await.expect("reopen commit");
    assert_eq!(
        standalone_review_task(&database, &first.upstream, 42)
            .await
            .expect("task")
            .expect("task")["stage"],
        "cancelled"
    );
    let round_status: String = sqlx::query_scalar(
        "SELECT status FROM tasks.standalone_review_rounds WHERE task_id=$1 AND number=2",
    )
    .bind(owner.1)
    .fetch_one(database.pool())
    .await
    .expect("reopened round");
    assert_eq!(round_status, "cancelled");
    let old = StandaloneReviewRequest {
        id: Uuid::now_v7(),
        source_id: 12,
        source_at: source_at + Duration::milliseconds(500),
        ..first.clone()
    };
    let mut work = database.begin().await.expect("late source");
    record_standalone_review_request(&mut work, &old)
        .await
        .expect("record late source");
    work.commit().await.expect("late source commit");
    let mut work = database.begin().await.expect("supersede late source");
    apply_standalone_review_request(
        &mut work,
        old.id,
        StandaloneReviewAuthority {
            upstream: &old.upstream,
            pr_number: 42,
            reviewer_id: 99,
            runner_repository_id: 8,
            actor_writer: true,
            pr_open: true,
            reviewer_requested: true,
            source_defect: None,
            snapshot: Some(&snapshot),
        },
    )
    .await
    .expect("late request");
    work.commit().await.expect("late decision commit");
    let status: String =
        sqlx::query_scalar("SELECT status FROM tasks.standalone_review_requests WHERE id=$1")
            .bind(old.id)
            .fetch_one(database.pool())
            .await
            .expect("late disposition");
    assert_eq!(status, "superseded");

    let rerequest = StandaloneReviewRequest {
        id: Uuid::now_v7(),
        source_id: 35,
        source_at: source_at + Duration::milliseconds(3500),
        ..first.clone()
    };
    let mut work = database
        .begin()
        .await
        .expect("record request after cancellation");
    record_standalone_review_request(&mut work, &rerequest)
        .await
        .expect("record new request");
    work.commit().await.expect("new request source commit");
    let mut work = database
        .begin()
        .await
        .expect("open round after cancellation");
    assert!(
        apply_standalone_review_request(
            &mut work,
            rerequest.id,
            StandaloneReviewAuthority {
                upstream: &rerequest.upstream,
                pr_number: 42,
                reviewer_id: 99,
                runner_repository_id: 8,
                actor_writer: true,
                pr_open: true,
                reviewer_requested: true,
                source_defect: None,
                snapshot: Some(&snapshot)
            },
        )
        .await
        .expect("apply request after cancellation")
    );
    work.commit().await.expect("new round commit");
    let reopened = standalone_review_task(&database, &first.upstream, 42)
        .await
        .expect("task")
        .expect("standalone task");
    assert_eq!(reopened["round"], 3);
    assert_eq!(reopened["stage"], "queued");

    sqlx::query("INSERT INTO tasks.pr_ownership (upstream,pr_number,owner_kind,owner_id) VALUES ('acme/demo',43,'contribution',$1)")
        .bind(Uuid::now_v7())
        .execute(database.pool())
        .await
        .expect("contribution owner");
    let owned = StandaloneReviewRequest {
        id: Uuid::now_v7(),
        pr_number: 43,
        source_id: 40,
        source_at: source_at + Duration::seconds(4),
        ..first
    };
    let mut work = database.begin().await.expect("record owned PR request");
    record_standalone_review_request(&mut work, &owned)
        .await
        .expect("record owned request");
    work.commit().await.expect("owned source commit");
    let mut work = database.begin().await.expect("refuse owned PR");
    apply_standalone_review_request(
        &mut work,
        owned.id,
        StandaloneReviewAuthority {
            upstream: &owned.upstream,
            pr_number: 43,
            reviewer_id: 99,
            runner_repository_id: 8,
            actor_writer: true,
            pr_open: true,
            reviewer_requested: true,
            source_defect: None,
            snapshot: Some(&snapshot),
        },
    )
    .await
    .expect("owned decision");
    work.commit().await.expect("owned decision commit");
    let reason: String =
        sqlx::query_scalar("SELECT reason FROM tasks.standalone_review_requests WHERE id=$1")
            .bind(owned.id)
            .fetch_one(database.pool())
            .await
            .expect("ownership refusal");
    assert_eq!(reason, "pr_owned_elsewhere");

    let batch = [
        StandaloneReviewRequest {
            id: Uuid::now_v7(),
            pr_number: 44,
            source_id: 50,
            source_at: source_at + Duration::seconds(5),
            action: "review_requested".to_owned(),
            ..owned.clone()
        },
        StandaloneReviewRequest {
            id: Uuid::now_v7(),
            pr_number: 44,
            source_id: 51,
            source_at: source_at + Duration::seconds(6),
            action: "review_request_removed".to_owned(),
            ..owned.clone()
        },
        StandaloneReviewRequest {
            id: Uuid::now_v7(),
            pr_number: 44,
            source_id: 52,
            source_at: source_at + Duration::seconds(7),
            action: "review_requested".to_owned(),
            ..owned
        },
    ];
    let mut work = database.begin().await.expect("record batched sources");
    for request in &batch {
        record_standalone_review_request(&mut work, request)
            .await
            .expect("batch source");
    }
    work.commit().await.expect("batch source commit");
    assert!(
        !standalone_review_source_is_current(&database, &batch[0])
            .await
            .expect("older batch source")
    );
    assert!(
        standalone_review_source_is_current(&database, &batch[2])
            .await
            .expect("latest batch source")
    );
    for request in &batch {
        let mut work = database.begin().await.expect("apply batch source");
        apply_standalone_review_request(
            &mut work,
            request.id,
            StandaloneReviewAuthority {
                upstream: &request.upstream,
                pr_number: request.pr_number,
                reviewer_id: 99,
                runner_repository_id: 8,
                actor_writer: true,
                pr_open: true,
                reviewer_requested: true,
                source_defect: None,
                snapshot: Some(&snapshot),
            },
        )
        .await
        .expect("batch source decision");
        work.commit().await.expect("batch decision commit");
    }
    let dispositions: Vec<(i64, String)> = sqlx::query_as(
        "SELECT source_id,status FROM tasks.standalone_review_requests \
         WHERE upstream='acme/demo' AND pr_number=44 ORDER BY source_id",
    )
    .fetch_all(database.pool())
    .await
    .expect("batch dispositions");
    assert_eq!(
        dispositions,
        vec![
            (50, "superseded".to_owned()),
            (51, "superseded".to_owned()),
            (52, "applied".to_owned()),
        ]
    );
    assert_eq!(
        standalone_review_task(&database, "acme/demo", 44)
            .await
            .expect("batch task")
            .expect("latest request task")["round"],
        1
    );
}

/// A later assessment answers an earlier finding and queues its native thread reply.
#[tokio::test]
async fn standalone_second_round_records_own_finding_disposition() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("task schema");
    let request = StandaloneReviewRequest {
        id: Uuid::now_v7(),
        repository_id: 8,
        upstream: "acme/demo".to_owned(),
        pr_number: 42,
        source_id: 20,
        source_at: Utc::now(),
        actor: Actor {
            id: 7,
            login: "writer".to_owned(),
        },
        action: "review_requested".to_owned(),
    };
    let snapshot = json!({
        "head":"a".repeat(40),
        "panel":[{"id":"reviewer","provider":"openai","model":"test","roles":["pr_review"]}],
        "changed_paths":["src/lib.rs"]
    });
    let mut work = database.begin().await.expect("source transaction");
    record_standalone_review_request(&mut work, &request)
        .await
        .expect("source");
    apply_standalone_review_request(
        &mut work,
        request.id,
        StandaloneReviewAuthority {
            upstream: &request.upstream,
            pr_number: request.pr_number,
            reviewer_id: 99,
            runner_repository_id: 8,
            actor_writer: true,
            pr_open: true,
            reviewer_requested: true,
            source_defect: None,
            snapshot: Some(&snapshot),
        },
    )
    .await
    .expect("first round");
    work.commit().await.expect("source commit");
    let task: Uuid = sqlx::query_scalar(
        "SELECT id FROM tasks.standalone_review_tasks WHERE upstream=$1 AND pr_number=$2",
    )
    .bind(&request.upstream)
    .bind(request.pr_number)
    .fetch_one(database.pool())
    .await
    .expect("task");
    sqlx::query(
        "INSERT INTO tasks.standalone_review_findings \
         (task_id,sequence,id,reviewer_id,status,head,finding,thread_node_id) \
         VALUES ($1,1,'F1','reviewer','open',$2,$3,'PRRT_500')",
    )
    .bind(task)
    .bind("a".repeat(40))
    .bind(sqlx::types::Json(json!({
        "path":"src/lib.rs","start_line":null,"line":1,"detail":"Handle empty input"
    })))
    .execute(database.pool())
    .await
    .expect("earlier finding");
    let request2 = StandaloneReviewRequest {
        id: Uuid::now_v7(),
        source_id: 21,
        source_at: request.source_at + Duration::seconds(1),
        ..request
    };
    sqlx::query("UPDATE tasks.standalone_review_tasks SET stage='complete' WHERE id=$1")
        .bind(task)
        .execute(database.pool())
        .await
        .expect("finish first round");
    let mut work = database.begin().await.expect("second source transaction");
    record_standalone_review_request(&mut work, &request2)
        .await
        .expect("second source");
    apply_standalone_review_request(
        &mut work,
        request2.id,
        StandaloneReviewAuthority {
            upstream: &request2.upstream,
            pr_number: request2.pr_number,
            reviewer_id: 99,
            runner_repository_id: 8,
            actor_writer: true,
            pr_open: true,
            reviewer_requested: true,
            source_defect: None,
            snapshot: Some(&snapshot),
        },
    )
    .await
    .expect("second round");
    work.commit().await.expect("second source commit");
    let (owned, own_findings) = standalone_review_outstanding_findings(&database, task, "reviewer")
        .await
        .expect("own finding context");
    assert_eq!(owned, vec!["F1"]);
    assert_eq!(own_findings[0]["status"], "open");
    assert_eq!(own_findings[0]["detail"], "Handle empty input");
    let turn = Uuid::now_v7();
    let submission = TurnSubmission {
        turn,
        expected: json!({}),
        models: vec![],
        budget: 1,
        lease: 1,
        payload: json!({"request":{"context":{"reviewer_identity":snapshot["panel"][0]}}}),
        pin: None,
        binding: json!({}),
    };
    let report = json!({
        "verdict":"approve","summary":"Earlier issue no longer applies",
        "findings":[],"dispositions":[{"id":"F1","status":"obsolete",
            "evidence":"The affected path was removed"}],"design_issue":null
    });
    sqlx::query(
        "INSERT INTO tasks.attempts \
         (id,standalone_task_id,operation,expected_revision,staffing_group,fork,config_sha, \
          submit_body,initial_deadline,deadline,unbounded_running,status) \
         VALUES ($1,$2,'standalone_review',1,'standalone-second','acme/demo',$3, \
                 $4,now()+interval '1 hour',now()+interval '1 hour',false,'finished')",
    )
    .bind(turn)
    .bind(task)
    .bind("a".repeat(40))
    .bind(serde_json::to_vec(&submission).expect("submission JSON"))
    .execute(database.pool())
    .await
    .expect("second attempt");
    sqlx::query(
        "INSERT INTO tasks.standalone_review_attempts \
         (attempt_id,task_id,round,reviewer_id,status,report,report_raw) \
         VALUES ($1,$2,2,'reviewer','completed',$3,$4)",
    )
    .bind(turn)
    .bind(task)
    .bind(sqlx::types::Json(&report))
    .bind(serde_json::to_vec(&report).expect("report JSON"))
    .execute(database.pool())
    .await
    .expect("second report");
    sqlx::query("UPDATE tasks.standalone_review_tasks SET stage='review' WHERE id=$1")
        .bind(task)
        .execute(database.pool())
        .await
        .expect("review stage");
    let mut adopted = snapshot.clone();
    adopted["panel"] = json!([
        {"id":"reviewer-b","provider":"openai","model":"test","roles":["pr_review"]},
        {"id":"reviewer-c","provider":"openai","model":"test","roles":["pr_review"]}
    ]);
    sqlx::query(
        "UPDATE tasks.standalone_review_rounds SET snapshot=$3 \
         WHERE task_id=$1 AND number=$2",
    )
    .bind(task)
    .bind(2)
    .bind(sqlx::types::Json(&adopted))
    .execute(database.pool())
    .await
    .expect("adopt new panel while old report is in flight");
    let pending = standalone_review_result(&database, turn)
        .await
        .expect("pending result")
        .expect("second result");
    assert_eq!(pending.reviewer.id, "reviewer");
    let validated =
        validate_standalone_pr_review(pending.report, &owned).expect("second-round disposition");
    let mut work = database.begin().await.expect("result transaction");
    assert!(
        apply_standalone_review_result(&mut work, turn, Ok(validated))
            .await
            .expect("record disposition")
    );
    work.commit().await.expect("result commit");
    let status: String = sqlx::query_scalar(
        "SELECT status FROM tasks.standalone_review_findings WHERE task_id=$1 AND id='F1'",
    )
    .bind(task)
    .fetch_one(database.pool())
    .await
    .expect("disposition state");
    assert_eq!(status, "obsolete");
    let reply: String = sqlx::query_scalar(
        "SELECT body FROM tasks.standalone_review_dispositions \
         WHERE turn_id=$1 AND finding_id='F1'",
    )
    .bind(turn)
    .fetch_one(database.pool())
    .await
    .expect("native reply intent");
    assert!(reply.contains("The affected path was removed"));
    sqlx::query(
        "UPDATE tasks.standalone_review_attempts SET status='published' WHERE attempt_id=$1",
    )
    .bind(turn)
    .execute(database.pool())
    .await
    .expect("old seat publication");
    for reviewer in ["reviewer-b", "reviewer-c"] {
        let seat_turn = Uuid::now_v7();
        sqlx::query(
            "INSERT INTO tasks.attempts \
             (id,standalone_task_id,operation,expected_revision,staffing_group,fork,config_sha, \
              submit_body,initial_deadline,deadline,unbounded_running,status) \
             VALUES ($1,$2,'standalone_review',1,$3,'acme/demo',$4, \
                     $5,now()+interval '1 hour',now()+interval '1 hour',false,'finished')",
        )
        .bind(seat_turn)
        .bind(task)
        .bind(format!("standalone-{reviewer}"))
        .bind("a".repeat(40))
        .bind(b"{}".as_slice())
        .execute(database.pool())
        .await
        .expect("retained current seat");
        sqlx::query(
            "INSERT INTO tasks.standalone_review_attempts \
             (attempt_id,task_id,round,reviewer_id,status) \
             VALUES ($1,$2,2,$3,'published')",
        )
        .bind(seat_turn)
        .bind(task)
        .bind(reviewer)
        .execute(database.pool())
        .await
        .expect("current seat publication");
    }
    sqlx::query("UPDATE tasks.standalone_review_tasks SET stage='review' WHERE id=$1")
        .bind(task)
        .execute(database.pool())
        .await
        .expect("current panel review");
    let mut work = database.begin().await.expect("panel settlement");
    observe_standalone_review_pr_state(&mut work, task, false)
        .await
        .expect("close PR");
    observe_standalone_review_pr_state(&mut work, task, true)
        .await
        .expect("reopen PR");
    work.commit().await.expect("panel settlement commit");
    let stage: String =
        sqlx::query_scalar("SELECT stage FROM tasks.standalone_review_tasks WHERE id=$1")
            .bind(task)
            .fetch_one(database.pool())
            .await
            .expect("current panel outcome");
    assert_eq!(stage, "complete");
}
