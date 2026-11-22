//! Session-key admission, provider accounting and settled turn facts.

use chrono::{DateTime, Utc};
use concurro_contracts::{
    Admission, AdmissionRequest, Envelope, EventId, ExchangeTurnFinished, FinishRequest, Grant,
    InferenceUsage, ProbeResponse, ProviderFailure, ProviderRequests, SessionStatusResponse,
    Timestamp, TurnResultV2, TurnUsage, UsageTokens,
};
use concurro_db::UnitOfWork;
use sha2::Digest;
use sqlx::{FromRow, types::Json};
use uuid::Uuid;

use super::{Store, StoreError, append_status, lock, observe, refused};

/// Frozen fields required to admit one session-key request.
#[derive(FromRow)]
struct SessionRow {
    /// Session identity.
    session_id: Uuid,
    /// Grant identity.
    grant_id: String,
    /// Granted route and limits.
    body: Json<Grant>,
    /// Grant lifecycle.
    grant_status: String,
    /// Session model allowance.
    models: Json<Vec<String>>,
    /// Token allowance for this session.
    budget: i64,
    /// Settled token total.
    spent_tokens: i64,
    /// Lease cutoff.
    deadline: DateTime<Utc>,
    /// Runner retirement time.
    ended: Option<DateTime<Utc>>,
    /// Most recent runner renewal.
    last_seen: Option<DateTime<Utc>>,
}

/// Durable request identity and its first accepted finish.
#[derive(FromRow)]
struct RequestRow {
    /// Request identity.
    request_id: Uuid,
    /// Serving session.
    session_id: Uuid,
    /// Running turn.
    turn_id: Uuid,
    /// Funding grant.
    grant_id: String,
    /// Model named at admission.
    model: String,
    /// Admission time.
    started: DateTime<Utc>,
    /// First finish time.
    finished: Option<DateTime<Utc>>,
    /// Whether dispatch reached a provider.
    dispatched: Option<bool>,
    /// Settled usage.
    usage: Option<Json<UsageTokens>>,
    /// Missing usage reason.
    usage_unavailable: Option<String>,
}

/// Terminal turn fields used for its one settled fact.
#[derive(FromRow)]
struct FinishedTurn {
    /// Attempt identity.
    attempt_id: Uuid,
    /// Lifecycle status.
    status: String,
    /// Native result.
    result: Option<Json<TurnResultV2>>,
    /// First trusted provider failure.
    provider_failure: Option<Json<ProviderFailure>>,
    /// Whether a fact has already been appended.
    finished_event_emitted: bool,
}

impl Store {
    /// Admit a provider call, or check a session without recording a model probe.
    pub async fn admit(
        &self,
        body: &AdmissionRequest,
    ) -> Result<Result<Admission, ProbeResponse>, StoreError> {
        let mut work = self.database.begin().await?;
        lock(&mut work).await?;
        let digest = hex::encode(sha2::Sha256::digest(body.key.as_bytes()));
        let session: Option<SessionRow> = sqlx::query_as(
            "SELECT s.session_id,s.grant_id,g.body,g.status AS grant_status,s.models,s.budget,s.spent_tokens,s.deadline,s.ended,s.last_seen FROM exchange.sessions s JOIN exchange.grants g ON g.grant_id=s.grant_id WHERE s.session_key_hash=$1 FOR UPDATE OF s"
        ).bind(digest).fetch_optional(work.connection()).await?;
        let Some(session) = session else {
            return Err(refused(403, "invalid_key"));
        };
        if session.grant_status != "issued" {
            return Err(refused(403, "grant_revoked"));
        }
        if session.ended.is_some()
            || session.deadline <= Utc::now()
            || session
                .last_seen
                .is_none_or(|seen| seen <= Utc::now() - chrono::Duration::seconds(180))
        {
            return Err(refused(403, "session_ended"));
        }
        if body.probe == Some(true) {
            work.commit().await?;
            return Ok(Err(ProbeResponse {
                models: session.models.0,
            }));
        }
        if !session.models.0.contains(&body.model) {
            return Err(refused(403, "model_denied"));
        }
        if session.body.0.credential.custody.as_deref() == Some("relay") {
            return Err(refused(503, "relay_unavailable"));
        }
        let running: Vec<(Uuid, Option<Json<ProviderFailure>>)> = sqlx::query_as(
            "SELECT turn_id,provider_failure FROM exchange.turns WHERE session_id=$1 AND status='running'"
        ).bind(session.session_id).fetch_all(work.connection()).await?;
        if running.len() != 1 {
            return Err(refused(403, "turn_not_running"));
        }
        let (turn, failure) = &running[0];
        if failure.is_some() {
            return Err(refused(403, "provider_condition"));
        }
        let request = body.request_id.unwrap_or_else(Uuid::now_v7);
        let previous: Option<(Uuid, Uuid, String, Option<String>)> = sqlx::query_as(
            "SELECT session_id,turn_id,model,category FROM exchange.inference_requests WHERE request_id=$1"
        ).bind(request).fetch_optional(work.connection()).await?;
        if let Some((prior_session, prior_turn, prior_model, category)) = previous {
            if prior_session != session.session_id
                || prior_turn != *turn
                || prior_model != body.model
            {
                return Err(refused(409, "request_reused"));
            }
            if category.as_deref() == Some("budget_denied") {
                return Err(refused(403, "budget_denied"));
            }
            if category.is_some() {
                return Err(refused(409, "request_finished"));
            }
        } else if session.spent_tokens >= session.budget {
            sqlx::query("INSERT INTO exchange.inference_requests(request_id,session_id,turn_id,grant_id,model) VALUES($1,$2,$3,$4,$5)")
                .bind(request).bind(session.session_id).bind(turn).bind(&session.grant_id).bind(&body.model)
                .execute(work.connection()).await?;
            settle_request(
                &mut work,
                self,
                request,
                &FinishRequest {
                    dispatched: false,
                    category: "budget_denied".to_owned(),
                    usage: None,
                    usage_unavailable: None,
                    retry_after: None,
                },
            )
            .await?;
            work.commit().await?;
            return Err(refused(403, "budget_denied"));
        } else {
            sqlx::query("INSERT INTO exchange.inference_requests(request_id,session_id,turn_id,grant_id,model) VALUES($1,$2,$3,$4,$5)")
                .bind(request).bind(session.session_id).bind(turn).bind(&session.grant_id).bind(&body.model)
                .execute(work.connection()).await?;
        }
        let admission = Admission {
            request_id: request,
            session_id: session.session_id,
            turn_id: *turn,
            grant_id: session.grant_id,
            credential: session.body.0.credential,
            models: session.models.0,
            remaining_tokens: session.budget - session.spent_tokens,
            deadline: Timestamp(session.deadline),
            claim: None,
        };
        work.commit().await?;
        Ok(Ok(admission))
    }

    /// Record the first trusted provider report and append its usage fact once.
    pub async fn finish_request(
        &self,
        request: Uuid,
        body: &FinishRequest,
    ) -> Result<(), StoreError> {
        if !matches!(
            body.category.as_str(),
            "ok" | "auth"
                | "quota_exhausted"
                | "rate_limited"
                | "budget_denied"
                | "model_denied"
                | "unavailable"
                | "unknown"
        ) || body.usage.as_ref().is_some_and(|usage| {
            usage
                .input_tokens
                .checked_add(usage.output_tokens)
                .is_none_or(|sum| sum > i64::MAX as u64)
        }) {
            return Err(refused(422, "invalid_finish"));
        }
        let mut work = self.database.begin().await?;
        lock(&mut work).await?;
        settle_request(&mut work, self, request, body).await?;
        work.commit().await?;
        Ok(())
    }

    /// Return the sessions still allowed to serve their current provider streams.
    pub async fn sessions_status(
        &self,
        sessions: &[Uuid],
    ) -> Result<SessionStatusResponse, StoreError> {
        let live = sqlx::query_scalar(
            "SELECT s.session_id FROM exchange.sessions s JOIN exchange.grants g ON g.grant_id=s.grant_id WHERE s.session_id=ANY($1) AND s.ended IS NULL AND s.deadline>now() AND s.last_seen>now()-interval '180 seconds' AND g.status='issued' AND EXISTS(SELECT 1 FROM exchange.turns t WHERE t.session_id=s.session_id AND t.status='running' AND t.provider_failure IS NULL)"
        ).bind(sessions).fetch_all(self.database.pool()).await?;
        Ok(SessionStatusResponse { live })
    }

    /// Close requests whose five-minute report window has elapsed.
    pub async fn reconcile_requests(&self) -> Result<u64, StoreError> {
        let pending: Vec<Uuid> = sqlx::query_scalar(
            "SELECT request_id FROM exchange.inference_requests WHERE finished IS NULL AND settlement_deadline<=now()"
        ).fetch_all(self.database.pool()).await?;
        for request in &pending {
            let mut work = self.database.begin().await?;
            lock(&mut work).await?;
            close_unreported(&mut work, self, *request).await?;
            work.commit().await?;
        }
        Ok(pending.len() as u64)
    }
}

/// Start the five-minute report window for a terminal turn and append its fact if ready.
pub(crate) async fn arm_settlement(
    work: &mut UnitOfWork<'_>,
    store: &Store,
    turn: Uuid,
) -> Result<(), StoreError> {
    sqlx::query("UPDATE exchange.inference_requests SET settlement_deadline=COALESCE(settlement_deadline,now()+interval '300 seconds') WHERE turn_id=$1 AND finished IS NULL")
        .bind(turn).execute(work.connection()).await?;
    emit_finished_if_ready(work, store, turn).await
}

/// Apply the first request report inside the same unit of work as its facts.
async fn settle_request(
    work: &mut UnitOfWork<'_>,
    store: &Store,
    request: Uuid,
    body: &FinishRequest,
) -> Result<(), StoreError> {
    let row: Option<RequestRow> = sqlx::query_as(
        "SELECT request_id,session_id,turn_id,grant_id,model,started,finished,dispatched,usage,usage_unavailable FROM exchange.inference_requests WHERE request_id=$1 FOR UPDATE"
    ).bind(request).fetch_optional(work.connection()).await?;
    let Some(row) = row else {
        return Err(refused(404, "unknown_request"));
    };
    if row.finished.is_some() {
        return Ok(());
    }
    if let Some(deadline) = sqlx::query_scalar::<_, Option<DateTime<Utc>>>(
        "SELECT settlement_deadline FROM exchange.inference_requests WHERE request_id=$1",
    )
    .bind(request)
    .fetch_one(work.connection())
    .await?
        && deadline <= Utc::now()
    {
        close_unreported(work, store, request).await?;
        return Ok(());
    }
    let tokens = body
        .usage
        .as_ref()
        .map_or(0, |usage| (usage.input_tokens + usage.output_tokens) as i64);
    sqlx::query("UPDATE exchange.inference_requests SET finished=now(),dispatched=$2,category=$3,usage=$4,usage_unavailable=$5,retry_after=$6 WHERE request_id=$1")
        .bind(request).bind(body.dispatched).bind(&body.category).bind(body.usage.as_ref().map(Json))
        .bind(&body.usage_unavailable).bind(body.retry_after).execute(work.connection()).await?;
    sqlx::query("UPDATE exchange.sessions SET spent_tokens=spent_tokens+$2 WHERE session_id=$1")
        .bind(row.session_id)
        .bind(tokens)
        .execute(work.connection())
        .await?;
    if matches!(
        body.category.as_str(),
        "auth"
            | "quota_exhausted"
            | "rate_limited"
            | "budget_denied"
            | "model_denied"
            | "unavailable"
    ) {
        let grant: Json<Grant> =
            sqlx::query_scalar("SELECT body FROM exchange.grants WHERE grant_id=$1")
                .bind(&row.grant_id)
                .fetch_one(work.connection())
                .await?;
        let failure = ProviderFailure {
            category: body.category.clone(),
            model: row.model.clone(),
            retry_after: body.retry_after,
            request_id: request,
            grant_id: row.grant_id.clone(),
            record_id: grant.0.credential.record_id,
            version: grant.0.credential.version,
        };
        let revision: Option<i64> = sqlx::query_scalar(
            "UPDATE exchange.turns SET provider_failure=$2,revision=revision+1 WHERE turn_id=$1 AND provider_failure IS NULL RETURNING revision"
        ).bind(row.turn_id).bind(Json(&failure)).fetch_optional(work.connection()).await?;
        if let Some(revision) = revision {
            let observation = observe(work, row.turn_id).await?.expect("admitted turn");
            append_status(work, &store.outbox, &observation, revision, None).await?;
        }
    }
    append_usage(work, store, &row, body, Utc::now()).await?;
    emit_finished_if_ready(work, store, row.turn_id).await
}

/// Record a missing gateway report after the bounded retry window.
async fn close_unreported(
    work: &mut UnitOfWork<'_>,
    store: &Store,
    request: Uuid,
) -> Result<(), StoreError> {
    let row: Option<RequestRow> = sqlx::query_as(
        "SELECT request_id,session_id,turn_id,grant_id,model,started,finished,dispatched,usage,usage_unavailable FROM exchange.inference_requests WHERE request_id=$1 FOR UPDATE"
    ).bind(request).fetch_optional(work.connection()).await?;
    let Some(row) = row else {
        return Ok(());
    };
    if row.finished.is_some() {
        return Ok(());
    }
    sqlx::query("UPDATE exchange.inference_requests SET finished=now(),category='unknown',usage_unavailable='not_reported' WHERE request_id=$1")
        .bind(request).execute(work.connection()).await?;
    append_usage(
        work,
        store,
        &row,
        &FinishRequest {
            dispatched: false,
            category: "unknown".to_owned(),
            usage: None,
            usage_unavailable: Some("not_reported".to_owned()),
            retry_after: None,
        },
        Utc::now(),
    )
    .await?;
    emit_finished_if_ready(work, store, row.turn_id).await
}

/// Append one request's immutable usage identity to the shared outbox.
async fn append_usage(
    work: &mut UnitOfWork<'_>,
    store: &Store,
    row: &RequestRow,
    body: &FinishRequest,
    finished: DateTime<Utc>,
) -> Result<(), StoreError> {
    let grant: Json<Grant> =
        sqlx::query_scalar("SELECT body FROM exchange.grants WHERE grant_id=$1")
            .bind(&row.grant_id)
            .fetch_one(work.connection())
            .await?;
    let event = InferenceUsage {
        request_id: row.request_id,
        session_id: row.session_id,
        turn_id: row.turn_id,
        grant_id: row.grant_id.clone(),
        record_id: grant.0.credential.record_id,
        version: grant.0.credential.version,
        model: row.model.clone(),
        category: body.category.clone(),
        usage: body.usage.clone(),
        usage_unavailable: body.usage_unavailable.clone(),
        started: Timestamp(row.started),
        finished: Timestamp(finished),
    };
    store
        .outbox
        .append(
            work,
            &Envelope::fact(
                EventId(Uuid::new_v5(
                    &Uuid::NAMESPACE_OID,
                    format!("inference-usage:{}", row.request_id).as_bytes(),
                )),
                "exchange",
                Timestamp(finished),
                event,
            ),
        )
        .await?;
    Ok(())
}

/// Append the terminal fact once every admitted request has settled.
async fn emit_finished_if_ready(
    work: &mut UnitOfWork<'_>,
    store: &Store,
    turn: Uuid,
) -> Result<(), StoreError> {
    let row: Option<FinishedTurn> = sqlx::query_as(
        "SELECT attempt_id,status,result,provider_failure,finished_event_emitted FROM exchange.turns WHERE turn_id=$1 FOR UPDATE"
    ).bind(turn).fetch_optional(work.connection()).await?;
    let Some(row) = row else {
        return Ok(());
    };
    if row.finished_event_emitted
        || !matches!(row.status.as_str(), "finished" | "lost" | "cancelled")
    {
        return Ok(());
    }
    let requests: Vec<RequestRow> = sqlx::query_as(
        "SELECT request_id,session_id,turn_id,grant_id,model,started,finished,dispatched,usage,usage_unavailable FROM exchange.inference_requests WHERE turn_id=$1 ORDER BY started,request_id"
    ).bind(turn).fetch_all(work.connection()).await?;
    if requests.iter().any(|request| request.finished.is_none()) {
        return Ok(());
    }
    let provider_requests = if requests
        .iter()
        .any(|request| request.dispatched == Some(true))
    {
        ProviderRequests::Some
    } else if requests
        .iter()
        .any(|request| request.usage_unavailable.as_deref() == Some("not_reported"))
    {
        ProviderRequests::Unknown
    } else {
        ProviderRequests::None
    };
    let usage = TurnUsage {
        requests: requests.len() as u64,
        input_tokens: requests
            .iter()
            .filter_map(|request| request.usage.as_ref())
            .map(|usage| usage.input_tokens)
            .sum(),
        output_tokens: requests
            .iter()
            .filter_map(|request| request.usage.as_ref())
            .map(|usage| usage.output_tokens)
            .sum(),
        unavailable: requests
            .iter()
            .filter(|request| request.usage.is_none())
            .count() as u64,
    };
    sqlx::query("UPDATE exchange.turns SET finished_event_emitted=true,provider_requests=$2 WHERE turn_id=$1")
        .bind(turn).bind(match provider_requests { ProviderRequests::Some=>"some", ProviderRequests::None=>"none", ProviderRequests::Unknown=>"unknown" })
        .execute(work.connection()).await?;
    let event = ExchangeTurnFinished {
        turn_id: turn,
        observation: Some(
            observe(work, turn)
                .await?
                .expect("locked terminal turn exists"),
        ),
        attempt_id: row.attempt_id,
        status: row.status,
        result: row.result.map(|value| value.0),
        provider_failure: row.provider_failure.map(|value| value.0),
        provider_requests,
        usage,
    };
    store
        .outbox
        .append(
            work,
            &Envelope::fact(
                EventId(Uuid::new_v5(
                    &Uuid::NAMESPACE_OID,
                    format!("exchange-turn-finished:{turn}").as_bytes(),
                )),
                "exchange",
                Timestamp(Utc::now()),
                event,
            ),
        )
        .await?;
    Ok(())
}
