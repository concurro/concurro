//! Runner lifecycle, token hashes, turn results and blob metadata.

use chrono::{DateTime, Duration, Utc};
use concurro_contracts::{ExpectedClaims, TurnBinding, TurnPayloadV2, TurnResultV2};
use serde_json::Value;
use sqlx::{FromRow, types::Json};
use uuid::Uuid;

use super::{Store, StoreError, append_status, arm_settlement, lock, observe, refused, staff_next};

/// Expected OIDC identity and immutable task binding for a dispatched session.
pub struct ClaimContext {
    /// Exact expected GitHub claims.
    pub expected: ExpectedClaims,
    /// Bound task and engine.
    pub binding: TurnBinding,
    /// First accepted run, if already claimed.
    pub run_id: Option<String>,
    /// Session deadline.
    pub deadline: DateTime<Utc>,
}

/// Frozen key response fields, assembled only after token authorization.
pub struct KeyContext {
    /// Frozen task binding.
    pub binding: TurnBinding,
    /// Session models.
    pub models: Vec<String>,
    /// Session deadline.
    pub deadline: DateTime<Utc>,
}

/// A queued turn assigned to an authenticated runner.
pub struct RunnerTurn {
    /// Turn identity.
    pub turn_id: Uuid,
    /// Frozen execution payload.
    pub payload: TurnPayloadV2,
    /// Session deadline.
    pub deadline: DateTime<Utc>,
}

/// Session columns needed to verify an incoming GitHub claim.
#[derive(FromRow)]
struct ClaimRow {
    /// Frozen GitHub identity.
    expected_claims: Json<ExpectedClaims>,
    /// Frozen task and engine.
    binding: Json<TurnBinding>,
    /// First accepted workflow run.
    run_id: Option<String>,
    /// Lease cutoff.
    deadline: DateTime<Utc>,
    /// Session retirement time.
    ended: Option<DateTime<Utc>>,
    /// Initial dispatch time.
    created: DateTime<Utc>,
    /// Most recent heartbeat.
    last_seen: Option<DateTime<Utc>>,
}

/// Locked session columns used while recording the first claim.
#[derive(FromRow)]
struct BoundClaimRow {
    /// First accepted workflow run.
    run_id: Option<String>,
    /// Stored digest of its runner token.
    runner_token_hash: Option<String>,
    /// Session retirement time.
    ended: Option<DateTime<Utc>>,
    /// Lease cutoff.
    deadline: DateTime<Utc>,
    /// Initial dispatch time.
    created: DateTime<Utc>,
    /// Grant fenced by revocation.
    grant_id: String,
    /// Most recent heartbeat.
    last_seen: Option<DateTime<Utc>>,
}

/// Session columns used to authorize and renew a runner token.
#[derive(FromRow)]
struct TouchRow {
    /// Stored digest of the runner token.
    runner_token_hash: Option<String>,
    /// Session retirement time.
    ended: Option<DateTime<Utc>>,
    /// Lease cutoff.
    deadline: DateTime<Utc>,
    /// Most recent heartbeat.
    last_seen: Option<DateTime<Utc>>,
    /// Grant fenced by revocation.
    grant_id: String,
}

/// Frozen response columns for the engine key handshake.
#[derive(FromRow)]
struct KeyRow {
    /// Frozen engine and task.
    binding: Json<TurnBinding>,
    /// Model allowance for the session.
    models: Json<Vec<String>>,
    /// Lease cutoff.
    deadline: DateTime<Utc>,
    /// Stored digest of the derived key.
    session_key_hash: Option<String>,
}

impl Store {
    /// Read the frozen claim context without consuming an OIDC token.
    pub async fn claim_context(&self, session: Uuid) -> Result<ClaimContext, StoreError> {
        let row: Option<ClaimRow> = sqlx::query_as(
            "SELECT expected_claims,binding,run_id,deadline,ended,created,last_seen FROM exchange.sessions WHERE session_id=$1"
        ).bind(session).fetch_optional(self.database.pool()).await?;
        let Some(ClaimRow {
            expected_claims: Json(expected),
            binding: Json(binding),
            run_id,
            deadline,
            ended,
            created,
            last_seen: seen,
        }) = row
        else {
            return Err(refused(404, "unknown_session"));
        };
        if ended.is_some()
            || deadline < Utc::now()
            || (run_id.is_none() && created + Duration::seconds(1200) < Utc::now())
            || seen.is_some_and(|value| value + Duration::seconds(180) < Utc::now())
        {
            return Err(refused(410, "session_ended"));
        }
        Ok(ClaimContext {
            expected,
            binding,
            run_id,
            deadline,
        })
    }

    /// Persist the single-use OIDC ID and first run under the operator lock.
    pub async fn bind_claim(
        &self,
        session: Uuid,
        jti: &str,
        expiry: DateTime<Utc>,
        run: &str,
        runner_hash: &str,
    ) -> Result<(), StoreError> {
        let mut work = self.database.begin().await?;
        lock(&mut work).await?;
        let row: Option<BoundClaimRow> = sqlx::query_as(
            "SELECT run_id,runner_token_hash,ended,deadline,created,grant_id,last_seen FROM exchange.sessions WHERE session_id=$1 FOR UPDATE"
        ).bind(session).fetch_optional(work.connection()).await?;
        let Some(BoundClaimRow {
            run_id: bound,
            runner_token_hash: stored_hash,
            ended,
            deadline,
            created,
            grant_id: grant,
            last_seen: seen,
        }) = row
        else {
            return Err(refused(404, "unknown_session"));
        };
        if ended.is_some()
            || deadline < Utc::now()
            || (bound.is_none() && created + Duration::seconds(1200) < Utc::now())
            || seen.is_some_and(|value| value + Duration::seconds(180) < Utc::now())
        {
            return Err(refused(410, "session_ended"));
        }
        let issued: bool =
            sqlx::query_scalar("SELECT status='issued' FROM exchange.grants WHERE grant_id=$1")
                .bind(grant)
                .fetch_one(work.connection())
                .await?;
        if !issued {
            return Err(refused(410, "grant_revoked"));
        }
        if bound.as_deref().is_some_and(|value| value != run)
            || stored_hash
                .as_deref()
                .is_some_and(|value| value != runner_hash)
        {
            return Err(refused(403, "another_run"));
        }
        sqlx::query("DELETE FROM exchange.oidc_jti WHERE expires<now()-interval '60 seconds'")
            .execute(work.connection())
            .await?;
        let inserted = sqlx::query(
            "INSERT INTO exchange.oidc_jti(jti,expires) VALUES($1,$2) ON CONFLICT DO NOTHING",
        )
        .bind(jti)
        .bind(expiry)
        .execute(work.connection())
        .await?
        .rows_affected();
        if inserted == 0 {
            return Err(refused(401, "token_reused"));
        }
        sqlx::query("UPDATE exchange.sessions SET run_id=COALESCE(run_id,$2),runner_token_hash=COALESCE(runner_token_hash,$3),bound_at=COALESCE(bound_at,now()),last_seen=now() WHERE session_id=$1")
            .bind(session).bind(run).bind(runner_hash).execute(work.connection()).await?;
        if bound.is_none() {
            let turn: Option<Uuid> = sqlx::query_scalar("SELECT turn_id FROM exchange.turns WHERE session_id=$1 AND status='queued' ORDER BY created LIMIT 1")
                .bind(session).fetch_optional(work.connection()).await?;
            if let Some(turn) = turn {
                let revision: i64 = sqlx::query_scalar("UPDATE exchange.turns SET revision=revision+1 WHERE turn_id=$1 RETURNING revision")
                    .bind(turn).fetch_one(work.connection()).await?;
                let observation = observe(&mut work, turn).await?.expect("bound turn");
                append_status(&mut work, &self.outbox, &observation, revision, None).await?;
            }
        }
        work.commit().await?;
        Ok(())
    }

    /// Retain an OIDC refusal on a same-repository session.
    pub async fn claim_refusal(&self, session: Uuid, reason: &str) -> Result<(), StoreError> {
        let mut work = self.database.begin().await?;
        lock(&mut work).await?;
        sqlx::query(
            "UPDATE exchange.sessions SET refusal=$2 WHERE session_id=$1 AND ended IS NULL",
        )
        .bind(session)
        .bind(reason)
        .execute(work.connection())
        .await?;
        let turn: Option<Uuid> = sqlx::query_scalar(
            "SELECT turn_id FROM exchange.turns WHERE session_id=$1 AND status='queued' LIMIT 1",
        )
        .bind(session)
        .fetch_optional(work.connection())
        .await?;
        if let Some(turn) = turn {
            let revision: i64 = sqlx::query_scalar(
                "UPDATE exchange.turns SET revision=revision+1 WHERE turn_id=$1 RETURNING revision",
            )
            .bind(turn)
            .fetch_one(work.connection())
            .await?;
            let observation = observe(&mut work, turn).await?.expect("refused turn");
            append_status(&mut work, &self.outbox, &observation, revision, None).await?;
        }
        work.commit().await?;
        Ok(())
    }

    /// Authenticate a derived runner token and refresh its heartbeat.
    pub async fn runner_touch(&self, session: Uuid, token_hash: &str) -> Result<(), StoreError> {
        let row: Option<TouchRow> = sqlx::query_as(
            "SELECT runner_token_hash,ended,deadline,last_seen,grant_id FROM exchange.sessions WHERE session_id=$1"
        ).bind(session).fetch_optional(self.database.pool()).await?;
        let Some(TouchRow {
            runner_token_hash: stored,
            ended,
            deadline,
            last_seen: seen,
            grant_id: grant,
        }) = row
        else {
            return Err(refused(404, "unknown_session"));
        };
        if stored.as_deref() != Some(token_hash) {
            return Err(refused(401, "invalid_runner_token"));
        }
        let issued: bool =
            sqlx::query_scalar("SELECT status='issued' FROM exchange.grants WHERE grant_id=$1")
                .bind(grant)
                .fetch_one(self.database.pool())
                .await?;
        if ended.is_some()
            || deadline < Utc::now()
            || seen.is_some_and(|value| value + Duration::seconds(180) < Utc::now())
            || !issued
        {
            return Err(refused(410, "session_ended"));
        }
        sqlx::query(
            "UPDATE exchange.sessions SET last_seen=now() WHERE session_id=$1 AND ended IS NULL",
        )
        .bind(session)
        .execute(self.database.pool())
        .await?;
        Ok(())
    }

    /// Store only the derived key's hash after the engine commit matches.
    pub async fn runner_key(
        &self,
        session: Uuid,
        engine_sha: &str,
        key_hash: &str,
    ) -> Result<KeyContext, StoreError> {
        let row: Option<KeyRow> = sqlx::query_as(
            "SELECT binding,models,deadline,session_key_hash FROM exchange.sessions WHERE session_id=$1"
        ).bind(session).fetch_optional(self.database.pool()).await?;
        let Some(KeyRow {
            binding: Json(binding),
            models: Json(models),
            deadline,
            session_key_hash: prior,
        }) = row
        else {
            return Err(refused(404, "unknown_session"));
        };
        if binding.engine.sha != engine_sha {
            self.claim_refusal(session, "runner engine differs from the session's")
                .await?;
            return Err(refused(409, "engine_mismatch"));
        }
        if prior.as_deref().is_some_and(|value| value != key_hash) {
            return Err(refused(410, "key_rotated"));
        }
        sqlx::query("UPDATE exchange.sessions SET session_key_hash=COALESCE(session_key_hash,$2) WHERE session_id=$1")
            .bind(session).bind(key_hash).execute(self.database.pool()).await?;
        Ok(KeyContext {
            binding,
            models,
            deadline,
        })
    }

    /// Atomically take the queued turn assigned to this session.
    pub async fn runner_next(&self, session: Uuid) -> Result<Option<RunnerTurn>, StoreError> {
        let mut work = self.database.begin().await?;
        lock(&mut work).await?;
        let row: Option<(Uuid, Json<Value>, DateTime<Utc>)> = sqlx::query_as(
            "SELECT t.turn_id,t.body,s.deadline FROM exchange.turns t JOIN exchange.sessions s ON s.session_id=t.session_id JOIN exchange.grants g ON g.grant_id=s.grant_id WHERE t.session_id=$1 AND t.status='queued' AND s.ended IS NULL AND s.session_key_hash IS NOT NULL AND s.deadline>now() AND g.status='issued' ORDER BY t.created LIMIT 1"
        ).bind(session).fetch_optional(work.connection()).await?;
        let Some((turn, Json(body), deadline)) = row else {
            work.commit().await?;
            return Ok(None);
        };
        let revision: i64 = sqlx::query_scalar("UPDATE exchange.turns SET status='running',started=now(),revision=revision+1 WHERE turn_id=$1 AND status='queued' RETURNING revision")
            .bind(turn).fetch_one(work.connection()).await?;
        let observation = observe(&mut work, turn).await?.expect("running turn");
        append_status(&mut work, &self.outbox, &observation, revision, None).await?;
        work.commit().await?;
        let payload: TurnPayloadV2 =
            serde_json::from_value(body["payload"].clone()).map_err(anyhow::Error::from)?;
        Ok(Some(RunnerTurn {
            turn_id: turn,
            payload,
            deadline,
        }))
    }

    /// Check a content-addressed artifact already uploaded by this runner.
    pub async fn record_blob(
        &self,
        session: Uuid,
        sha: &str,
        kind: &str,
        size: usize,
    ) -> Result<(), StoreError> {
        let mut work = self.database.begin().await?;
        sqlx::query(
            "INSERT INTO exchange.blobs(sha256,kind,size) VALUES($1,$2,$3) ON CONFLICT DO NOTHING",
        )
        .bind(sha)
        .bind(kind)
        .bind(size as i64)
        .execute(work.connection())
        .await?;
        let (stored_kind, stored_size): (String, i64) =
            sqlx::query_as("SELECT kind,size FROM exchange.blobs WHERE sha256=$1")
                .bind(sha)
                .fetch_one(work.connection())
                .await?;
        if stored_kind != kind || stored_size != size as i64 {
            return Err(refused(409, "blob_conflict"));
        }
        sqlx::query("INSERT INTO exchange.blob_uploads(session_id,sha256) VALUES($1,$2) ON CONFLICT DO NOTHING")
            .bind(session).bind(sha).execute(work.connection()).await?;
        work.commit().await?;
        Ok(())
    }

    /// Authorize a referenced blob and return its object-store kind.
    pub async fn blob_kind(&self, session: Uuid, sha: &str) -> Result<String, StoreError> {
        let uploaded: Option<String> = sqlx::query_scalar(
            "SELECT b.kind FROM exchange.blobs b JOIN exchange.blob_uploads u ON u.sha256=b.sha256 WHERE b.sha256=$1 AND u.session_id=$2 AND EXISTS (SELECT 1 FROM exchange.turns t WHERE t.session_id=$2 AND (t.result #>> '{workspace,sha256}'=$1 OR t.result #>> '{transcript,sha256}'=$1 OR t.result #>> '{retained,sha256}'=$1))"
        ).bind(sha).bind(session).fetch_optional(self.database.pool()).await?;
        if let Some(kind) = uploaded {
            return Ok(kind);
        }
        let retained: bool = sqlx::query_scalar(
            "SELECT EXISTS(SELECT 1 FROM exchange.turns WHERE session_id=$1 AND body #>> '{payload,retained,sha256}'=$2)"
        ).bind(session).bind(sha).fetch_one(self.database.pool()).await?;
        if retained {
            Ok("archive".to_owned())
        } else {
            Err(refused(404, "unknown_blob"))
        }
    }

    /// Accept a result once, checking that every referenced upload exists.
    pub async fn runner_result(
        &self,
        session: Uuid,
        turn: Uuid,
        token_hash: &str,
        result: &TurnResultV2,
    ) -> Result<(), StoreError> {
        let mut work = self.database.begin().await?;
        lock(&mut work).await?;
        let stored: Option<Option<String>> = sqlx::query_scalar(
            "SELECT runner_token_hash FROM exchange.sessions WHERE session_id=$1 FOR UPDATE",
        )
        .bind(session)
        .fetch_optional(work.connection())
        .await?;
        let Some(stored) = stored else {
            return Err(refused(404, "unknown_session"));
        };
        if stored.as_deref() != Some(token_hash) {
            return Err(refused(401, "invalid_runner_token"));
        }
        let row: Option<(String, Option<Json<TurnResultV2>>)> = sqlx::query_as(
            "SELECT status,result FROM exchange.turns WHERE turn_id=$1 AND session_id=$2",
        )
        .bind(turn)
        .bind(session)
        .fetch_optional(work.connection())
        .await?;
        let Some((status, prior)) = row else {
            return Err(refused(409, "turn_not_running"));
        };
        if status == "cancelled" {
            return Err(refused(409, "result_conflict"));
        }
        if status == "finished" && prior.as_ref().is_some_and(|value| value.0 == *result) {
            work.commit().await?;
            return Ok(());
        }
        if status != "running" {
            return Err(refused(409, "result_conflict"));
        }
        let live: bool = sqlx::query_scalar(
            "SELECT s.ended IS NULL AND s.deadline>now() AND s.last_seen>now()-interval '180 seconds' AND g.status='issued' FROM exchange.sessions s JOIN exchange.grants g ON g.grant_id=s.grant_id WHERE s.session_id=$1"
        ).bind(session).fetch_one(work.connection()).await?;
        if !live {
            return Err(refused(410, "session_ended"));
        }
        sqlx::query("UPDATE exchange.sessions SET last_seen=now() WHERE session_id=$1")
            .bind(session)
            .execute(work.connection())
            .await?;
        for (artifact, kind) in [
            (&result.workspace, "workspace"),
            (&result.transcript, "archive"),
            (&result.retained, "archive"),
        ]
        .into_iter()
        .filter_map(|(artifact, kind)| artifact.as_ref().map(|value| (value, kind)))
        {
            let size =
                i64::try_from(artifact.size).map_err(|_| refused(422, "invalid_blob_size"))?;
            let exists: bool = sqlx::query_scalar(
                "SELECT EXISTS(SELECT 1 FROM exchange.blobs b JOIN exchange.blob_uploads u ON u.sha256=b.sha256 WHERE b.sha256=$1 AND b.size=$2 AND b.kind=$3 AND u.session_id=$4)",
            )
            .bind(&artifact.sha256)
            .bind(size)
            .bind(kind)
            .bind(session)
            .fetch_one(work.connection())
            .await?;
            if !exists {
                return Err(refused(422, "missing_blob"));
            }
        }
        let revision: i64 = sqlx::query_scalar(
            "UPDATE exchange.turns SET status='finished',result=$2,finished=now(),revision=revision+1 WHERE turn_id=$1 RETURNING revision"
        ).bind(turn).bind(Json(result)).fetch_one(work.connection()).await?;
        let observation = observe(&mut work, turn).await?.expect("finished turn");
        append_status(&mut work, &self.outbox, &observation, revision, None).await?;
        arm_settlement(&mut work, self, turn).await?;
        work.commit().await?;
        Ok(())
    }

    /// End a runner session and mark its currently assigned work lost.
    pub async fn runner_end(&self, session: Uuid) -> Result<(), StoreError> {
        let mut work = self.database.begin().await?;
        lock(&mut work).await?;
        let group: Option<String> = sqlx::query_scalar(
            "UPDATE exchange.sessions SET ended=COALESCE(ended,now()),end_reason=COALESCE(end_reason,'runner_end') WHERE session_id=$1 RETURNING staffing_group"
        ).bind(session).fetch_optional(work.connection()).await?;
        let Some(group) = group else {
            return Err(refused(404, "unknown_session"));
        };
        let turns: Vec<(Uuid, String)> = sqlx::query_as(
            "SELECT turn_id,status FROM exchange.turns WHERE session_id=$1 AND status IN ('queued','running') ORDER BY created"
        ).bind(session).fetch_all(work.connection()).await?;
        for (turn, status) in turns {
            if status == "queued" {
                sqlx::query("UPDATE exchange.turns SET session_id=NULL WHERE turn_id=$1")
                    .bind(turn)
                    .execute(work.connection())
                    .await?;
                continue;
            }
            let revision: i64 = sqlx::query_scalar(
                "UPDATE exchange.turns SET status='lost',finished=now(),revision=revision+1 WHERE turn_id=$1 RETURNING revision"
            ).bind(turn).fetch_one(work.connection()).await?;
            let observation = observe(&mut work, turn).await?.expect("lost turn");
            append_status(&mut work, &self.outbox, &observation, revision, None).await?;
            arm_settlement(&mut work, self, turn).await?;
        }
        staff_next(&mut work, &self.outbox, &group, None).await?;
        work.commit().await?;
        Ok(())
    }

    /// End stale sessions and replace their queued work after restart or timer wake.
    pub async fn reconcile_sessions(&self) -> Result<u64, StoreError> {
        let sessions: Vec<Uuid> = sqlx::query_scalar(
            "SELECT session_id FROM exchange.sessions WHERE ended IS NULL AND (deadline<now() OR (bound_at IS NULL AND created<now()-interval '1200 seconds') OR (bound_at IS NOT NULL AND last_seen<now()-interval '180 seconds'))"
        ).fetch_all(self.database.pool()).await?;
        for session in &sessions {
            self.runner_end(*session).await?;
        }
        Ok(sessions.len() as u64)
    }
}
