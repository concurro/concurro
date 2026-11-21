//! Durable sponsorship writes through PostgreSQL and the real Exchange client.

use std::{collections::BTreeMap, sync::Arc};

use axum::{
    Json, Router,
    body::Bytes,
    extract::State,
    http::{HeaderMap, StatusCode},
    routing::post,
};
use chrono::{DateTime, Utc};
use concurro_db::Database;
use concurro_events_postgres::{Outbox, migrate as migrate_events};
use concurro_exchange_v1::Operator;
use concurro_tasks::run_sponsorship_transition;
use concurro_tasks_core::{
    Actor, Authority, FundingMode, IssueKey, Provider, Request, RequestKind, RequestStatus,
    SourceOrder, initial_sponsorship_transitions, initialize_consent,
};
use concurro_tasks_postgres::{
    activate_funding_policy, apply_request, funding_policy_at, migrate, pending_effects,
    record_request, record_sponsorship_transition,
};
use concurro_testkit::PostgresContainer;
use serde_json::{Value, json};
use tokio::sync::Mutex;
use uuid::Uuid;

#[tokio::test]
async fn uncertain_exchange_send_replays_the_persisted_transition() {
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
    let actor = Actor {
        id: 42,
        login: "writer".to_owned(),
    };
    let issue = IssueKey {
        repository_id: 7,
        number: 12,
    };
    let request = Request {
        id: Uuid::now_v7(),
        channel: "comment".to_owned(),
        reply: "issue_comment:1".to_owned(),
        comment_id: Some(1),
        issue,
        issue_author: actor.clone(),
        actor: actor.clone(),
        kind: RequestKind::Start,
        order: SourceOrder {
            at: Utc::now(),
            source_id: 1,
            source: "start:1".to_owned(),
            index: 0,
        },
    };
    let mut work = database.begin().await.expect("record start");
    record_request(&mut work, &request).await.expect("record");
    work.commit().await.expect("record commit");
    let mut work = database.begin().await.expect("apply start");
    let task = apply_request(
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
    .expect("decision")
    .expect("pending")
    .1;
    assert_eq!(task.status, RequestStatus::Applied);
    let task = task.task.expect("task");
    work.commit().await.expect("start commit");
    let available = BTreeMap::from([
        (Provider::Openai, Some(false)),
        (Provider::Anthropic, Some(false)),
    ]);
    let initial =
        initialize_consent(&[], &request.order, &actor, &available).expect("known boundary");
    let transition = initial_sponsorship_transitions(
        &initial,
        "acme/demo",
        issue,
        &actor,
        1,
        None,
        FundingMode::Org,
        Some(FundingMode::Org),
        "comment",
    )
    .expect("transition")
    .into_iter()
    .next()
    .expect("provider");
    let exact = serde_json::to_vec(&transition).expect("body");
    let mut work = database.begin().await.expect("record transition");
    let identity = record_sponsorship_transition(&mut work, task.id.0, &transition)
        .await
        .expect("record transition");
    work.commit().await.expect("transition commit");

    let received = Arc::new(Mutex::new(Vec::<Vec<u8>>::new()));
    let app = Router::new()
        .route(
            "/exchange/operator/sponsorship",
            post(
                |State(received): State<Arc<Mutex<Vec<Vec<u8>>>>>,
                 headers: HeaderMap,
                 body: Bytes| async move {
                    assert_eq!(headers["authorization"], "Bearer operator-only");
                    let mut calls = received.lock().await;
                    calls.push(body.to_vec());
                    if calls.len() == 1 {
                        return (
                            StatusCode::SERVICE_UNAVAILABLE,
                            Json(json!({"reason":"retry"})),
                        );
                    }
                    let request: Value = serde_json::from_slice(&body).expect("request");
                    (
                        StatusCode::OK,
                        Json(json!({"transitions":[{
                            "transition":request["transition"],
                            "epoch":request["slot"]["epoch"]
                        }]})),
                    )
                },
            ),
        )
        .with_state(received.clone());
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind");
    let address = listener.local_addr().expect("address");
    let server = tokio::spawn(async move { axum::serve(listener, app).await });
    let operator = Operator::new(
        reqwest::Client::new(),
        format!("http://{address}"),
        "operator-only",
    );
    assert!(
        run_sponsorship_transition(&database, &operator, &identity)
            .await
            .is_err()
    );
    assert!(
        pending_effects(&database)
            .await
            .expect("operator diagnostics")
            .iter()
            .any(|effect| {
                effect["kind"] == "sponsorship_transition"
                    && effect["status"] == "sending"
                    && effect["resolution"] == "retry_exact"
            })
    );
    run_sponsorship_transition(&database, &operator, &identity)
        .await
        .expect("idempotent replay");
    let calls = received.lock().await;
    assert_eq!(calls.as_slice(), &[exact.clone(), exact]);
    let status: String =
        sqlx::query_scalar("SELECT status FROM tasks.sponsorship_transitions WHERE transition=$1")
            .bind(identity)
            .fetch_one(database.pool())
            .await
            .expect("transition status");
    assert_eq!(status, "acknowledged");
    server.abort();
}

#[tokio::test]
async fn funding_policy_uses_local_activation_time_and_exact_config_digest() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("task schema");
    let before = Utc::now();
    let original_digest = "a".repeat(64);
    let replacement_digest = "b".repeat(64);
    let mut work = database.begin().await.expect("activate org policy");
    assert!(
        activate_funding_policy(
            &mut work,
            7,
            &"c".repeat(40),
            &original_digest,
            FundingMode::Org,
        )
        .await
        .expect("activation")
    );
    work.commit().await.expect("policy commit");
    let first: DateTime<Utc> = sqlx::query_scalar(
        "SELECT started_at FROM tasks.funding_policy_activations WHERE repository_id=7 AND ended_at IS NULL",
    )
    .fetch_one(database.pool())
    .await
    .expect("first activation");
    assert_eq!(
        funding_policy_at(&database, 7, &original_digest, before)
            .await
            .expect("before source"),
        None
    );
    assert_eq!(
        funding_policy_at(&database, 7, &original_digest, first)
            .await
            .expect("org source"),
        Some(FundingMode::Org)
    );
    let mut work = database.begin().await.expect("observe same policy");
    assert!(
        !activate_funding_policy(
            &mut work,
            7,
            &"c".repeat(40),
            &original_digest,
            FundingMode::Org,
        )
        .await
        .expect("unchanged")
    );
    work.commit().await.expect("unchanged commit");
    let mut work = database.begin().await.expect("activate contributed policy");
    assert!(
        activate_funding_policy(
            &mut work,
            7,
            &"d".repeat(40),
            &replacement_digest,
            FundingMode::Contributed,
        )
        .await
        .expect("replacement")
    );
    work.commit().await.expect("replacement commit");
    let second: DateTime<Utc> = sqlx::query_scalar(
        "SELECT started_at FROM tasks.funding_policy_activations WHERE repository_id=7 AND ended_at IS NULL",
    )
    .fetch_one(database.pool())
    .await
    .expect("second activation");
    assert_eq!(
        funding_policy_at(&database, 7, &original_digest, first)
            .await
            .expect("old interval"),
        Some(FundingMode::Org)
    );
    assert_eq!(
        funding_policy_at(&database, 7, &original_digest, second)
            .await
            .expect("closed interval"),
        None
    );
    assert_eq!(
        funding_policy_at(&database, 7, &replacement_digest, second)
            .await
            .expect("new interval"),
        Some(FundingMode::Contributed)
    );
}
