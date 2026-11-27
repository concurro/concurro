//! Real operator router, client, PostgreSQL and outbox workflow checks.

use axum::{
    Router,
    http::{HeaderMap, StatusCode},
    routing::post,
};
use chrono::{Duration, Utc};
use concurro_contracts::{
    AdmissionRequest, ExpectedClaims, Grant, GrantCredential, GrantLimits, RequestActor,
    RunnerEngine, TurnBinding, TurnPayloadV2, TurnSubmissionV2,
};
use concurro_db::Database;
use concurro_events_postgres::{Outbox, migrate as migrate_events};
use concurro_exchange::{OperatorState, deliver_pending_releases, operator_router};
use concurro_exchange_client::{ClientError, OperatorClient};
use concurro_exchange_postgres::{RelaySigner, Store, migrate as migrate_exchange};
use concurro_relay_hosted_api::claim::VerificationKeys;
use concurro_tasks_core::python_digest;
use concurro_testkit::PostgresContainer;
use serde_json::{Map, Value, json};
use uuid::Uuid;

/// Start a disposable operator API backed by the shared event migrations.
async fn setup() -> (
    PostgresContainer,
    Database,
    Store,
    String,
    tokio::task::JoinHandle<()>,
) {
    let postgres = PostgresContainer::start().await.expect("postgres");
    let database = Database::connect(postgres.url(), 8)
        .await
        .expect("database");
    migrate_events(&database).await.expect("event migrations");
    migrate_exchange(&database)
        .await
        .expect("exchange migrations");
    let store = Store::new(
        database.clone(),
        Outbox::new(
            1_048_576,
            vec!["exchange.turn.>".to_owned(), "inference.usage".to_owned()],
        ),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind");
    let address = listener.local_addr().expect("address");
    let server = tokio::spawn({
        let state = OperatorState {
            store: store.clone(),
            bearer: "operator-test".to_owned(),
        };
        async move {
            axum::serve(listener, operator_router(state))
                .await
                .expect("serve")
        }
    });
    (
        postgres,
        database,
        store,
        format!("http://{address}"),
        server,
    )
}

/// Construct a valid org turn with the Python-compatible staffing identity.
fn submission(turn: Uuid, grant_id: &str) -> TurnSubmissionV2 {
    let claims = ExpectedClaims {
        repository_id: "123".to_owned(),
        repository_owner_id: "7".to_owned(),
        job_workflow_ref: "org/fork/.github/workflows/agent.yml@refs/tags/v2".to_owned(),
        actor_id: "9".to_owned(),
        job_workflow_sha: None,
    };
    let binding = TurnBinding {
        repository: "org/upstream".to_owned(),
        issue: 42,
        role: "intake:claude".to_owned(),
        engine: RunnerEngine {
            repository: "org/engine".to_owned(),
            sha: "ab123".to_owned(),
        },
        fork: "org/fork".to_owned(),
        upstream: "org/upstream".to_owned(),
        config_sha: "config".to_owned(),
        base: "base".to_owned(),
        extra: Map::new(),
    };
    let models = vec!["claude-test".to_owned()];
    let budget = 1000;
    let lease_seconds = 3600;
    let staffing_group = python_digest(&json!([
        claims,
        binding,
        models,
        budget,
        lease_seconds as f64
    ]))
    .expect("staffing digest");
    TurnSubmissionV2 {
        attempt_id: turn,
        task_id: Uuid::now_v7(),
        grant: Grant {
            grant_id: grant_id.to_owned(),
            purpose: "task_org".to_owned(),
            payer: None,
            credential: GrantCredential {
                route: "claude_org".to_owned(),
                entry_id: None,
                record_id: None,
                version: None,
                custody: None,
                relay_id: None,
            },
            models: models.clone(),
            limits: GrantLimits {
                tokens: 2000,
                lease_seconds: 7200,
            },
        },
        binding,
        staffing_group,
        expected_claims: claims,
        models,
        budget,
        lease_seconds,
        payload: TurnPayloadV2 {
            request: json!({"stage":"intake","provider":"claude"}),
            feedback: json!({}),
            spec: json!({"head":"base"}),
            timeout: Some(60.0),
            retained: None,
        },
    }
}

/// Assert one stable public refusal code.
fn refusal(error: ClientError, status: u16, code: &str) {
    std::assert_matches!(error, ClientError::Refused { status: got, code: got_code }
        if got == status && got_code == code);
}

/// Exercise immutable submit, replay, cancel, pruning, pre-submit fences and bearer isolation.
#[tokio::test]
async fn operator_workflow_survives_replay_pruning_and_restart() {
    let (_postgres, database, store, base, server) = setup().await;
    let client = OperatorClient::new(reqwest::Client::new(), &base, "operator-test");
    let turn = Uuid::now_v7();
    let body = submission(turn, "org-1");
    let first = client.submit(turn, &body).await.expect("submit");
    assert_eq!(first.status, "queued");
    let dispatch = first.dispatch.as_ref().expect("dispatch").session_id;
    assert_eq!(client.observe(turn).await.expect("observe"), first);
    assert_eq!(client.submit(turn, &body).await.expect("replay"), first);
    let mut explicit_null = serde_json::to_value(&body).expect("wire body");
    explicit_null["grant"]["payer"] = serde_json::Value::Null;
    let response = reqwest::Client::new()
        .put(format!("{base}/v1/turns/{turn}"))
        .bearer_auth("operator-test")
        .json(&explicit_null)
        .send()
        .await
        .expect("null replay");
    assert_eq!(
        response.status().as_u16(),
        409,
        "omission and explicit null are distinct canonical bodies"
    );
    let mut unknown = serde_json::to_value(&body).expect("wire body");
    unknown["unregistered"] = json!(true);
    let response = reqwest::Client::new()
        .put(format!("{base}/v1/turns/{turn}"))
        .bearer_auth("operator-test")
        .json(&unknown)
        .send()
        .await
        .expect("unknown field");
    assert_eq!(response.status().as_u16(), 422);
    let queued = Uuid::now_v7();
    let queued_body = submission(queued, "org-1");
    let waiting = client
        .submit(queued, &queued_body)
        .await
        .expect("queued behind group");
    assert!(waiting.dispatch.is_none());
    assert!(waiting.session_id.is_none());
    let conflicting_grant = Uuid::now_v7();
    let mut grant_body = submission(conflicting_grant, "org-1");
    grant_body.grant.credential.route = "another_org_route".to_owned();
    refusal(
        client
            .submit(conflicting_grant, &grant_body)
            .await
            .expect_err("grant immutable"),
        409,
        "grant_in_use",
    );
    let mut changed = body.clone();
    changed.payload.request = json!({"stage":"changed"});
    refusal(
        client.submit(turn, &changed).await.expect_err("conflict"),
        409,
        "turn_in_use",
    );
    let outsider = OperatorClient::new(reqwest::Client::new(), &base, "wrong");
    refusal(
        outsider.observe(turn).await.expect_err("bearer"),
        401,
        "unauthorized",
    );
    let anonymous = reqwest::Client::new()
        .get(format!("{base}/v1/turns/{turn}"))
        .send()
        .await
        .expect("anonymous reply");
    assert_eq!(anonymous.status(), 401);
    let events: i64 = sqlx::query_scalar("SELECT count(*) FROM events.outbox WHERE subject=$1")
        .bind(format!("exchange.turn.{turn}.status"))
        .fetch_one(database.pool())
        .await
        .expect("status count");
    assert_eq!(events, 1);
    let retired = client.cancel(turn).await.expect("cancel");
    assert_eq!(retired.sessions, vec![dispatch]);
    let advanced = client.observe(queued).await.expect("FIFO advance");
    assert_eq!(advanced.status, "queued");
    assert!(advanced.dispatch.is_some());
    assert_ne!(
        advanced.dispatch.as_ref().expect("new dispatch").session_id,
        dispatch
    );
    assert_eq!(client.cancel(turn).await.expect("cancel replay"), retired);
    assert_eq!(
        client
            .observe(turn)
            .await
            .expect("cancel observation")
            .status,
        "cancelled"
    );
    assert_eq!(
        store
            .prune(Utc::now() + Duration::seconds(1))
            .await
            .expect("prune"),
        1
    );
    assert_eq!(
        client.cancel(turn).await.expect("pruned retirement"),
        retired
    );
    refusal(
        client.submit(turn, &body).await.expect_err("expired"),
        410,
        "turn_expired",
    );
    refusal(
        client
            .submit(turn, &changed)
            .await
            .expect_err("pruned conflict"),
        409,
        "turn_in_use",
    );
    let tombstone_result: Option<serde_json::Value> =
        sqlx::query_scalar("SELECT result FROM exchange.turn_tombstones WHERE turn_id=$1")
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("tombstone result");
    assert!(tombstone_result.is_none());
    let unseen = Uuid::now_v7();
    assert!(
        client
            .cancel(unseen)
            .await
            .expect("fence unseen")
            .sessions
            .is_empty()
    );
    refusal(
        client
            .submit(unseen, &submission(unseen, "org-2"))
            .await
            .expect_err("cancel fence"),
        410,
        "turn_cancelled",
    );
    let pre_revoked = "org/with?reserved#parts%";
    assert!(
        client
            .revoke(pre_revoked)
            .await
            .expect("pre-revoke")
            .sessions
            .is_empty()
    );
    let future = Uuid::now_v7();
    refusal(
        client
            .submit(future, &submission(future, pre_revoked))
            .await
            .expect_err("grant fence"),
        410,
        "grant_revoked",
    );
    let stored_grant: String =
        sqlx::query_scalar("SELECT grant_id FROM exchange.grants WHERE grant_id=$1")
            .bind(pre_revoked)
            .fetch_one(database.pool())
            .await
            .expect("exact reserved grant path segment");
    assert_eq!(stored_grant, pre_revoked);
    server.abort();
    let reopened = Store::new(
        database.clone(),
        Outbox::new(1_048_576, vec!["exchange.turn.>".to_owned()]),
    );
    assert_eq!(
        reopened.cancel(turn).await.expect("restart retirement"),
        retired
    );
    assert_eq!(
        reopened
            .revoke(pre_revoked)
            .await
            .expect("restart grant")
            .sessions,
        Vec::<Uuid>::new()
    );
}

/// Keep one live session per group and promote turns in cross-grant FIFO order.
#[tokio::test]
async fn staffing_group_fifo_never_reuses_another_grants_session() {
    let (_postgres, database, _store, base, server) = setup().await;
    let client = OperatorClient::new(reqwest::Client::new(), &base, "operator-test");
    let a = Uuid::now_v7();
    let c = Uuid::now_v7();
    let b = Uuid::now_v7();
    let d = Uuid::now_v7();
    let first = client
        .submit(a, &submission(a, "grant-one"))
        .await
        .expect("A");
    let first_session = first.dispatch.expect("A dispatch").session_id;
    assert!(
        client
            .submit(c, &submission(c, "grant-two"))
            .await
            .expect("C")
            .dispatch
            .is_none()
    );
    assert!(
        client
            .submit(b, &submission(b, "grant-one"))
            .await
            .expect("B")
            .dispatch
            .is_none()
    );
    assert!(
        client
            .submit(d, &submission(d, "grant-two"))
            .await
            .expect("D")
            .dispatch
            .is_none()
    );
    let live: i64 =
        sqlx::query_scalar("SELECT count(*) FROM exchange.sessions WHERE ended IS NULL")
            .fetch_one(database.pool())
            .await
            .expect("live count");
    assert_eq!(live, 1);

    client.cancel(a).await.expect("retire A");
    let c_session = client
        .observe(c)
        .await
        .expect("C promoted")
        .dispatch
        .expect("C dispatch")
        .session_id;
    assert_ne!(c_session, first_session);
    assert!(
        client
            .observe(b)
            .await
            .expect("B waiting")
            .session_id
            .is_none()
    );
    client.cancel(c).await.expect("retire C");
    let b_session = client
        .observe(b)
        .await
        .expect("B promoted")
        .dispatch
        .expect("B dispatch")
        .session_id;
    assert_ne!(b_session, c_session);
    assert!(
        client
            .observe(d)
            .await
            .expect("D waiting")
            .session_id
            .is_none()
    );
    client.cancel(b).await.expect("retire B");
    assert!(
        client
            .observe(d)
            .await
            .expect("D promoted")
            .dispatch
            .is_some()
    );
    server.abort();
}

/// An idle live session still cannot serve a later grant in the same staffing group.
#[tokio::test]
async fn live_idle_session_is_not_reused_across_grants() {
    let (_postgres, database, _store, base, server) = setup().await;
    let client = OperatorClient::new(reqwest::Client::new(), &base, "operator-test");
    let first_turn = Uuid::now_v7();
    let first = client
        .submit(first_turn, &submission(first_turn, "grant-one"))
        .await
        .expect("first grant")
        .dispatch
        .expect("first dispatch")
        .session_id;
    sqlx::query("UPDATE exchange.turns SET status='finished',finished=now() WHERE turn_id=$1")
        .bind(first_turn)
        .execute(database.pool())
        .await
        .expect("finish without retiring session");
    let still_live: bool =
        sqlx::query_scalar("SELECT ended IS NULL FROM exchange.sessions WHERE session_id=$1")
            .bind(first)
            .fetch_one(database.pool())
            .await
            .expect("live first session");
    assert!(still_live);
    let second_turn = Uuid::now_v7();
    let second = client
        .submit(second_turn, &submission(second_turn, "grant-two"))
        .await
        .expect("second grant")
        .dispatch
        .expect("second dispatch")
        .session_id;
    assert_ne!(first, second);
    server.abort();
}

/// A new submit replaces the oldest expired assignment before newer waiters.
#[tokio::test]
async fn expired_unbound_session_is_replaced_without_leapfrogging() {
    let (_postgres, database, store, base, server) = setup().await;
    let client = OperatorClient::new(reqwest::Client::new(), &base, "operator-test");
    let a = Uuid::now_v7();
    let b = Uuid::now_v7();
    let c = Uuid::now_v7();
    let old = client
        .submit(a, &submission(a, "grant-one"))
        .await
        .expect("A")
        .dispatch
        .expect("initial dispatch")
        .session_id;
    assert!(
        client
            .submit(b, &submission(b, "grant-two"))
            .await
            .expect("B")
            .session_id
            .is_none()
    );
    store
        .claim_refusal(old, "OIDC setup refused")
        .await
        .expect("original refusal");
    sqlx::query("UPDATE exchange.sessions SET deadline=now()-interval '1 second',created=now()-interval '1201 seconds' WHERE session_id=$1")
        .bind(old).execute(database.pool()).await.expect("expire unbound session");
    assert!(
        client
            .submit(c, &submission(c, "grant-three"))
            .await
            .expect("C")
            .session_id
            .is_none()
    );
    let replaced = client.observe(a).await.expect("A replaced");
    assert_eq!(replaced.refusal.as_deref(), Some("OIDC setup refused"));
    let replacement = replaced.dispatch.expect("replacement dispatch").session_id;
    assert_ne!(replacement, old);
    assert!(
        client
            .observe(b)
            .await
            .expect("B waiting")
            .session_id
            .is_none()
    );
    assert!(
        client
            .observe(c)
            .await
            .expect("C waiting")
            .session_id
            .is_none()
    );
    let old_ended: bool =
        sqlx::query_scalar("SELECT ended IS NOT NULL FROM exchange.sessions WHERE session_id=$1")
            .bind(old)
            .fetch_one(database.pool())
            .await
            .expect("old ended");
    assert!(old_ended);
    let retired = client.cancel(a).await.expect("retire A");
    assert_eq!(retired.sessions.len(), 2);
    assert!(retired.sessions.contains(&old) && retired.sessions.contains(&replacement));
    assert!(
        client
            .observe(b)
            .await
            .expect("B promoted")
            .dispatch
            .is_some()
    );
    server.abort();
}

/// Explicit optional null participates in the raw staffing identity.
#[tokio::test]
async fn raw_claim_null_uses_the_submitted_staffing_digest() {
    let (_postgres, _database, _store, base, server) = setup().await;
    let turn = Uuid::now_v7();
    let mut body = serde_json::to_value(submission(turn, "org-raw-null")).expect("body");
    body["expected_claims"]["job_workflow_sha"] = Value::Null;
    let mut claims = body["expected_claims"].clone();
    claims.as_object_mut().expect("claims object").remove("ref");
    body["staffing_group"] = json!(
        python_digest(&json!([
            claims,
            body["binding"],
            body["models"],
            body["budget"],
            body["lease_seconds"].as_f64().expect("lease")
        ]))
        .expect("raw digest")
    );
    let response = reqwest::Client::new()
        .put(format!("{base}/v1/turns/{turn}"))
        .bearer_auth("operator-test")
        .json(&body)
        .send()
        .await
        .expect("raw submit");
    assert_eq!(response.status().as_u16(), 200);
    server.abort();
}

/// Revoke org sessions and keep a relay release pending until acknowledged later.
#[tokio::test]
async fn revoke_fences_existing_turn_and_does_not_acknowledge_pending_release() {
    let (_postgres, database, _store, base, server) = setup().await;
    let client = OperatorClient::new(reqwest::Client::new(), &base, "operator-test");
    let turn = Uuid::now_v7();
    let body = submission(turn, "org-revoke");
    let session = client
        .submit(turn, &body)
        .await
        .expect("submit")
        .dispatch
        .expect("dispatch")
        .session_id;
    let follower = Uuid::now_v7();
    assert!(
        client
            .submit(follower, &submission(follower, "org-follow"))
            .await
            .expect("cross-grant follower")
            .dispatch
            .is_none()
    );
    sqlx::query("INSERT INTO exchange.relay_claims(claim_id,session_id,release_status) VALUES($1,$2,'pending')")
        .bind(Uuid::now_v7()).bind(session).execute(database.pool()).await.expect("pending claim");
    refusal(
        client
            .revoke("org-revoke")
            .await
            .expect_err("release pending"),
        502,
        "release_pending",
    );
    assert_eq!(
        client.observe(turn).await.expect("revoked turn").status,
        "lost"
    );
    assert!(
        client
            .observe(follower)
            .await
            .expect("follower promoted on revoke")
            .dispatch
            .is_some()
    );
    let ended: bool =
        sqlx::query_scalar("SELECT ended IS NOT NULL FROM exchange.sessions WHERE session_id=$1")
            .bind(session)
            .fetch_one(database.pool())
            .await
            .expect("ended");
    assert!(ended);
    refusal(
        client
            .revoke("org-revoke")
            .await
            .expect_err("still pending"),
        502,
        "release_pending",
    );
    sqlx::query(
        "UPDATE exchange.relay_claims SET release_status='acknowledged' WHERE session_id=$1",
    )
    .bind(session)
    .execute(database.pool())
    .await
    .expect("fake verifier acknowledgement");
    assert_eq!(
        client
            .revoke("org-revoke")
            .await
            .expect("acknowledged")
            .sessions,
        vec![session]
    );
    assert_eq!(
        client
            .submit(turn, &body)
            .await
            .expect("replay after revoke")
            .status,
        "lost"
    );
    let next = Uuid::now_v7();
    refusal(
        client
            .submit(next, &submission(next, "org-revoke"))
            .await
            .expect_err("grant revoked"),
        410,
        "grant_revoked",
    );
    server.abort();
}

/// Cancel preserves an existing terminal result and pruning retains its exact JSON.
#[tokio::test]
async fn terminal_result_survives_cancel_and_tombstone() {
    let (_postgres, database, store, base, server) = setup().await;
    let client = OperatorClient::new(reqwest::Client::new(), &base, "operator-test");
    let turn = Uuid::now_v7();
    let body = submission(turn, "org-terminal");
    let session = client
        .submit(turn, &body)
        .await
        .expect("submit")
        .session_id
        .expect("session");
    let result = json!({"outcome":"ok","response":{"text":"done"},
        "workspace":null,"transcript":null,"retained":null});
    sqlx::query(
        "UPDATE exchange.turns SET status='finished',result=$2,finished=now(),finished_event_emitted=true WHERE turn_id=$1",
    )
    .bind(turn)
    .bind(&result)
    .execute(database.pool())
    .await
    .expect("synthetic S4b result");
    assert_eq!(
        client.cancel(turn).await.expect("terminal cancel").sessions,
        vec![session]
    );
    let observed = client.observe(turn).await.expect("terminal observation");
    assert_eq!(observed.status, "finished");
    assert_eq!(
        observed.result.expect("result").response,
        json!({"text":"done"})
    );
    assert_eq!(
        store
            .prune(Utc::now() + Duration::seconds(1))
            .await
            .expect("prune"),
        1
    );
    let retained: serde_json::Value =
        sqlx::query_scalar("SELECT result FROM exchange.turn_tombstones WHERE turn_id=$1")
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("retained result");
    assert_eq!(retained, result);
    assert_eq!(
        client.cancel(turn).await.expect("pruned cancel").sessions,
        vec![session]
    );
    server.abort();
}

/// Admit an exact contributed credential version under its numeric payer identity.
#[tokio::test]
async fn contributed_grant_validates_payer_and_custody() {
    let (_postgres, database, _store, base, server) = setup().await;
    let client = OperatorClient::new(reqwest::Client::new(), &base, "operator-test");
    let turn = Uuid::now_v7();
    let mut body = submission(turn, "personal-1");
    body.grant.purpose = "task_contributed".to_owned();
    body.grant.payer = Some(RequestActor {
        id: 35576205,
        login: "payer".to_owned(),
    });
    body.grant.credential = GrantCredential {
        route: "anthropic-api-key".to_owned(),
        entry_id: Some("entry".to_owned()),
        record_id: Some("record".to_owned()),
        version: Some(2),
        custody: Some("secret_store".to_owned()),
        relay_id: None,
    };
    assert!(
        client
            .submit(turn, &body)
            .await
            .expect("contributed submit")
            .dispatch
            .is_some()
    );
    let mut unknown_payer = serde_json::to_value(&body).expect("wire body");
    unknown_payer["grant"]["payer"]["unregistered"] = json!(true);
    let response = reqwest::Client::new()
        .put(format!("{base}/v1/turns/{turn}"))
        .bearer_auth("operator-test")
        .json(&unknown_payer)
        .send()
        .await
        .expect("unknown payer field");
    assert_eq!(response.status().as_u16(), 422);
    let next = Uuid::now_v7();
    let mut invalid = body.clone();
    invalid.attempt_id = next;
    invalid.grant.grant_id = "personal-invalid".to_owned();
    invalid.grant.credential.version = Some(0);
    refusal(
        client
            .submit(next, &invalid)
            .await
            .expect_err("invalid version"),
        422,
        "invalid_grant",
    );
    let stored: i64 = sqlx::query_scalar("SELECT count(*) FROM exchange.grants")
        .fetch_one(database.pool())
        .await
        .expect("grant count");
    assert_eq!(stored, 1);
    server.abort();
}

/// An outbox refusal rolls back its grant, turn and session together.
#[tokio::test]
async fn status_event_failure_rolls_back_operator_state() {
    let (_postgres, database, _store, _base, server) = setup().await;
    let broken = Store::new(
        database.clone(),
        Outbox::new(1_048_576, vec!["wrong.>".to_owned()]),
    );
    let turn = Uuid::now_v7();
    assert!(
        broken
            .submit(
                turn,
                &serde_json::to_value(submission(turn, "org-rollback")).expect("body")
            )
            .await
            .is_err()
    );
    let counts: (i64, i64, i64, i64) = sqlx::query_as("SELECT (SELECT count(*) FROM exchange.turns), (SELECT count(*) FROM exchange.grants), (SELECT count(*) FROM exchange.sessions), (SELECT count(*) FROM events.outbox)")
        .fetch_one(database.pool()).await.expect("rollback counts");
    assert_eq!(counts, (0, 0, 0, 0));
    server.abort();
}

/// Grants built from the accounts service's credential metadata are admitted for both
/// custodies; the metadata's own `vault` custody is not a grant custody.
#[tokio::test]
async fn accounts_metadata_grants_are_admitted() {
    let (_postgres, _database, _store, base, server) = setup().await;
    let client = OperatorClient::new(reqwest::Client::new(), &base, "operator-test");
    let metadata: concurro_vault_v1::Metadata = serde_json::from_value(json!({
        "history_complete": true,
        "lists": {"anthropic": {"revision": 2, "entries": ["relay-entry", "store-entry"]}},
        "credentials": [
            {"record_id": "relay-record", "user_id": "35576205", "route": "claude-subscription",
             "version": 2, "status": "valid", "models": null, "created": "2027-01-15T08:25:00+00:00",
             "updated": "2027-01-15T08:25:00+00:00", "kind": "subscription", "custody": "relay",
             "relay_id": "relay-0002", "expiry_known": false, "expires_at": null,
             "entry_id": "relay-entry", "provider": "anthropic", "position": 0},
            {"record_id": "store-record", "user_id": "35576205", "route": "anthropic-api-key",
             "version": 1, "status": "valid", "models": null, "created": "2027-01-15T08:24:00+00:00",
             "updated": "2027-01-15T08:24:00+00:00", "kind": "api_key", "custody": "vault",
             "expiry_known": false, "expires_at": null, "entry_id": "store-entry",
             "provider": "anthropic", "position": 1}
        ]
    }))
    .expect("accounts metadata");
    for (index, credential) in metadata.credentials.iter().enumerate() {
        let turn = Uuid::now_v7();
        let mut body = submission(turn, &format!("personal-{index}"));
        body.grant.purpose = "task_contributed".to_owned();
        body.grant.payer = Some(RequestActor {
            id: 35576205,
            login: "payer".to_owned(),
        });
        body.grant.credential = credential.grant_credential().expect("grant credential");
        client
            .submit(turn, &body)
            .await
            .unwrap_or_else(|error| panic!("{} grant refused: {error:?}", credential.custody));
        let raw = Uuid::now_v7();
        let mut unnormalized = submission(raw, &format!("raw-{index}"));
        unnormalized.grant = body.grant.clone();
        unnormalized.grant.credential.custody = Some(credential.custody.clone());
        if credential.custody == "vault" {
            std::assert_matches!(
                client.submit(raw, &unnormalized).await,
                Err(ClientError::Refused { status: 422, ref code }) if code == "invalid_grant"
            );
        }
    }
    server.abort();
}

/// An exchange-signed relay claim survives admission replay and stays fenced until S1 releases it.
#[tokio::test]
async fn relay_claim_is_signed_once_and_release_is_durable() {
    use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
    use sha2::{Digest, Sha256};

    let (_postgres, database, _store, _base, server) = setup().await;
    let key = URL_SAFE_NO_PAD
        .decode("MC4CAQAwBQYDK2VwBCIEIJ1hsZ3v_VpguoRK9JLsLMREScVpezJpGXA7rAMcrn9g")
        .expect("PKCS8 fixture");
    let store = Store::new(
        database.clone(),
        Outbox::new(
            1_048_576,
            vec!["exchange.turn.>".to_owned(), "inference.usage".to_owned()],
        ),
    )
    .with_relay(RelaySigner::new("fixture".to_owned(), key, 1000).expect("signer"));
    let turn = Uuid::now_v7();
    let mut body = submission(turn, "relay-grant");
    body.grant.purpose = "task_contributed".to_owned();
    body.grant.payer = Some(RequestActor {
        id: 35576205,
        login: "payer".to_owned(),
    });
    body.grant.credential = GrantCredential {
        route: "claude-subscription".to_owned(),
        entry_id: Some("entry".to_owned()),
        record_id: Some("record".to_owned()),
        version: Some(2),
        custody: Some("relay".to_owned()),
        relay_id: Some("relay-test".to_owned()),
    };
    let observation = store
        .submit(turn, &serde_json::to_value(body).expect("body"))
        .await
        .expect("submit");
    let session = observation.session_id.expect("session");
    let secret = "derived-key";
    sqlx::query(
        "UPDATE exchange.sessions SET session_key_hash=$2,last_seen=now() WHERE session_id=$1",
    )
    .bind(session)
    .bind(hex::encode(Sha256::digest(secret.as_bytes())))
    .execute(database.pool())
    .await
    .expect("key");
    sqlx::query("UPDATE exchange.turns SET status='running' WHERE turn_id=$1")
        .bind(turn)
        .execute(database.pool())
        .await
        .expect("running");
    let request = AdmissionRequest {
        key: secret.to_owned(),
        model: "claude-test".to_owned(),
        request_id: Some(Uuid::now_v7()),
        probe: None,
    };
    let admitted = store
        .admit(&request)
        .await
        .expect("admit")
        .expect("request");
    let second = store
        .admit(&request)
        .await
        .expect("replay")
        .expect("request");
    assert_eq!(admitted.claim, second.claim);
    let keys = VerificationKeys::from_jwks(br#"{"keys":[{"kid":"fixture","kty":"OKP","crv":"Ed25519","x":"11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"}]}"#).expect("keys");
    let claim = keys
        .verify(
            admitted.claim.as_deref().expect("claim"),
            Utc::now().timestamp(),
        )
        .expect("S1 verifier signature");
    assert_eq!(claim.sub, session.to_string());
    assert_eq!(claim.relay_id, "relay-test");
    assert_eq!(claim.record_id, "record");
    assert_eq!(claim.version, 2);
    std::assert_matches!(
        store.revoke("relay-grant").await,
        Err(concurro_exchange_postgres::StoreError::Refused {
            status: 502,
            code: "release_pending"
        })
    );
    let pending = store.pending_releases().await.expect("pending releases");
    assert_eq!(pending.len(), 1);
    assert_eq!(pending[0].to_string(), claim.jti);
    let restarted = Store::new(
        database.clone(),
        Outbox::new(
            1_048_576,
            vec!["exchange.turn.>".to_owned(), "inference.usage".to_owned()],
        ),
    );
    if let Ok(binary) = std::env::var("CONCURRO_S1_HOSTED_BIN") {
        use std::{fs, net::TcpListener, process::Stdio, time::Duration as StdDuration};
        use tokio::process::Command;

        let directory = std::env::temp_dir().join(format!("concurro-s4d-s1-{}", Uuid::now_v7()));
        fs::create_dir_all(&directory).expect("verifier files");
        let certificate = directory.join("cert.pem");
        let private_key = directory.join("key.pem");
        assert!(
            std::process::Command::new("openssl")
                .args([
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-days",
                    "1",
                    "-subj",
                    "/CN=localhost",
                    "-keyout"
                ])
                .arg(&private_key)
                .arg("-out")
                .arg(&certificate)
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status()
                .expect("openssl")
                .success()
        );
        let registry = directory.join("relays.json");
        fs::write(
            &registry,
            serde_json::to_vec(&json!({
                "pending":{},"blocked":[],"approved":{"relay-test":[vec![1_u8;32],{
                    "user_id":35576205,"login":"payer","route":"claude-subscription",
                    "entry_id":"entry","record_id":"record","version":2
                }]}
            }))
            .expect("registry"),
        )
        .expect("approved verifier registry");
        let jwks = directory.join("exchange.jwks");
        fs::write(&jwks, br#"{"keys":[{"kid":"fixture","kty":"OKP","crv":"Ed25519","x":"11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"}]}"#)
            .expect("public JWKS");
        for (name, value) in [
            ("owner", "owner-test"),
            ("inference", "inference-test"),
            ("release", "release-test"),
        ] {
            fs::write(directory.join(name), value).expect("private verifier bearer");
        }
        let private_port = TcpListener::bind("127.0.0.1:0").expect("private port");
        let private_addr = private_port.local_addr().expect("private address");
        drop(private_port);
        let relay_port = TcpListener::bind("127.0.0.1:0").expect("relay port");
        let relay_addr = relay_port.local_addr().expect("relay address");
        drop(relay_port);
        let private_url = format!("http://{private_addr}");
        let args = vec![
            format!("--certificate={}", certificate.display()),
            format!("--private-key={}", private_key.display()),
            format!("--registry={}", registry.display()),
            format!("--owner-token={}", directory.join("owner").display()),
            format!(
                "--inference-token={}",
                directory.join("inference").display()
            ),
            format!("--release-token={}", directory.join("release").display()),
            format!("--exchange-public-keys={}", jwks.display()),
            format!("--private-listen={private_addr}"),
            format!("--relay-listen={relay_addr}"),
            "--verification-url=https://example.test/credentials/relay".to_owned(),
        ];
        let http = reqwest::Client::new();
        for after_restart in [false, true] {
            let mut child = Command::new(&binary)
                .args(&args)
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .kill_on_drop(true)
                .spawn()
                .expect("S1 verifier process");
            tokio::time::timeout(StdDuration::from_secs(5), async {
                loop {
                    if http
                        .get(format!("{private_url}/owner/v1/relays?user_id=35576205"))
                        .bearer_auth("owner-test")
                        .send()
                        .await
                        .is_ok()
                    {
                        break;
                    }
                    tokio::time::sleep(StdDuration::from_millis(25)).await;
                }
            })
            .await
            .expect("S1 private listener ready");
            let response = http
                .post(format!("{private_url}/dispatch/v2/messages"))
                .bearer_auth("inference-test")
                .header(
                    "x-concurro-claim",
                    admitted.claim.as_deref().expect("claim"),
                )
                .header("x-concurro-request", Uuid::now_v7().to_string())
                .json(&json!({"model":"claude-test","messages":[]}))
                .send()
                .await
                .expect("S1 claim dispatch");
            let status = response.status();
            let error: serde_json::Value = response.json().await.expect("S1 refusal");
            if after_restart {
                assert_eq!(status.as_u16(), 403);
                assert_eq!(error["error"]["code"], "claim_refused");
            } else {
                assert_eq!(status.as_u16(), 503);
                assert_eq!(
                    error["error"]["code"], "unavailable",
                    "valid claim reached a verifier without a connected Sponsor"
                );
                let release = http
                    .post(format!(
                        "{private_url}{}",
                        concurro_exchange_postgres::release_path(pending[0])
                    ))
                    .bearer_auth("release-test")
                    .send()
                    .await
                    .expect("S1 release");
                assert_eq!(release.status().as_u16(), 204);
                assert_eq!(
                    deliver_pending_releases(&restarted, &http, &private_url, "release-test")
                        .await
                        .expect("retry lost release acknowledgement"),
                    1
                );
                assert!(restarted.revoke("relay-grant").await.is_ok());
            }
            child.kill().await.expect("stop S1");
            child.wait().await.expect("S1 exit");
        }
        fs::remove_dir_all(directory).expect("remove verifier fixture");
    } else {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("fake verifier listener");
        let base = format!(
            "http://{}",
            listener.local_addr().expect("verifier address")
        );
        let expected_claim = pending[0];
        let calls = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let verifier = Router::new().route(
            "/release/v1/claims/{claim_id}",
            post({
                let calls = calls.clone();
                move |headers: HeaderMap,
                      axum::extract::Path(claim_id): axum::extract::Path<Uuid>| {
                    let calls = calls.clone();
                    async move {
                        assert_eq!(headers["authorization"], "Bearer release-test");
                        assert_eq!(claim_id, expected_claim);
                        calls.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                        StatusCode::NO_CONTENT
                    }
                }
            }),
        );
        let verifier_server = tokio::spawn(async move {
            axum::serve(listener, verifier)
                .await
                .expect("fake verifier");
        });
        let http = reqwest::Client::new();
        let lost = http
            .post(format!(
                "{base}{}",
                concurro_exchange_postgres::release_path(pending[0])
            ))
            .bearer_auth("release-test")
            .send()
            .await
            .expect("unrecorded S1 release");
        assert_eq!(lost.status().as_u16(), 204);
        assert_eq!(
            restarted.pending_releases().await.expect("restart scan"),
            pending
        );
        assert_eq!(
            deliver_pending_releases(&restarted, &http, &base, "release-test")
                .await
                .expect("retry lost release acknowledgement"),
            1
        );
        assert_eq!(calls.load(std::sync::atomic::Ordering::SeqCst), 2);
        assert!(restarted.revoke("relay-grant").await.is_ok());
        verifier_server.abort();
    }
    assert!(
        restarted
            .pending_releases()
            .await
            .expect("acknowledged")
            .is_empty()
    );
    server.abort();
}
