//! Current target settings and viewer-scoped operational projection reads.

use std::collections::BTreeMap;

use chrono::Utc;
use reqwest::Url;

use super::*;

/// Proxy the task service's allowlisted settings only after fresh writer authorization.
pub async fn target_configuration(
    State(state): State<ApiState>,
    Path((owner, repo)): Path<(String, String)>,
    headers: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    let upstream = format!("{owner}/{repo}");
    let target = targets(&state)?
        .into_iter()
        .find(|target| target.upstream.eq_ignore_ascii_case(&upstream))
        .ok_or(ApiError(StatusCode::NOT_FOUND, "target_not_found"))?;
    if tier(&state, &headers, &target, &target.issue_repository, 0)
        .await?
        .tier
        != "writer"
    {
        return Err(ApiError(StatusCode::FORBIDDEN, "writer_required"));
    }
    let (owner, repo) = target.upstream.split_once('/').ok_or(ApiError(
        StatusCode::SERVICE_UNAVAILABLE,
        "target_unavailable",
    ))?;
    let mut url = Url::parse(&state.task_api_url).map_err(unavailable)?;
    url.path_segments_mut()
        .map_err(|_| ApiError(StatusCode::SERVICE_UNAVAILABLE, "target_unavailable"))?
        .pop_if_empty()
        .extend(["v1", "targets", owner, repo, "configuration"]);
    let result = state
        .client
        .get(url)
        .bearer_auth(&state.task_read_bearer)
        .send()
        .await
        .map_err(unavailable)?
        .error_for_status()
        .map_err(unavailable)?
        .json()
        .await
        .map_err(unavailable)?;
    Ok(Json(result))
}

/// Expose generic projection health and only issue-specific writer task operations.
pub async fn operator(
    State(state): State<ApiState>,
    headers: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    let mut writer = false;
    for target in targets(&state)? {
        if tier(&state, &headers, &target, &target.issue_repository, 0)
            .await?
            .tier
            == "writer"
        {
            writer = true;
        }
    }
    if !writer {
        return Err(ApiError(StatusCode::FORBIDDEN, "writer_required"));
    }
    let projection = storage::projection_health(&state.database)
        .await
        .map_err(unavailable)?;
    let mut tasks = Vec::new();
    let mut stages: BTreeMap<String, usize> = BTreeMap::new();
    let mut turns: BTreeMap<String, i64> = BTreeMap::new();
    if projection.ready {
        for row in storage::operational_tasks(&state.database)
            .await
            .map_err(unavailable)?
        {
            let Some((target, number, repository)) = task_source(&state, &row.task.body) else {
                continue;
            };
            if tier(&state, &headers, &target, &repository, number)
                .await?
                .tier
                != "writer"
            {
                continue;
            }
            let mut task = summary(
                &row.task.entity,
                &row.task.body,
                row.task.revision,
                &repository,
            );
            task["id"] = json!(row.task.entity_id);
            *stages
                .entry(task["stage"].as_str().unwrap_or("unknown").to_owned())
                .or_default() += 1;
            for (status, count) in &row.turns {
                *turns.entry(status.clone()).or_default() += count;
            }
            task["turns"] = json!(row.turns);
            tasks.push(task);
        }
    }
    Ok(Json(json!({"kind":"live","observed_at":Utc::now(),
        "projection":projection,"tasks":if projection.ready { json!(tasks) } else { Value::Null },
        "stages":if projection.ready { json!(stages) } else { Value::Null },
        "turns":if projection.ready { json!(turns) } else { Value::Null }})))
}

/// Return legacy evidence only to current writers of its exact source issue or PR.
pub async fn historical(
    State(state): State<ApiState>,
    Path((owner, repo)): Path<(String, String)>,
    Query(query): Query<IndexQuery>,
    headers: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    let repository = format!("{owner}/{repo}");
    let target = targets(&state)?
        .into_iter()
        .find(|target| {
            [target.upstream.as_str(), target.issue_repository.as_str()]
                .iter()
                .any(|name| name.eq_ignore_ascii_case(&repository))
        })
        .ok_or(ApiError(StatusCode::NOT_FOUND, "target_not_found"))?;
    if !matches!(query.kind.as_str(), "contribution" | "review") {
        return Err(ApiError(StatusCode::BAD_REQUEST, "historical_kind_invalid"));
    }
    let source = if query.kind == "review" {
        &target.upstream
    } else {
        &target.issue_repository
    };
    if tier(&state, &headers, &target, source, 0).await?.tier != "writer" {
        return Err(ApiError(StatusCode::FORBIDDEN, "writer_required"));
    }
    let mut request = state
        .client
        .get(format!(
            "{}/v1/historical",
            state.task_api_url.trim_end_matches('/')
        ))
        .bearer_auth(&state.task_read_bearer)
        .query(&[
            ("repository", source.as_str()),
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
        .ok_or(ApiError(StatusCode::BAD_GATEWAY, "historical_invalid"))?
    {
        let number = row["record"]["number"]
            .as_i64()
            .ok_or(ApiError(StatusCode::BAD_GATEWAY, "historical_invalid"))?;
        if tier(&state, &headers, &target, source, number).await?.tier == "writer" {
            rows.push(row.clone());
        }
    }
    Ok(Json(json!({"rows":rows,"next_cursor":page["next_cursor"]})))
}

/// Read recorded turn activity only for issues the current viewer may administer.
pub async fn activity(
    State(state): State<ApiState>,
    headers: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    ready_revision(&state).await?;
    session_viewer(&state, &headers).await?;
    let mut events = Vec::new();
    for mut row in storage::activity_turns(&state.database)
        .await
        .map_err(unavailable)?
    {
        let Some((target, number, repository)) = task_source(&state, &row["source"]) else {
            continue;
        };
        if tier(&state, &headers, &target, &repository, number)
            .await?
            .tier
            != "writer"
        {
            continue;
        }
        row.as_object_mut()
            .expect("activity object")
            .remove("source");
        row["repository"] = json!(repository);
        row["issue"] = json!(number);
        events.push(row);
        if events.len() == 30 {
            break;
        }
    }
    Ok(Json(json!({"events":events})))
}
