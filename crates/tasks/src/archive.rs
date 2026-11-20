//! Durable object upload of original runner transcript archives.

use std::{fmt, sync::Arc};

use anyhow::Result;
use concurro_db::Database;
use concurro_tasks_postgres::{fail_archive_upload, pending_archive, verify_archive};
use graphile_worker::{IntoTaskHandlerResult, TaskHandler, WorkerContext};
use object_store::{ObjectStore, ObjectStoreExt, path::Path};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

/// Object store and key prefix shared by archive jobs.
#[derive(Clone)]
pub struct ArchiveContext {
    /// Authenticated private object store.
    pub store: Arc<dyn ObjectStore>,
    /// Prefix beneath the configured bucket for gzip archives.
    pub prefix: String,
}

impl fmt::Debug for ArchiveContext {
    /// Hide store configuration and credentials from worker diagnostics.
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ArchiveContext")
            .finish_non_exhaustive()
    }
}

/// One content-addressed original gzip upload.
#[derive(Deserialize, Serialize)]
pub struct UploadArchiveJob {
    /// SHA-256 digest of the original gzip bytes.
    pub sha: String,
}

impl TaskHandler for UploadArchiveJob {
    const IDENTIFIER: &'static str = "upload_archive";

    /// Upload and read back one original archive, retaining bytes on failure.
    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let archive = context
            .get_ext::<ArchiveContext>()
            .ok_or("archive storage unavailable")?;
        let database = Database::from_pool(context.pg_pool().clone());
        upload_archive(&database, archive, &self.sha)
            .await
            .map_err(|error| error.to_string())
    }
}

/// Verify an object's exact bytes before discarding its PostgreSQL source copy.
pub async fn upload_archive(
    database: &Database,
    archive: &ArchiveContext,
    sha: &str,
) -> Result<()> {
    let Some((bytes, attempts)) = pending_archive(database, sha).await? else {
        return Ok(());
    };
    let result: Result<(), (&'static str, anyhow::Error)> = async {
        if hex::encode(Sha256::digest(&bytes)) != sha {
            return Err((
                "digest_mismatch",
                anyhow::anyhow!("archive digest mismatch"),
            ));
        }
        let key = Path::parse(format!(
            "{}/sha256/{sha}.tar.gz",
            archive.prefix.trim_matches('/')
        ))
        .map_err(|error| ("object_key_invalid", error.into()))?;
        if attempts > 0 {
            match archive.store.get(&key).await {
                Ok(object) => {
                    let readback = object
                        .bytes()
                        .await
                        .map_err(|error| ("get_failed", error.into()))?;
                    if readback.as_ref() == bytes {
                        return Ok(());
                    }
                    return Err((
                        "readback_mismatch",
                        anyhow::anyhow!("archive readback mismatch"),
                    ));
                }
                Err(object_store::Error::NotFound { .. }) => {}
                Err(error) => return Err(("get_failed", error.into())),
            }
        }
        archive
            .store
            .put(&key, bytes.clone().into())
            .await
            .map_err(|error| ("put_failed", error.into()))?;
        let readback = archive
            .store
            .get(&key)
            .await
            .map_err(|error| ("get_failed", error.into()))?
            .bytes()
            .await
            .map_err(|error| ("get_failed", error.into()))?;
        if readback.as_ref() != bytes {
            return Err((
                "readback_mismatch",
                anyhow::anyhow!("archive readback mismatch"),
            ));
        }
        Ok(())
    }
    .await;
    match result {
        Ok(()) => {
            let mut work = database.begin().await?;
            verify_archive(&mut work, sha).await?;
            work.commit().await?;
            Ok(())
        }
        Err((stage, error)) => {
            let mut work = database.begin().await?;
            let exhausted = fail_archive_upload(&mut work, sha, stage).await?;
            work.commit().await?;
            if exhausted {
                tracing::warn!(%error, sha, "archive upload exhausted retries");
                Ok(())
            } else {
                Err(error)
            }
        }
    }
}
