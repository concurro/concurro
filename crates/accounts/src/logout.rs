//! Encrypted token revocation with a durable Graphile Worker retry.

use std::{fmt, sync::Arc};

use aes_gcm::{
    Aes256Gcm, Nonce,
    aead::{Aead, KeyInit, Payload},
};
use anyhow::Result;
use concurro_accounts_postgres as storage;
use concurro_db::Database;
use graphile_worker::{IntoTaskHandlerResult, TaskHandler, WorkerContext};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::oauth::OAuth;

/// One revocation job's services, shared with the inline attempt and worker.
#[derive(Clone)]
pub struct Logout {
    database: Database,
    cipher: Arc<Aes256Gcm>,
    oauth: OAuth,
}

impl fmt::Debug for Logout {
    /// Show no key, database URL or token material in diagnostics.
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.debug_struct("Logout").finish_non_exhaustive()
    }
}

/// Anonymous browser receipt and remote outcome.
pub struct LogoutResult {
    /// Durable receipt, absent when storage failed.
    pub receipt: Option<Uuid>,
    /// Revoked, pending, or unavailable.
    pub remote: &'static str,
}

/// Graphile retry payload containing only a receipt identity.
#[derive(Deserialize, Serialize)]
pub struct RevokeTokenJob {
    /// Anonymous logout receipt.
    pub receipt: Uuid,
}

impl TaskHandler for RevokeTokenJob {
    const IDENTIFIER: &'static str = "revoke_oauth_token";

    /// Retry an unconfirmed remote revocation through Graphile Worker.
    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let logout = context.get_ext::<Logout>().expect("logout worker context");
        match logout.attempt(self.receipt).await {
            Ok("revoked" | "unknown" | "none") => Ok(()),
            Ok(_) => Err("GitHub revocation pending".to_owned()),
            Err(_) => Err("logout storage unavailable".to_owned()),
        }
    }
}

impl Logout {
    /// Bind the accounts database, key and OAuth revoker.
    pub fn new(database: Database, key: &[u8], oauth: OAuth) -> Result<Self> {
        let cipher = Aes256Gcm::new_from_slice(key)
            .map_err(|_| anyhow::anyhow!("logout key must be 32 bytes"))?;
        Ok(Self {
            database,
            cipher: Arc::new(cipher),
            oauth,
        })
    }

    /// End remote authority once and persist every retry before the inline call.
    pub async fn revoke(&self, token: &str) -> LogoutResult {
        let receipt = Uuid::now_v7();
        let nonce = rand::random::<[u8; 12]>();
        let sealed = self.cipher.encrypt(
            &Nonce::from(nonce),
            Payload {
                msg: token.as_bytes(),
                aad: receipt.as_bytes(),
            },
        );
        if let Ok(sealed) = sealed {
            match storage::record_logout(&self.database, receipt, &nonce, &sealed).await {
                Ok(()) => {
                    return LogoutResult {
                        receipt: Some(receipt),
                        remote: self.attempt(receipt).await.unwrap_or("pending"),
                    };
                }
                Err(error) => tracing::warn!(%error, "logout receipt unavailable"),
            }
        }
        LogoutResult {
            receipt: None,
            remote: if self.oauth.revoke(token).await {
                "revoked"
            } else {
                "unavailable"
            },
        }
    }

    /// Read a receipt without ever returning token material.
    pub async fn status(&self, receipt: Option<Uuid>) -> &'static str {
        let Some(receipt) = receipt else {
            return "none";
        };
        match storage::receipt_state(&self.database, receipt).await {
            Ok(Some(state)) if state == "revoked" => "revoked",
            Ok(Some(state)) if state == "unknown" => "unknown",
            Ok(Some(_)) => match storage::revocation_job(&self.database, receipt).await {
                Ok(Some(job))
                    if self
                        .open(receipt, job.nonce.as_deref(), job.sealed.as_deref())
                        .is_some() =>
                {
                    "pending"
                }
                Ok(_) => "unknown",
                Err(_) => "unavailable",
            },
            Ok(None) => "none",
            Err(_) => "unavailable",
        }
    }

    /// Attempt one pending job; a worker retries errors with Graphile backoff.
    pub async fn attempt(&self, receipt: Uuid) -> Result<&'static str> {
        let Some(job) = storage::revocation_job(&self.database, receipt).await? else {
            return Ok("none");
        };
        if job.state == "done" {
            return Ok("revoked");
        }
        if job.state == "unknown" {
            return Ok("unknown");
        }
        let Some(token) = self.open(receipt, job.nonce.as_deref(), job.sealed.as_deref()) else {
            storage::unknown_logout(&self.database, receipt).await?;
            return Ok("unknown");
        };
        if self.oauth.revoke(&token).await {
            storage::complete_logout(&self.database, receipt).await?;
            Ok("revoked")
        } else {
            Ok("pending")
        }
    }

    /// Decrypt one job under its receipt identity.
    fn open(&self, receipt: Uuid, nonce: Option<&[u8]>, sealed: Option<&[u8]>) -> Option<String> {
        let nonce: [u8; 12] = nonce?.try_into().ok()?;
        let bytes = self
            .cipher
            .decrypt(
                &Nonce::from(nonce),
                Payload {
                    msg: sealed?,
                    aad: receipt.as_bytes(),
                },
            )
            .ok()?;
        String::from_utf8(bytes)
            .ok()
            .filter(|token| !token.is_empty())
    }
}
