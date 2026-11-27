//! Relay dispatch preserves the exchange claim and provider request identity.

use std::{
    collections::HashMap,
    env, fs,
    path::{Path, PathBuf},
    process::Stdio,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};

use axum::{
    Json, Router,
    http::{HeaderMap, StatusCode},
    response::IntoResponse,
    routing::post,
};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use chrono::Utc;
use concurro_db::Database;
use concurro_events_postgres::{Outbox, migrate as migrate_events};
use concurro_exchange::{AdmissionState, admission_router};
use concurro_exchange_postgres::{RelaySigner, Store, migrate as migrate_exchange};
use concurro_inference_gateway::{GatewayState, RelayDispatch, router};
use concurro_relay_hosted_api::{
    claim::VerificationKeys,
    dispatch::{CLAIM_HEADER, REQUEST_HEADER},
};
use concurro_secrets::{SecretRef, Secrets};
use concurro_tasks_core::python_digest;
use concurro_testkit::PostgresContainer;
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tokio::{io::AsyncWriteExt, process::Command, time};
use uuid::Uuid;

/// A verifier's reused-ID refusal is forwarded once with the same signed authority.
#[tokio::test]
async fn relay_dispatch_does_not_retry_request_reused_with_a_new_id() {
    dispatch_case(false, None).await;
}

/// Gemini keeps canonical claims, unwraps SSE and settles late usage exactly once.
#[tokio::test]
async fn antigravity_dispatch_preserves_claims_and_settles_usage() {
    dispatch_case(true, None).await;
}

/// Local hosted verifier and paired Sponsor supplied by the joined-test launcher.
#[derive(Deserialize)]
struct Joined {
    /// Loopback production verifier private listener.
    verifier_url: String,
    /// Exact identity approved by the fixture owner API.
    relay_id: String,
    /// Private location for CLI homes and settlement evidence.
    output_dir: PathBuf,
}

/// Real agy, gateway, signed admission, hosted verifier and Sponsor reach the live provider.
#[tokio::test]
#[ignore = "requires explicitly configured local verifier and signed-in Sponsor; consumes subscription quota"]
async fn agy_joined_subscription() {
    let path = env::var_os("CONCURRO_AGY_JOINED_CONFIG").expect("joined fixture config");
    let joined = serde_json::from_slice(&fs::read(path).unwrap()).unwrap();
    dispatch_case(true, Some(joined)).await;
}

/// Exercise fresh and resumed CLI turns and retain only nonsecret settlement evidence.
async fn joined_turns(gateway: &str, joined: &Joined, database: &Database, turn: Uuid) {
    fs::create_dir_all(&joined.output_dir).unwrap();
    let schema = joined.output_dir.join("schema.json");
    fs::write(&schema, r#"{"type":"object","properties":{"marker":{"type":"integer"}},"required":["marker"],"additionalProperties":false}"#).unwrap();
    let binary = env::var_os("CONCURRO_AGY_RUNNER").map_or_else(
        || Path::new(env!("CARGO_MANIFEST_DIR")).join("../../target/debug/agy-runner"),
        PathBuf::from,
    );
    let mut conversation = Value::Null;
    let mut observations = Vec::new();
    for index in 0..2 {
        let base = joined.output_dir.join(format!("turn-{index}"));
        fs::create_dir_all(base.join("checkout")).unwrap();
        let invocation = json!({
            "model":"gemini-4-neon", "effort":"low", "workspace":base.join("checkout"),
            "scratch":base.join("scratch"), "retained_root":joined.output_dir.join("retained"),
            "resume_id":conversation, "gateway_url":gateway, "schema_path":schema,
            "prompt":if index == 0 { "Return marker 741 using the finish tool. Do not read files, run commands or use other tools." } else { "Return the previous marker plus one using the finish tool. Do not read files, run commands or use other tools." },
            "timeout":180, "mcp_servers":{}
        });
        let started = time::Instant::now();
        let mut child = Command::new(&binary)
            .env("CONCURRO_GATEWAY_KEY", "relay-session")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .spawn()
            .expect("agy-runner");
        child
            .stdin
            .take()
            .unwrap()
            .write_all(&serde_json::to_vec(&invocation).unwrap())
            .await
            .unwrap();
        let result = time::timeout(Duration::from_secs(200), child.wait_with_output())
            .await
            .unwrap()
            .unwrap();
        assert!(
            result.status.success(),
            "agy-runner failed: {}",
            String::from_utf8_lossy(&result.stderr)
        );
        let completed: Value = serde_json::from_slice(&result.stdout).expect("structured output");
        assert_eq!(completed["structured_output"]["marker"], 741 + index);
        if index > 0 {
            assert_eq!(completed["conversation_id"], conversation);
        }
        conversation = completed["conversation_id"].clone();
        observations.push(json!({"turn":index,"elapsed_seconds":started.elapsed().as_secs_f64(),"completed":completed}));
    }
    time::timeout(Duration::from_secs(10), async {
        loop {
            let pending: i64 = sqlx::query_scalar("SELECT count(*) FROM exchange.inference_requests WHERE turn_id=$1 AND finished IS NULL").bind(turn).fetch_one(database.pool()).await.unwrap();
            if pending == 0 { break; }
            time::sleep(Duration::from_millis(50)).await;
        }
    }).await.unwrap();
    let settled: Vec<Value> = sqlx::query_scalar("SELECT jsonb_build_object('request_id',request_id,'category',category,'dispatched',dispatched,'usage',usage) FROM exchange.inference_requests WHERE turn_id=$1 ORDER BY started").bind(turn).fetch_all(database.pool()).await.unwrap();
    assert!(
        settled
            .iter()
            .filter(|r| r["dispatched"] == true && r["category"] == "ok")
            .count()
            >= 2
    );
    assert!(
        settled
            .iter()
            .filter(|r| r["dispatched"] == true)
            .all(|r| r["category"] == "ok"),
        "{settled:?}"
    );
    let evidence = json!({"turns":observations,"settled":settled});
    fs::write(
        joined.output_dir.join("joined-result.json"),
        serde_json::to_vec_pretty(&evidence).unwrap(),
    )
    .unwrap();
    eprintln!(
        "joined subscription passed: {}",
        joined.output_dir.display()
    );
}

/// Run either provider through real exchange admission and a deterministic verifier.
async fn dispatch_case(google: bool, joined: Option<Joined>) {
    let model = if google {
        "gemini-4-neon"
    } else {
        "claude-test"
    };
    let route = if google {
        "antigravity-subscription"
    } else {
        "claude-subscription"
    };
    let postgres = PostgresContainer::start().await.expect("PostgreSQL");
    let database = Database::connect(postgres.url(), 8)
        .await
        .expect("database");
    migrate_events(&database).await.expect("events");
    migrate_exchange(&database).await.expect("exchange");
    let private = URL_SAFE_NO_PAD
        .decode("MC4CAQAwBQYDK2VwBCIEIJ1hsZ3v_VpguoRK9JLsLMREScVpezJpGXA7rAMcrn9g")
        .expect("PKCS8 fixture");
    let store = Store::new(
        database.clone(),
        Outbox::new(
            1_048_576,
            vec!["exchange.turn.>".to_owned(), "inference.usage".to_owned()],
        ),
    )
    .with_relay(RelaySigner::new("fixture".to_owned(), private, 1000).expect("signer"));
    let mut body: Value = serde_json::from_str(include_str!(
        "../../../contracts/fixtures/exchange.http.v1/operator_submit.json"
    ))
    .expect("fixture");
    let turn = Uuid::now_v7();
    body["attempt_id"] = json!(turn);
    body["grant"]["grant_id"] = json!(turn.to_string());
    body["grant"]["purpose"] = json!("task_contributed");
    body["grant"]["payer"] = json!({"id":35576205,"login":"payer"});
    body["grant"]["credential"] = json!({"route":route,"entry_id":"entry",
        "record_id":"record","version":1,"custody":"relay","relay_id":"relay-1"});
    if let Some(joined) = &joined {
        body["grant"]["credential"]["relay_id"] = json!(joined.relay_id);
        body["grant"]["credential"]["entry_id"] = json!("1".repeat(32));
        body["grant"]["credential"]["record_id"] = json!("2".repeat(64));
        body["grant"]["limits"]["tokens"] = json!(50000);
        body["budget"] = json!(50000);
    }
    body["grant"]["models"] = json!([model]);
    body["models"] = json!([model]);
    body["staffing_group"] = json!(
        python_digest(&json!([
            body["expected_claims"],
            body["binding"],
            body["models"],
            body["budget"],
            body["lease_seconds"].as_f64().unwrap()
        ]))
        .unwrap()
    );
    let session = store
        .submit(turn, &body)
        .await
        .expect("submit")
        .dispatch
        .expect("dispatch")
        .session_id;
    sqlx::query("UPDATE exchange.sessions SET session_key_hash=$2,bound_at=now(),last_seen=now() WHERE session_id=$1")
        .bind(session).bind(hex::encode(Sha256::digest(b"relay-session")))
        .execute(database.pool()).await.expect("session key");
    sqlx::query("UPDATE exchange.turns SET status='running' WHERE turn_id=$1")
        .bind(turn)
        .execute(database.pool())
        .await
        .expect("running");
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("admission listener");
    let admission_url = format!("http://{}", listener.local_addr().expect("address"));
    let admission_server = tokio::spawn({
        let store = store.clone();
        async move {
            axum::serve(
                listener,
                admission_router(AdmissionState {
                    store,
                    bearer: "admission-test".to_owned(),
                }),
            )
            .await
            .expect("admission server");
        }
    });
    let keys = Arc::new(VerificationKeys::from_jwks(br#"{"keys":[{"kid":"fixture","kty":"OKP","crv":"Ed25519","x":"11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"}]}"#)
        .expect("S1 keys"));
    let calls = Arc::new(AtomicUsize::new(0));
    let verifier = Router::new().route(
        if google { "/dispatch/v2/antigravity" } else { "/dispatch/v2/messages" },
        post({
            let calls = calls.clone();
            move |headers: HeaderMap, Json(body): Json<Value>| {
                let calls = calls.clone();
                let keys = keys.clone();
                async move {
                    calls.fetch_add(1, Ordering::SeqCst);
                    assert_eq!(
                        headers.get("authorization").expect("verifier credential"),
                        "Bearer inference-test"
                    );
                    let claim = keys
                        .verify(
                            headers
                                .get(CLAIM_HEADER)
                                .expect("claim")
                                .to_str()
                                .expect("JWS"),
                            Utc::now().timestamp(),
                        )
                        .expect("S1 claim verifier");
                    assert_eq!(claim.sub, session.to_string());
                    assert!(
                        Uuid::parse_str(
                            headers
                                .get(REQUEST_HEADER)
                                .expect("request ID")
                                .to_str()
                                .expect("UUID")
                        )
                        .is_ok()
                    );
                    assert_eq!(claim.route, route);
                    assert_eq!(claim.models, vec![model]);
                    assert!(headers.get("x-goog-api-key").is_none());
                    assert!(headers.get("x-goog-user-project").is_none());
                    let request = if google {
                        assert_eq!(body["model"], model);
                        assert!(body.get("project").is_none());
                        assert_eq!(body["request"]["contents"][0]["parts"][0]["thoughtSignature"], "signed-history");
                        &body["request"]
                    } else { &body };
                    if google && request["case"] == "success" {
                        return ([("content-type", "text/event-stream")], concat!(
                            "data: {\"response\":{\"candidates\":[{\"content\":{\"parts\":[{\"text\":\"ok\",\"thoughtSignature\":\"signed-response\"}]},\"finishReason\":\"STOP\"}]}}\n\n",
                            "data: {\"response\":{\"usageMetadata\":{\"promptTokenCount\":2,\"toolUsePromptTokenCount\":1,\"candidatesTokenCount\":1,\"thoughtsTokenCount\":1}}}\n\n"
                        )).into_response();
                    }
                    match request["case"].as_str() {
                        Some("claim_refused") => (
                            StatusCode::FORBIDDEN,
                            Json(json!({"error":{"code":"claim_refused"}})),
                        ),
                        Some("unavailable") => (
                            StatusCode::SERVICE_UNAVAILABLE,
                            Json(json!({"error":{"code":"unavailable"}})),
                        ),
                        Some("success") => (
                            StatusCode::OK,
                            Json(json!({
                                "type":"message","stop_reason":"end_turn",
                                "usage":{"input_tokens":3,"output_tokens":2}
                            })),
                        ),
                        _ => (
                            StatusCode::CONFLICT,
                            Json(
                                json!({"error":{"code":"request_reused","message":"already used"}}),
                            ),
                        ),
                    }.into_response()
                }
            }
        }),
    );
    let verifier_listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("verifier listener");
    let verifier_url = format!(
        "http://{}",
        verifier_listener.local_addr().expect("verifier address")
    );
    let verifier_server = tokio::spawn(async move {
        axum::serve(verifier_listener, verifier)
            .await
            .expect("verifier");
    });
    let token_file =
        std::env::temp_dir().join(format!("concurro-s4d-inference-{}", Uuid::now_v7()));
    std::fs::write(&token_file, "inference-test\n").expect("verifier credential file");
    let gateway = router(GatewayState {
        admission: concurro_exchange_client::AdmissionClient::new(
            reqwest::Client::new(),
            admission_url,
            "admission-test",
        ),
        provider: reqwest::Client::new(),
        routes: Arc::new(HashMap::new()),
        personal_routes: Arc::new(HashMap::new()),
        relay: Some(RelayDispatch {
            verifier: joined
                .as_ref()
                .map_or(verifier_url, |joined| joined.verifier_url.clone()),
            credential: SecretRef::File(token_file.clone()),
        }),
        secrets: Arc::new(Secrets::new(None, Duration::from_secs(60))),
    });
    let gateway_listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("gateway listener");
    let gateway_url = format!(
        "http://{}",
        gateway_listener.local_addr().expect("gateway address")
    );
    let gateway_server = tokio::spawn(async move {
        axum::serve(gateway_listener, gateway)
            .await
            .expect("gateway");
    });
    if let Some(joined) = &joined {
        joined_turns(&gateway_url, joined, &database, turn).await;
        gateway_server.abort();
        verifier_server.abort();
        admission_server.abort();
        fs::remove_file(token_file).unwrap();
        return;
    }
    for (index, case, path, status, category, dispatched) in [
        (
            1_i64,
            "request_reused",
            "/v1/messages",
            StatusCode::CONFLICT,
            "unknown",
            true,
        ),
        (
            2,
            "claim_refused",
            "/v1/messages",
            StatusCode::FORBIDDEN,
            "unknown",
            true,
        ),
        (
            3,
            "unsupported",
            "/v1/messages/count_tokens",
            StatusCode::UNPROCESSABLE_ENTITY,
            "unknown",
            false,
        ),
        (4, "success", "/v1/messages", StatusCode::OK, "ok", true),
        (
            5,
            "unavailable",
            "/v1/messages",
            StatusCode::SERVICE_UNAVAILABLE,
            "unavailable",
            true,
        ),
    ] {
        let path = if google {
            if case == "unsupported" {
                "/v1beta/models/gemini-4-neon:generateContent"
            } else {
                "/v1beta/models/gemini-4-neon:streamGenerateContent?alt=sse"
            }
        } else {
            path
        };
        let response = reqwest::Client::new()
            .post(format!("{gateway_url}{path}"))
            .header(if google { "x-goog-api-key" } else { "authorization" }, if google { "relay-session" } else { "Bearer relay-session" })
            .header("x-goog-user-project", "caller-project")
            .json(&json!({"model":model,"messages":[],"case":case,"contents":[{"role":"model","parts":[{"thoughtSignature":"signed-history"}]}]}))
            .send()
            .await
            .expect("relay dispatch");
        assert_eq!(response.status(), status, "{case}");
        let response = response.bytes().await.expect("complete relay response");
        if google && case == "success" {
            let text = std::str::from_utf8(&response).unwrap();
            assert!(text.contains("signed-response"));
            assert!(!text.contains("\"response\""));
        }
        tokio::time::timeout(Duration::from_secs(5), async {
            loop {
                let finished: i64 = sqlx::query_scalar(
                    "SELECT count(*) FROM exchange.inference_requests WHERE turn_id=$1 AND finished IS NOT NULL"
                ).bind(turn).fetch_one(database.pool()).await.expect("settled request count");
                if finished >= index { break; }
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
        }).await.expect("relay finish report");
        let recorded: (String, bool) = sqlx::query_as(
            "SELECT category,dispatched FROM exchange.inference_requests WHERE turn_id=$1 ORDER BY finished DESC LIMIT 1"
        ).bind(turn).fetch_one(database.pool()).await.expect("settled relay request");
        assert_eq!(recorded, (category.to_owned(), dispatched), "{case}");
        if case == "success" {
            let usage: (i64, i64) = sqlx::query_as(
                "SELECT (usage->>'input_tokens')::bigint,(usage->>'output_tokens')::bigint \
                 FROM exchange.inference_requests WHERE turn_id=$1 ORDER BY finished DESC LIMIT 1",
            )
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("relay usage");
            assert_eq!(usage, (3, 2));
        }
    }
    assert_eq!(calls.load(Ordering::SeqCst), 4);
    let requests: i64 =
        sqlx::query_scalar("SELECT count(*) FROM exchange.inference_requests WHERE turn_id=$1")
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("request count");
    assert_eq!(requests, 5);
    let failure: Value =
        sqlx::query_scalar("SELECT provider_failure FROM exchange.turns WHERE turn_id=$1")
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("relay failure");
    assert_eq!(failure["category"], "unavailable");
    gateway_server.abort();
    verifier_server.abort();
    admission_server.abort();
    std::fs::remove_file(token_file).expect("remove verifier credential");
}
