//! Admission client round trips the shared HTTP request and response fixtures.

use axum::{
    Json, Router,
    body::Bytes,
    http::{HeaderMap, StatusCode},
    routing::{post, put},
};
use concurro_contracts::{AdmissionRequest, FinishRequest, SessionStatusRequest};
use concurro_exchange_client::{AdmissionClient, OperatorClient};
use serde_json::{Value, json};
use uuid::Uuid;

const REQUEST: &str =
    include_str!("../../../contracts/fixtures/exchange.http.v1/admission_request.json");
const REPLY: &str =
    include_str!("../../../contracts/fixtures/exchange.http.v1/admission_reply.json");
const FINISH: &str =
    include_str!("../../../contracts/fixtures/exchange.http.v1/finish_request.json");
const PROBE: &str = include_str!("../../../contracts/fixtures/exchange.http.v1/probe_reply.json");
const SESSIONS: &str =
    include_str!("../../../contracts/fixtures/exchange.http.v1/sessions_request.json");
const LIVE: &str = include_str!("../../../contracts/fixtures/exchange.http.v1/sessions_reply.json");
const SUBMIT: &str =
    include_str!("../../../contracts/fixtures/exchange.http.v1/operator_submit.json");
const OBSERVATION: &str =
    include_str!("../../../contracts/fixtures/exchange.http.v1/operator_observation.json");
const RETIREMENT: &str =
    include_str!("../../../contracts/fixtures/exchange.http.v1/operator_retirement.json");

/// Exercise each operator client route and preserve a persisted submit body's bytes.
#[tokio::test]
async fn operator_client_round_trips_fixture_routes() {
    let app = Router::new()
        .route(
            "/v1/turns/{turn}",
            put(|headers: HeaderMap, body: Bytes| async move {
                assert_eq!(headers["authorization"], "Bearer operator-test");
                assert_eq!(body.as_ref(), SUBMIT.as_bytes());
                Json(serde_json::from_str::<Value>(OBSERVATION).expect("observation fixture"))
            })
            .get(|| async {
                Json(serde_json::from_str::<Value>(OBSERVATION).expect("observation fixture"))
            })
            .delete(|| async {
                Json(serde_json::from_str::<Value>(RETIREMENT).expect("retirement fixture"))
            }),
        )
        .route(
            "/v1/grants/{grant}/revoke",
            post(|| async {
                Json(serde_json::from_str::<Value>(RETIREMENT).expect("retirement fixture"))
            }),
        );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind");
    let address = listener.local_addr().expect("address");
    let server = tokio::spawn(async move { axum::serve(listener, app).await.expect("serve") });
    let client = OperatorClient::new(
        reqwest::Client::new(),
        format!("http://{address}"),
        "operator-test",
    );
    let turn = Uuid::parse_str("00000000-0000-0000-0000-000000000001").expect("turn fixture");
    let submitted = client
        .submit_bytes(turn, SUBMIT.as_bytes())
        .await
        .expect("submit bytes");
    assert_eq!(
        serde_json::to_value(&submitted).expect("observation JSON")["turn_id"],
        turn.to_string()
    );
    assert_eq!(client.observe(turn).await.expect("observe"), submitted);
    let expected = serde_json::from_str::<Value>(RETIREMENT).expect("retirement fixture");
    assert_eq!(
        serde_json::to_value(client.cancel(turn).await.expect("cancel")).expect("cancel JSON"),
        expected
    );
    assert_eq!(
        serde_json::to_value(client.revoke("org-1").await.expect("revoke")).expect("revoke JSON"),
        expected
    );
    server.abort();
}

/// Exercise every admission client call against its contract fixture route.
#[tokio::test]
async fn admission_client_round_trips_fixture_routes() {
    let app = Router::new()
        .route(
            "/internal/v1/requests",
            post(|headers: HeaderMap, Json(body): Json<Value>| async move {
                assert_eq!(headers["authorization"], "Bearer admission-test");
                if body.get("probe") == Some(&json!(true)) {
                    assert_eq!(body["key"], "sk-concurro-fixture");
                    Json(serde_json::from_str::<Value>(PROBE).expect("probe fixture"))
                } else {
                    assert_eq!(
                        body,
                        serde_json::from_str::<Value>(REQUEST).expect("request fixture")
                    );
                    Json(serde_json::from_str::<Value>(REPLY).expect("reply fixture"))
                }
            }),
        )
        .route(
            "/internal/v1/requests/{request}/finish",
            post(
                |headers: HeaderMap,
                 axum::extract::Path(request): axum::extract::Path<Uuid>,
                 Json(body): Json<Value>| async move {
                    assert_eq!(headers["authorization"], "Bearer admission-test");
                    assert_eq!(request.to_string(), "00000000-0000-0000-0000-000000000003");
                    assert_eq!(
                        body,
                        serde_json::from_str::<Value>(FINISH).expect("finish fixture")
                    );
                    StatusCode::NO_CONTENT
                },
            ),
        )
        .route(
            "/internal/v1/sessions/status",
            post(|headers: HeaderMap, Json(body): Json<Value>| async move {
                assert_eq!(headers["authorization"], "Bearer admission-test");
                assert_eq!(
                    body,
                    serde_json::from_str::<Value>(SESSIONS).expect("sessions fixture")
                );
                Json(serde_json::from_str::<Value>(LIVE).expect("live fixture"))
            }),
        );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind");
    let address = listener.local_addr().expect("address");
    let server = tokio::spawn(async move { axum::serve(listener, app).await.expect("serve") });
    let client = AdmissionClient::new(
        reqwest::Client::new(),
        format!("http://{address}"),
        "admission-test",
    );
    let request: AdmissionRequest = serde_json::from_str(REQUEST).expect("request fixture");
    let admission = client.request(&request).await.expect("request");
    assert_eq!(
        serde_json::to_value(admission).expect("admission JSON"),
        serde_json::from_str::<Value>(REPLY).expect("reply fixture")
    );
    assert_eq!(
        serde_json::to_value(client.probe("sk-concurro-fixture").await.expect("probe"))
            .expect("probe JSON"),
        serde_json::from_str::<Value>(PROBE).expect("probe fixture")
    );
    let finish: FinishRequest = serde_json::from_str(FINISH).expect("finish fixture");
    client
        .finish(request.request_id.expect("request id"), &finish)
        .await
        .expect("finish");
    let sessions: SessionStatusRequest = serde_json::from_str(SESSIONS).expect("sessions fixture");
    assert_eq!(
        serde_json::to_value(client.sessions_status(&sessions).await.expect("status"))
            .expect("status JSON"),
        serde_json::from_str::<Value>(LIVE).expect("live fixture")
    );
    server.abort();
}
