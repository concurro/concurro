//! GitHub collection to retained SQL rows, including unavailable enrichment and source attribution.

use axum::{
    Json, Router,
    extract::{Path, Query},
    http::StatusCode,
    response::IntoResponse,
    routing::{get, post},
};
use chrono::{Duration, Utc};
use concurro_db::Database;
use concurro_github_v1::AppClient;
use concurro_tasks::{IndexTarget, scan_target_index};
use concurro_tasks_postgres::{
    index_page, index_run_links, index_scan_status, migrate, own_charged_calls, record_index_scan,
};
use concurro_testkit::PostgresContainer;
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, HashMap},
    process::Command,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
};
use uuid::Uuid;

/// Real scoped App reads preserve counts/check pages and degrade individual PR enrichment explicitly.
#[tokio::test]
async fn scans_complete_metadata_and_keeps_unavailable_pr_enrichment_unknown() {
    let postgres = PostgresContainer::start().await.unwrap();
    let database = Database::connect(postgres.url(), 3).await.unwrap();
    migrate(&database).await.unwrap();
    let checks_fail = Arc::new(AtomicBool::new(false));
    let fail = checks_fail.clone();
    let app = Router::new()
        .route("/repos/acme/{repo}/installation", get(|| async {Json(json!({"id":7,"repository_selection":"selected","account":{"login":"acme"}}))}))
        .route("/app/installations/7/access_tokens", post(|Json(body):Json<Value>| async move {
            Json(json!({"token":"scoped-token","permissions":body["permissions"],"expires_at":(Utc::now()+Duration::minutes(55)).to_rfc3339()}))
        }))
        .route("/installation/repositories", get(|| async {Json(json!({"total_count":1,"repositories":[{"full_name":"acme/demo"}]}))}))
        .route("/repos/acme/demo", get(|| async {Json(json!({"id":8}))}))
        .route("/repos/acme/demo/issues", get(|| async {Json(json!([{ "number":7,"title":"Historical issue","body":"Original requirements","state":"open","labels":[{"name":"concurro:stage:gates"}],"user":{"login":"author"},"updated_at":"2026-11-21T10:00:00Z"}]))}))
        .route("/repos/acme/demo/pulls", get(|| async {Json(json!([
            {"number":9,"title":"PR nine","state":"open","draft":false,"head":{"ref":"concurro/issue-7-aabb","sha":"head-nine"},"updated_at":"2026-11-21T10:00:00Z"},
            {"number":10,"title":"Listing survives","state":"open","head":{"ref":"concurro/issue-7-ccdd","sha":"head-ten"},"updated_at":"2026-11-21T10:00:00Z"},
            {"number":11,"head":{"ref":"feature/unrelated"},"updated_at":"2026-11-21T10:00:00Z"}
        ]))}))
        .route("/repos/acme/demo/pulls/{number}", get(|Path(number):Path<i64>| async move {
            if number == 10 { return StatusCode::NOT_FOUND.into_response(); }
            Json(json!({"number":number,"title":"Detailed PR","state":"open","draft":false,"head":{"ref":"concurro/issue-7-aabb","sha":"head-nine"},"base":{"ref":"main"},"additions":1300,"deletions":92,"updated_at":"2026-11-21T10:00:00Z"})).into_response()
        }))
        .route("/repos/acme/demo/commits/{sha}/check-runs", get(move |Path(sha):Path<String>,Query(query):Query<HashMap<String,String>>| {
            let fail = fail.clone(); async move {
                if sha == "head-ten" || fail.load(Ordering::SeqCst) { return StatusCode::FORBIDDEN.into_response(); }
                assert_eq!(query["per_page"], "100");
                let checks = if query["page"] == "1" { vec![json!({"status":"completed","conclusion":"success"});100] } else {vec![json!({"status":"completed","conclusion":"skipped"})]};
                Json(json!({"check_runs":checks})).into_response()
            }
        }))
        .route("/repos/acme/demo/actions/workflows/concurro-executor.yml/runs", get(|| async {Json(json!({"workflow_runs":[{"id":123,"run_attempt":1,"status":"completed","conclusion":"success","name":"Executor","run_started_at":"2026-11-21T10:00:00Z","created_at":"2026-11-21T09:59:00Z","updated_at":"2026-11-21T10:02:00Z"}]}))}));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let server = tokio::spawn(async move { axum::serve(listener, app).await });
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
    let github = AppClient::new(reqwest::Client::new(), &url, 42, &key.stdout).unwrap();
    let permissions = BTreeMap::from([
        ("checks".into(), "read".into()),
        ("pull_requests".into(), "read".into()),
    ]);
    let target = IndexTarget {
        issue_repository_id: 8,
        issue_repository: "acme/demo".into(),
        upstream: "acme/demo".into(),
        fork: "acme/demo".into(),
        issue_permissions: permissions.clone(),
        upstream_permissions: permissions.clone(),
        fork_permissions: permissions,
    };
    scan_target_index(&database, &github, &target)
        .await
        .unwrap();
    let issues = index_page(&database, "acme/demo", "issue", "")
        .await
        .unwrap();
    assert_eq!(issues[0].document["body"], "Original requirements");
    let pulls = index_page(&database, "acme/demo", "pull", "")
        .await
        .unwrap();
    assert_eq!(pulls.len(), 2);
    let complete = pulls.iter().find(|row| row.item_id == "9").unwrap();
    assert_eq!(complete.document["additions"], 1300);
    assert_eq!(
        complete.document["check_runs"].as_array().unwrap().len(),
        101
    );
    let unavailable = pulls.iter().find(|row| row.item_id == "10").unwrap();
    assert_eq!(unavailable.document["title"], "Listing survives");
    assert!(unavailable.document["check_runs"].is_null());
    assert!(unavailable.document["additions"].is_null());
    assert_eq!(
        index_scan_status(&database, "acme/demo", "pull")
            .await
            .unwrap()["state"],
        "current"
    );
    record_index_scan(&database, &[("acme/demo", "pull")], false)
        .await
        .unwrap();
    let failed = index_scan_status(&database, "acme/demo", "pull")
        .await
        .unwrap();
    assert_eq!(failed["state"], "failed");
    assert!(failed["verified"].is_string());
    assert_eq!(
        index_page(&database, "acme/demo", "pull", "")
            .await
            .unwrap()
            .len(),
        2
    );
    checks_fail.store(true, Ordering::SeqCst);
    scan_target_index(&database, &github, &target)
        .await
        .unwrap();
    assert!(
        index_page(&database, "acme/demo", "pull", "")
            .await
            .unwrap()
            .iter()
            .all(|row| row.document["check_runs"].is_null())
    );
    let run = index_page(&database, "acme/demo", "run", "").await.unwrap();
    assert_eq!(run[0].document["run_started_at"], "2026-11-21T10:00:00Z");
    // Existing execution rows supply attribution and own debit counts; no index row invents them.
    let task = Uuid::now_v7();
    let attempt = Uuid::now_v7();
    sqlx::query("INSERT INTO tasks.issue_controls(repository_id,issue_number) VALUES (8,7)")
        .execute(database.pool())
        .await
        .unwrap();
    sqlx::query("INSERT INTO tasks.task_state(repository_id,issue_number,task_id,revision,state) VALUES (8,7,$1,1,'{}')").bind(task).execute(database.pool()).await.unwrap();
    sqlx::query("INSERT INTO tasks.attempts(id,task_id,operation,expected_revision,staffing_group,fork,config_sha,submit_body,initial_deadline,unbounded_running,status) VALUES ($1,$2,'plan',1,'planner','acme/demo',$3,''::bytea,now(),false,'finished')").bind(attempt).bind(task).bind("a".repeat(40)).execute(database.pool()).await.unwrap();
    sqlx::query("INSERT INTO tasks.runner_sessions(session_id,turn_id,repository_id,fork,config_sha,run_id) VALUES ($1,$2,8,'acme/demo',$3,123)").bind(Uuid::now_v7()).bind(attempt).bind("a".repeat(40)).execute(database.pool()).await.unwrap();
    sqlx::query("INSERT INTO tasks.config_targets(body,revision) VALUES ($1,1)")
        .bind(json!([{"repository_id":8,"issue_repository":"acme/demo","upstream":"acme/demo","fork":"acme/demo"}]))
        .execute(database.pool())
        .await
        .unwrap();
    let links = index_run_links(&database, "acme/demo").await.unwrap();
    assert_eq!(links[0]["turn"], 1);
    assert_eq!(links[0]["issue"], 7);
    sqlx::query("INSERT INTO tasks.funding_grants(attempt_id,task_id,purpose,payer_id,provider,slot_epoch,slot_grant_id,credential_record_id,credential_version,route) VALUES ($1,$2,'task_contributed',42,'openai',1,'grant','credential',1,'openai')").bind(attempt).bind(task).execute(database.pool()).await.unwrap();
    assert_eq!(
        own_charged_calls(&database, 42).await.unwrap()[0]["calls"],
        1
    );
    assert!(own_charged_calls(&database, 43).await.unwrap().is_empty());
    sqlx::query("UPDATE tasks.funding_grants SET reservation_status='released'")
        .execute(database.pool())
        .await
        .unwrap();
    assert!(own_charged_calls(&database, 42).await.unwrap().is_empty());
    sqlx::query("INSERT INTO tasks.python_import_turns(deployment,source_id,repository,issue_number,stage,status,staffing,source_record,import_status,serving_run) VALUES ('before-cutover','old-turn','acme/demo',7,'plan','finished','[]',$1,'no_archive','123/2')")
        .bind(json!({"request":"{\"task_kind\":\"contribution\"}"})).execute(database.pool()).await.unwrap();
    let review = Uuid::now_v7();
    let review_attempt = Uuid::now_v7();
    sqlx::query("INSERT INTO tasks.standalone_review_tasks(id,repository_id,runner_repository_id,upstream,pr_number,reviewer_id,stage) VALUES ($1,8,8,'acme/demo',33,42,'complete')").bind(review).execute(database.pool()).await.unwrap();
    sqlx::query("INSERT INTO tasks.attempts(id,standalone_task_id,operation,expected_revision,staffing_group,fork,config_sha,submit_body,initial_deadline,unbounded_running,status) VALUES ($1,$2,'review',1,'reviewer','acme/demo',$3,''::bytea,now(),false,'finished')").bind(review_attempt).bind(review).bind("a".repeat(40)).execute(database.pool()).await.unwrap();
    sqlx::query("INSERT INTO tasks.runner_sessions(session_id,turn_id,repository_id,fork,config_sha,run_id) VALUES ($1,$2,8,'acme/demo',$3,999)").bind(Uuid::now_v7()).bind(review_attempt).bind("a".repeat(40)).execute(database.pool()).await.unwrap();
    let links = index_run_links(&database, "acme/demo").await.unwrap();
    let imported = links
        .iter()
        .find(|row| row["source_id"] == "old-turn")
        .unwrap();
    assert_eq!(imported["run_id"], 123);
    assert_eq!(imported["attempt"], 2);
    assert_eq!(imported["turn"], 1);
    let standalone = links.iter().find(|row| row["run_id"] == 999).unwrap();
    assert_eq!(standalone["kind"], "review");
    assert_eq!(standalone["issue"], 33);
    server.abort();
}
