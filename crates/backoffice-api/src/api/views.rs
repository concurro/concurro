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
