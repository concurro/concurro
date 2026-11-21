//! Turn event transport through the task stream and a restarted consumer.

use std::time::Duration;

use async_nats::{
    HeaderMap,
    jetstream::{self, stream},
};
use axum::{Router, http::StatusCode};
use concurro_contracts::{Envelope, TaskId, TurnFinished, TurnStatus, decode};
use concurro_db::{Database, UnitOfWork};
use concurro_events::{Handler, Outcome};
use concurro_events_nats::{
    ConsumerStream, Forwarder, ensure_pull_consumer, ensure_stream, run_consumer,
};
use concurro_events_postgres::{ForwarderLease, Outbox, migrate as migrate_events};
use concurro_exchange_v1::{Operator, TurnObservation};
use concurro_tasks::run_exchange_poll;
use concurro_tasks_core::{Actor, IssueKey, Stage, TaskState};
use concurro_tasks_postgres::{migrate, record_observation};
use concurro_testkit::{NatsContainer, PostgresContainer};
use serde_json::json;
use sqlx::types::Json;
use uuid::Uuid;

/// Count only committed finished-turn effects, letting the shared receipt deduplicate.
struct FinishedHandler;

impl Handler<TurnFinished> for FinishedHandler {
    async fn handle(&self, uow: &mut UnitOfWork<'_>, event: Envelope<TurnFinished>) -> Outcome {
        sqlx::query("INSERT INTO applied_turns (turn_id) VALUES ($1)")
            .bind(event.data.turn_id)
            .execute(uow.connection())
            .await
            .expect("consumer effect");
        Outcome::Done
    }
}

#[tokio::test]
async fn finished_fact_crosses_tasks_stream_once_after_consumer_restart() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let nats = NatsContainer::start().await.expect("NATS");
    let database = Database::connect(postgres.url(), 4)
        .await
        .expect("database");
    migrate(&database).await.expect("tasks schema");
    migrate_events(&database).await.expect("event schema");
    sqlx::query("CREATE TABLE applied_turns (turn_id uuid NOT NULL)")
        .execute(database.pool())
        .await
        .expect("consumer table");
    let outbox = Outbox::new(1024 * 1024, vec!["turn.>".to_owned()]);
    let task_id = Uuid::now_v7();
    let turn = Uuid::now_v7();
    let issue = IssueKey {
        repository_id: 8,
        number: 12,
    };
    let state = TaskState {
        id: TaskId(task_id),
        issue,
        author: Actor {
            id: 7,
            login: "writer".to_owned(),
        },
        stage: Stage::Plan,
        revision: 1,
        stage_reason: None,
        retry_stage: None,
        pins: None,
        used_model_calls: 1,
        current_attempt: None,
    };
    sqlx::query("INSERT INTO tasks.issue_controls (repository_id,issue_number) VALUES (8,12)")
        .execute(database.pool())
        .await
        .expect("issue");
    sqlx::query(
        "INSERT INTO tasks.task_state (repository_id,issue_number,task_id,revision,state) \
         VALUES (8,12,$1,1,$2)",
    )
    .bind(task_id)
    .bind(Json(&state))
    .execute(database.pool())
    .await
    .expect("task");
    sqlx::query(
        "INSERT INTO tasks.attempts (id,task_id,operation,expected_revision,staffing_group, \
         fork,config_sha,submit_body,initial_deadline,unbounded_running,status) \
         VALUES ($1,$2,'intake',1,'group','acme/fork',$3,'{}',now()+interval '1 hour',false,'queued')",
    )
    .bind(turn)
    .bind(task_id)
    .bind("a".repeat(40))
    .execute(database.pool())
    .await
    .expect("attempt");
    let observation: TurnObservation = serde_json::from_value(json!({
        "turn":turn,"status":"done","created":1.0,"finished":2.0,
        "result":{"outcome":"ok","response":{"decision":"proceed"}}
    }))
    .expect("exchange observation");
    let mut work = database.begin().await.expect("result transaction");
    record_observation(&mut work, &outbox, &observation, None)
        .await
        .expect("record terminal turn");
    work.commit().await.expect("result commit");
    let raw: Vec<u8> = sqlx::query_scalar("SELECT wire_bytes FROM events.outbox WHERE type=$1")
        .bind(format!("turn.{turn}.finished"))
        .fetch_one(database.pool())
        .await
        .expect("finished fact");
    assert_eq!(
        decode::<TurnFinished>(&raw).expect("contract").data.turn_id,
        turn
    );

    let client = async_nats::connect(nats.url()).await.expect("NATS client");
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
            duplicate_window: Duration::from_secs(1),
            ..Default::default()
        },
    )
    .await
    .expect("task stream");
    let consumer = ensure_pull_consumer(
        &context,
        "TASKS",
        "finished-test",
        "turn.*.finished",
        Duration::from_millis(500),
        Duration::from_millis(20),
    )
    .await
    .expect("finished consumer");
    let mut lease = ForwarderLease::try_acquire(&database)
        .await
        .expect("lease")
        .expect("sole forwarder");
    let forwarder = Forwarder::new(client, Duration::from_millis(20));
    assert!(
        forwarder
            .forward_one(&mut lease)
            .await
            .expect("status publish")
    );
    assert!(
        forwarder
            .forward_one(&mut lease)
            .await
            .expect("finished publish")
    );

    let first = tokio::spawn({
        let context = context.clone();
        let database = database.clone();
        let consumer = consumer.clone();
        async move {
            run_consumer::<TurnFinished, _>(
                ConsumerStream {
                    pull: &consumer,
                    context: &context,
                    name: "TASKS",
                },
                &database,
                "finished-test",
                3,
                Duration::from_millis(20),
                &FinishedHandler,
            )
            .await
        }
    });
    tokio::time::timeout(Duration::from_secs(10), async {
        loop {
            let count: i64 = sqlx::query_scalar("SELECT count(*) FROM applied_turns")
                .fetch_one(database.pool())
                .await
                .expect("effect count");
            if count == 1 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .expect("first delivery");
    first.abort();
    let _ = first.await;

    tokio::time::sleep(Duration::from_millis(1200)).await;
    let mut headers = HeaderMap::new();
    let event = decode::<TurnFinished>(&raw).expect("event id");
    headers.insert("Nats-Msg-Id", format!("{}:{}", event.id.0, event.version));
    context
        .publish_with_headers(event.subject(), headers, raw.into())
        .await
        .expect("duplicate publish")
        .await
        .expect("broker acknowledgement");
    let mut observer = consumer.clone();
    tokio::time::timeout(Duration::from_secs(10), async {
        tokio::select! {
            result = run_consumer::<TurnFinished, _>(
                ConsumerStream { pull: &consumer, context: &context, name: "TASKS" },
                &database, "finished-test", 3, Duration::from_millis(20), &FinishedHandler
            ) => panic!("consumer ended: {result:?}"),
            () = async {
                loop {
                    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM events.consumed WHERE consumer='finished-test'")
                        .fetch_one(database.pool()).await.expect("receipt count");
                    if count == 1 && observer.info().await.expect("consumer info").ack_floor.stream_sequence >= 3 {
                        break;
                    }
                    tokio::time::sleep(Duration::from_millis(20)).await;
                }
            } => {}
        }
    }).await.expect("duplicate acknowledgement");
    let applied: i64 = sqlx::query_scalar("SELECT count(*) FROM applied_turns")
        .fetch_one(database.pool())
        .await
        .expect("applied count");
    assert_eq!(applied, 1);
    let receipts: i64 =
        sqlx::query_scalar("SELECT count(*) FROM events.consumed WHERE consumer='finished-test'")
            .fetch_one(database.pool())
            .await
            .expect("receipt count");
    assert_eq!(receipts, 1);
    let published: i64 =
        sqlx::query_scalar("SELECT count(*) FROM events.outbox WHERE published_at IS NOT NULL")
            .fetch_one(database.pool())
            .await
            .expect("published count");
    assert_eq!(published, 2);
}

/// A runner timeout remains uncertain and org funding has no inferred request evidence.
#[tokio::test]
async fn timeout_fact_keeps_org_funding_unknown_and_run_attempt_separate() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("tasks schema");
    migrate_events(&database).await.expect("event schema");
    let outbox = Outbox::new(1024 * 1024, vec!["turn.>".to_owned()]);
    let task_id = Uuid::now_v7();
    let turn = Uuid::now_v7();
    let session = Uuid::now_v7();
    let state = TaskState {
        id: TaskId(task_id),
        issue: IssueKey {
            repository_id: 8,
            number: 13,
        },
        author: Actor {
            id: 7,
            login: "writer".to_owned(),
        },
        stage: Stage::Plan,
        revision: 1,
        stage_reason: None,
        retry_stage: None,
        pins: None,
        used_model_calls: 1,
        current_attempt: None,
    };
    sqlx::query("INSERT INTO tasks.issue_controls (repository_id,issue_number) VALUES (8,13)")
        .execute(database.pool())
        .await
        .expect("issue");
    sqlx::query(
        "INSERT INTO tasks.task_state (repository_id,issue_number,task_id,revision,state) VALUES (8,13,$1,1,$2)",
    )
    .bind(task_id)
    .bind(Json(&state))
    .execute(database.pool())
    .await
    .expect("task");
    sqlx::query(
        "INSERT INTO tasks.attempts (id,task_id,operation,expected_revision,staffing_group, \
         fork,config_sha,submit_body,initial_deadline,unbounded_running,status) \
         VALUES ($1,$2,'intake',1,'group','acme/fork',$3,'{}',now()+interval '1 hour',false,'queued')",
    )
    .bind(turn)
    .bind(task_id)
    .bind("a".repeat(40))
    .execute(database.pool())
    .await
    .expect("attempt");
    let auth_listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("auth fixture bind");
    let auth_address = auth_listener.local_addr().expect("auth fixture address");
    let auth_server = tokio::spawn(async move {
        axum::serve(
            auth_listener,
            Router::new().fallback(|| async { StatusCode::UNAUTHORIZED }),
        )
        .await
    });
    let exchange = Operator::new(
        reqwest::Client::new(),
        format!("http://{auth_address}"),
        "token",
    );
    run_exchange_poll(&database, &exchange, &outbox, turn, 0)
        .await
        .expect("401 is a polling outage");
    auth_server.abort();
    let auth_listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("second auth fixture bind");
    let auth_address = auth_listener
        .local_addr()
        .expect("second auth fixture address");
    let auth_server = tokio::spawn(async move {
        axum::serve(
            auth_listener,
            Router::new().fallback(|| async { StatusCode::FORBIDDEN }),
        )
        .await
    });
    let exchange = Operator::new(
        reqwest::Client::new(),
        format!("http://{auth_address}"),
        "token",
    );
    run_exchange_poll(&database, &exchange, &outbox, turn, 1)
        .await
        .expect("403 is a polling outage");
    auth_server.abort();
    let poll: (String, i32, i64) = sqlx::query_as(
        "SELECT status,poll_failures,poll_generation FROM tasks.attempts WHERE id=$1",
    )
    .bind(turn)
    .fetch_one(database.pool())
    .await
    .expect("poll state");
    assert_eq!(poll, ("queued".to_owned(), 2, 2));
    let retirements: i64 =
        sqlx::query_scalar("SELECT count(*) FROM tasks.effects WHERE turn_id=$1 AND kind='retire'")
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("retirement count");
    assert_eq!(retirements, 0);
    for run_attempt in [1, 2] {
        let running: TurnObservation = serde_json::from_value(json!({
            "turn":turn,"status":"running","created":1.0,"started":1.5,
            "runner":{"session":session,"run_id":format!("123/{run_attempt}")}
        }))
        .expect("running observation");
        let mut work = database.begin().await.expect("running");
        record_observation(&mut work, &outbox, &running, None)
            .await
            .expect("record running identity");
        work.commit().await.expect("running commit");
    }
    let observation: TurnObservation = serde_json::from_value(json!({
        "turn":turn,"status":"done","created":1.0,"finished":2.0,
        "runner":{"session":session,"run_id":"123/2"},
        "result":{"outcome":"timed_out","provider_requests":"none","response":{"decision":"proceed"}}
    }))
    .expect("observation");
    let mut work = database.begin().await.expect("result");
    record_observation(&mut work, &outbox, &observation, None)
        .await
        .expect("record outcome");
    work.commit().await.expect("commit");
    let finished: Vec<u8> =
        sqlx::query_scalar("SELECT wire_bytes FROM events.outbox WHERE type=$1")
            .bind(format!("turn.{turn}.finished"))
            .fetch_one(database.pool())
            .await
            .expect("finished");
    let finished = decode::<TurnFinished>(&finished).expect("contract");
    assert_eq!(finished.data.outcome, "unknown");
    assert_eq!(finished.data.reason.as_deref(), Some("timed_out"));
    assert_eq!(finished.data.provider_requests, "unknown");
    assert!(!finished.data.funding_ended);
    let statuses: Vec<Vec<u8>> =
        sqlx::query_scalar("SELECT wire_bytes FROM events.outbox WHERE type=$1 ORDER BY sequence")
            .bind(format!("turn.{turn}.status"))
            .fetch_all(database.pool())
            .await
            .expect("status revisions");
    assert_eq!(statuses.len(), 3);
    let first = decode::<TurnStatus>(&statuses[0]).expect("first status");
    let second = decode::<TurnStatus>(&statuses[1]).expect("second status");
    assert_eq!(
        first.data.runner.expect("first runner").run_attempt,
        Some(1)
    );
    assert_eq!(
        second.data.runner.expect("changed runner").run_attempt,
        Some(2)
    );
    let status = decode::<TurnStatus>(&statuses[2]).expect("terminal status");
    let runner = status.data.runner.expect("runner identity");
    assert_eq!(runner.run_id.as_deref(), Some("123"));
    assert_eq!(runner.run_attempt, Some(2));
}
