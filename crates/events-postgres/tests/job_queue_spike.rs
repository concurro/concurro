//! Validate Graphile Worker queue admission against a service-owned database.

use std::{
    process::Command,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};

use concurro_db::Database;
use concurro_testkit::PostgresContainer;
use graphile_worker::{
    IntoTaskHandlerResult, JobSpec, JobSpecBuilder, TaskHandler, WorkerContext, WorkerOptions,
    WorkerUtils,
};
use serde::{Deserialize, Serialize};

/// Connect as the owner of a private service database in this test server.
async fn service_database(postgres: &PostgresContainer) -> Database {
    let admin = Database::connect(postgres.url(), 1)
        .await
        .expect("admin database");
    for statement in [
        "CREATE ROLE job_service LOGIN PASSWORD 'job-service'",
        "CREATE DATABASE job_service OWNER job_service",
        "REVOKE CONNECT ON DATABASE job_service FROM PUBLIC",
    ] {
        sqlx::query(statement)
            .execute(admin.pool())
            .await
            .expect("service setup");
    }
    let url = postgres
        .url()
        .replace("postgres:test", "job_service:job-service")
        .replace("/concurro_test", "/job_service");
    Database::connect(&url, 5).await.expect("service database")
}

/// Count executions across a restarted or retried task.
#[derive(Clone, Debug)]
struct ExecutionCount(Arc<AtomicUsize>);

/// A job whose domain effect is keyed by a stable identity.
#[derive(Deserialize, Serialize)]
struct ProbeJob {
    /// Stable effect identity.
    id: String,
    /// Keep failing until the configured attempt limit.
    always_fail: bool,
}

/// A job that holds its first process open after committing an idempotent effect.
#[derive(Deserialize, Serialize)]
struct KillProbeJob {
    /// Stable effect identity across worker processes.
    id: String,
}

impl TaskHandler for KillProbeJob {
    const IDENTIFIER: &'static str = "kill_probe_job";

    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let pool = context.pg_pool();
        sqlx::query("INSERT INTO job_effect (id) VALUES ($1) ON CONFLICT DO NOTHING")
            .bind(&self.id)
            .execute(pool)
            .await
            .map_err(|error| error.to_string())?;
        sqlx::query("INSERT INTO job_runs (id) VALUES ($1)")
            .bind(&self.id)
            .execute(pool)
            .await
            .map_err(|error| error.to_string())?;
        if std::env::var_os("CONCURRO_KILL_PROBE_CHILD").is_some() {
            tokio::time::sleep(Duration::from_secs(60)).await;
        }
        Ok::<(), String>(())
    }
}

/// Run a worker in a separate process so the parent can kill it mid-job.
#[tokio::test]
async fn graphile_worker_process_kill_child() {
    let Ok(url) = std::env::var("CONCURRO_KILL_PROBE_URL") else {
        return;
    };
    let database = Database::connect(&url, 3).await.expect("child database");
    let worker = WorkerOptions::default()
        .pg_pool(database.pool().clone())
        .schema("graphile_worker")
        .define_job::<KillProbeJob>()
        .heartbeat_interval(Duration::from_millis(200))
        .sweep_interval(Duration::from_millis(200))
        .sweep_threshold(Duration::from_millis(600))
        .recovery_delay(Duration::ZERO)
        .init()
        .await
        .expect("child worker init");
    worker.run().await.expect("child worker run");
}

/// A killed worker's locked job is recovered without duplicating its effect.
#[tokio::test]
async fn graphile_worker_recovers_after_process_kill() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let database = service_database(&postgres).await;
    sqlx::query("CREATE TABLE job_effect (id text PRIMARY KEY)")
        .execute(database.pool())
        .await
        .expect("effect table");
    sqlx::query("CREATE TABLE job_runs (id text NOT NULL)")
        .execute(database.pool())
        .await
        .expect("run table");
    let worker = WorkerOptions::default()
        .pg_pool(database.pool().clone())
        .schema("graphile_worker")
        .define_job::<KillProbeJob>()
        .heartbeat_interval(Duration::from_millis(200))
        .sweep_interval(Duration::from_millis(200))
        .sweep_threshold(Duration::from_millis(600))
        .recovery_delay(Duration::ZERO)
        .init()
        .await
        .expect("parent worker init");
    worker
        .create_utils()
        .add_job(
            KillProbeJob {
                id: "stable".to_owned(),
            },
            JobSpecBuilder::new().max_attempts(3).build(),
        )
        .await
        .expect("enqueue probe");
    let mut child = Command::new(std::env::current_exe().expect("test executable"))
        .args(["--exact", "graphile_worker_process_kill_child"])
        .env(
            "CONCURRO_KILL_PROBE_URL",
            postgres
                .url()
                .replace("postgres:test", "job_service:job-service")
                .replace("/concurro_test", "/job_service"),
        )
        .env("CONCURRO_KILL_PROBE_CHILD", "1")
        .spawn()
        .expect("worker process");
    tokio::time::timeout(Duration::from_secs(10), async {
        loop {
            let runs: i64 = sqlx::query_scalar("SELECT count(*) FROM job_runs")
                .fetch_one(database.pool())
                .await
                .expect("run count");
            if runs == 1 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    })
    .await
    .expect("first process entered job");
    child.kill().expect("kill worker process");
    assert!(!child.wait().expect("reap worker").success());
    let running = tokio::spawn(async move { worker.run().await });
    tokio::time::timeout(Duration::from_secs(20), async {
        loop {
            let runs: i64 = sqlx::query_scalar("SELECT count(*) FROM job_runs")
                .fetch_one(database.pool())
                .await
                .expect("run count");
            let pending: i64 =
                sqlx::query_scalar("SELECT count(*) FROM graphile_worker._private_jobs")
                    .fetch_one(database.pool())
                    .await
                    .expect("job count");
            if runs >= 2 && pending == 0 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    })
    .await
    .expect("job recovered after process kill");
    let effects: i64 = sqlx::query_scalar("SELECT count(*) FROM job_effect")
        .fetch_one(database.pool())
        .await
        .expect("effect count");
    let runs: i64 = sqlx::query_scalar("SELECT count(*) FROM job_runs")
        .fetch_one(database.pool())
        .await
        .expect("run count");
    assert_eq!((effects, runs), (1, 2));
    running.abort();
}

impl TaskHandler for ProbeJob {
    const IDENTIFIER: &'static str = "probe_job";

    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let pool = context.pg_pool();
        sqlx::query("INSERT INTO job_effect (id) VALUES ($1) ON CONFLICT DO NOTHING")
            .bind(self.id)
            .execute(pool)
            .await
            .map_err(|error| error.to_string())?;
        let counter = context
            .get_ext::<ExecutionCount>()
            .ok_or("execution counter unavailable")?;
        let run = counter.0.fetch_add(1, Ordering::SeqCst);
        if self.always_fail || run == 0 {
            Err("injected job failure".to_owned())
        } else {
            Ok(())
        }
    }
}

/// Transaction rollback, job keys, and repeatable migrations work with workspace pins.
#[tokio::test]
async fn graphile_worker_transactional_enqueue_and_job_key() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let database = service_database(&postgres).await;
    let utils = WorkerUtils::new(database.pool().clone(), "graphile_worker");
    utils.migrate().await.expect("first migration");
    utils.migrate().await.expect("repeat migration");

    let mut transaction = database.pool().begin().await.expect("enqueue transaction");
    let mut scoped = utils.clone().with_executor(&mut transaction);
    scoped
        .add_raw_job(
            "probe",
            serde_json::json!({"id": "rolled-back"}),
            JobSpec::default(),
        )
        .await
        .expect("transactional enqueue");
    drop(scoped);
    transaction.rollback().await.expect("rollback");
    let jobs: i64 = sqlx::query_scalar("SELECT count(*) FROM graphile_worker._private_jobs")
        .fetch_one(database.pool())
        .await
        .expect("job count");
    assert_eq!(jobs, 0);

    let first = utils
        .add_raw_job(
            "probe",
            serde_json::json!({"value": 1}),
            JobSpecBuilder::new().job_key("probe:stable").build(),
        )
        .await
        .expect("first keyed job");
    let replacement = utils
        .add_raw_job(
            "probe",
            serde_json::json!({"value": 2}),
            JobSpecBuilder::new().job_key("probe:stable").build(),
        )
        .await
        .expect("replacement job");
    assert_eq!(first.id(), replacement.id());
    let jobs: i64 = sqlx::query_scalar("SELECT count(*) FROM graphile_worker._private_jobs")
        .fetch_one(database.pool())
        .await
        .expect("job count");
    assert_eq!(jobs, 1);
}

/// Retried jobs keep one committed effect and retain terminal failure details.
#[tokio::test]
async fn graphile_worker_retry_and_stable_effect() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let database = service_database(&postgres).await;
    sqlx::query("CREATE TABLE job_effect (id text PRIMARY KEY)")
        .execute(database.pool())
        .await
        .expect("effect table");
    let counter = ExecutionCount(Arc::new(AtomicUsize::new(0)));
    let worker = WorkerOptions::default()
        .pg_pool(database.pool().clone())
        .schema("graphile_worker")
        .define_job::<ProbeJob>()
        .add_extension(counter.clone())
        .init()
        .await
        .expect("worker init");
    let utils = worker.create_utils();
    utils
        .add_job(
            ProbeJob {
                id: "stable".to_owned(),
                always_fail: false,
            },
            JobSpecBuilder::new().max_attempts(3).build(),
        )
        .await
        .expect("retry job");
    let running = tokio::spawn(async move { worker.run().await });
    tokio::time::timeout(Duration::from_secs(60), async {
        loop {
            let effects: i64 = sqlx::query_scalar("SELECT count(*) FROM job_effect")
                .fetch_one(database.pool())
                .await
                .expect("effect count");
            let jobs: i64 =
                sqlx::query_scalar("SELECT count(*) FROM graphile_worker._private_jobs")
                    .fetch_one(database.pool())
                    .await
                    .expect("job count");
            if effects == 1 && jobs == 0 && counter.0.load(Ordering::SeqCst) >= 2 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    })
    .await
    .expect("retry then complete");
    utils
        .add_job(
            ProbeJob {
                id: "permanent".to_owned(),
                always_fail: true,
            },
            JobSpecBuilder::new().max_attempts(2).build(),
        )
        .await
        .expect("permanent failure job");
    tokio::time::timeout(Duration::from_secs(10), async {
        loop {
            let row: Option<(i16, bool, Option<String>)> = sqlx::query_as(
                "SELECT attempts, run_at > now(), last_error FROM graphile_worker._private_jobs \
                 WHERE payload->>'id' = 'permanent'",
            )
            .fetch_optional(database.pool())
            .await
            .expect("retry schedule");
            if let Some((1, delayed, Some(error))) = row {
                assert!(delayed, "failed job should back off before retry");
                assert!(error.contains("injected job failure"));
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .expect("first retry scheduled");
    tokio::time::timeout(Duration::from_secs(60), async {
        loop {
            let row: Option<(i16, Option<String>)> = sqlx::query_as(
                "SELECT attempts, last_error FROM graphile_worker._private_jobs LIMIT 1",
            )
            .fetch_optional(database.pool())
            .await
            .expect("failed job row");
            if let Some((2, Some(error))) = row {
                assert!(error.contains("injected job failure"));
                break;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    })
    .await
    .expect("terminal failure retained");
    running.abort();
    let effects: i64 = sqlx::query_scalar("SELECT count(*) FROM job_effect")
        .fetch_one(database.pool())
        .await
        .expect("effect count");
    assert_eq!(effects, 2);
}
