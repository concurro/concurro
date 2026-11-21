//! Durable logout revocation receipts and encrypted token jobs.

use concurro_db::Database;
use graphile_worker::WorkerUtils;
use sqlx::migrate::Migrator;
use uuid::Uuid;

static MIGRATIONS: Migrator = sqlx::migrate!("./migrations");

/// One pending sealed access token, or a completed job with cleared ciphertext.
#[derive(sqlx::FromRow)]
pub struct RevocationJob {
    /// Nonce stored alongside the sealed bytes while pending.
    pub nonce: Option<Vec<u8>>,
    /// AES-GCM ciphertext; absent after completion or an invalid-key finding.
    pub sealed: Option<Vec<u8>>,
    /// Pending, done, or unknown.
    pub state: String,
}

/// Install the accounts schema and shared durable job queue.
pub async fn migrate(database: &Database) -> anyhow::Result<()> {
    database.migrate_in_schema("accounts", &MIGRATIONS).await?;
    WorkerUtils::new(database.pool().clone(), "graphile_worker")
        .migrate()
        .await?;
    Ok(())
}

/// Record a sealed token and its retry in one transaction.
pub async fn record_logout(
    database: &Database,
    receipt: Uuid,
    nonce: &[u8],
    sealed: &[u8],
) -> anyhow::Result<()> {
    let mut work = database.begin().await?;
    sqlx::query("INSERT INTO accounts.logout_receipts (id,state) VALUES ($1,'pending')")
        .bind(receipt)
        .execute(work.connection())
        .await?;
    sqlx::query(
        "INSERT INTO accounts.logout_jobs (id,receipt_id,nonce,sealed,state) \
         VALUES ($1,$1,$2,$3,'pending')",
    )
    .bind(receipt)
    .bind(nonce)
    .bind(sealed)
    .execute(work.connection())
    .await?;
    let _: i64 = sqlx::query_scalar(
        "SELECT (graphile_worker.add_job( \
         identifier => 'revoke_oauth_token', payload => json_build_object('receipt',$1), \
         run_at => now() + interval '5 seconds', max_attempts => 32000, \
         job_key => $2)).id",
    )
    .bind(receipt)
    .bind(format!("logout:{receipt}"))
    .fetch_one(work.connection())
    .await?;
    work.commit().await?;
    Ok(())
}

/// Read a receipt's anonymous state.
pub async fn receipt_state(
    database: &Database,
    receipt: Uuid,
) -> Result<Option<String>, sqlx::Error> {
    sqlx::query_scalar("SELECT state FROM accounts.logout_receipts WHERE id=$1")
        .bind(receipt)
        .fetch_optional(database.pool())
        .await
}

/// Read the sealed job for one receipt without exposing plaintext to storage.
pub async fn revocation_job(
    database: &Database,
    receipt: Uuid,
) -> Result<Option<RevocationJob>, sqlx::Error> {
    sqlx::query_as("SELECT nonce,sealed,state FROM accounts.logout_jobs WHERE receipt_id=$1")
        .bind(receipt)
        .fetch_optional(database.pool())
        .await
}

/// Confirm remote revocation and remove recoverable ciphertext.
pub async fn complete_logout(database: &Database, receipt: Uuid) -> Result<(), sqlx::Error> {
    let mut work = database.begin().await?;
    sqlx::query(
        "UPDATE accounts.logout_jobs SET state='done',nonce=NULL,sealed=NULL,updated_at=now() \
         WHERE receipt_id=$1 AND state='pending'",
    )
    .bind(receipt)
    .execute(work.connection())
    .await?;
    sqlx::query(
        "UPDATE accounts.logout_receipts SET state='revoked' WHERE id=$1 AND state='pending'",
    )
    .bind(receipt)
    .execute(work.connection())
    .await?;
    work.commit().await
}

/// Mark an undecryptable job without retaining its ciphertext.
pub async fn unknown_logout(database: &Database, receipt: Uuid) -> Result<(), sqlx::Error> {
    let mut work = database.begin().await?;
    sqlx::query(
        "UPDATE accounts.logout_jobs SET state='unknown',nonce=NULL,sealed=NULL,updated_at=now() \
         WHERE receipt_id=$1 AND state='pending'",
    )
    .bind(receipt)
    .execute(work.connection())
    .await?;
    sqlx::query(
        "UPDATE accounts.logout_receipts SET state='unknown' WHERE id=$1 AND state='pending'",
    )
    .bind(receipt)
    .execute(work.connection())
    .await?;
    work.commit().await
}
