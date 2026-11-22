//! Session-token visibility and Backoffice App writer checks.

use std::collections::BTreeMap;

use concurro_contracts::ConfigTarget;
use concurro_github_v1::{AppClient, response_status};
use reqwest::StatusCode;
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use serde_json::Value;

/// Confirmed account fields returned by GitHub's `/user` endpoint.
#[derive(Clone, Deserialize, Serialize)]
pub struct User {
    /// Stable GitHub account ID.
    pub id: i64,
    /// GitHub login used for writer permission checks.
    pub login: String,
    /// Display name, if provided.
    pub name: Option<String>,
    /// Public avatar URL, if provided.
    pub avatar_url: Option<String>,
}

/// A GitHub read could not establish authority.
pub enum AccessError {
    /// GitHub refused the signed-in user's token.
    Revoked,
    /// GitHub explicitly hid the requested resource.
    Hidden,
    /// A dependency failed without proving a negative permission.
    Unavailable,
}

/// Separate user-bearer and read-only App operations over one API client.
#[derive(Clone)]
pub struct Access {
    app: AppClient,
    read_permissions: BTreeMap<String, String>,
}

impl Access {
    /// Keep App token permissions read-only.
    pub fn new(app: AppClient) -> Self {
        Self {
            app,
            read_permissions: BTreeMap::from([("contents".to_owned(), "read".to_owned())]),
        }
    }

    /// Identify the new OAuth bearer through GitHub, without browser claims.
    pub async fn user(&self, token: &str) -> Result<User, AccessError> {
        self.user_read(token, &["user"]).await
    }

    /// Decide one issue-specific tier from user visibility and App permission.
    pub async fn tier(
        &self,
        token: &str,
        user: &User,
        target: &ConfigTarget,
        issue_repository: &str,
        issue_number: i64,
    ) -> Result<&'static str, AccessError> {
        let upstream = self.repository_visible(token, &target.upstream).await?;
        let fork = self.repository_visible(token, &target.fork).await?;
        if upstream
            && fork
            && self
                .app
                .writer_permission(&target.upstream, &user.login, &self.read_permissions)
                .await
                .map_err(|_| AccessError::Unavailable)?
        {
            return Ok("writer");
        }
        let (owner, repo) = issue_repository
            .split_once('/')
            .ok_or(AccessError::Unavailable)?;
        let number = issue_number.to_string();
        match self
            .user_read::<Value>(token, &["repos", owner, repo, "issues", &number])
            .await
        {
            Ok(_) => Ok("reader"),
            Err(AccessError::Hidden) => Ok("none"),
            Err(error) => Err(error),
        }
    }

    /// Require visibility of both repositories for consent withdrawal.
    pub async fn target_visible(
        &self,
        token: &str,
        target: &ConfigTarget,
    ) -> Result<bool, AccessError> {
        Ok(self.repository_visible(token, &target.upstream).await?
            && self.repository_visible(token, &target.fork).await?)
    }

    /// Resolve the repository's numeric identity under the signed-in user bearer.
    pub async fn repository_id(&self, token: &str, repository: &str) -> Result<i64, AccessError> {
        let (owner, repo) = repository.split_once('/').ok_or(AccessError::Unavailable)?;
        let metadata: Value = self.user_read(token, &["repos", owner, repo]).await?;
        metadata
            .get("id")
            .and_then(Value::as_i64)
            .ok_or(AccessError::Unavailable)
    }

    /// Distinguish hidden repositories from refused or unavailable user tokens.
    async fn repository_visible(&self, token: &str, repository: &str) -> Result<bool, AccessError> {
        let (owner, repo) = repository.split_once('/').ok_or(AccessError::Unavailable)?;
        match self
            .app
            .user_read_bearer::<Value>(token, &["repos", owner, repo], &[])
            .await
        {
            Ok(_) => Ok(true),
            Err(error) if response_status(&error) == Some(StatusCode::NOT_FOUND) => Ok(false),
            Err(error) if response_status(&error) == Some(StatusCode::UNAUTHORIZED) => {
                Err(AccessError::Revoked)
            }
            Err(_) => Err(AccessError::Unavailable),
        }
    }

    /// Read one GitHub resource with the session's own bearer.
    async fn user_read<T: DeserializeOwned>(
        &self,
        token: &str,
        segments: &[&str],
    ) -> Result<T, AccessError> {
        self.app
            .user_read_bearer(token, segments, &[])
            .await
            .map_err(|error| {
                if response_status(&error) == Some(StatusCode::UNAUTHORIZED) {
                    AccessError::Revoked
                } else if response_status(&error) == Some(StatusCode::NOT_FOUND) {
                    AccessError::Hidden
                } else {
                    AccessError::Unavailable
                }
            })
    }
}
