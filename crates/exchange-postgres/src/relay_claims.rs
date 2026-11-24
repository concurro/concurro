//! Signed verifier authority and durable release delivery state.

use chrono::{DateTime, Utc};
use concurro_contracts::{Grant, TurnBinding};
use concurro_db::UnitOfWork;
use concurro_relay_hosted_api::{claim, release::ReleaseClaim};
use uuid::Uuid;

use super::{Store, StoreError, refused};

/// Inputs shared by `relay_claim`.
pub struct RelayClaimLimits<'a> {
    /// Allowed model routes bound to the relay claim.
    pub models: &'a [String],
    /// Maximum authorized tokens for the relay claim.
    pub budget: i64,
    /// Absolute session expiry bound to the relay claim.
    pub deadline: DateTime<Utc>,
}

/// Signing configuration held in memory; debug output never includes the private key.
pub struct RelaySigner {
    kid: String,
    pkcs8: Vec<u8>,
    calls: u64,
}

impl std::fmt::Debug for RelaySigner {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("RelaySigner")
            .field("kid", &self.kid)
            .finish_non_exhaustive()
    }
}

impl RelaySigner {
    /// Hold one Ed25519 PKCS #8 key and session call allowance.
    pub fn new(kid: String, pkcs8: Vec<u8>, calls: u64) -> anyhow::Result<Self> {
        anyhow::ensure!(
            !kid.is_empty() && !pkcs8.is_empty() && calls > 0,
            "relay signing settings missing"
        );
        Ok(Self { kid, pkcs8, calls })
    }
}

impl Store {
    /// Issue once per session, returning the same compact JWS on every admission.
    pub(crate) async fn relay_claim(
        &self,
        work: &mut UnitOfWork<'_>,
        session: Uuid,
        grant: &Grant,
        binding: &TurnBinding,
        inputs: RelayClaimLimits<'_>,
    ) -> Result<String, StoreError> {
        let RelayClaimLimits {
            models,
            budget,
            deadline,
        } = inputs;
        if let Some(compact) = sqlx::query_scalar::<_, Option<String>>(
            "SELECT compact FROM exchange.relay_claims WHERE session_id=$1",
        )
        .bind(session)
        .fetch_optional(work.connection())
        .await?
        .flatten()
        {
            return Ok(compact);
        }
        let signer = self
            .relay
            .as_ref()
            .ok_or_else(|| refused(503, "relay_unavailable"))?;
        let credential = &grant.credential;
        let payer = grant
            .payer
            .as_ref()
            .ok_or_else(|| refused(422, "invalid_grant"))?;
        let now = Utc::now();
        let claim_id = Uuid::now_v7();
        let claim = claim::SessionClaim {
            jti: claim_id.to_string(),
            iss: claim::ISSUER.to_owned(),
            sub: session.to_string(),
            iat: now.timestamp(),
            exp: deadline.timestamp(),
            relay_id: credential
                .relay_id
                .clone()
                .ok_or_else(|| refused(422, "invalid_grant"))?,
            user_id: u64::try_from(payer.id).map_err(|_| refused(422, "invalid_grant"))?,
            entry_id: credential
                .entry_id
                .clone()
                .ok_or_else(|| refused(422, "invalid_grant"))?,
            record_id: credential
                .record_id
                .clone()
                .ok_or_else(|| refused(422, "invalid_grant"))?,
            version: u64::try_from(
                credential
                    .version
                    .ok_or_else(|| refused(422, "invalid_grant"))?,
            )
            .map_err(|_| refused(422, "invalid_grant"))?,
            route: credential.route.clone(),
            models: models.to_vec(),
            calls: signer.calls,
            tokens: u64::try_from(budget).map_err(|_| refused(422, "invalid_grant"))?,
            repository: binding.repository.clone(),
            issue: u64::try_from(binding.issue).map_err(|_| refused(422, "invalid_grant"))?,
        };
        let compact = claim::sign(&claim, &signer.kid, &signer.pkcs8)?;
        sqlx::query(
            "INSERT INTO exchange.relay_claims(claim_id,session_id,kid,compact,expires,release_status) \
             VALUES($1,$2,$3,$4,$5,'active')",
        )
        .bind(claim_id)
        .bind(session)
        .bind(&signer.kid)
        .bind(&compact)
        .bind(deadline)
        .execute(work.connection())
        .await?;
        Ok(compact)
    }

    /// Return claim IDs awaiting verifier release, including newly ended sessions.
    pub async fn pending_releases(&self) -> Result<Vec<Uuid>, StoreError> {
        let mut work = self.database.begin().await?;
        sqlx::query(
            "UPDATE exchange.relay_claims c SET release_status='pending' \
             FROM exchange.sessions s JOIN exchange.grants g ON g.grant_id=s.grant_id \
             WHERE c.session_id=s.session_id AND c.release_status='active' \
             AND (s.ended IS NOT NULL OR s.deadline<=now() OR g.status='revoked')",
        )
        .execute(work.connection())
        .await?;
        let pending = sqlx::query_scalar(
            "SELECT claim_id FROM exchange.relay_claims WHERE release_status='pending' ORDER BY claim_id LIMIT 100",
        )
        .fetch_all(work.connection())
        .await?;
        work.commit().await?;
        Ok(pending)
    }

    /// Mark a verifier's idempotent 204 response durable.
    pub async fn acknowledge_release(&self, claim_id: Uuid) -> Result<(), StoreError> {
        sqlx::query("UPDATE exchange.relay_claims SET release_status='acknowledged' WHERE claim_id=$1 AND release_status='pending'")
            .bind(claim_id).execute(self.database.pool()).await?;
        Ok(())
    }
}

/// Queue releases for sessions just retired by an operator transaction.
pub(crate) async fn queue_releases(
    work: &mut UnitOfWork<'_>,
    sessions: &[Uuid],
) -> Result<(), StoreError> {
    sqlx::query(
        "UPDATE exchange.relay_claims c SET release_status='pending' \
        FROM exchange.sessions s WHERE c.session_id=s.session_id AND c.session_id=ANY($1) \
        AND c.release_status='active' AND s.ended IS NOT NULL",
    )
    .bind(sessions)
    .execute(work.connection())
    .await?;
    Ok(())
}

/// Shared verifier release path for an opaque claim identity.
pub fn release_path(claim_id: Uuid) -> String {
    ReleaseClaim {
        claim_id: claim_id.to_string().into(),
    }
    .path()
}
