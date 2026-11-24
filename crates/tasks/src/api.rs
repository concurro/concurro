//! Private task requests API and browser command proof verification.

use std::{collections::HashMap, sync::Arc};

use axum::{
    Json, Router,
    body::Body,
    extract::{Path, Query, State},
    http::{HeaderMap, StatusCode, header},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use chrono::{DateTime, Utc};
use concurro_contracts::{
    CommandClaims, CommandContent, CommandSubmission, RequestActor, RequestCommand,
};
use concurro_db::Database;
use concurro_github_v1::AppDeliveryPage;
use concurro_tasks_core::{IssueKey, LaterCommand, Request, RequestKind, SourceOrder};
use concurro_tasks_postgres::StandaloneActionSource;
use concurro_tasks_postgres::{
    BaselineRow, TranscriptLine, baseline_page, browser_action, browser_standalone_action,
    create_baseline, current_config_targets, finish_baseline, historical_display,
    imported_transcript_page, index_item, index_page, index_run_links, index_scan_status,
    own_charged_calls, record_browser_action, record_browser_standalone_action, request_record,
    standalone_action_record, task_detail, transcript_page, turn_artifact,
    unavailable_historical_usage,
};
use jsonwebtoken::{Algorithm, DecodingKey, Validation};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use uuid::Uuid;

use crate::{DecisionContext, DecisionTarget};

/// Dependencies for the internal task API; the key grants no signing authority.
#[derive(Clone)]
pub struct TaskApi {
    /// Durable task state.
    pub database: Database,
    /// Existing decision source and target map.
    pub decisions: DecisionContext,
    /// Configured upstream PR repositories by numeric GitHub ID.
    pub standalone_targets: HashMap<i64, DecisionTarget>,
    /// PEM public key for command proofs.
    pub proof_public_key: Arc<Vec<u8>>,
    /// Read-scoped bearer for request status and baseline reads.
    pub read_bearer: Arc<String>,
    /// Ingress-only bearer for App delivery recovery.
    pub delivery_bearer: Option<Arc<String>>,
}

/// Current state of an admitted request.
#[derive(Serialize)]
pub struct CommandResult {
    /// Durable request ID.
    pub request_id: Uuid,
    /// Pending or final disposition.
    pub status: String,
    /// Decision reason when present.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    /// Task affected by a settled request.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub task_id: Option<Uuid>,
    /// Time of the durable decision.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub applied_at: Option<DateTime<Utc>>,
}

/// One consistent page of durable task source records.
#[derive(Serialize)]
pub struct BaselinePage {
    /// Identity of the frozen source snapshot.
    pub snapshot_id: Uuid,
    /// Source facts in stable order.
    pub rows: Vec<BaselineRow>,
    /// Cursor for the next page, absent at the end.
    pub next_cursor: Option<String>,
}

/// Optional cursor for a previously frozen baseline.
#[derive(Deserialize)]
pub struct BaselineQuery {
    /// Snapshot identity and last received ordinal.
    pub cursor: Option<String>,
}

/// One page of original transcript lines for a turn.
#[derive(Serialize)]
pub struct TranscriptPage {
    /// Transcript lines in source order.
    pub lines: Vec<TranscriptLine>,
    /// Next one-based source position, absent at the end.
    pub next_cursor: Option<i64>,
}

/// Optional transcript position for a turn-scoped diagnostic read.
#[derive(Deserialize)]
pub struct TranscriptQuery {
    /// Last received transcript position.
    pub cursor: Option<i64>,
}

/// One configured repository list and its last item identity.
#[derive(Deserialize)]
pub struct IndexQuery {
    /// GitHub owner/name, checked by the internal viewer API.
    pub repository: String,
    /// Issue, PR or executor run list.
    pub kind: String,
    /// Last item key returned by the prior page.
    pub cursor: Option<String>,
}

/// Exact durable index key for a current issue or pull request document.
#[derive(Deserialize)]
pub struct IndexItemQuery {
    /// Source repository owner/name.
    pub repository: String,
    /// Issue, pull request or executor run kind.
    pub kind: String,
    /// Stable source item key.
    pub item_id: String,
}

/// Exact source identity for a durable history read.
#[derive(Deserialize)]
pub struct HistoryQuery {
    /// Numeric source repository identity.
    pub repository_id: i64,
    /// Issue or pull request number.
    pub number: i64,
}

/// One page of durable target index rows.
#[derive(Serialize)]
pub struct IndexPage {
    /// Last completed GitHub scan and any refresh failure.
    pub cache: Value,
    /// Rows in stable item order.
    pub rows: Vec<Value>,
    /// Next item key, absent at the end.
    pub next_cursor: Option<String>,
}

/// Fixed protocol error without internal details.
#[derive(Debug)]
pub struct ApiError(StatusCode, &'static str);

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.0, Json(json!({"error": self.1}))).into_response()
    }
}

/// Serve signed requests and read-scoped outcome lookup.
pub fn router(api: TaskApi) -> Router {
    Router::new()
        .route("/v1/requests", post(post_request))
        .route("/v1/requests/{id}", get(get_request))
        .route("/v1/baseline", get(get_baseline))
        .route("/v1/targets", get(get_targets))
        .route("/v1/index", get(get_index))
        .route("/v1/historical", get(get_historical))
        .route("/v1/billing/{payer}", get(get_billing))
        .route(
            "/v1/targets/{owner}/{repo}/configuration",
            get(get_target_configuration),
        )
        .route("/v1/index/item", get(get_index_item))
        .route("/v1/history", get(get_history))
        .route("/v1/deliveries", get(get_deliveries))
        .route("/v1/deliveries/{id}/redeliver", post(redeliver))
        .route("/v1/turns/{turn}/transcript", get(get_transcript))
        .route(
            "/v1/imports/{deployment}/turns/{source}/transcript",
            get(get_imported_transcript),
        )
        .route(
            "/v1/turns/{turn}/artifacts/{digest}",
            get(get_turn_artifact),
        )
        .with_state(api)
}

/// Serve one revisioned target snapshot without a bearer on the private listener.
async fn get_targets(State(api): State<TaskApi>) -> Result<Json<Value>, ApiError> {
    let Some((revision, targets)) = current_config_targets(&api.database)
        .await
        .map_err(|_| ApiError(StatusCode::SERVICE_UNAVAILABLE, "targets_unavailable"))?
    else {
        return Err(ApiError(
            StatusCode::SERVICE_UNAVAILABLE,
            "targets_unavailable",
        ));
    };
    Ok(Json(json!({"revision":revision,"targets":targets})))
}

/// Read allowlisted current target settings at the fork's exact default commit.
async fn get_target_configuration(
    State(api): State<TaskApi>,
    Path((owner, repo)): Path<(String, String)>,
    headers: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    read_authorized(&headers, &api)?;
    let upstream = format!("{owner}/{repo}");
    let target = api
        .decisions
        .targets
        .values()
        .chain(api.standalone_targets.values())
        .filter_map(|target| target.initialization.as_ref())
        .find(|target| target.upstream.eq_ignore_ascii_case(&upstream))
        .ok_or(ApiError(StatusCode::NOT_FOUND, "target_not_found"))?;
    let result = crate::target_display::current_configuration(&api.decisions.github, target).await;
    result.map(Json).map_err(|error| {
        tracing::warn!(%error, "current target configuration unavailable");
        ApiError(
            StatusCode::SERVICE_UNAVAILABLE,
            "target_configuration_unavailable",
        )
    })
}

/// Read one internal page of persistent target list rows.
async fn get_index(
    State(api): State<TaskApi>,
    Query(query): Query<IndexQuery>,
    headers: HeaderMap,
) -> Result<Json<IndexPage>, ApiError> {
    read_authorized(&headers, &api)?;
    if !matches!(query.kind.as_str(), "issue" | "pull" | "run")
        || query.repository.split_once('/').is_none()
    {
        return Err(ApiError(StatusCode::BAD_REQUEST, "index_query_invalid"));
    }
    let rows = index_page(
        &api.database,
        &query.repository,
        &query.kind,
        query.cursor.as_deref().unwrap_or(""),
    )
    .await
    .map_err(unavailable)?;
    let next_cursor = (rows.len() > 100).then(|| rows[99].item_id.clone());
    let links = if query.kind == "run" {
        index_run_links(&api.database, &query.repository)
            .await
            .map_err(unavailable)?
    } else {
        Vec::new()
    };
    let rows = rows
        .into_iter()
        .take(100)
        .map(|row| {
            let mut row = json!(row);
            if query.kind == "run" {
                let attempt = row["document"]["attempt"]
                    .as_i64()
                    .or_else(|| row["document"]["run_attempt"].as_i64())
                    .unwrap_or(1);
                row["document"]["attribution"] = links
                    .iter()
                    .find(|link| {
                        link["run_id"] == row["document"]["id"] && link["attempt"] == attempt
                    })
                    .cloned()
                    .unwrap_or(Value::Null);
            }
            row
        })
        .collect();
    let cache = index_scan_status(&api.database, &query.repository, &query.kind)
        .await
        .map_err(unavailable)?;
    Ok(Json(IndexPage {
        rows,
        next_cursor,
        cache,
    }))
}

/// Read one current source document under the read-only service credential.
async fn get_index_item(
    State(api): State<TaskApi>,
    Query(query): Query<IndexItemQuery>,
    headers: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    read_authorized(&headers, &api)?;
    if !matches!(query.kind.as_str(), "issue" | "pull" | "run")
        || query.repository.split_once('/').is_none()
        || query.item_id.is_empty()
    {
        return Err(ApiError(StatusCode::BAD_REQUEST, "index_query_invalid"));
    }
    let row = index_item(
        &api.database,
        &query.repository,
        &query.kind,
        &query.item_id,
    )
    .await
    .map_err(unavailable)?
    .ok_or(ApiError(StatusCode::NOT_FOUND, "index_item_not_found"))?;
    Ok(Json(json!(row)))
}

/// Read actual retained sponsor debits under the internal read credential.
async fn get_billing(
    State(api): State<TaskApi>,
    Path(payer): Path<i64>,
    headers: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    read_authorized(&headers, &api)?;
    let rows = own_charged_calls(&api.database, payer)
        .await
        .map_err(unavailable)?;
    let unavailable = unavailable_historical_usage(&api.database)
        .await
        .map_err(unavailable)?;
    Ok(Json(json!({"rows":rows,"unavailable":unavailable})))
}

/// Read historical display snapshots under the internal read credential.
async fn get_historical(
    State(api): State<TaskApi>,
    Query(query): Query<IndexQuery>,
    headers: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    read_authorized(&headers, &api)?;
    if !matches!(query.kind.as_str(), "contribution" | "review") {
        return Err(ApiError(StatusCode::BAD_REQUEST, "historical_kind_invalid"));
    }
    let after = query
        .cursor
        .as_deref()
        .unwrap_or("0")
        .parse::<i64>()
        .map_err(|_| ApiError(StatusCode::BAD_REQUEST, "cursor_invalid"))?;
    let mut rows = historical_display(&api.database, &query.repository, &query.kind, after)
        .await
        .map_err(unavailable)?;
    let next_cursor = (rows.len() > 100).then(|| rows[99]["record"]["number"].to_string());
    rows.truncate(100);
    Ok(Json(json!({"rows":rows,"next_cursor":next_cursor})))
}

/// Read retained task or standalone history under the internal read credential.
async fn get_history(
    State(api): State<TaskApi>,
    Query(query): Query<HistoryQuery>,
    headers: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    read_authorized(&headers, &api)?;
    if query.repository_id <= 0 || query.number <= 0 {
        return Err(ApiError(StatusCode::BAD_REQUEST, "history_query_invalid"));
    }
    let history = task_detail(
        &api.database,
        IssueKey {
            repository_id: query.repository_id,
            number: query.number,
        },
    )
    .await
    .map_err(unavailable)?
    .ok_or(ApiError(StatusCode::NOT_FOUND, "task_not_found"))?;
    Ok(Json(history))
}

/// List App webhook deliveries for ingress recovery with its separate credential.
async fn get_deliveries(
    State(api): State<TaskApi>,
    Query(query): Query<BaselineQuery>,
    headers: HeaderMap,
) -> Result<Json<AppDeliveryPage>, ApiError> {
    delivery_authorized(&headers, &api)?;
    api.decisions
        .github
        .app_deliveries(query.cursor.as_deref())
        .await
        .map(Json)
        .map_err(unavailable)
}

/// Request one harmless GitHub redelivery; ingress deduplicates its GUID.
async fn redeliver(
    State(api): State<TaskApi>,
    Path(id): Path<u64>,
    headers: HeaderMap,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    delivery_authorized(&headers, &api)?;
    api.decisions
        .github
        .redeliver_app_delivery(id)
        .await
        .map_err(unavailable)?;
    Ok((StatusCode::ACCEPTED, Json(json!({}))))
}

/// Verify the signature and every request binding before consulting state.
pub fn verify_proof(
    public_key: &[u8],
    action_id: Uuid,
    content: &CommandContent,
    proof: &str,
) -> Result<CommandClaims, ApiError> {
    let key = DecodingKey::from_rsa_pem(public_key)
        .map_err(|_| ApiError(StatusCode::INTERNAL_SERVER_ERROR, "proof_key_invalid"))?;
    let mut validation = Validation::new(Algorithm::RS256);
    validation.leeway = 0;
    let claims = jsonwebtoken::decode::<CommandClaims>(proof, &key, &validation)
        .map_err(|_| ApiError(StatusCode::FORBIDDEN, "proof_invalid"))?
        .claims;
    if claims.action_id != action_id || claims.content != *content {
        return Err(ApiError(StatusCode::FORBIDDEN, "proof_mismatch"));
    }
    Ok(claims)
}

/// Map the typed wire command onto the existing task decision grammar.
fn request_kind(content: &CommandContent) -> Result<RequestKind, ApiError> {
    let wire = json!({"kind": content.kind, "argument": content.argument});
    let without_empty_argument = json!({"kind": content.kind});
    let command: RequestCommand = serde_json::from_value(wire.clone())
        .or_else(|_| serde_json::from_value(without_empty_argument.clone()))
        .map_err(|_| ApiError(StatusCode::UNPROCESSABLE_ENTITY, "command_invalid"))?;
    let canonical = serde_json::to_value(&command).ok();
    if canonical != Some(wire)
        && !(content.argument == json!({}) && canonical == Some(without_empty_argument))
    {
        return Err(ApiError(
            StatusCode::UNPROCESSABLE_ENTITY,
            "command_invalid",
        ));
    }
    Ok(match command {
        RequestCommand::Start => RequestKind::Start,
        RequestCommand::Stop => RequestKind::Stop,
        RequestCommand::Unenroll => RequestKind::Later(LaterCommand::Unenroll),
        RequestCommand::Approve { plan_id } => {
            RequestKind::Later(LaterCommand::Approve { plan_id })
        }
        RequestCommand::Extend { plan_revisions } => RequestKind::Later(LaterCommand::Retry {
            plan_revisions: Some(plan_revisions),
        }),
        RequestCommand::Resume { description_only } => {
            RequestKind::Later(LaterCommand::Resume { description_only })
        }
        RequestCommand::Sponsor { provider } => {
            RequestKind::Later(LaterCommand::Sponsor { provider })
        }
        RequestCommand::Withdraw { provider } => {
            RequestKind::Later(LaterCommand::Withdraw { provider })
        }
        RequestCommand::Funding { provider, mode } => {
            RequestKind::Later(LaterCommand::Funding { provider, mode })
        }
        RequestCommand::Retry => RequestKind::Later(LaterCommand::Retry {
            plan_revisions: None,
        }),
        RequestCommand::Invalidate => RequestKind::Later(LaterCommand::Invalidate),
    })
}

/// Admit a signed command through the source-ordered task decision queue.
async fn post_request(
    State(api): State<TaskApi>,
    headers: HeaderMap,
    Json(submission): Json<CommandSubmission>,
) -> Result<(StatusCode, Json<CommandResult>), ApiError> {
    let action_id = headers
        .get("idempotency-key")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.parse::<Uuid>().ok())
        .ok_or(ApiError(StatusCode::BAD_REQUEST, "idempotency_key_invalid"))?;
    let claims = verify_proof(
        &api.proof_public_key,
        action_id,
        &submission.content,
        &submission.proof,
    )?;
    let content = serde_json::to_value(&submission.content)
        .map_err(|_| ApiError(StatusCode::BAD_REQUEST, "command_invalid"))?;
    let existing = browser_action(&api.database, action_id, claims.actor.id, &content)
        .await
        .map_err(action_error)?;
    if let Some(existing) = existing {
        let record = request_record(&api.database, existing.request_id)
            .await
            .map_err(unavailable)?
            .ok_or(ApiError(
                StatusCode::SERVICE_UNAVAILABLE,
                "request_unavailable",
            ))?;
        return Ok((
            StatusCode::ACCEPTED,
            Json(CommandResult {
                request_id: record.request_id,
                status: record.status,
                reason: record.reason,
                task_id: record.task_id,
                applied_at: record.applied_at,
            }),
        ));
    }
    if let Some(record) =
        browser_standalone_action(&api.database, action_id, claims.actor.id, &content)
            .await
            .map_err(action_error)?
    {
        return Ok((
            StatusCode::ACCEPTED,
            Json(CommandResult {
                request_id: record.request_id,
                status: record.status,
                reason: record.reason,
                task_id: record.task_id,
                applied_at: record.applied_at,
            }),
        ));
    }
    let kind = request_kind(&submission.content)?;
    let target = api
        .decisions
        .targets
        .get(&submission.content.issue.repository_id)
        .or_else(|| {
            api.standalone_targets
                .get(&submission.content.issue.repository_id)
        })
        .ok_or(ApiError(StatusCode::NOT_FOUND, "target_not_found"))?;
    let (owner, repository) = target
        .repository
        .split_once('/')
        .ok_or(ApiError(StatusCode::SERVICE_UNAVAILABLE, "target_invalid"))?;
    let number = submission.content.issue.number.to_string();
    #[derive(Deserialize)]
    struct IssueAuthor {
        user: RequestActor,
        pull_request: Option<Value>,
    }
    let issue: IssueAuthor = api
        .decisions
        .github
        .read(
            &target.repository,
            &target.read_permissions,
            &["repos", owner, repository, "issues", &number],
            &[],
        )
        .await
        .map_err(|_| ApiError(StatusCode::SERVICE_UNAVAILABLE, "issue_unavailable"))?;
    if issue.pull_request.is_some() {
        let target = api
            .standalone_targets
            .get(&submission.content.issue.repository_id)
            .ok_or(ApiError(
                StatusCode::NOT_FOUND,
                "standalone_target_not_found",
            ))?;
        let mut work = api.database.begin().await.map_err(unavailable)?;
        record_browser_standalone_action(
            &mut work,
            action_id,
            &content,
            submission.content.issue,
            StandaloneActionSource {
                upstream: &target.repository,
                actor: &claims.actor,
                kind,
                source_at: submission.content.source_at,
            },
        )
        .await
        .map_err(action_error)?;
        work.commit().await.map_err(unavailable)?;
        return Ok((
            StatusCode::ACCEPTED,
            Json(CommandResult {
                request_id: action_id,
                status: "pending".to_owned(),
                reason: None,
                task_id: None,
                applied_at: None,
            }),
        ));
    }
    let request = Request {
        id: action_id,
        channel: "browser".to_owned(),
        reply: String::new(),
        comment_id: None,
        issue: submission.content.issue,
        issue_author: issue.user,
        actor: claims.actor,
        kind,
        order: SourceOrder {
            at: submission.content.source_at,
            source_id: 0,
            source: format!("browser:{action_id}"),
            index: 0,
        },
    };
    let mut work = api.database.begin().await.map_err(unavailable)?;
    let result = record_browser_action(&mut work, action_id, &content, &request)
        .await
        .map_err(action_error)?;
    work.commit().await.map_err(unavailable)?;
    Ok((
        StatusCode::ACCEPTED,
        Json(CommandResult {
            request_id: result.request_id,
            status: result.status,
            reason: result.reason,
            task_id: None,
            applied_at: None,
        }),
    ))
}

/// Return a request status only to the read-scoped internal credential.
async fn get_request(
    State(api): State<TaskApi>,
    Path(id): Path<Uuid>,
    headers: HeaderMap,
) -> Result<Json<CommandResult>, ApiError> {
    read_authorized(&headers, &api)?;
    let record = request_record(&api.database, id)
        .await
        .map_err(unavailable)?
        .or(standalone_action_record(&api.database, id)
            .await
            .map_err(unavailable)?)
        .ok_or(ApiError(StatusCode::NOT_FOUND, "request_not_found"))?;
    Ok(Json(CommandResult {
        request_id: record.request_id,
        status: record.status,
        reason: record.reason,
        task_id: record.task_id,
        applied_at: record.applied_at,
    }))
}

/// Return stable pages from one materialized producer snapshot.
async fn get_baseline(
    State(api): State<TaskApi>,
    Query(query): Query<BaselineQuery>,
    headers: HeaderMap,
) -> Result<Json<BaselinePage>, ApiError> {
    read_authorized(&headers, &api)?;
    let (snapshot_id, after) = if let Some(cursor) = query.cursor {
        let (id, after) = cursor
            .split_once(':')
            .ok_or(ApiError(StatusCode::BAD_REQUEST, "cursor_invalid"))?;
        let snapshot_id = id
            .parse::<Uuid>()
            .map_err(|_| ApiError(StatusCode::BAD_REQUEST, "cursor_invalid"))?;
        let after = after
            .parse::<i64>()
            .map_err(|_| ApiError(StatusCode::BAD_REQUEST, "cursor_invalid"))?;
        if after < 0 {
            return Err(ApiError(StatusCode::BAD_REQUEST, "cursor_invalid"));
        }
        (snapshot_id, after)
    } else {
        (
            create_baseline(&api.database).await.map_err(unavailable)?,
            0,
        )
    };
    let mut rows = baseline_page(&api.database, snapshot_id, after, 101)
        .await
        .map_err(unavailable)?
        .ok_or(ApiError(StatusCode::NOT_FOUND, "baseline_not_found"))?;
    let next_cursor = if rows.len() > 100 {
        rows.pop();
        rows.last()
            .map(|row| format!("{snapshot_id}:{}", row.ordinal))
    } else {
        None
    };
    if next_cursor.is_none() {
        finish_baseline(&api.database, snapshot_id)
            .await
            .map_err(unavailable)?;
    }
    Ok(Json(BaselinePage {
        snapshot_id,
        rows,
        next_cursor,
    }))
}

/// Return a turn's retained transcript only to the internal read credential.
async fn get_transcript(
    State(api): State<TaskApi>,
    Path(turn): Path<Uuid>,
    Query(query): Query<TranscriptQuery>,
    headers: HeaderMap,
) -> Result<Json<TranscriptPage>, ApiError> {
    read_authorized(&headers, &api)?;
    let after = query.cursor.unwrap_or(0);
    if after < 0 {
        return Err(ApiError(StatusCode::BAD_REQUEST, "cursor_invalid"));
    }
    let mut lines = transcript_page(&api.database, turn, after, 101)
        .await
        .map_err(unavailable)?
        .ok_or(ApiError(StatusCode::NOT_FOUND, "transcript_not_found"))?;
    let next_cursor = if lines.len() > 100 {
        lines.pop();
        lines.last().map(|line| line.position)
    } else {
        None
    };
    Ok(Json(TranscriptPage { lines, next_cursor }))
}

/// Return an imported Python turn's original transcript to the internal reader.
async fn get_imported_transcript(
    State(api): State<TaskApi>,
    Path((deployment, source)): Path<(String, String)>,
    Query(query): Query<TranscriptQuery>,
    headers: HeaderMap,
) -> Result<Json<TranscriptPage>, ApiError> {
    read_authorized(&headers, &api)?;
    let after = query.cursor.unwrap_or(0);
    if after < 0 {
        return Err(ApiError(StatusCode::BAD_REQUEST, "cursor_invalid"));
    }
    let mut lines = imported_transcript_page(&api.database, &deployment, &source, after, 101)
        .await
        .map_err(unavailable)?
        .ok_or(ApiError(StatusCode::NOT_FOUND, "transcript_not_found"))?;
    let next_cursor = if lines.len() > 100 {
        lines.pop();
        lines.last().map(|line| line.position)
    } else {
        None
    };
    Ok(Json(TranscriptPage { lines, next_cursor }))
}

/// Return an original turn artifact only through its owning turn link.
async fn get_turn_artifact(
    State(api): State<TaskApi>,
    Path((turn, digest)): Path<(Uuid, String)>,
    headers: HeaderMap,
) -> Result<Response, ApiError> {
    read_authorized(&headers, &api)?;
    let (media_type, body) = turn_artifact(&api.database, turn, &digest)
        .await
        .map_err(unavailable)?
        .ok_or(ApiError(StatusCode::NOT_FOUND, "artifact_not_found"))?;
    let mut response = Response::new(Body::from(body));
    response.headers_mut().insert(
        header::CONTENT_TYPE,
        media_type
            .parse()
            .map_err(|_| ApiError(StatusCode::SERVICE_UNAVAILABLE, "artifact_unavailable"))?,
    );
    Ok(response)
}

/// Require the service's read credential before consulting durable state.
fn read_authorized(headers: &HeaderMap, api: &TaskApi) -> Result<(), ApiError> {
    let bearer = headers
        .get("authorization")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.strip_prefix("Bearer "));
    if bearer != Some(api.read_bearer.as_str()) {
        return Err(ApiError(StatusCode::UNAUTHORIZED, "read_unauthorized"));
    }
    Ok(())
}

/// Require ingress's recovery bearer, never the broader backoffice read bearer.
fn delivery_authorized(headers: &HeaderMap, api: &TaskApi) -> Result<(), ApiError> {
    let bearer = headers
        .get("authorization")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.strip_prefix("Bearer "));
    if bearer.is_none() || bearer != api.delivery_bearer.as_deref().map(String::as_str) {
        return Err(ApiError(StatusCode::UNAUTHORIZED, "delivery_unauthorized"));
    }
    Ok(())
}

/// Keep database failures out of the public protocol body.
fn unavailable(_error: impl std::fmt::Display) -> ApiError {
    ApiError(StatusCode::SERVICE_UNAVAILABLE, "request_unavailable")
}

/// Distinguish an exact action-content conflict from unavailable storage.
fn action_error(error: anyhow::Error) -> ApiError {
    if error.to_string() == "idempotency_conflict" {
        ApiError(StatusCode::CONFLICT, "idempotency_conflict")
    } else if error.to_string() == "pr_owned_elsewhere" {
        ApiError(StatusCode::CONFLICT, "pr_owned_elsewhere")
    } else {
        unavailable(error)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use concurro_contracts::TaskIssue;

    #[test]
    fn browser_body_cannot_supply_an_actor() {
        let raw = json!({
            "issue": {"repository_id": 1, "number": 2},
            "kind": "start", "argument": {}, "detail": {},
            "source_at": "2026-11-20T00:00:00Z", "proof": "unused",
            "actor": {"id": 3, "login": "other"}
        });
        assert!(serde_json::from_value::<CommandSubmission>(raw).is_err());
    }

    #[test]
    fn browser_command_matches_existing_request_kind() {
        for (kind, argument, expected) in [
            ("start", json!({}), RequestKind::Start),
            (
                "sponsor",
                json!({}),
                RequestKind::Later(LaterCommand::Sponsor { provider: None }),
            ),
            (
                "withdraw",
                json!({"provider":"openai"}),
                RequestKind::Later(LaterCommand::Withdraw {
                    provider: "openai".to_owned(),
                }),
            ),
            (
                "extend",
                json!({"plan_revisions": 2}),
                RequestKind::Later(LaterCommand::Retry {
                    plan_revisions: Some(2),
                }),
            ),
        ] {
            let content = CommandContent {
                issue: TaskIssue {
                    repository_id: 1,
                    number: 2,
                },
                kind: kind.to_owned(),
                argument,
                detail: json!({}),
                source_at: Utc::now(),
            };
            assert_eq!(
                request_kind(&content).unwrap_or_else(|error| panic!("{kind}: {error:?}")),
                expected
            );
        }
    }
}
