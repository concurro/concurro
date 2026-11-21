//! Read-only viewer decisions from the separate accounts session authority.

use anyhow::{Context, Result};
use axum::http::{HeaderMap, header};
use reqwest::Client;
use serde::Deserialize;

/// Stable signed-in user identity from the session authority.
#[derive(Clone, Deserialize)]
pub struct ViewerUser {
    /// Numeric GitHub account ID.
    pub id: i64,
    /// Current login for display.
    pub login: String,
}

/// Issue-specific viewer entitlement checked through the user's own token.
#[derive(Clone, Deserialize)]
pub struct Viewer {
    /// Stable signed-in identity.
    pub user: ViewerUser,
    /// `writer`, `reader`, or `none`.
    pub tier: String,
    /// Authority's check time.
    pub checked_at: String,
}

/// Read only the viewer cookie or edge-supplied viewer header.
pub fn viewer_token(headers: &HeaderMap) -> Option<&str> {
    if let Some(token) = headers
        .get("x-concurro-viewer")
        .and_then(|value| value.to_str().ok())
    {
        return Some(token);
    }
    headers
        .get(header::COOKIE)
        .and_then(|value| value.to_str().ok())?
        .split(';')
        .find_map(|part| part.trim().strip_prefix("concurro_viewer="))
}

/// Ask the separate session listener for a fresh issue-specific tier.
pub async fn viewer_for_issue(
    client: &Client,
    authority_url: &str,
    service_bearer: &str,
    token: &str,
    upstream: &str,
    issue_repository: &str,
    issue_number: i64,
) -> Result<Option<Viewer>> {
    let response = client
        .get(format!(
            "{}/internal/v1/viewer",
            authority_url.trim_end_matches('/')
        ))
        .bearer_auth(service_bearer)
        .header("x-concurro-viewer", token)
        .query(&[
            ("upstream", upstream),
            ("issue_repository", issue_repository),
            ("issue_number", &issue_number.to_string()),
        ])
        .send()
        .await
        .context("viewer authority unavailable")?;
    if response.status() == reqwest::StatusCode::UNAUTHORIZED {
        return Ok(None);
    }
    let response = response.error_for_status()?;
    Ok(Some(response.json().await?))
}
