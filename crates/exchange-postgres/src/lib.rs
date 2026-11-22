//! Durable exchange operator state and transactional event append.

use chrono::{DateTime, Duration, Utc};
use concurro_contracts::{
    Dispatch, Envelope, EventId, ExchangeTurnStatus, ProviderFailure, ProviderRequests,
    RetiredSessions, Timestamp, TurnObservation, TurnResultV2, TurnSubmissionV2, TurnUsage,
};
use concurro_db::{Database, UnitOfWork};
use concurro_events_postgres::{Outbox, OutboxError};
use concurro_tasks_core::python_digest;
use serde_json::Value;
use sqlx::{FromRow, migrate::Migrator, types::Json};
use uuid::Uuid;

static MIGRATIONS: Migrator = sqlx::migrate!("./migrations");

mod admission;
mod runner;
use admission::arm_settlement;
pub use runner::{ClaimContext, KeyContext, RunnerTurn};

/// Install exchange tables in the exchange schema.
pub async fn migrate(database: &Database) -> anyhow::Result<()> {
    database.migrate_in_schema("exchange", &MIGRATIONS).await
}

/// A refused operator operation or a storage/outbox failure.
#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    /// A public, stable protocol refusal.
    #[error("{code}")]
    Refused {
        /// HTTP status to return.
        status: u16,
        /// Stable error code.
        code: &'static str,
    },
    /// PostgreSQL was unavailable or rejected the transaction.
    #[error(transparent)]
    Database(#[from] sqlx::Error),
    /// A transaction's event could not be appended.
    #[error(transparent)]
    Outbox(#[from] OutboxError),
    /// The canonical body could not be encoded.
    #[error(transparent)]
    Encode(#[from] anyhow::Error),
}

/// Exchange operator persistence with one service-owned database and outbox.
#[derive(Clone, Debug)]
pub struct Store {
    database: Database,
    outbox: Outbox,
}

/// Columns needed to assemble a current operator observation.
#[derive(FromRow)]
struct TurnRow {
    /// Turn identity.
    turn_id: Uuid,
    /// Monotonic lifecycle revision.
    revision: i64,
    /// Current lifecycle status.
    status: String,
    /// Assigned or last serving session.
    session_id: Option<Uuid>,
    /// Durable admission time.
    created: DateTime<Utc>,
    /// Runner start time.
    started: Option<DateTime<Utc>>,
    /// Terminal time.
    finished: Option<DateTime<Utc>>,
    /// Terminal result, when available.
    result: Option<Json<TurnResultV2>>,
    /// First trusted provider failure.
    provider_failure: Option<Json<ProviderFailure>>,
    /// Request evidence after settlement.
    provider_requests: Option<String>,
}

impl Store {
    /// Bind the operator store to an exchange event stream.
    pub fn new(database: Database, outbox: Outbox) -> Self {
        Self { database, outbox }
    }

    /// Submit a canonical turn once and return the live observation on replay.
    pub async fn submit(&self, turn: Uuid, value: &Value) -> Result<TurnObservation, StoreError> {
        if value.pointer("/grant/payer").is_some_and(|payer| {
            !payer.is_null()
                && payer
                    .as_object()
                    .is_none_or(|fields| fields.keys().any(|key| key != "id" && key != "login"))
        }) {
            return Err(refused(422, "invalid_body"));
        }
        let body: TurnSubmissionV2 =
            serde_json::from_value(value.clone()).map_err(|_| refused(422, "invalid_body"))?;
        validate_submission(turn, &body, value)?;
        let digest = python_digest(value)?;
        let grant_value = value.get("grant").expect("typed grant exists");
        let grant_digest = python_digest(grant_value)?;
        let mut work = self.database.begin().await?;
        lock(&mut work).await?;
        if let Some(tombstone) = sqlx::query_scalar::<_, Option<String>>(
            "SELECT digest FROM exchange.turn_tombstones WHERE turn_id=$1",
        )
        .bind(turn)
        .fetch_optional(work.connection())
        .await?
        {
            if tombstone
                .as_deref()
                .is_some_and(|existing| existing != digest)
            {
                return Err(refused(409, "turn_in_use"));
            }
            return Err(refused(
                410,
                if tombstone.is_some() {
                    "turn_expired"
                } else {
                    "turn_cancelled"
                },
            ));
        }
        if let Some(existing) =
            sqlx::query_scalar::<_, String>("SELECT digest FROM exchange.turns WHERE turn_id=$1")
                .bind(turn)
                .fetch_optional(work.connection())
                .await?
        {
            if existing != digest {
                return Err(refused(409, "turn_in_use"));
            }
            let observation = observe(&mut work, turn).await?.expect("existing turn");
            work.commit().await?;
            return Ok(observation);
        }
        match sqlx::query_as::<_, (Option<String>, String)>(
            "SELECT digest,status FROM exchange.grants WHERE grant_id=$1",
        )
        .bind(&body.grant.grant_id)
        .fetch_optional(work.connection())
        .await?
        {
            Some((_, status)) if status == "revoked" => return Err(refused(410, "grant_revoked")),
            Some((Some(existing), _)) if existing != grant_digest => {
                return Err(refused(409, "grant_in_use"));
            }
            Some(_) => {}
            None => {
                sqlx::query("INSERT INTO exchange.grants(grant_id,body,digest,status) VALUES($1,$2,$3,'issued')")
                    .bind(&body.grant.grant_id).bind(Json(grant_value)).bind(&grant_digest)
                    .execute(work.connection()).await?;
            }
        }
        let now = Utc::now();
        sqlx::query("INSERT INTO exchange.turns(turn_id,attempt_id,task_id,grant_id,digest,body,staffing_group,status,revision,created) VALUES($1,$2,$3,$4,$5,$6,$7,'queued',1,$8)")
            .bind(turn).bind(body.attempt_id).bind(body.task_id).bind(&body.grant.grant_id)
            .bind(&digest).bind(Json(value)).bind(&body.staffing_group).bind(now)
            .execute(work.connection()).await?;
        let promoted =
            staff_next(&mut work, &self.outbox, &body.staffing_group, Some(turn)).await?;
        let observation = observe(&mut work, turn).await?.expect("inserted turn");
        if promoted != Some(turn) {
            append_status(&mut work, &self.outbox, &observation, 1, None).await?;
        }
        work.commit().await?;
        Ok(observation)
    }

    /// Read an existing turn without changing its admission decision.
    pub async fn observation(&self, turn: Uuid) -> Result<TurnObservation, StoreError> {
        let mut work = self.database.begin().await?;
        let observation = observe(&mut work, turn)
            .await?
            .ok_or_else(|| refused(404, "unknown_turn"))?;
        work.commit().await?;
        Ok(observation)
    }

    /// Cancel a turn, fence an unseen ID, and retire its complete session set.
    pub async fn cancel(&self, turn: Uuid) -> Result<RetiredSessions, StoreError> {
        let mut work = self.database.begin().await?;
        lock(&mut work).await?;
        let existed =
            sqlx::query_scalar::<_, String>("SELECT status FROM exchange.turns WHERE turn_id=$1")
                .bind(turn)
                .fetch_optional(work.connection())
                .await?;
        let changed = if existed
            .as_deref()
            .is_some_and(|value| value == "queued" || value == "running")
        {
            sqlx::query_scalar::<_, i64>("UPDATE exchange.turns SET status='cancelled',finished=now(),revision=revision+1 WHERE turn_id=$1 RETURNING revision")
                .bind(turn).fetch_optional(work.connection()).await?
        } else {
            None
        };
        if existed.is_none() {
            sqlx::query("INSERT INTO exchange.turn_tombstones(turn_id,digest) VALUES($1,NULL) ON CONFLICT DO NOTHING")
                .bind(turn).execute(work.connection()).await?;
        }
        let sessions = session_ids(&mut work, turn).await?;
        let groups: Vec<String> = sqlx::query_scalar(
            "SELECT DISTINCT staffing_group FROM exchange.sessions WHERE session_id=ANY($1)",
        )
        .bind(&sessions)
        .fetch_all(work.connection())
        .await?;
        for session in &sessions {
            sqlx::query("UPDATE exchange.sessions SET ended=COALESCE(ended,now()),end_reason=COALESCE(end_reason,'turn_cancelled') WHERE session_id=$1 AND NOT EXISTS (SELECT 1 FROM exchange.turns WHERE session_id=$1 AND turn_id<>$2 AND status IN ('queued','running'))")
                .bind(session).bind(turn).execute(work.connection()).await?;
        }
        if let Some(revision) = changed {
            let observation = observe(&mut work, turn).await?.expect("updated turn");
            append_status(&mut work, &self.outbox, &observation, revision, None).await?;
            arm_settlement(&mut work, self, turn).await?;
        }
        for group in groups {
            staff_next(&mut work, &self.outbox, &group, None).await?;
        }
        let pending: i64 = sqlx::query_scalar("SELECT count(*) FROM exchange.relay_claims WHERE session_id=ANY($1) AND release_status='pending'")
            .bind(&sessions).fetch_one(work.connection()).await?;
        work.commit().await?;
        if pending > 0 {
            return Err(refused(502, "release_pending"));
        }
        Ok(RetiredSessions { sessions })
    }

    /// Fence a grant before or after submit and retire all sessions it ever allocated.
    pub async fn revoke(&self, grant_id: &str) -> Result<RetiredSessions, StoreError> {
        if grant_id.is_empty() {
            return Err(refused(422, "invalid_grant"));
        }
        let mut work = self.database.begin().await?;
        lock(&mut work).await?;
        sqlx::query("INSERT INTO exchange.grants(grant_id,status,revoked_at) VALUES($1,'revoked',now()) ON CONFLICT(grant_id) DO UPDATE SET status='revoked',revoked_at=COALESCE(exchange.grants.revoked_at,now())")
            .bind(grant_id).execute(work.connection()).await?;
        let sessions: Vec<Uuid> = sqlx::query_scalar(
            "SELECT session_id FROM exchange.sessions WHERE grant_id=$1 ORDER BY session_id",
        )
        .bind(grant_id)
        .fetch_all(work.connection())
        .await?;
        let groups: Vec<String> = sqlx::query_scalar(
            "SELECT DISTINCT staffing_group FROM exchange.sessions WHERE session_id=ANY($1)",
        )
        .bind(&sessions)
        .fetch_all(work.connection())
        .await?;
        for session in &sessions {
            sqlx::query("UPDATE exchange.sessions SET ended=COALESCE(ended,now()),end_reason=COALESCE(end_reason,'grant_revoked') WHERE session_id=$1")
                .bind(session).execute(work.connection()).await?;
        }
        let turns: Vec<Uuid> = sqlx::query_scalar("SELECT turn_id FROM exchange.turns WHERE grant_id=$1 AND status IN ('queued','running') ORDER BY turn_id")
            .bind(grant_id).fetch_all(work.connection()).await?;
        for turn in turns {
            let revision: i64 = sqlx::query_scalar("UPDATE exchange.turns SET status='lost',finished=now(),revision=revision+1 WHERE turn_id=$1 RETURNING revision")
                .bind(turn).fetch_one(work.connection()).await?;
            let observation = observe(&mut work, turn).await?.expect("updated turn");
            append_status(&mut work, &self.outbox, &observation, revision, None).await?;
            arm_settlement(&mut work, self, turn).await?;
        }
        for group in groups {
            staff_next(&mut work, &self.outbox, &group, None).await?;
        }
        let pending: i64 = sqlx::query_scalar("SELECT count(*) FROM exchange.relay_claims WHERE session_id=ANY($1) AND release_status='pending'")
            .bind(&sessions).fetch_one(work.connection()).await?;
        work.commit().await?;
        if pending > 0 {
            return Err(refused(502, "release_pending"));
        }
        Ok(RetiredSessions { sessions })
    }

    /// Move terminal turns older than the cutoff to durable tombstones.
    pub async fn prune(&self, cutoff: DateTime<Utc>) -> Result<u64, StoreError> {
        let mut work = self.database.begin().await?;
        lock(&mut work).await?;
        sqlx::query("INSERT INTO exchange.turn_tombstones(turn_id,digest,result) SELECT turn_id,digest,result FROM exchange.turns WHERE finished<$1 AND finished_event_emitted ON CONFLICT DO NOTHING")
            .bind(cutoff).execute(work.connection()).await?;
        let deleted =
            sqlx::query("DELETE FROM exchange.turns WHERE finished<$1 AND finished_event_emitted")
                .bind(cutoff)
                .execute(work.connection())
                .await?
                .rows_affected();
        work.commit().await?;
        Ok(deleted)
    }
}

/// Refuse malformed new submissions while allowing exact prior replays in the store.
fn validate_submission(
    turn: Uuid,
    body: &TurnSubmissionV2,
    value: &Value,
) -> Result<(), StoreError> {
    if turn != body.attempt_id
        || body.grant.grant_id.is_empty()
        || body.models.is_empty()
        || body
            .models
            .iter()
            .any(|model| model.is_empty() || !body.grant.models.contains(model))
        || body.budget <= 0
        || body.budget > body.grant.limits.tokens
        || body.lease_seconds <= 0
        || body.lease_seconds > body.grant.limits.lease_seconds
        || body.expected_claims.actor_id.is_empty()
        || body.expected_claims.repository_id.is_empty()
        || body.expected_claims.repository_owner_id.is_empty()
        || body.expected_claims.job_workflow_ref.is_empty()
        || body.binding.engine.repository.is_empty()
        || body.binding.engine.sha.is_empty()
    {
        return Err(refused(422, "invalid_submission"));
    }
    let credential = &body.grant.credential;
    match body.grant.purpose.as_str() {
        "task_org"
            if body.grant.payer.is_none()
                && credential.entry_id.is_none()
                && credential.record_id.is_none()
                && credential.version.is_none()
                && credential.custody.is_none()
                && credential.relay_id.is_none()
                && !credential.route.is_empty() => {}
        "task_contributed"
            if body
                .grant
                .payer
                .as_ref()
                .is_some_and(|payer| payer.id > 0 && !payer.login.is_empty())
                && credential
                    .entry_id
                    .as_deref()
                    .is_some_and(|id| !id.is_empty())
                && credential
                    .record_id
                    .as_deref()
                    .is_some_and(|id| !id.is_empty())
                && credential.version.is_some_and(|version| version > 0)
                && !credential.route.is_empty()
                && (credential.custody.as_deref() == Some("secret_store")
                    && credential.relay_id.is_none()
                    || credential.custody.as_deref() == Some("relay")
                        && credential
                            .relay_id
                            .as_deref()
                            .is_some_and(|id| !id.is_empty())) => {}
        _ => return Err(refused(422, "invalid_grant")),
    }
    let mut claims = value["expected_claims"].clone();
    claims.as_object_mut().expect("claims object").remove("ref");
    let staffing = python_digest(&serde_json::json!([
        claims,
        value["binding"],
        value["models"],
        body.budget,
        body.lease_seconds as f64
    ]))?;
    if staffing != body.staffing_group {
        return Err(refused(422, "invalid_staffing_group"));
    }
    Ok(())
}

/// Serialize absent-row decisions across submit, cancel and revoke.
async fn lock(work: &mut UnitOfWork<'_>) -> Result<(), sqlx::Error> {
    sqlx::query("SELECT pg_advisory_xact_lock(hashtext('concurro-exchange-operator'))")
        .execute(work.connection())
        .await?;
    Ok(())
}

/// Assign an idle group session without crossing grants, or create a dispatch.
async fn staff(
    work: &mut UnitOfWork<'_>,
    turn: Uuid,
    body: &TurnSubmissionV2,
    now: DateTime<Utc>,
) -> Result<Option<(Uuid, Option<Dispatch>)>, StoreError> {
    let live: Option<(Uuid, String)> = sqlx::query_as(
        "SELECT session_id,grant_id FROM exchange.sessions WHERE staffing_group=$1 AND ended IS NULL AND deadline>now() AND (bound_at IS NOT NULL OR created>now()-interval '1200 seconds') ORDER BY created LIMIT 1"
    ).bind(&body.staffing_group).fetch_optional(work.connection()).await?;
    let reusable = if let Some((session, grant)) = live {
        let active: bool = sqlx::query_scalar(
            "SELECT EXISTS(SELECT 1 FROM exchange.turns WHERE session_id=$1 AND status IN ('queued','running'))",
        )
        .bind(session)
        .fetch_one(work.connection())
        .await?;
        if active {
            return Ok(None);
        }
        if grant == body.grant.grant_id {
            Some(session)
        } else {
            sqlx::query("UPDATE exchange.sessions SET ended=now(),end_reason='grant_switch' WHERE session_id=$1")
                .bind(session).execute(work.connection()).await?;
            None
        }
    } else {
        None
    };
    let (session, dispatch) = if let Some(session) = reusable {
        (session, None)
    } else {
        let session = Uuid::now_v7();
        let deadline = now + Duration::seconds(body.lease_seconds);
        sqlx::query("INSERT INTO exchange.sessions(session_id,staffing_group,grant_id,expected_claims,binding,models,budget,lease_seconds,created,deadline) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)")
            .bind(session).bind(&body.staffing_group).bind(&body.grant.grant_id)
            .bind(Json(&body.expected_claims)).bind(Json(&body.binding)).bind(Json(&body.models))
            .bind(body.budget).bind(body.lease_seconds).bind(now).bind(deadline)
            .execute(work.connection()).await?;
        (
            session,
            Some(Dispatch {
                session_id: session,
            }),
        )
    };
    sqlx::query("UPDATE exchange.turns SET session_id=$2 WHERE turn_id=$1")
        .bind(turn)
        .bind(session)
        .execute(work.connection())
        .await?;
    sqlx::query("INSERT INTO exchange.turn_sessions(turn_id,session_id) VALUES($1,$2) ON CONFLICT DO NOTHING")
        .bind(turn).bind(session).execute(work.connection()).await?;
    Ok(Some((session, dispatch)))
}

/// Promote the oldest eligible turn whenever its group has capacity.
async fn staff_next(
    work: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    group: &str,
    new_turn: Option<Uuid>,
) -> Result<Option<Uuid>, StoreError> {
    let next = sqlx::query_as::<_, (Uuid, Json<TurnSubmissionV2>, Option<Uuid>)>(
        "SELECT t.turn_id,t.body,t.session_id FROM exchange.turns t LEFT JOIN exchange.sessions s ON s.session_id=t.session_id WHERE t.staffing_group=$1 AND t.status='queued' AND (t.session_id IS NULL OR s.ended IS NOT NULL OR s.deadline<=now() OR (s.bound_at IS NULL AND s.created<=now()-interval '1200 seconds')) ORDER BY t.created,t.turn_id LIMIT 1"
    ).bind(group).fetch_optional(work.connection()).await?;
    let Some((turn, Json(body), previous)) = next else {
        return Ok(None);
    };
    let grant_status: String =
        sqlx::query_scalar("SELECT status FROM exchange.grants WHERE grant_id=$1")
            .bind(&body.grant.grant_id)
            .fetch_one(work.connection())
            .await?;
    if grant_status != "issued" {
        return Ok(None);
    }
    let Some((session, dispatch)) = staff(work, turn, &body, Utc::now()).await? else {
        return Ok(None);
    };
    if let Some(previous) = previous.filter(|previous| *previous != session) {
        sqlx::query("UPDATE exchange.sessions SET ended=COALESCE(ended,now()),end_reason=COALESCE(end_reason,'session_expired') WHERE session_id=$1")
            .bind(previous).execute(work.connection()).await?;
    }
    let observation = observe(work, turn).await?.expect("waiting turn");
    if new_turn == Some(turn) {
        append_status(work, outbox, &observation, 1, dispatch).await?;
    } else {
        let revision: i64 = sqlx::query_scalar(
            "UPDATE exchange.turns SET revision=revision+1 WHERE turn_id=$1 RETURNING revision",
        )
        .bind(turn)
        .fetch_one(work.connection())
        .await?;
        append_status(work, outbox, &observation, revision, dispatch).await?;
    }
    Ok(Some(turn))
}

/// Read an observation and include dispatch while its assigned session remains unbound.
async fn observe(
    work: &mut UnitOfWork<'_>,
    turn: Uuid,
) -> Result<Option<TurnObservation>, StoreError> {
    let row = sqlx::query_as::<_, TurnRow>("SELECT turn_id,revision,status,session_id,created,started,finished,result,provider_failure,provider_requests FROM exchange.turns WHERE turn_id=$1")
        .bind(turn).fetch_optional(work.connection()).await?;
    let Some(row) = row else {
        return Ok(None);
    };
    let session = if let Some(id) = row.session_id {
        sqlx::query_as::<_, (Option<String>, Option<String>, Option<DateTime<Utc>>)>(
            "SELECT run_id,refusal,ended FROM exchange.sessions WHERE session_id=$1",
        )
        .bind(id)
        .fetch_optional(work.connection())
        .await?
    } else {
        None
    };
    let (run_id, mut refusal, ended) = session.unwrap_or((None, None, None));
    if refusal.is_none() {
        refusal = sqlx::query_scalar(
            "SELECT s.refusal FROM exchange.turn_sessions ts JOIN exchange.sessions s USING (session_id) \
             WHERE ts.turn_id=$1 AND s.refusal IS NOT NULL ORDER BY s.created DESC LIMIT 1",
        )
        .bind(turn)
        .fetch_optional(work.connection())
        .await?;
    }
    let dispatch = row
        .session_id
        .filter(|_| row.status == "queued" && run_id.is_none() && ended.is_none())
        .map(|session_id| Dispatch { session_id });
    let usage = if row.provider_requests.is_some() {
        let (requests, input_tokens, output_tokens, unavailable): (i64, i64, i64, i64) =
            sqlx::query_as("SELECT count(*),coalesce(sum((usage->>'input_tokens')::bigint),0)::bigint,coalesce(sum((usage->>'output_tokens')::bigint),0)::bigint,count(*) FILTER (WHERE usage IS NULL) FROM exchange.inference_requests WHERE turn_id=$1")
                .bind(turn).fetch_one(work.connection()).await?;
        Some(TurnUsage {
            requests: requests as u64,
            input_tokens: input_tokens as u64,
            output_tokens: output_tokens as u64,
            unavailable: unavailable as u64,
        })
    } else {
        None
    };
    Ok(Some(TurnObservation {
        turn_id: row.turn_id,
        revision: Some(row.revision as u64),
        status: row.status,
        session_id: row.session_id,
        run_id,
        dispatch,
        refusal,
        created: Timestamp(row.created),
        started: row.started.map(Timestamp),
        finished: row.finished.map(Timestamp),
        result: row.result.map(|value| value.0),
        provider_failure: row.provider_failure.map(|value| value.0),
        provider_requests: row.provider_requests.as_deref().map(|value| match value {
            "some" => ProviderRequests::Some,
            "none" => ProviderRequests::None,
            "unknown" => ProviderRequests::Unknown,
            _ => unreachable!("constrained request evidence"),
        }),
        usage,
    }))
}

/// Append one lifecycle snapshot using the row's monotonic revision.
async fn append_status(
    work: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    observation: &TurnObservation,
    revision: i64,
    dispatch: Option<Dispatch>,
) -> Result<(), StoreError> {
    let event = ExchangeTurnStatus {
        turn_id: observation.turn_id,
        status: observation.status.clone(),
        session_id: observation.session_id,
        run_id: observation.run_id.clone(),
        dispatch,
        refusal: observation.refusal.clone(),
        provider_failure: observation.provider_failure.clone(),
        created: observation.created,
        started: observation.started,
        finished: observation.finished,
    };
    let identity = format!("exchange-turn-status:{}:{revision}", observation.turn_id);
    outbox
        .append(
            work,
            &Envelope::snapshot(
                EventId(Uuid::new_v5(&Uuid::NAMESPACE_OID, identity.as_bytes())),
                "exchange",
                Timestamp(Utc::now()),
                format!("turn:{}", observation.turn_id),
                u64::try_from(revision).expect("positive revision"),
                event,
            ),
        )
        .await?;
    Ok(())
}

/// Return all sessions ever associated with a turn, including after pruning.
async fn session_ids(work: &mut UnitOfWork<'_>, turn: Uuid) -> Result<Vec<Uuid>, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT session_id FROM exchange.turn_sessions WHERE turn_id=$1 ORDER BY session_id",
    )
    .bind(turn)
    .fetch_all(work.connection())
    .await
}

/// Construct a stable public refusal.
fn refused(status: u16, code: &'static str) -> StoreError {
    StoreError::Refused { status, code }
}
