//! Durable object upload of original runner transcript archives.

use std::{fmt, sync::Arc};

use anyhow::Result;
use base64::Engine;
use concurro_db::Database;
use concurro_tasks_postgres::{
    RetainedPreparation, fail_archive_upload, pending_archive, verify_archive,
};
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

/// A permanent missing or corrupt predecessor detected before admission.
#[derive(Debug)]
pub(crate) struct RetainedPreflightRefusal(
    /// Stable refusal reason recorded on the task.
    pub &'static str,
);

impl fmt::Display for RetainedPreflightRefusal {
    /// Show the durable diagnostic reason.
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.0)
    }
}

impl std::error::Error for RetainedPreflightRefusal {}

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

/// Read and hash the exact predecessor gzip before a continuation can be funded.
pub(crate) async fn preflight_retained(
    archive: &ArchiveContext,
    preparation: &RetainedPreparation,
) -> Result<Option<String>> {
    let Some(sha) = preparation.gzip_sha256.as_deref() else {
        if preparation.turn != 0 {
            return Err(RetainedPreflightRefusal("retained_predecessor_archive_missing").into());
        }
        return Ok(None);
    };
    let bytes = if let Some(bytes) = &preparation.pending_bytes {
        bytes.clone()
    } else {
        let key = Path::parse(format!(
            "{}/sha256/{sha}.tar.gz",
            archive.prefix.trim_matches('/')
        ))?;
        match archive.store.get(&key).await {
            Ok(object) => object.bytes().await?.to_vec(),
            Err(object_store::Error::NotFound { .. }) => {
                return Err(RetainedPreflightRefusal("retained_object_missing").into());
            }
            Err(error) => return Err(error.into()),
        }
    };
    if hex::encode(Sha256::digest(&bytes)) != sha {
        return Err(RetainedPreflightRefusal("retained_digest_mismatch").into());
    }
    Ok(Some(
        base64::engine::general_purpose::STANDARD.encode(bytes),
    ))
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use base64::{Engine, engine::general_purpose::STANDARD};
    use concurro_tasks_postgres::RetainedPreparation;
    use object_store::{ObjectStoreExt, memory::InMemory, path::Path};
    use sha2::{Digest, Sha256};

    use super::{ArchiveContext, RetainedPreflightRefusal, preflight_retained};

    /// A verified object must be fetched and hashed before a paid continuation.
    #[tokio::test]
    async fn retained_preflight_uses_exact_pending_or_verified_gzip() {
        let store = Arc::new(InMemory::new());
        let archive = ArchiveContext {
            store: store.clone(),
            prefix: "private/turns".to_owned(),
        };
        let bytes = b"original gzip".to_vec();
        let sha = hex::encode(Sha256::digest(&bytes));
        let pending = RetainedPreparation {
            turn: 1,
            predecessor_digest: Some("prior".to_owned()),
            gzip_sha256: Some(sha.clone()),
            pending_bytes: Some(bytes.clone()),
        };
        assert_eq!(
            preflight_retained(&archive, &pending)
                .await
                .expect("pending bytes"),
            Some(STANDARD.encode(&bytes))
        );
        let key = Path::parse(format!("private/turns/sha256/{sha}.tar.gz")).expect("key");
        store
            .put(&key, bytes.clone().into())
            .await
            .expect("verified object");
        let verified = RetainedPreparation {
            pending_bytes: None,
            ..pending
        };
        assert_eq!(
            preflight_retained(&archive, &verified)
                .await
                .expect("verified bytes"),
            Some(STANDARD.encode(&bytes))
        );
        store
            .put(&key, b"corrupt".to_vec().into())
            .await
            .expect("corrupt object");
        let error = preflight_retained(&archive, &verified)
            .await
            .expect_err("digest mismatch");
        assert_eq!(
            error
                .downcast_ref::<RetainedPreflightRefusal>()
                .expect("refusal")
                .0,
            "retained_digest_mismatch"
        );
    }
}
