use std::sync::{
    Arc, Mutex,
    atomic::{AtomicU8, AtomicUsize, Ordering},
};

use axum::{
    Router,
    body::Bytes,
    extract::Path as AxumPath,
    http::{HeaderMap, StatusCode},
    routing::put,
};
use concurro_db::Database;
use concurro_tasks::{ArchiveContext, upload_archive};
use concurro_tasks_postgres::{migrate, retry_archive_upload};
use concurro_testkit::PostgresContainer;
use object_store::{ObjectStoreExt, aws::AmazonS3Builder, memory::InMemory, path::Path};
use sha2::{Digest, Sha256};

#[tokio::test]
async fn archive_upload_verifies_original_bytes_and_operator_retry_is_idempotent() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("task schema");
    let compression: (String, String) = sqlx::query_as(
        "SELECT CASE a.attcompression WHEN 'l' THEN 'lz4' WHEN 'p' THEN 'pglz' END, \
         (SELECT CASE WHEN 'lz4'=ANY(enumvals) THEN 'lz4' ELSE 'pglz' END \
          FROM pg_settings WHERE name='default_toast_compression') \
         FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid \
         JOIN pg_namespace n ON n.oid=c.relnamespace \
         WHERE n.nspname='tasks' AND c.relname='archive_uploads' AND a.attname='gzip_bytes'",
    )
    .fetch_one(database.pool())
    .await
    .expect("archive compression");
    assert_eq!(compression.0, compression.1);
    let original = b"original gzip bytes".to_vec();
    let sha = hex::encode(Sha256::digest(&original));
    sqlx::query(
        "INSERT INTO tasks.archive_uploads (gzip_sha256,gzip_bytes,byte_count,status) \
         VALUES ($1,$2,$3,'pending')",
    )
    .bind(&sha)
    .bind(&original)
    .bind(original.len() as i64)
    .execute(database.pool())
    .await
    .expect("pending archive");
    let store = Arc::new(InMemory::new());
    let invalid = ArchiveContext {
        store: store.clone(),
        prefix: "../invalid".to_owned(),
    };
    assert!(upload_archive(&database, &invalid, &sha).await.is_err());
    let retained: (String, i32, Vec<u8>) = sqlx::query_as(
        "SELECT status,upload_attempts,gzip_bytes FROM tasks.archive_uploads WHERE gzip_sha256=$1",
    )
    .bind(&sha)
    .fetch_one(database.pool())
    .await
    .expect("retained source");
    assert_eq!(retained, ("retrying".to_owned(), 1, original.clone()));
    let context = ArchiveContext {
        store: store.clone(),
        prefix: "private/turns".to_owned(),
    };
    upload_archive(&database, &context, &sha)
        .await
        .expect("verified upload");
    let object = store
        .get(&Path::parse(format!("private/turns/sha256/{sha}.tar.gz")).expect("key"))
        .await
        .expect("stored object")
        .bytes()
        .await
        .expect("object bytes");
    assert_eq!(object.as_ref(), original);
    let state: (String, Option<Vec<u8>>) =
        sqlx::query_as("SELECT status,gzip_bytes FROM tasks.archive_uploads WHERE gzip_sha256=$1")
            .bind(&sha)
            .fetch_one(database.pool())
            .await
            .expect("verified state");
    assert_eq!(state, ("verified".to_owned(), None));
    let mut noop = database.begin().await.expect("noop retry");
    assert!(
        !retry_archive_upload(&mut noop, &sha, "operator")
            .await
            .expect("verified retry")
    );
    noop.commit().await.expect("noop commit");

    sqlx::query(
        "UPDATE tasks.archive_uploads SET status='exhausted',gzip_bytes=$2 WHERE gzip_sha256=$1",
    )
    .bind(&sha)
    .bind(&original)
    .execute(database.pool())
    .await
    .expect("simulate exhausted upload");
    let mut retry = database.begin().await.expect("retry");
    assert!(
        retry_archive_upload(&mut retry, &sha, "operator")
            .await
            .expect("operator retry")
    );
    retry.commit().await.expect("retry commit");
    let mut repeated = database.begin().await.expect("repeated retry");
    assert!(
        !retry_archive_upload(&mut repeated, &sha, "operator")
            .await
            .expect("repeated retry")
    );
    repeated.commit().await.expect("repeated commit");
    let jobs: i64 =
        sqlx::query_scalar("SELECT count(*) FROM graphile_worker.jobs WHERE key LIKE 'archive:%'")
            .fetch_one(database.pool())
            .await
            .expect("archive jobs");
    assert_eq!(jobs, 1);
    // Graphile can exhaust independently when workers fail before app failure accounting.
    sqlx::query(
        "UPDATE graphile_worker._private_jobs SET attempts=max_attempts,locked_at=NULL \
         WHERE key=$1",
    )
    .bind(format!("archive:{sha}:1"))
    .execute(database.pool())
    .await
    .expect("exhaust worker job");
    sqlx::query("UPDATE tasks.archive_uploads SET status='retrying' WHERE gzip_sha256=$1")
        .bind(&sha)
        .execute(database.pool())
        .await
        .expect("stranded app upload");
    let retry_once = async {
        let mut work = database.begin().await.expect("first concurrent retry");
        let queued = retry_archive_upload(&mut work, &sha, "operator")
            .await
            .expect("first retry");
        work.commit().await.expect("first retry commit");
        queued
    };
    let retry_twice = async {
        let mut work = database.begin().await.expect("second concurrent retry");
        let queued = retry_archive_upload(&mut work, &sha, "operator")
            .await
            .expect("second retry");
        work.commit().await.expect("second retry commit");
        queued
    };
    let (first, second) = tokio::join!(retry_once, retry_twice);
    assert_ne!(first, second);
    let active: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM graphile_worker.jobs WHERE key LIKE $1 \
         AND (locked_at IS NOT NULL OR attempts<max_attempts)",
    )
    .bind(format!("archive:{sha}:%"))
    .fetch_one(database.pool())
    .await
    .expect("one active job");
    assert_eq!(active, 1);
}

/// Exercise the configured S3 adapter's signed path-style PUT and GET requests.
#[tokio::test]
async fn s3_compatible_adapter_reads_back_the_original_object() {
    let captured = Arc::new(Mutex::new(Vec::new()));
    let app = Router::new().route(
        "/{*key}",
        put({
            let captured = Arc::clone(&captured);
            move |AxumPath(key): AxumPath<String>, headers: HeaderMap, body: Bytes| {
                let captured = Arc::clone(&captured);
                async move {
                    assert_eq!(key, "archive-test/private/turns/original.tar.gz");
                    assert!(
                        headers["authorization"]
                            .to_str()
                            .expect("signature")
                            .starts_with("AWS4-HMAC-SHA256 ")
                    );
                    *captured.lock().expect("capture") = body.to_vec();
                    (StatusCode::OK, [("etag", "\"original\"")])
                }
            }
        })
        .get({
            let captured = Arc::clone(&captured);
            move |AxumPath(key): AxumPath<String>| {
                let captured = Arc::clone(&captured);
                async move {
                    assert_eq!(key, "archive-test/private/turns/original.tar.gz");
                    (
                        StatusCode::OK,
                        [("etag", "\"original\"")],
                        captured.lock().expect("readback").clone(),
                    )
                }
            }
        }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind");
    let address = listener.local_addr().expect("address");
    let server = tokio::spawn(async move { axum::serve(listener, app).await });
    let store = AmazonS3Builder::new()
        .with_endpoint(format!("http://{address}"))
        .with_bucket_name("archive-test")
        .with_region("test")
        .with_access_key_id("test-key")
        .with_secret_access_key("test-secret")
        .with_allow_http(true)
        .build()
        .expect("S3 client");
    let key = Path::parse("private/turns/original.tar.gz").expect("key");
    store
        .put(&key, b"original gzip".to_vec().into())
        .await
        .expect("signed upload");
    let readback = store
        .get(&key)
        .await
        .expect("signed readback")
        .bytes()
        .await
        .expect("bytes");
    assert_eq!(readback.as_ref(), b"original gzip");
    server.abort();
}

/// Exercise upload failure stages and adoption of an uncertain earlier PUT.
#[tokio::test]
async fn s3_upload_recovers_existing_object_without_a_second_put() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("task schema");
    let original = b"original compressed archive".to_vec();
    let sha = hex::encode(Sha256::digest(&original));
    sqlx::query("INSERT INTO tasks.archive_uploads (gzip_sha256,gzip_bytes,byte_count,status) VALUES ($1,$2,$3,'pending')")
        .bind(&sha).bind(&original).bind(original.len() as i64)
        .execute(database.pool()).await.expect("pending archive");
    let mode = Arc::new(AtomicU8::new(0));
    let puts = Arc::new(AtomicUsize::new(0));
    let app = Router::new().route(
        "/{*key}",
        put({
            let mode = Arc::clone(&mode);
            let puts = Arc::clone(&puts);
            let sha = sha.clone();
            move |AxumPath(key): AxumPath<String>, headers: HeaderMap, _: Bytes| {
                let mode = Arc::clone(&mode);
                let puts = Arc::clone(&puts);
                let sha = sha.clone();
                async move {
                    assert_eq!(
                        key,
                        format!("archive-test/private/turns/sha256/{sha}.tar.gz")
                    );
                    assert!(
                        headers["authorization"]
                            .to_str()
                            .expect("signature")
                            .starts_with("AWS4-HMAC-SHA256 ")
                    );
                    puts.fetch_add(1, Ordering::SeqCst);
                    if mode.load(Ordering::SeqCst) == 0 {
                        (StatusCode::SERVICE_UNAVAILABLE, [("etag", "\"failed\"")])
                    } else {
                        (StatusCode::OK, [("etag", "\"stored\"")])
                    }
                }
            }
        })
        .get({
            let mode = Arc::clone(&mode);
            let original = original.clone();
            let sha = sha.clone();
            move |AxumPath(key): AxumPath<String>, headers: HeaderMap| {
                let mode = Arc::clone(&mode);
                let original = original.clone();
                let sha = sha.clone();
                async move {
                    assert_eq!(
                        key,
                        format!("archive-test/private/turns/sha256/{sha}.tar.gz")
                    );
                    assert!(
                        headers["authorization"]
                            .to_str()
                            .expect("signature")
                            .starts_with("AWS4-HMAC-SHA256 ")
                    );
                    match mode.load(Ordering::SeqCst) {
                        1 => (
                            StatusCode::SERVICE_UNAVAILABLE,
                            [("etag", "\"error\"")],
                            Vec::new(),
                        ),
                        2 => (
                            StatusCode::OK,
                            [("etag", "\"wrong\"")],
                            b"wrong bytes".to_vec(),
                        ),
                        _ => (StatusCode::OK, [("etag", "\"stored\"")], original),
                    }
                }
            }
        }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind");
    let address = listener.local_addr().expect("address");
    let server = tokio::spawn(async move { axum::serve(listener, app).await });
    let store = AmazonS3Builder::new()
        .with_endpoint(format!("http://{address}"))
        .with_bucket_name("archive-test")
        .with_region("test")
        .with_access_key_id("test-key")
        .with_secret_access_key("test-secret")
        .with_allow_http(true)
        .build()
        .expect("S3 client");
    let context = ArchiveContext {
        store: Arc::new(store),
        prefix: "private/turns".to_owned(),
    };
    for (stage, current_mode) in [
        ("put_failed", 0),
        ("get_failed", 1),
        ("readback_mismatch", 2),
    ] {
        mode.store(current_mode, Ordering::SeqCst);
        assert!(upload_archive(&database, &context, &sha).await.is_err());
        let observed: String =
            sqlx::query_scalar("SELECT last_error FROM tasks.archive_uploads WHERE gzip_sha256=$1")
                .bind(&sha)
                .fetch_one(database.pool())
                .await
                .expect("failure stage");
        assert_eq!(observed, stage);
    }
    let attempted_puts = puts.load(Ordering::SeqCst);
    mode.store(3, Ordering::SeqCst);
    let restarted = Database::connect(postgres.url(), 2)
        .await
        .expect("restarted database");
    upload_archive(&restarted, &context, &sha)
        .await
        .expect("adopt existing object");
    assert_eq!(puts.load(Ordering::SeqCst), attempted_puts);
    let state: (String, Option<Vec<u8>>, Option<String>) = sqlx::query_as(
        "SELECT status,gzip_bytes,last_error FROM tasks.archive_uploads WHERE gzip_sha256=$1",
    )
    .bind(&sha)
    .fetch_one(restarted.pool())
    .await
    .expect("verified state");
    assert_eq!(state, ("verified".to_owned(), None, None));
    server.abort();
}
