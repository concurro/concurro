//! Offline Python history import through the real archive decoder and database.

use std::{fs, io::Write};

use concurro_db::Database;
use concurro_tasks_postgres::{
    baseline_page, create_baseline, import_python_manifest, imported_transcript_page, migrate,
};
use concurro_testkit::PostgresContainer;
use flate2::{Compression, write::GzEncoder};
use serde_json::json;
use sha2::{Digest, Sha256};
use tar::{Builder, Header};
use uuid::Uuid;

/// A rerun keeps original identities, source lines and one upload intent.
#[tokio::test]
async fn imports_python_turn_and_retained_archive_without_live_task() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 3)
        .await
        .expect("database");
    migrate(&database).await.expect("schema");
    let root = std::env::temp_dir().join(format!("concurro-import-{}", Uuid::now_v7()));
    fs::create_dir_all(&root).expect("source root");
    let mut gzip = GzEncoder::new(Vec::new(), Compression::default());
    {
        let mut tar = Builder::new(&mut gzip);
        let line = b"{\"type\":\"assistant\",\"text\":\"old turn\"}\n";
        let mut header = Header::new_gnu();
        header.set_size(line.len() as u64);
        header.set_mode(0o600);
        header.set_cksum();
        tar.append_data(&mut header, "claude/session.jsonl", &line[..])
            .expect("line");
        let journal = b"{\"scope\":\"planner\",\"turn\":0,\"status\":\"complete\"}";
        let mut header = Header::new_gnu();
        header.set_size(journal.len() as u64);
        header.set_mode(0o600);
        header.set_cksum();
        tar.append_data(&mut header, "turn.json", &journal[..])
            .expect("journal");
        tar.finish().expect("tar");
    }
    let bytes = gzip.finish().expect("gzip");
    let sha = hex::encode(Sha256::digest(&bytes));
    let blob = root.join(&sha);
    fs::File::create(&blob)
        .expect("blob")
        .write_all(&bytes)
        .expect("write blob");
    let manifest = json!({
        "deployment":"before-cutover", "diagnostics_dir":root,
        "files":[{"kind":"diagnostics","source_path":format!("diagnostics/{}/{}",&sha[..2],sha),
            "path":blob}],
        "turns":[{"id":"python-turn-1","repository":"team/repo","issue":7,
            "generation":2,"stage":"plan","status":"finished","session_scope":"planner",
            "session_turn":1,"runner_sessions":"[\"staff-a\",\"staff-b\"]",
            "runner":"{\"session\":\"served-c\",\"run_id\":\"123/2\"}",
            "request":"{}","transcript":sha,"transcript_bytes":bytes.len()}]
    });
    for _ in 0..2 {
        let report = import_python_manifest(&database, &manifest)
            .await
            .expect("import");
        assert_eq!(report["incomplete_turns"], 0);
    }
    let row: (Option<i64>, i64, String, String) = sqlx::query_as(
        "SELECT completed_turn,requested_turn,serving_session,serving_run \
         FROM tasks.python_import_turns WHERE deployment='before-cutover'",
    )
    .fetch_one(database.pool())
    .await
    .expect("imported turn");
    assert_eq!(row, (Some(0), 1, "served-c".into(), "123/2".into()));
    let upload_count: i64 = sqlx::query_scalar("SELECT count(*) FROM tasks.archive_uploads")
        .fetch_one(database.pool())
        .await
        .expect("upload intent");
    assert_eq!(upload_count, 1);
    let live_count: i64 = sqlx::query_scalar("SELECT count(*) FROM tasks.attempts")
        .fetch_one(database.pool())
        .await
        .expect("live attempts");
    assert_eq!(live_count, 0);
    sqlx::query("UPDATE tasks.archive_uploads SET status='verified' WHERE gzip_sha256=$1")
        .bind(&sha)
        .execute(database.pool())
        .await
        .expect("simulate object verification");
    let lines = imported_transcript_page(&database, "before-cutover", "python-turn-1", 0, 10)
        .await
        .expect("transcript query")
        .expect("linked transcript");
    assert_eq!(lines.len(), 1);
    assert_eq!(lines[0].json.as_ref().unwrap()["text"], "old turn");
    let snapshot = create_baseline(&database).await.expect("baseline");
    let rows = baseline_page(&database, snapshot, 0, 100)
        .await
        .expect("page")
        .expect("snapshot");
    assert!(rows.iter().any(|row| row.entity == "imported_turn"));
    fs::remove_dir_all(root).expect("cleanup");
}
