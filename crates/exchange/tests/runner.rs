//! Real runner listener, signed OIDC and PostgreSQL workflow checks.

use std::{fs, process::Command, sync::Arc, time::Duration};

use axum::{
    Json, Router,
    http::{HeaderMap, StatusCode, header},
    routing::{get, post},
};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use chrono::Utc;
use concurro_contracts::{BlobRef, RunnerClaimReply, RunnerKeyReply, RunnerNextTurn, TurnResultV2};
use concurro_db::Database;
use concurro_events_postgres::{Outbox, migrate as migrate_events};
use concurro_exchange::{RunnerState, runner_router};
use concurro_exchange_postgres::{Store, migrate as migrate_exchange};
use concurro_secrets::{SecretRef, Secrets};
use concurro_testkit::PostgresContainer;
use jsonwebtoken::{Algorithm, EncodingKey, Header};
use object_store::{ObjectStoreExt, memory::InMemory, path::Path};
use rand::thread_rng;
use rsa::{RsaPrivateKey, pkcs1::EncodeRsaPrivateKey, traits::PublicKeyParts};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tokio::sync::Mutex;
use uuid::Uuid;

/// Create a local RSA issuer and its published public JWK.
fn issuer() -> (EncodingKey, Value) {
    let private = RsaPrivateKey::new(&mut thread_rng(), 2048).expect("RSA key");
    let public = private.to_public_key();
    let pem = private.to_pkcs1_pem(Default::default()).expect("PEM");
    let key = EncodingKey::from_rsa_pem(pem.as_bytes()).expect("encoding key");
    let jwks = json!({"keys":[{
        "kty":"RSA", "alg":"RS256", "use":"sig", "kid":"fake-github",
        "n":URL_SAFE_NO_PAD.encode(public.n().to_bytes_be()),
        "e":URL_SAFE_NO_PAD.encode(public.e().to_bytes_be())
    }]});
    (key, jwks)
}

/// Mint one GitHub Actions proof for the configured session and run.
fn token(key: &EncodingKey, session: Uuid, jti: &str, change: Option<(&str, Value)>) -> String {
    let now = Utc::now().timestamp();
    let mut header = Header::new(Algorithm::RS256);
    header.kid = Some("fake-github".to_owned());
    let mut claims = json!({
        "iss":"https://token.actions.githubusercontent.com", "aud":"concurro-gateway",
        "iat":now, "nbf":now, "exp":now+300, "jti":jti,
        "repository_id":"123", "repository_owner_id":"7",
        "job_workflow_ref":"org/fork/.github/workflows/agent.yml@refs/tags/v2",
        "actor_id":"9", "event_name":"workflow_dispatch",
        "ref":format!("refs/heads/concurro/runs/{session}"),
        "runner_environment":"github-hosted", "run_id":"900", "run_attempt":"1"
    });
    if let Some((name, value)) = change {
        claims[name] = value;
    }
    jsonwebtoken::encode(&header, &claims, key).expect("OIDC token")
}

/// Bind disposable PostgreSQL, object storage and fake GitHub to the real router.
async fn setup() -> (
    PostgresContainer,
    Database,
    Store,
    String,
    EncodingKey,
    Arc<InMemory>,
    tokio::task::JoinHandle<()>,
    std::path::PathBuf,
    String,
    RunnerState,
) {
    let postgres = PostgresContainer::start().await.expect("PostgreSQL");
    let database = Database::connect(postgres.url(), 8)
        .await
        .expect("database");
    migrate_events(&database).await.expect("events migrations");
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
    let (signer, jwks) = issuer();
    let jwks_listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("JWKS listener");
    let jwks_url = format!(
        "http://{}/jwks",
        jwks_listener.local_addr().expect("JWKS address")
    );
    tokio::spawn(async move {
        axum::serve(
            jwks_listener,
            Router::new().route("/jwks", get(move || async move { Json(jwks.clone()) })),
        )
        .await
        .expect("JWKS server");
    });
    let inference_listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("inference listener");
    let inference_base_url = format!(
        "http://{}/v1",
        inference_listener.local_addr().expect("inference address")
    );
    tokio::spawn(async move {
        axum::serve(
            inference_listener,
            Router::new().route(
                "/v1/canned",
                post(|headers: HeaderMap| async move {
                    let valid = headers
                        .get(header::AUTHORIZATION)
                        .and_then(|value| value.to_str().ok())
                        .is_some_and(|value| value.starts_with("Bearer sk-concurro-"));
                    if valid {
                        (StatusCode::OK, Json(json!({"message":"fake inference"})))
                    } else {
                        (
                            StatusCode::UNAUTHORIZED,
                            Json(json!({"error":"unauthorized"})),
                        )
                    }
                }),
            ),
        )
        .await
        .expect("fake inference");
    });
    let key_file = std::env::temp_dir().join(format!("exchange-token-key-{}", Uuid::now_v7()));
    fs::write(&key_file, "test-token-key-of-sufficient-length").expect("key file");
    let objects = Arc::new(InMemory::new());
    let state = RunnerState {
        store: store.clone(),
        secrets: Arc::new(Secrets::new(None, Duration::from_secs(60))),
        token_key: SecretRef::File(key_file.clone()),
        jwks_url,
        inference_base_url: inference_base_url.clone(),
        objects: objects.clone(),
        object_prefix: "exchange-test".to_owned(),
        http: reqwest::Client::new(),
        jwks: Arc::new(Mutex::new(None)),
    };
    let restart_state = state.clone();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("runner listener");
    let base = format!("http://{}", listener.local_addr().expect("runner address"));
    let server = tokio::spawn(async move {
        axum::serve(listener, runner_router(state))
            .await
            .expect("runner server")
    });
    (
        postgres,
        database,
        store,
        base,
        signer,
        objects,
        server,
        key_file,
        inference_base_url,
        restart_state,
    )
}

/// Exercise claim, lost-reply replay, key, blobs, result and historical retirement.
#[tokio::test]
async fn runner_canned_turn_and_shared_session_retirement() {
    let (_pg, database, store, base, signer, _objects, server, key_file, inference_url, _restart) =
        setup().await;
    let client = reqwest::Client::new();
    let first: Value = serde_json::from_str(include_str!(
        "../../../contracts/fixtures/exchange.http.v1/operator_submit.json"
    ))
    .expect("fixture");
    let turn = first["attempt_id"]
        .as_str()
        .expect("turn")
        .parse::<Uuid>()
        .expect("UUID");
    let session = store
        .submit(turn, &first)
        .await
        .expect("submit")
        .dispatch
        .expect("dispatch")
        .session_id;
    let route = format!("{base}/runner/v2/sessions/{session}");
    let proof = token(&signer, session, "jti-1", None);
    let reply = client
        .post(format!("{route}/claim"))
        .bearer_auth(&proof)
        .send()
        .await
        .expect("claim");
    assert_eq!(reply.status(), 200);
    let claim: RunnerClaimReply = reply.json().await.expect("claim body");
    assert_eq!(claim.engine.sha, "ab123");
    let repeat = client
        .post(format!("{route}/claim"))
        .bearer_auth(token(&signer, session, "jti-2", None))
        .send()
        .await
        .expect("repeat claim");
    assert_eq!(repeat.status(), 200);
    let repeated: RunnerClaimReply = repeat.json().await.expect("repeat body");
    assert_eq!(repeated.runner_token, claim.runner_token);
    assert_eq!(
        client
            .post(format!("{route}/claim"))
            .bearer_auth(&proof)
            .send()
            .await
            .expect("reused jti")
            .status(),
        401
    );
    let key: RunnerKeyReply = client
        .post(format!("{route}/key"))
        .bearer_auth(&claim.runner_token)
        .json(&json!({"engine_sha":"ab123"}))
        .send()
        .await
        .expect("key")
        .json()
        .await
        .expect("key body");
    assert_eq!(key.inference_base_url, inference_url);
    let first_turn: RunnerNextTurn = client
        .get(format!("{route}/turns/next?wait=0"))
        .bearer_auth(&claim.runner_token)
        .send()
        .await
        .expect("next")
        .json()
        .await
        .expect("turn");
    assert_eq!(first_turn.turn_id, turn);
    let workspace = br#"{"edits":[],"removed":[]}"#;
    let sha = hex::encode(Sha256::digest(workspace));
    let upload = client
        .put(format!("{route}/blobs/workspace/{sha}"))
        .bearer_auth(&claim.runner_token)
        .body(workspace.to_vec())
        .send()
        .await
        .expect("upload");
    assert_eq!(upload.status(), 204);
    assert_eq!(
        client
            .get(format!("{route}/blobs/{sha}"))
            .bearer_auth(&claim.runner_token)
            .send()
            .await
            .expect("unreferenced blob")
            .status(),
        404
    );
    let result = TurnResultV2 {
        outcome: "ok".to_owned(),
        response: json!({"text":"done"}),
        termination_reason: None,
        elapsed_seconds: None,
        workspace: Some(BlobRef {
            sha256: sha.clone(),
            size: workspace.len() as u64,
        }),
        transcript: None,
        retained: None,
    };
    let reply = client
        .put(format!("{route}/turns/{turn}/result"))
        .bearer_auth(&claim.runner_token)
        .json(&result)
        .send()
        .await
        .expect("result");
    assert_eq!(reply.status(), 204);
    let blob = client
        .get(format!("{route}/blobs/{sha}"))
        .bearer_auth(&claim.runner_token)
        .send()
        .await
        .expect("download");
    assert_eq!(blob.bytes().await.expect("bytes").as_ref(), workspace);
    assert_eq!(
        client
            .put(format!("{route}/turns/{turn}/result"))
            .bearer_auth(&claim.runner_token)
            .json(&result)
            .send()
            .await
            .expect("replay result")
            .status(),
        204
    );
    let mut changed = result.clone();
    changed.response = json!({"text":"changed"});
    assert_eq!(
        client
            .put(format!("{route}/turns/{turn}/result"))
            .bearer_auth(&claim.runner_token)
            .json(&changed)
            .send()
            .await
            .expect("conflict")
            .status(),
        409
    );
    let second = Uuid::now_v7();
    let mut second_body = first.clone();
    second_body["attempt_id"] = json!(second);
    let reused = store
        .submit(second, &second_body)
        .await
        .expect("second submit");
    assert_eq!(reused.session_id, Some(session));
    let next_turn: RunnerNextTurn = client
        .get(format!("{route}/turns/next?wait=0"))
        .bearer_auth(&claim.runner_token)
        .send()
        .await
        .expect("next second")
        .json()
        .await
        .expect("second turn");
    assert_eq!(next_turn.turn_id, second);
    assert_eq!(
        store.cancel(turn).await.expect("late retirement").sessions,
        vec![session]
    );
    assert_eq!(
        client
            .post(format!("{route}/heartbeat"))
            .bearer_auth(&claim.runner_token)
            .send()
            .await
            .expect("heartbeat")
            .status(),
        204
    );
    assert_eq!(
        client
            .put(format!("{route}/turns/{second}/result"))
            .bearer_auth(&claim.runner_token)
            .json(&result)
            .send()
            .await
            .expect("second result")
            .status(),
        204
    );
    let snapshots: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM events.outbox WHERE subject LIKE 'exchange.turn.%.status'",
    )
    .fetch_one(database.pool())
    .await
    .expect("status snapshots");
    assert!(snapshots >= 7);
    let settled: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM events.outbox WHERE subject LIKE 'exchange.turn.%.finished'",
    )
    .fetch_one(database.pool())
    .await
    .expect("finished facts");
    assert_eq!(settled, 2, "one fact per result with no open requests");
    assert_eq!(
        client
            .delete(route)
            .bearer_auth(&claim.runner_token)
            .send()
            .await
            .expect("end")
            .status(),
        204
    );
    let third = Uuid::now_v7();
    let mut later = first.clone();
    later["attempt_id"] = json!(third);
    later["grant"]["grant_id"] = json!("org-2");
    let other_session = store
        .submit(third, &later)
        .await
        .expect("other grant")
        .dispatch
        .expect("new session")
        .session_id;
    assert_ne!(other_session, session);
    let other_route = format!("{base}/runner/v2/sessions/{other_session}");
    let other_claim: RunnerClaimReply = client
        .post(format!("{other_route}/claim"))
        .bearer_auth(token(&signer, other_session, "other-session", None))
        .send()
        .await
        .expect("other claim")
        .json()
        .await
        .expect("other claim reply");
    assert_eq!(
        client
            .post(format!("{other_route}/key"))
            .bearer_auth(&other_claim.runner_token)
            .json(&json!({"engine_sha":"ab123"}))
            .send()
            .await
            .expect("other key")
            .status(),
        200
    );
    assert_eq!(
        client
            .get(format!("{other_route}/turns/next?wait=0"))
            .bearer_auth(&other_claim.runner_token)
            .send()
            .await
            .expect("other next")
            .status(),
        200
    );
    assert_eq!(
        client
            .put(format!("{other_route}/blobs/workspace/{sha}"))
            .bearer_auth(&other_claim.runner_token)
            .body(workspace.to_vec())
            .send()
            .await
            .expect("shared blob upload")
            .status(),
        204
    );
    assert_eq!(
        client
            .put(format!("{other_route}/turns/{third}/result"))
            .bearer_auth(&other_claim.runner_token)
            .json(&result)
            .send()
            .await
            .expect("shared blob result")
            .status(),
        204
    );
    let uploads: i64 =
        sqlx::query_scalar("SELECT count(*) FROM exchange.blob_uploads WHERE sha256=$1")
            .bind(&sha)
            .fetch_one(database.pool())
            .await
            .expect("blob upload links");
    assert_eq!(uploads, 2);
    fs::remove_file(key_file).expect("remove test key");
    server.abort();
}

/// Withdrawal rejects a late result and promotes the next queued turn.
#[tokio::test]
async fn cancelled_turn_rejects_result_and_promotes_next() {
    let (_pg, _database, store, base, signer, _objects, server, key_file, _inference, _restart) =
        setup().await;
    let mut body: Value = serde_json::from_str(include_str!(
        "../../../contracts/fixtures/exchange.http.v1/operator_submit.json"
    ))
    .expect("fixture");
    let first: Uuid = body["attempt_id"]
        .as_str()
        .expect("first turn")
        .parse()
        .expect("UUID");
    let session = store
        .submit(first, &body)
        .await
        .expect("submit")
        .dispatch
        .expect("dispatch")
        .session_id;
    let client = reqwest::Client::new();
    let route = format!("{base}/runner/v2/sessions/{session}");
    let claim: RunnerClaimReply = client
        .post(format!("{route}/claim"))
        .bearer_auth(token(&signer, session, "cancelled", None))
        .send()
        .await
        .expect("claim")
        .json()
        .await
        .expect("claim reply");
    assert_eq!(
        client
            .post(format!("{route}/key"))
            .bearer_auth(&claim.runner_token)
            .json(&json!({"engine_sha":"ab123"}))
            .send()
            .await
            .expect("key")
            .status(),
        200
    );
    assert_eq!(
        client
            .get(format!("{route}/turns/next?wait=0"))
            .bearer_auth(&claim.runner_token)
            .send()
            .await
            .expect("first next")
            .status(),
        200
    );
    let second = Uuid::now_v7();
    body["attempt_id"] = json!(second);
    assert_eq!(
        store
            .submit(second, &body)
            .await
            .expect("second submit")
            .session_id,
        None
    );
    store.cancel(first).await.expect("withdraw first");
    let late = TurnResultV2 {
        outcome: "ok".to_owned(),
        response: json!({}),
        termination_reason: None,
        elapsed_seconds: None,
        workspace: None,
        transcript: None,
        retained: None,
    };
    assert_eq!(
        client
            .put(format!("{route}/turns/{first}/result"))
            .bearer_auth("wrong-runner")
            .json(&late)
            .send()
            .await
            .expect("wrong token")
            .status(),
        401
    );
    assert_eq!(
        client
            .put(format!("{route}/turns/{first}/result"))
            .bearer_auth(&claim.runner_token)
            .json(&late)
            .send()
            .await
            .expect("late result")
            .status(),
        409
    );
    let promoted = store.observation(second).await.expect("promoted turn");
    assert_ne!(promoted.session_id, Some(session));
    assert!(promoted.dispatch.is_some());
    fs::remove_file(key_file).expect("test key cleanup");
    server.abort();
}

/// Run the actual Python engine loop, Git workspace and artifact upload against Rust.
#[tokio::test]
async fn python_engine_completes_canned_turn_with_fake_inference() {
    let (_pg, database, store, base, signer, objects, server, key_file, _inference, _restart) =
        setup().await;
    let repo = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let helper = repo.join("engine/tests/s4b_canned.py");
    let root = std::env::temp_dir().join(format!("concurro-s4b-engine-{}", Uuid::now_v7()));
    let env_path = format!(
        "{}:{}",
        repo.join("engine").display(),
        repo.join("engine/tests").display()
    );
    let prepared = Command::new("python3")
        .arg(&helper)
        .arg("prepare")
        .arg(&root)
        .env("PYTHONPATH", &env_path)
        .current_dir(&repo)
        .output()
        .expect("prepare fixture");
    assert!(
        prepared.status.success(),
        "fixture preparation: {}",
        String::from_utf8_lossy(&prepared.stderr)
    );
    let base_sha = String::from_utf8(prepared.stdout)
        .expect("SHA text")
        .trim()
        .to_owned();
    assert_eq!(base_sha.len(), 40);
    let retained = fs::read(root.join("retained-input.tar.gz")).expect("retained input");
    let retained_sha = hex::encode(Sha256::digest(&retained));
    objects
        .put(
            &Path::parse(format!("exchange-test/sha256/{retained_sha}.tar.gz"))
                .expect("object key"),
            retained.clone().into(),
        )
        .await
        .expect("input blob");
    let mut body: Value = serde_json::from_str(include_str!(
        "../../../contracts/fixtures/exchange.http.v1/operator_submit.json"
    ))
    .expect("submission fixture");
    let turn = Uuid::now_v7();
    body["attempt_id"] = json!(turn);
    body["payload"] = json!({
        "request": {"stage":"develop", "provider":"claude", "model":"claude-test",
            "gateway":{"url":"http://untrusted.invalid/v1"},
            "session":{"scope":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd","turn":0},
            "context":{"base":base_sha}},
        "feedback":{},
        "spec":{"refs":{"main":base_sha}, "head":"main", "base_branch":"main", "candidate":null},
        "timeout":30.0,
        "retained":{"sha256":retained_sha,"size":retained.len()}
    });
    let session = store
        .submit(turn, &body)
        .await
        .expect("submit")
        .dispatch
        .expect("dispatch")
        .session_id;
    let oidc = token(&signer, session, "python-engine-jti", None);
    let output = tokio::task::spawn_blocking(move || {
        Command::new("python3")
            .arg(helper)
            .arg("serve")
            .arg(&root)
            .arg(base)
            .arg(session.to_string())
            .arg(oidc)
            .env("PYTHONPATH", env_path)
            .current_dir(repo)
            .output()
    })
    .await
    .expect("engine join")
    .expect("engine process");
    assert!(
        output.status.success(),
        "engine failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let observed = store
        .observation(turn)
        .await
        .expect("completed observation");
    assert_eq!(observed.status, "finished");
    let result = observed.result.expect("native result");
    assert_eq!(result.outcome, "ok");
    assert!(result.workspace.is_some() && result.retained.is_some());
    let snapshots: i64 = sqlx::query_scalar("SELECT count(*) FROM events.outbox WHERE subject=$1")
        .bind(format!("exchange.turn.{turn}.status"))
        .fetch_one(database.pool())
        .await
        .expect("status snapshots");
    assert!(snapshots >= 4);
    fs::remove_file(key_file).expect("test key cleanup");
    server.abort();
}

/// Refuse foreign and mismatched proofs, then replay a lost claim after restart.
#[tokio::test]
async fn oidc_refusals_and_claim_replay_survive_restart() {
    let (_pg, database, store, base, signer, _objects, server, key_file, _inference, mut restart) =
        setup().await;
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
    let route = format!("{base}/runner/v2/sessions/{session}/claim");
    let client = reqwest::Client::new();
    let foreign = token(
        &signer,
        session,
        "foreign",
        Some(("repository_id", json!("other"))),
    );
    assert_eq!(
        client
            .post(&route)
            .bearer_auth(foreign)
            .send()
            .await
            .expect("foreign")
            .status(),
        403
    );
    let refusal: Option<String> =
        sqlx::query_scalar("SELECT refusal FROM exchange.sessions WHERE session_id=$1")
            .bind(session)
            .fetch_one(database.pool())
            .await
            .expect("refusal");
    assert!(
        refusal.is_none(),
        "foreign repositories leave no session diagnostic"
    );
    let foreign_owner = token(
        &signer,
        session,
        "foreign-owner",
        Some(("repository_owner_id", json!("other"))),
    );
    assert_eq!(
        client
            .post(&route)
            .bearer_auth(foreign_owner)
            .send()
            .await
            .expect("foreign owner")
            .status(),
        403
    );
    let refusal: Option<String> =
        sqlx::query_scalar("SELECT refusal FROM exchange.sessions WHERE session_id=$1")
            .bind(session)
            .fetch_one(database.pool())
            .await
            .expect("refusal");
    assert!(
        refusal.is_none(),
        "foreign owners leave no session diagnostic"
    );
    for (index, field, value, status) in [
        (0, "iss", json!("https://untrusted.invalid"), 401),
        (1, "aud", json!("other"), 401),
        (2, "exp", json!(Utc::now().timestamp() - 120), 401),
        (3, "nbf", json!(Utc::now().timestamp() + 120), 401),
        (4, "iat", json!(Utc::now().timestamp() - 700), 401),
        (5, "jti", json!(""), 401),
        (6, "job_workflow_ref", json!("other"), 403),
        (7, "event_name", json!("push"), 403),
        (8, "ref", json!("refs/heads/main"), 403),
        (9, "runner_environment", json!("self-hosted"), 403),
        (10, "run_attempt", json!("2"), 403),
    ] {
        let proof = token(
            &signer,
            session,
            &format!("invalid-{index}"),
            Some((field, value)),
        );
        assert_eq!(
            client
                .post(&route)
                .bearer_auth(proof)
                .send()
                .await
                .expect(field)
                .status()
                .as_u16(),
            status,
            "{field}"
        );
    }
    let wrong_actor = token(
        &signer,
        session,
        "wrong-actor",
        Some(("actor_id", json!("other"))),
    );
    assert_eq!(
        client
            .post(&route)
            .bearer_auth(wrong_actor)
            .send()
            .await
            .expect("actor")
            .status(),
        403
    );
    let refusal: Option<String> =
        sqlx::query_scalar("SELECT refusal FROM exchange.sessions WHERE session_id=$1")
            .bind(session)
            .fetch_one(database.pool())
            .await
            .expect("refusal");
    assert!(refusal.is_some());
    let valid = token(&signer, session, "first-claim", None);
    let first: RunnerClaimReply = client
        .post(&route)
        .bearer_auth(&valid)
        .send()
        .await
        .expect("valid claim")
        .json()
        .await
        .expect("reply");
    let key_route = format!("{base}/runner/v2/sessions/{session}/key");
    let first_key: RunnerKeyReply = client
        .post(&key_route)
        .bearer_auth(&first.runner_token)
        .json(&json!({"engine_sha":"ab123"}))
        .send()
        .await
        .expect("first key")
        .json()
        .await
        .expect("first key reply");
    server.abort();
    restart.store = Store::new(
        database.clone(),
        Outbox::new(
            1_048_576,
            vec!["exchange.turn.>".to_owned(), "inference.usage".to_owned()],
        ),
    );
    restart.secrets = Arc::new(Secrets::new(None, Duration::ZERO));
    restart.jwks = Arc::new(Mutex::new(None));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("restart listener");
    let reopened = format!(
        "http://{}/runner/v2/sessions/{session}/claim",
        listener.local_addr().expect("address")
    );
    let server = tokio::spawn(async move {
        axum::serve(listener, runner_router(restart))
            .await
            .expect("restarted runner");
    });
    let repeat: RunnerClaimReply = client
        .post(&reopened)
        .bearer_auth(token(&signer, session, "second-claim", None))
        .send()
        .await
        .expect("retry after restart")
        .json()
        .await
        .expect("repeat reply");
    assert_eq!(repeat.runner_token, first.runner_token);
    let reopened_key = reopened.replace("/claim", "/key");
    let second_key: RunnerKeyReply = client
        .post(&reopened_key)
        .bearer_auth(&repeat.runner_token)
        .json(&json!({"engine_sha":"ab123"}))
        .send()
        .await
        .expect("restarted key")
        .json()
        .await
        .expect("restarted key reply");
    assert_eq!(second_key.session_key, first_key.session_key);
    assert_eq!(
        client
            .post(&reopened)
            .bearer_auth(valid)
            .send()
            .await
            .expect("used jti")
            .status(),
        401
    );
    assert_eq!(
        client
            .post(&reopened)
            .bearer_auth(token(
                &signer,
                session,
                "other-run",
                Some(("run_id", json!("901")))
            ))
            .send()
            .await
            .expect("other run")
            .status(),
        403
    );
    fs::remove_file(key_file).expect("test key cleanup");
    server.abort();
}

/// Replace an unbound dispatch and lose a stale running turn during reconciliation.
#[tokio::test]
async fn reconciliation_replaces_unbound_and_loses_stale_running_work() {
    let (_pg, database, store, base, signer, _objects, server, key_file, _inference, _restart) =
        setup().await;
    let body: Value = serde_json::from_str(include_str!(
        "../../../contracts/fixtures/exchange.http.v1/operator_submit.json"
    ))
    .expect("fixture");
    let turn: Uuid = body["attempt_id"]
        .as_str()
        .expect("turn")
        .parse()
        .expect("UUID");
    let old = store
        .submit(turn, &body)
        .await
        .expect("submit")
        .dispatch
        .expect("dispatch")
        .session_id;
    sqlx::query(
        "UPDATE exchange.sessions SET created=now()-interval '1201 seconds' WHERE session_id=$1",
    )
    .bind(old)
    .execute(database.pool())
    .await
    .expect("age unbound");
    assert_eq!(store.reconcile_sessions().await.expect("reconcile"), 1);
    let fresh = store
        .observation(turn)
        .await
        .expect("replacement")
        .dispatch
        .expect("new dispatch")
        .session_id;
    assert_ne!(fresh, old);
    let client = reqwest::Client::new();
    let route = format!("{base}/runner/v2/sessions/{fresh}");
    let claim: RunnerClaimReply = client
        .post(format!("{route}/claim"))
        .bearer_auth(token(&signer, fresh, "stale-session", None))
        .send()
        .await
        .expect("claim")
        .json()
        .await
        .expect("claim reply");
    assert_eq!(
        client
            .post(format!("{route}/key"))
            .bearer_auth(&claim.runner_token)
            .json(&json!({"engine_sha":"ab123"}))
            .send()
            .await
            .expect("key")
            .status(),
        200
    );
    assert_eq!(
        client
            .get(format!("{route}/turns/next?wait=0"))
            .bearer_auth(&claim.runner_token)
            .send()
            .await
            .expect("next")
            .status(),
        200
    );
    sqlx::query(
        "UPDATE exchange.sessions SET last_seen=now()-interval '181 seconds' WHERE session_id=$1",
    )
    .bind(fresh)
    .execute(database.pool())
    .await
    .expect("age heartbeat");
    assert_eq!(
        store.reconcile_sessions().await.expect("reconcile running"),
        1
    );
    assert_eq!(
        store.observation(turn).await.expect("lost turn").status,
        "lost"
    );
    assert_eq!(
        client
            .post(format!("{route}/heartbeat"))
            .bearer_auth(&claim.runner_token)
            .send()
            .await
            .expect("ended heartbeat")
            .status(),
        410
    );
    fs::remove_file(key_file).expect("test key cleanup");
    server.abort();
}
