//! Private current-target API authorization, pinning and redaction.

use std::{
    collections::{BTreeMap, HashMap},
    process::Command,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
};

use axum::{
    Json, Router,
    extract::{Path, Query},
    http::StatusCode,
    response::IntoResponse,
    routing::{get, post},
};
use base64::{Engine, engine::general_purpose::STANDARD};
use chrono::{Duration, Utc};
use concurro_db::Database;
use concurro_events_postgres::Outbox;
use concurro_github_v1::AppClient;
use concurro_tasks::{
    DecisionContext, DecisionTarget, InitializationTarget,
    api::{TaskApi, router},
};
use serde_json::{Value, json};
use sqlx::postgres::PgPoolOptions;

/// Every config candidate is read at the current default commit under the read bearer.
#[tokio::test]
async fn current_target_configuration_is_pinned_allowlisted_and_requires_the_read_bearer() {
    let sha = Arc::new(Mutex::new("a".repeat(40)));
    let observed = Arc::new(Mutex::new(Vec::new()));
    let broken = Arc::new(AtomicBool::new(false));
    let read_sha = sha.clone();
    let file_reads = observed.clone();
    let broken_source = broken.clone();
    let mut config: Value = serde_json::from_str(include_str!(
        "../../../engine/tests/fixtures/legacy-config.json"
    ))
    .unwrap();
    config["repository"] = json!("acme/fork");
    config["mcp"] = json!({"servers":{"tool":{"transport":"stdio","command":"tool","env":{"SECRET":"private-value"}}}});
    let text = serde_json::to_string(&config).unwrap();
    let app=Router::new()
        .route("/repos/acme/fork/installation",get(|| async {Json(json!({"id":7,"repository_selection":"selected","account":{"login":"acme"}}))}))
        .route("/app/installations/7/access_tokens",post(|Json(body):Json<Value>| async move {
            assert_eq!(body["permissions"]["contents"],"read");
            Json(json!({"token":"scoped-token","permissions":body["permissions"],"expires_at":(Utc::now()+Duration::minutes(55)).to_rfc3339()}))
        }))
        .route("/installation/repositories",get(|| async {Json(json!({"total_count":1,"repositories":[{"full_name":"acme/fork"}]}))}))
        .route("/repos/acme/fork",get(|| async {Json(json!({"default_branch":"executor"}))}))
        .route("/repos/acme/fork/git/ref/heads/executor",get(move || {
            let sha=read_sha.clone(); async move {Json(json!({"object":{"sha":sha.lock().unwrap().clone()}}))}
        }))
        .route("/repos/acme/fork/contents/{*file}",get(move |Path(file):Path<String>,Query(query):Query<HashMap<String,String>>| {
            let observed=file_reads.clone(); let text=text.clone(); let broken=broken_source.clone();
            async move {
                observed.lock().unwrap().push((file.clone(),query["ref"].clone()));
                if file.ends_with(".yaml") {return StatusCode::NOT_FOUND.into_response();}
                let content=if broken.load(Ordering::SeqCst) {"{invalid private-value"} else {&text};
                Json(json!({"type":"file","path":file,"size":content.len(),"encoding":"base64","content":STANDARD.encode(content)})).into_response()
            }
        }));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let github_url = format!("http://{}", listener.local_addr().unwrap());
    let github_server = tokio::spawn(async move { axum::serve(listener, app).await });
    let key = Command::new("openssl")
        .args([
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
        ])
        .output()
        .unwrap();
    assert!(key.status.success());
    let github = AppClient::new(reqwest::Client::new(), &github_url, 42, &key.stdout).unwrap();
    let permissions = BTreeMap::from([("contents".to_owned(), "read".to_owned())]);
    let target = InitializationTarget {
        issue_repository: "acme/issues".into(),
        fork: "acme/fork".into(),
        upstream: "acme/upstream".into(),
        issue_permissions: BTreeMap::new(),
        fork_permissions: permissions.clone(),
        upstream_permissions: BTreeMap::new(),
        bot_login: "concurro[bot]".into(),
        publisher_logins: vec![],
        engine_sha: "c".repeat(40),
    };
    let api = TaskApi {
        database: Database::from_pool(
            PgPoolOptions::new()
                .connect_lazy("postgres://unused:unused@127.0.0.1:1/unused")
                .unwrap(),
        ),
        decisions: DecisionContext {
            github,
            targets: HashMap::from([(
                8,
                DecisionTarget {
                    repository: "acme/issues".into(),
                    read_permissions: BTreeMap::new(),
                    fork: "acme/fork".into(),
                    fork_permissions: permissions,
                    initialization: Some(target),
                    publication: None,
                },
            )]),
            outbox: Outbox::new(1024, vec![]),
        },
        standalone_targets: HashMap::new(),
        proof_public_key: Arc::new(vec![]),
        read_bearer: Arc::new("private-read".into()),
        delivery_bearer: Some(Arc::new("delivery".into())),
    };
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!(
        "http://{}/v1/targets/acme/upstream/configuration",
        listener.local_addr().unwrap()
    );
    let server = tokio::spawn(async move { axum::serve(listener, router(api)).await });
    let client = reqwest::Client::new();
    for bearer in ["", "delivery", "wrong"] {
        assert_eq!(
            client
                .get(&url)
                .bearer_auth(bearer)
                .send()
                .await
                .unwrap()
                .status(),
            StatusCode::UNAUTHORIZED
        );
    }
    assert!(observed.lock().unwrap().is_empty());
    for expected in ["a".repeat(40), "b".repeat(40)] {
        *sha.lock().unwrap() = expected.clone();
        let response = client
            .get(&url)
            .bearer_auth("private-read")
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let view: Value = response.json().await.unwrap();
        assert_eq!(view["source"]["sha"], expected);
        assert_eq!(view["default_branch"], "executor");
        assert_eq!(view["settings"]["base"], "main");
        assert_eq!(view["settings"]["max_model_calls"], 48);
        assert!(!view.to_string().contains("private-value"));
        let reads = observed.lock().unwrap();
        assert_eq!(reads[reads.len() - 1].1, expected);
        assert_eq!(reads[reads.len() - 2].1, expected);
    }
    broken.store(true, Ordering::SeqCst);
    let response = client
        .get(&url)
        .bearer_auth("private-read")
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
    let error: Value = response.json().await.unwrap();
    assert_eq!(error, json!({"error":"target_configuration_unavailable"}));
    server.abort();
    github_server.abort();
}
