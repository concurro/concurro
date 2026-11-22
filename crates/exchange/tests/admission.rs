//! Real PostgreSQL and HTTP admission/settlement regressions.

use concurro_contracts::{
    Admission, AdmissionRequest, ExchangeTurnFinished, FinishRequest, ProviderRequests,
    SessionStatusRequest, SessionStatusResponse, TurnResultV2, UsageTokens,
};
use concurro_db::Database;
use concurro_events_postgres::{Outbox, migrate as migrate_events};
use concurro_exchange::{AdmissionState, admission_router};
use concurro_exchange_postgres::{Store, migrate as migrate_exchange};
use concurro_testkit::PostgresContainer;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use uuid::Uuid;

/// Start an admitted running turn with a disposable session key and real listener.
async fn setup() -> (
    PostgresContainer,
    Database,
    Store,
    String,
    Uuid,
    Uuid,
    tokio::task::JoinHandle<()>,
) {
    let postgres = PostgresContainer::start().await.expect("PostgreSQL");
    let database = Database::connect(postgres.url(), 8)
        .await
        .expect("database");
    migrate_events(&database).await.expect("event tables");
    migrate_exchange(&database).await.expect("exchange tables");
    let store = Store::new(
        database.clone(),
        Outbox::new(
            1_048_576,
            vec!["exchange.turn.>".to_owned(), "inference.usage".to_owned()],
        ),
    );
    let body: Value = serde_json::from_str(include_str!(
        "../../../contracts/fixtures/exchange.http.v1/operator_submit.json"
    ))
    .expect("fixture");
    let turn: Uuid = body["attempt_id"]
        .as_str()
        .expect("turn")
        .parse()
        .expect("UUID");
    let session = store
        .submit(turn, &body)
        .await
        .expect("submit")
        .dispatch
        .expect("dispatch")
        .session_id;
    sqlx::query("UPDATE exchange.sessions SET session_key_hash=$2,runner_token_hash=$3,bound_at=now(),last_seen=now() WHERE session_id=$1")
        .bind(session).bind(hex::encode(Sha256::digest(b"sk-test")))
        .bind(hex::encode(Sha256::digest(b"rt-test")))
        .execute(database.pool()).await.expect("bind session");
    sqlx::query("UPDATE exchange.turns SET status='running',started=now() WHERE turn_id=$1")
        .bind(turn)
        .execute(database.pool())
        .await
        .expect("running turn");
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("listener");
    let base = format!("http://{}", listener.local_addr().expect("address"));
    let server = tokio::spawn(async move {
        axum::serve(
            listener,
            admission_router(AdmissionState {
                store,
                bearer: "private-admission".to_owned(),
            }),
        )
        .await
        .expect("server");
    });
    let store = Store::new(
        database.clone(),
        Outbox::new(
            1_048_576,
            vec!["exchange.turn.>".to_owned(), "inference.usage".to_owned()],
        ),
    );
    (postgres, database, store, base, session, turn, server)
}

/// Finish reports and five-minute closure produce one fact per request and turn.
#[tokio::test]
async fn usage_settlement_and_late_finish_are_idempotent() {
    let (_pg, database, store, base, session, turn, server) = setup().await;
    let client = reqwest::Client::new();
    let route = format!("{base}/internal/v1/requests");
    let probe = client
        .post(&route)
        .bearer_auth("private-admission")
        .json(&AdmissionRequest {
            key: "sk-test".to_owned(),
            model: String::new(),
            request_id: None,
            probe: Some(true),
        })
        .send()
        .await
        .expect("probe");
    assert_eq!(probe.status(), 200);
    assert_eq!(
        probe.json::<Value>().await.expect("probe body")["models"],
        json!(["claude-test"])
    );
    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM exchange.inference_requests")
        .fetch_one(database.pool())
        .await
        .expect("probe count");
    assert_eq!(count, 0);
    let first = Uuid::now_v7();
    let request = AdmissionRequest {
        key: "sk-test".to_owned(),
        model: "claude-test".to_owned(),
        request_id: Some(first),
        probe: None,
    };
    let response: Admission = client
        .post(&route)
        .bearer_auth("private-admission")
        .json(&request)
        .send()
        .await
        .expect("admit")
        .json()
        .await
        .expect("admission");
    assert_eq!(response.turn_id, turn);
    assert_eq!(response.credential.route, "claude_org");
    let repeat: Admission = client
        .post(&route)
        .bearer_auth("private-admission")
        .json(&request)
        .send()
        .await
        .expect("repeat")
        .json()
        .await
        .expect("repeat admission");
    assert_eq!(repeat, response);
    let finish = FinishRequest {
        dispatched: true,
        category: "ok".to_owned(),
        usage: Some(UsageTokens {
            input_tokens: 19,
            output_tokens: 7,
            cache_read_input_tokens: Some(3),
            cache_creation_input_tokens: None,
        }),
        usage_unavailable: None,
        retry_after: None,
    };
    let finish_url = format!("{route}/{first}/finish");
    for _ in 0..2 {
        assert_eq!(
            client
                .post(&finish_url)
                .bearer_auth("private-admission")
                .json(&finish)
                .send()
                .await
                .expect("finish")
                .status(),
            204
        );
    }
    let second = Uuid::now_v7();
    let pending = AdmissionRequest {
        request_id: Some(second),
        ..request
    };
    assert_eq!(
        client
            .post(&route)
            .bearer_auth("private-admission")
            .json(&pending)
            .send()
            .await
            .expect("pending admit")
            .status(),
        200
    );
    let status: SessionStatusResponse = client
        .post(format!("{base}/internal/v1/sessions/status"))
        .bearer_auth("private-admission")
        .json(&SessionStatusRequest {
            session_ids: vec![session],
        })
        .send()
        .await
        .expect("session status")
        .json()
        .await
        .expect("status body");
    assert_eq!(status.live, vec![session]);
    let result = TurnResultV2 {
        outcome: "failed".to_owned(),
        response: json!({}),
        termination_reason: None,
        elapsed_seconds: None,
        workspace: None,
        transcript: None,
        retained: None,
    };
    store
        .runner_result(
            session,
            turn,
            &hex::encode(Sha256::digest(b"rt-test")),
            &result,
        )
        .await
        .expect("runner result");
    let facts: i64 = sqlx::query_scalar("SELECT count(*) FROM events.outbox WHERE subject=$1")
        .bind(format!("exchange.turn.{turn}.finished"))
        .fetch_one(database.pool())
        .await
        .expect("unsettled fact count");
    assert_eq!(facts, 0, "failed result waits for missing report");
    sqlx::query("UPDATE exchange.inference_requests SET settlement_deadline=now()-interval '1 second' WHERE request_id=$1")
        .bind(second).execute(database.pool()).await.expect("age report");
    assert_eq!(store.reconcile_requests().await.expect("reconcile"), 1);
    let late = client
        .post(format!("{route}/{second}/finish"))
        .bearer_auth("private-admission")
        .json(&finish)
        .send()
        .await
        .expect("late finish");
    assert_eq!(late.status(), 204);
    let facts: i64 = sqlx::query_scalar("SELECT count(*) FROM events.outbox WHERE subject=$1")
        .bind(format!("exchange.turn.{turn}.finished"))
        .fetch_one(database.pool())
        .await
        .expect("settled fact count");
    assert_eq!(facts, 1);
    let usages: i64 =
        sqlx::query_scalar("SELECT count(*) FROM events.outbox WHERE subject='inference.usage'")
            .fetch_one(database.pool())
            .await
            .expect("usage facts");
    assert_eq!(usages, 2);
    let event: Value = sqlx::query_scalar("SELECT message FROM events.outbox WHERE subject=$1")
        .bind(format!("exchange.turn.{turn}.finished"))
        .fetch_one(database.pool())
        .await
        .expect("fact body");
    let finished: ExchangeTurnFinished =
        serde_json::from_value(event["data"].clone()).expect("finished contract");
    assert_eq!(finished.provider_requests, ProviderRequests::Some);
    assert_eq!(finished.usage.requests, 2);
    assert_eq!(finished.usage.input_tokens, 19);
    assert_eq!(finished.usage.unavailable, 1);
    server.abort();
}

/// Reaching the spent budget records its own refusal and fences later requests.
#[tokio::test]
async fn budget_denial_and_revocation_fence_admission() {
    let (_pg, database, store, base, session, turn, server) = setup().await;
    let client = reqwest::Client::new();
    let route = format!("{base}/internal/v1/requests");
    assert_eq!(
        client
            .post(&route)
            .bearer_auth("wrong")
            .json(&json!({"key":"sk-test","model":"claude-test"}))
            .send()
            .await
            .expect("private bearer")
            .status(),
        401
    );
    assert_eq!(
        client
            .post(&route)
            .bearer_auth("private-admission")
            .json(&json!({"key":"sk-test","model":"wrong"}))
            .send()
            .await
            .expect("model")
            .status(),
        403
    );
    let first = Uuid::now_v7();
    assert_eq!(
        client
            .post(&route)
            .bearer_auth("private-admission")
            .json(&json!({"key":"sk-test","model":"claude-test","request_id":first}))
            .send()
            .await
            .expect("admit")
            .status(),
        200
    );
    assert_eq!(
        client
            .post(format!("{route}/{first}/finish"))
            .bearer_auth("private-admission")
            .json(&FinishRequest {
                dispatched: true,
                category: "ok".to_owned(),
                usage: Some(UsageTokens {
                    input_tokens: 700,
                    output_tokens: 300,
                    cache_read_input_tokens: None,
                    cache_creation_input_tokens: None
                }),
                usage_unavailable: None,
                retry_after: None
            })
            .send()
            .await
            .expect("finish")
            .status(),
        204
    );
    let denied = Uuid::now_v7();
    assert_eq!(
        client
            .post(&route)
            .bearer_auth("private-admission")
            .json(&json!({"key":"sk-test","model":"claude-test","request_id":denied}))
            .send()
            .await
            .expect("budget")
            .status(),
        403
    );
    let failure: Value =
        sqlx::query_scalar("SELECT provider_failure FROM exchange.turns WHERE turn_id=$1")
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("provider failure");
    assert_eq!(failure["category"], "budget_denied");
    assert_eq!(
        client
            .post(&route)
            .bearer_auth("private-admission")
            .json(&json!({"key":"sk-test","model":"claude-test"}))
            .send()
            .await
            .expect("failed turn")
            .status(),
        403
    );
    store.revoke("org-1").await.expect("revoke grant");
    let status: SessionStatusResponse = client
        .post(format!("{base}/internal/v1/sessions/status"))
        .bearer_auth("private-admission")
        .json(&SessionStatusRequest {
            session_ids: vec![session],
        })
        .send()
        .await
        .expect("status")
        .json()
        .await
        .expect("status body");
    assert!(status.live.is_empty());
    server.abort();
}
