//! Viewer-scoped read routes over the durable backoffice projection.

use std::{
    convert::Infallible,
    sync::{Arc, RwLock},
    time::Duration,
};

use axum::{
    Json, Router,
    body::Body,
    extract::{Path, Query, State},
    http::{HeaderMap, StatusCode, header},
    response::{
        IntoResponse, Response,
        sse::{Event, KeepAlive, Sse},
    },
    routing::get,
};
use concurro_backoffice_postgres as storage;
use concurro_db::Database;
use futures_util::stream;
use reqwest::Client;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use uuid::Uuid;

use crate::{Target, auth};

mod views;

/// Read-only dependencies; no GitHub, Exchange, Vault or command credential.
#[derive(Clone)]
pub struct ApiState {
    /// Durable read model.
    pub database: Database,
    /// Private HTTP client for read authorities.
    pub client: Client,
    /// Python session authority's platform address.
    pub authority_url: String,
    /// Read-scoped service credential for viewer lookups.
    pub authority_bearer: String,
    /// Task application's private address.
    pub task_api_url: String,
    /// Read-scoped task credential for diagnostics.
    pub task_read_bearer: String,
    /// Configured target identities, without any App key.
    pub targets: Arc<RwLock<Vec<Target>>>,
}

/// Shared fixed error shape for the frontend.
pub struct ApiError(StatusCode, &'static str);

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.0, Json(json!({"error":self.1}))).into_response()
    }
}

/// Optional target and stage filters with a stable source cursor.
#[derive(Deserialize)]
pub struct TaskQuery {
    /// Target upstream name.
    pub target: Option<String>,
    /// Task stage.
    pub stage: Option<String>,
    /// Contribution or standalone review.
    pub kind: Option<String>,
    /// Last source identity received.
    pub cursor: Option<String>,
}

/// One filtered task list page.
#[derive(Serialize)]
pub struct TaskPage {
    /// Viewer-safe summaries.
    pub tasks: Vec<Value>,
    /// Last examined source identity when another row remains.
    pub next_cursor: Option<String>,
    /// Read model revision for refetches.
    pub revision: i64,
}

/// One target list and its source cursor.
#[derive(Deserialize)]
pub struct IndexQuery {
    /// Issue, pull request or executor run list.
    pub kind: String,
    /// Last source key returned by the task API.
    pub cursor: Option<String>,
}

/// Optional source position for both current and imported transcript pages.
#[derive(Deserialize)]
pub struct TranscriptQuery {
    /// Last transcript position received by the viewer.
    pub cursor: Option<i64>,
}

/// Serve read-only task, issue and diagnostic routes.
pub fn router(state: ApiState) -> Router {
    Router::new()
        .route("/api/v2/tasks", get(list_tasks))
        .route("/api/v2/activity", get(views::activity))
        .route("/api/v2/targets", get(list_targets))
        .route(
            "/api/v2/targets/{owner}/{repo}/configuration",
            get(views::target_configuration),
        )
        .route("/api/v2/operator", get(views::operator))
        .route(
            "/api/v2/targets/{owner}/{repo}/historical",
            get(views::historical),
        )
        .route("/api/v2/targets/{owner}/{repo}/index", get(get_index))
        .route("/api/v2/me/sponsorships", get(my_sponsorships))
        .route("/api/v2/events", get(events))
        .route("/api/v2/tasks/{id}", get(get_task))
        .route("/api/v2/issues/{owner}/{repo}/{number}", get(get_issue))
        .route(
            "/api/v2/imports/{deployment}/turns/{source}",
            get(get_imported_turn),
        )
        .route(
            "/api/v2/imports/{deployment}/turns/{source}/transcript",
            get(get_imported_transcript),
        )
        .route(
            "/api/v2/tasks/{task}/turns/{turn}/transcript",
            get(get_transcript),
        )
        .route(
            "/api/v2/tasks/{task}/turns/{turn}/artifacts/{digest}",
            get(get_artifact),
        )
        .with_state(state)
}

/// Read the latest task-owned target identities without holding a lock during I/O.
fn targets(state: &ApiState) -> Result<Vec<Target>, ApiError> {
    state
        .targets
        .read()
        .map(|targets| targets.clone())
        .map_err(|_| ApiError(StatusCode::SERVICE_UNAVAILABLE, "target_unavailable"))
}

/// List targets for which the signed-in viewer has current writer authority.
async fn list_targets(
    State(state): State<ApiState>,
    headers: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    let mut visible = Vec::new();
    for target in targets(&state)? {
        let viewer = tier(&state, &headers, &target, &target.issue_repository, 0).await?;
        if viewer.tier == "writer" {
            visible.push(json!({"upstream":target.upstream,
                "issue_repository":target.issue_repository,"fork":target.fork}));
        }
    }
    Ok(Json(json!({"targets":visible})))
}

/// Filter durable target rows through a fresh session entitlement check.
async fn get_index(
    State(state): State<ApiState>,
    Path((owner, repo)): Path<(String, String)>,
    Query(query): Query<IndexQuery>,
    headers: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    let upstream = format!("{owner}/{repo}");
    let target = targets(&state)?
        .into_iter()
        .find(|target| target.upstream.eq_ignore_ascii_case(&upstream))
        .ok_or(ApiError(StatusCode::NOT_FOUND, "target_not_found"))?;
    let repository = match query.kind.as_str() {
        "issue" => &target.issue_repository,
        "pull" => &target.upstream,
        "run" => &target.fork,
        _ => return Err(ApiError(StatusCode::BAD_REQUEST, "index_kind_invalid")),
    };
    let writer = tier(&state, &headers, &target, &target.issue_repository, 0).await?;
    if query.kind == "run" && writer.tier != "writer" {
        return Err(ApiError(StatusCode::FORBIDDEN, "writer_required"));
    }
    let mut request = state
        .client
        .get(format!(
            "{}/v1/index",
            state.task_api_url.trim_end_matches('/')
        ))
        .bearer_auth(&state.task_read_bearer)
        .query(&[
            ("repository", repository.as_str()),
            ("kind", query.kind.as_str()),
        ]);
    if let Some(cursor) = query.cursor.as_deref() {
        request = request.query(&[("cursor", cursor)]);
    }
    let page: Value = request
        .send()
        .await
        .map_err(unavailable)?
        .error_for_status()
        .map_err(unavailable)?
        .json()
        .await
        .map_err(unavailable)?;
    let mut rows = Vec::new();
    for row in page["rows"]
        .as_array()
        .ok_or(ApiError(StatusCode::BAD_GATEWAY, "index_invalid"))?
    {
        if query.kind == "run" {
            rows.push(row.clone());
            continue;
        }
        let number = row["document"]["number"]
            .as_i64()
            .ok_or(ApiError(StatusCode::BAD_GATEWAY, "index_invalid"))?;
        let viewer = tier(&state, &headers, &target, repository, number).await?;
        if viewer.tier != "none" {
            rows.push(row.clone());
        }
    }
    Ok(Json(json!({"repository":repository,"kind":query.kind,
        "rows":rows,"next_cursor":page["next_cursor"],"cache":page["cache"]})))
}

/// Return current model revision only after a baseline has settled.
async fn ready_revision(state: &ApiState) -> Result<i64, ApiError> {
    let (ready, revision) = storage::model_status(&state.database)
        .await
        .map_err(unavailable)?;
    if !ready {
        return Err(ApiError(StatusCode::SERVICE_UNAVAILABLE, "model_loading"));
    }
    Ok(revision)
}

/// Resolve a model row's configured target and issue identity.
fn task_source(state: &ApiState, body: &Value) -> Option<(Target, i64, String)> {
    let repository_id = body["repository_id"].as_i64()?;
    let number = body["issue_number"]
        .as_i64()
        .or_else(|| body["pr_number"].as_i64())?;
    let standalone = body.get("pr_number").is_some();
    let target = state
        .targets
        .read()
        .ok()?
        .iter()
        .find(|target| {
            if standalone {
                target
                    .upstream_repository_id
                    .unwrap_or(target.repository_id)
                    == repository_id
            } else {
                target.repository_id == repository_id
            }
        })?
        .clone();
    let repository = if body.get("pr_number").is_some() {
        target.upstream.clone()
    } else {
        target.issue_repository.clone()
    };
    Some((target, number, repository))
}

/// Ask the Python authority for this request's issue-specific tier.
async fn tier(
    state: &ApiState,
    headers: &HeaderMap,
    target: &Target,
    repository: &str,
    number: i64,
) -> Result<auth::Viewer, ApiError> {
    let token =
        auth::viewer_token(headers).ok_or(ApiError(StatusCode::UNAUTHORIZED, "viewer_required"))?;
    auth::viewer_for_issue(
        &state.client,
        &state.authority_url,
        &state.authority_bearer,
        token,
        &target.upstream,
        repository,
        number,
    )
    .await
    .map_err(unavailable)?
    .ok_or(ApiError(StatusCode::UNAUTHORIZED, "viewer_expired"))
}

/// Resolve the session identity without granting access to any issue data.
async fn session_viewer(state: &ApiState, headers: &HeaderMap) -> Result<auth::Viewer, ApiError> {
    let token =
        auth::viewer_token(headers).ok_or(ApiError(StatusCode::UNAUTHORIZED, "viewer_required"))?;
    let target = targets(state)?.into_iter().next().ok_or(ApiError(
        StatusCode::SERVICE_UNAVAILABLE,
        "target_unavailable",
    ))?;
    auth::viewer_for_issue(
        &state.client,
        &state.authority_url,
        &state.authority_bearer,
        token,
        &target.upstream,
        &target.issue_repository,
        0,
    )
    .await
    .map_err(unavailable)?
    .ok_or(ApiError(StatusCode::UNAUTHORIZED, "viewer_expired"))
}

/// Render only fields available to a reader of the issue.
fn summary(entity: &str, body: &Value, revision: i64, repository: &str) -> Value {
    let state = body.get("snapshot").unwrap_or(&body["state"]);
    let standalone = entity == "standalone_review";
    let source_stage = if standalone {
        &body["stage"]
    } else {
        &state["stage"]
    };
    let stage = match source_stage.as_str().unwrap_or("") {
        "Initializing" | "initializing" => "intake",
        "Intake" => "intake",
        "Plan" => "plan",
        "PlanQc" => "plan_qc",
        "Planned" => "planned",
        "Implement" => "implement",
        "Publish" => "publish",
        "Review" => "review",
        "Gates" => "gates",
        "Ready" => "ready",
        "ObservedMerged" => "observed_merged",
        "Closed" => "closed",
        "Clarify" => "clarify",
        "Rejected" => "rejected",
        "Escalated" => "escalated",
        "Blocked" => "blocked",
        "Stopped" => "stopped",
        other => other,
    };
    json!({
        "id":body["task_id"],
        "kind":if standalone { "standalone_pr_review" } else { "contribution" },
        "repository_id":body["repository_id"],
        "repository":repository,
        "issue_number":if standalone { &body["pr_number"] } else { &body["issue_number"] },
        "stage":stage,
        "title":if standalone { body["snapshot"]["title"].clone() }
            else { state["title"].clone() },
        "updated_at":if standalone { body["snapshot"]["requested_at"].clone() }
            else { state["updated_at"].clone() },
        "author":if standalone { body["snapshot"]["author"].clone() }
            else { state["author"]["login"].clone() },
        "plan_id":if standalone { Value::Null } else { state["plan"]["id"].clone() },
        "reason":if standalone { &body["round_status"] } else { &state["stage_reason"] },
        "mode":if standalone { "manual" } else { state["mode"].as_str().unwrap_or("manual") },
        "pull_request":if standalone { json!(format!("https://github.com/{}/pull/{}",
            body["upstream"].as_str().unwrap_or(""),body["pr_number"].as_i64().unwrap_or(0))) }
            else { state["pull_request"]["url"].clone() },
        "revision":revision,
    })
}

/// Notify the browser when the durable read model advances, without exposing facts.
async fn events(
    State(state): State<ApiState>,
    headers: HeaderMap,
) -> Result<Sse<impl futures_util::Stream<Item = Result<Event, Infallible>>>, ApiError> {
    let token = auth::viewer_token(&headers)
        .ok_or(ApiError(StatusCode::UNAUTHORIZED, "viewer_required"))?
        .to_owned();
    session_viewer(&state, &headers).await?;
    let revision = ready_revision(&state).await?;
    let updates = stream::unfold(
        (state, token, revision),
        |(state, token, last)| async move {
            tokio::time::sleep(Duration::from_secs(5)).await;
            let target = targets(&state).ok()?.into_iter().next()?;
            match auth::viewer_for_issue(
                &state.client,
                &state.authority_url,
                &state.authority_bearer,
                &token,
                &target.upstream,
                &target.issue_repository,
                0,
            )
            .await
            {
                Ok(Some(_)) => {}
                _ => return None,
            }
            let revision = ready_revision(&state).await.ok()?;
            let event = Event::default()
                .event("change")
                .data(json!({"type":"reload","entity":null,"revision":revision}).to_string());
            if revision == last {
                return Some((
                    Ok(Event::default().comment("unchanged")),
                    (state, token, last),
                ));
            }
            Some((Ok(event), (state, token, revision)))
        },
    );
    Ok(Sse::new(updates).keep_alive(KeepAlive::default()))
}

/// Page model tasks and omit every issue the viewer cannot read.
async fn list_tasks(
    State(state): State<ApiState>,
    Query(query): Query<TaskQuery>,
    headers: HeaderMap,
) -> Result<Json<TaskPage>, ApiError> {
    let revision = ready_revision(&state).await?;
    let token = auth::viewer_token(&headers)
        .ok_or(ApiError(StatusCode::UNAUTHORIZED, "viewer_required"))?;
    let rows = storage::task_rows(&state.database)
        .await
        .map_err(unavailable)?;
    let mut tasks = Vec::new();
    let mut next_cursor = None;
    let mut after = query.cursor.is_none();
    for storage::ModelRow {
        entity,
        entity_id: id,
        revision: source_revision,
        body,
    } in rows
    {
        let cursor = format!("{entity}:{id}");
        if !after {
            after = query
                .cursor
                .as_deref()
                .is_some_and(|last| cursor.as_str() > last);
        }
        if !after {
            continue;
        }
        let Some((target, number, repository)) = task_source(&state, &body) else {
            continue;
        };
        if query
            .target
            .as_deref()
            .is_some_and(|filter| !target.upstream.eq_ignore_ascii_case(filter))
            || query.kind.as_deref().is_some_and(|filter| {
                !matches!(
                    (filter, entity.as_str()),
                    ("contribution", "contribution_task")
                        | ("standalone_pr_review", "standalone_review")
                )
            })
        {
            continue;
        }
        let item = summary(&entity, &body, source_revision, &repository);
        if query
            .stage
            .as_deref()
            .is_some_and(|filter| item["stage"] != filter)
        {
            continue;
        }
        let viewer = auth::viewer_for_issue(
            &state.client,
            &state.authority_url,
            &state.authority_bearer,
            token,
            &target.upstream,
            &repository,
            number,
        )
        .await
        .map_err(unavailable)?
        .ok_or(ApiError(StatusCode::UNAUTHORIZED, "viewer_expired"))?;
        if viewer.tier == "none" {
            continue;
        }
        if tasks.len() == 100 {
            next_cursor = tasks
                .last()
                .and_then(|task: &Value| task["cursor"].as_str().map(str::to_owned));
            break;
        }
        let mut item = item;
        if viewer.tier == "writer" && entity == "contribution_task" {
            item["consent"] = json!(
                storage::issue_consent(&state.database, target.repository_id, number)
                    .await
                    .map_err(unavailable)?
                    .into_iter()
                    .map(|(entity, body)| json!({"entity":entity,"body":body}))
                    .collect::<Vec<_>>()
            );
        }
        item["cursor"] = json!(cursor);
        tasks.push(item);
    }
    Ok(Json(TaskPage {
        tasks,
        next_cursor,
        revision,
    }))
}

/// Return the viewer's own retained consent history on still-visible issues.
async fn my_sponsorships(
    State(state): State<ApiState>,
    headers: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    let revision = ready_revision(&state).await?;
    let viewer = session_viewer(&state, &headers).await?;
    let token = auth::viewer_token(&headers)
        .ok_or(ApiError(StatusCode::UNAUTHORIZED, "viewer_required"))?;
    let rows = storage::sponsorship_rows(&state.database)
        .await
        .map_err(unavailable)?;
    let mut facts = Vec::new();
    let configured = targets(&state)?;
    for (entity, body) in rows {
        let own_slot = entity == "consent_state"
            && body["slots"].as_object().is_some_and(|slots| {
                slots.values().any(|slot| {
                    slot["sponsor_id"].as_str() == Some(viewer.user.id.to_string().as_str())
                })
            });
        if !own_slot && body["fact"]["actor"]["id"] != viewer.user.id {
            continue;
        }
        let Some(repository_id) = body["repository_id"].as_i64() else {
            continue;
        };
        let Some(target) = configured.iter().find(|target| {
            target.repository_id == repository_id
                || target.upstream_repository_id == Some(repository_id)
        }) else {
            continue;
        };
        let standalone = entity.starts_with("standalone_funding")
            || (entity == "consent_state" && body["kind"] == "standalone_pr_review");
        let number = if standalone {
            body["pr_number"].as_i64()
        } else {
            body["issue_number"].as_i64()
        };
        let Some(number) = number else { continue };
        let repository = if standalone {
            &target.upstream
        } else {
            &target.issue_repository
        };
        let allowed = auth::viewer_for_issue(
            &state.client,
            &state.authority_url,
            &state.authority_bearer,
            token,
            &target.upstream,
            repository,
            number,
        )
        .await
        .map_err(unavailable)?
        .ok_or(ApiError(StatusCode::UNAUTHORIZED, "viewer_expired"))?;
        if allowed.tier != "none" {
            let body = if entity == "consent_state" && allowed.tier != "writer" {
                let slots = body["slots"]
                    .as_object()
                    .cloned()
                    .unwrap_or_default()
                    .into_iter()
                    .filter(|(_, slot)| {
                        slot["sponsor_id"].as_str() == Some(viewer.user.id.to_string().as_str())
                    })
                    .collect::<serde_json::Map<_, _>>();
                json!({"repository_id":repository_id,"issue_number":body["issue_number"],
                    "pr_number":body["pr_number"],"slots":slots})
            } else {
                body
            };
            facts.push(json!({"entity":entity,"repository":repository,
                "number":number,"body":body}));
        }
    }
    let billing: Value = state
        .client
        .get(format!(
            "{}/v1/billing/{}",
            state.task_api_url.trim_end_matches('/'),
            viewer.user.id
        ))
        .bearer_auth(&state.task_read_bearer)
        .send()
        .await
        .map_err(unavailable)?
        .error_for_status()
        .map_err(unavailable)?
        .json()
        .await
        .map_err(unavailable)?;
    let mut charged_calls = Vec::new();
    let mut unavailable_sources = Vec::new();
    for (field, output) in [
        ("rows", &mut charged_calls),
        ("unavailable", &mut unavailable_sources),
    ] {
        for charge in billing[field].as_array().map(Vec::as_slice).unwrap_or(&[]) {
            let Some(repository) = charge["repository"].as_str() else {
                continue;
            };
            let Some(target) = configured.iter().find(|target| {
                [target.upstream.as_str(), target.issue_repository.as_str()]
                    .iter()
                    .any(|name| name.eq_ignore_ascii_case(repository))
            }) else {
                continue;
            };
            let Some(number) = charge["number"].as_i64() else {
                continue;
            };
            if tier(&state, &headers, target, repository, number)
                .await?
                .tier
                != "none"
            {
                output.push(charge.clone());
            }
        }
    }
    Ok(Json(
        json!({"facts":facts,"charged_calls":charged_calls,"unavailable":unavailable_sources,"revision":revision}),
    ))
}

/// Resolve a task by stable identity and enforce its current viewer tier.
async fn get_task(
    State(state): State<ApiState>,
    Path(id): Path<Uuid>,
    headers: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    let revision = ready_revision(&state).await?;
    let Some(storage::ModelRow {
        entity,
        revision: source_revision,
        body,
        ..
    }) = storage::task_row(&state.database, &id.to_string())
        .await
        .map_err(unavailable)?
    else {
        return Err(ApiError(StatusCode::NOT_FOUND, "task_not_found"));
    };
    let (target, number, repository) =
        task_source(&state, &body).ok_or(ApiError(StatusCode::NOT_FOUND, "task_not_found"))?;
    let viewer = tier(&state, &headers, &target, &repository, number).await?;
    if viewer.tier == "none" {
        return Err(ApiError(StatusCode::NOT_FOUND, "task_not_found"));
    }
    let summary = summary(&entity, &body, source_revision, &repository);
    let rows = storage::task_records(
        &state.database,
        &id.to_string(),
        body["repository_id"].as_i64().ok_or(ApiError(
            StatusCode::SERVICE_UNAVAILABLE,
            "task_source_invalid",
        ))?,
        number,
        &repository,
    )
    .await
    .map_err(unavailable)?;
    let own_consent = rows
        .iter()
        .filter(|(kind, record)| {
            matches!(
                kind.as_str(),
                "consent" | "consent_removal" | "standalone_funding" | "standalone_funding_removal"
            ) && record["fact"]["actor"]["id"] == viewer.user.id
        })
        .map(|(_, record)| record.clone())
        .chain(
            rows.iter()
                .filter(|(kind, _)| kind == "consent_state")
                .map(|(_, record)| {
                    let own = record["slots"]
                        .as_object()
                        .cloned()
                        .unwrap_or_default()
                        .into_iter()
                        .filter(|(_, slot)| {
                            slot["sponsor_id"].as_str() == Some(viewer.user.id.to_string().as_str())
                        })
                        .collect::<serde_json::Map<_, _>>();
                    json!({"task_id":record["task_id"],"slots":own})
                }),
        )
        .collect::<Vec<_>>();
    if viewer.tier == "reader" {
        return Ok(Json(json!({"task":summary,"own_consent":own_consent,
            "viewer":{"id":viewer.user.id,"login":viewer.user.login,
                "tier":viewer.tier,"checked_at":viewer.checked_at},
            "revision":revision})));
    }
    let history: Value = state
        .client
        .get(format!(
            "{}/v1/history",
            state.task_api_url.trim_end_matches('/')
        ))
        .bearer_auth(&state.task_read_bearer)
        .query(&[
            ("repository_id", body["repository_id"].to_string()),
            ("number", number.to_string()),
        ])
        .send()
        .await
        .map_err(unavailable)?
        .error_for_status()
        .map_err(unavailable)?
        .json()
        .await
        .map_err(unavailable)?;
    Ok(Json(json!({"task":summary,"records":rows.into_iter()
        .map(|(entity,body)| json!({"entity":entity,"body":body}))
        .collect::<Vec<_>>(),"viewer":{"id":viewer.user.id,"login":viewer.user.login,
            "tier":viewer.tier,"checked_at":viewer.checked_at},"revision":revision,
        "history":history})))
}

/// Resolve an issue-addressed page without relying on a task ID in the URL.
async fn get_issue(
    State(state): State<ApiState>,
    Path((owner, repo, number)): Path<(String, String, i64)>,
    headers: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    ready_revision(&state).await?;
    let repository = format!("{owner}/{repo}");
    let target = targets(&state)?
        .into_iter()
        .find(|target| target.issue_repository.eq_ignore_ascii_case(&repository))
        .ok_or(ApiError(StatusCode::NOT_FOUND, "issue_not_found"))?;
    let viewer = tier(&state, &headers, &target, &repository, number).await?;
    if viewer.tier == "none" {
        return Err(ApiError(StatusCode::NOT_FOUND, "issue_not_found"));
    }
    let task = storage::issue_task(&state.database, target.repository_id, number)
        .await
        .map_err(unavailable)?
        .map(|row| summary(&row.entity, &row.body, row.revision, &repository));
    let item_id = number.to_string();
    let index_response = state
        .client
        .get(format!(
            "{}/v1/index/item",
            state.task_api_url.trim_end_matches('/')
        ))
        .bearer_auth(&state.task_read_bearer)
        .query(&[
            ("repository", repository.as_str()),
            ("kind", "issue"),
            ("item_id", item_id.as_str()),
        ])
        .send()
        .await
        .map_err(unavailable)?;
    let document = if index_response.status() == StatusCode::NOT_FOUND {
        None
    } else {
        Some(
            index_response
                .error_for_status()
                .map_err(unavailable)?
                .json::<Value>()
                .await
                .map_err(unavailable)?["document"]
                .clone(),
        )
    };
    let consent_rows = storage::issue_consent(&state.database, target.repository_id, number)
        .await
        .map_err(unavailable)?;
    let consent = consent_rows
        .into_iter()
        .filter(|(_, body)| {
            viewer.tier == "writer"
                || body["fact"]["actor"]["id"] == viewer.user.id
                || body["slots"].as_object().is_some_and(|slots| {
                    slots.values().any(|slot| {
                        slot["sponsor_id"].as_str() == Some(viewer.user.id.to_string().as_str())
                    })
                })
        })
        .map(|(entity, body)| {
            let body = if entity == "consent_state" && viewer.tier != "writer" {
                let slots = body["slots"]
                    .as_object()
                    .cloned()
                    .unwrap_or_default()
                    .into_iter()
                    .filter(|(_, slot)| {
                        slot["sponsor_id"].as_str() == Some(viewer.user.id.to_string().as_str())
                    })
                    .collect::<serde_json::Map<_, _>>();
                json!({"repository_id":body["repository_id"],
                    "issue_number":body["issue_number"],"slots":slots})
            } else {
                body
            };
            json!({"entity":entity,"body":body})
        })
        .collect::<Vec<_>>();
    let historical_turns = if viewer.tier == "writer" {
        let rows = storage::imported_turns(&state.database, &repository, number)
            .await
            .map_err(unavailable)?;
        rows.into_iter()
            .map(|body| {
                json!({
                    "deployment":body["deployment"],"source_id":body["source_id"],
                    "stage":body["stage"],"status":body["status"]
                })
            })
            .collect::<Vec<_>>()
    } else {
        Vec::new()
    };
    Ok(Json(
        json!({"issue":{"repository":repository,"number":number,
                "document":document},"task":task,
            "consent":consent,"historical_turns":historical_turns,
            "viewer":{"tier":viewer.tier,"id":viewer.user.id,"login":viewer.user.login}}),
    ))
}

/// Enforce current writer authority for one imported Python turn.
async fn writer_for_import(
    state: &ApiState,
    headers: &HeaderMap,
    deployment: &str,
    source: &str,
) -> Result<Value, ApiError> {
    ready_revision(state).await?;
    let Some(body) = storage::imported_turn(&state.database, &format!("{deployment}:{source}"))
        .await
        .map_err(unavailable)?
    else {
        return Err(ApiError(StatusCode::NOT_FOUND, "turn_not_found"));
    };
    let repository = body["repository"].as_str().ok_or(ApiError(
        StatusCode::SERVICE_UNAVAILABLE,
        "turn_source_invalid",
    ))?;
    let target = targets(state)?
        .into_iter()
        .find(|target| {
            target.issue_repository.eq_ignore_ascii_case(repository)
                || target.upstream.eq_ignore_ascii_case(repository)
        })
        .ok_or(ApiError(StatusCode::NOT_FOUND, "turn_not_found"))?;
    let number = body["issue_number"].as_i64().ok_or(ApiError(
        StatusCode::SERVICE_UNAVAILABLE,
        "turn_source_invalid",
    ))?;
    let viewer = tier(state, headers, &target, repository, number).await?;
    if viewer.tier != "writer" {
        return Err(ApiError(StatusCode::NOT_FOUND, "turn_not_found"));
    }
    Ok(body)
}

/// Show an imported turn's original source metadata only to a current writer.
async fn get_imported_turn(
    State(state): State<ApiState>,
    Path((deployment, source)): Path<(String, String)>,
    headers: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    let body = writer_for_import(&state, &headers, &deployment, &source).await?;
    Ok(Json(body))
}

/// Proxy an imported turn's original transcript after a fresh writer check.
async fn get_imported_transcript(
    State(state): State<ApiState>,
    Path((deployment, source)): Path<(String, String)>,
    Query(query): Query<TranscriptQuery>,
    headers: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    writer_for_import(&state, &headers, &deployment, &source).await?;
    let mut request = state
        .client
        .get(format!(
            "{}/v1/imports/{deployment}/turns/{source}/transcript",
            state.task_api_url.trim_end_matches('/')
        ))
        .bearer_auth(&state.task_read_bearer);
    if let Some(cursor) = query.cursor {
        request = request.query(&[("cursor", cursor)]);
    }
    let response = request
        .send()
        .await
        .map_err(unavailable)?
        .error_for_status()
        .map_err(unavailable)?;
    Ok(Json(response.json().await.map_err(unavailable)?))
}

/// Verify task and turn ownership before proxying a transcript page.
async fn get_transcript(
    State(state): State<ApiState>,
    Path((task, turn)): Path<(Uuid, Uuid)>,
    Query(query): Query<TranscriptQuery>,
    headers: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    writer_for_turn(&state, &headers, task, turn).await?;
    let mut request = state
        .client
        .get(format!(
            "{}/v1/turns/{turn}/transcript",
            state.task_api_url.trim_end_matches('/')
        ))
        .bearer_auth(&state.task_read_bearer);
    if let Some(cursor) = query.cursor {
        request = request.query(&[("cursor", cursor)]);
    }
    let response = request
        .send()
        .await
        .map_err(unavailable)?
        .error_for_status()
        .map_err(unavailable)?;
    Ok(Json(response.json().await.map_err(unavailable)?))
}

/// Verify task and turn ownership before proxying an original artifact.
async fn get_artifact(
    State(state): State<ApiState>,
    Path((task, turn, digest)): Path<(Uuid, Uuid, String)>,
    headers: HeaderMap,
) -> Result<Response, ApiError> {
    writer_for_turn(&state, &headers, task, turn).await?;
    let response = state
        .client
        .get(format!(
            "{}/v1/turns/{turn}/artifacts/{digest}",
            state.task_api_url.trim_end_matches('/')
        ))
        .bearer_auth(&state.task_read_bearer)
        .send()
        .await
        .map_err(unavailable)?
        .error_for_status()
        .map_err(unavailable)?;
    let content_type = response.headers().get(header::CONTENT_TYPE).cloned();
    let bytes = response.bytes().await.map_err(unavailable)?;
    let mut result = Response::new(Body::from(bytes));
    if let Some(value) = content_type {
        result.headers_mut().insert(header::CONTENT_TYPE, value);
    }
    Ok(result)
}

/// A writer's turn must belong to the requested task at every diagnostic read.
async fn writer_for_turn(
    state: &ApiState,
    headers: &HeaderMap,
    task: Uuid,
    turn: Uuid,
) -> Result<(), ApiError> {
    ready_revision(state).await?;
    let Some(storage::ModelRow { body, .. }) =
        storage::task_row(&state.database, &task.to_string())
            .await
            .map_err(unavailable)?
    else {
        return Err(ApiError(StatusCode::NOT_FOUND, "task_not_found"));
    };
    let (target, number, repository) =
        task_source(state, &body).ok_or(ApiError(StatusCode::NOT_FOUND, "task_not_found"))?;
    let viewer = tier(state, headers, &target, &repository, number).await?;
    if viewer.tier != "writer" {
        return Err(ApiError(StatusCode::NOT_FOUND, "turn_not_found"));
    }
    let owned = storage::turn_owned(&state.database, &task.to_string(), &turn.to_string())
        .await
        .map_err(unavailable)?;
    if !owned {
        return Err(ApiError(StatusCode::NOT_FOUND, "turn_not_found"));
    }
    Ok(())
}

/// Keep storage and authority details out of HTTP error bodies.
fn unavailable(_error: impl std::fmt::Display) -> ApiError {
    ApiError(StatusCode::SERVICE_UNAVAILABLE, "read_unavailable")
}

#[cfg(test)]
#[path = "api_tests.rs"]
mod tests;

#[cfg(test)]
#[path = "api_views_tests.rs"]
mod views_tests;

#[cfg(test)]
#[path = "api_historical_tests.rs"]
mod historical_tests;
