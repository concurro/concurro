//! Repository-scoped GitHub App identity and read operations for task decisions.

pub mod user_grant;

use user_grant::UserGrant;

use std::{collections::BTreeMap, sync::Arc, time::Duration};

use anyhow::{Context, Result, bail, ensure};
use base64::{Engine, engine::general_purpose::STANDARD};
use chrono::{DateTime, Utc};
use jsonwebtoken::{Algorithm, EncodingKey, Header};
use reqwest::{Client, Method, StatusCode, Url, header::USER_AGENT};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use serde_json::{Value, json};
use tokio::sync::Mutex;

const TOKEN_MARGIN_SECONDS: i64 = 600;
const REQUEST_TIMEOUT_SECONDS: u64 = 60;

/// Current issue and writer authority observed with an App read token.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct IssueAuthority {
    /// Whether GitHub currently reports the issue open.
    pub open: bool,
    /// Whether GitHub currently grants the actor write, maintain or admin access.
    pub writer: bool,
}

/// A GitHub App client that mints one-repository installation tokens.
#[derive(Clone)]
pub struct AppClient {
    inner: Arc<Inner>,
}

struct Inner {
    client: Client,
    base: Url,
    app_id: i64,
    key: EncodingKey,
    tokens: Mutex<BTreeMap<(String, String), CachedToken>>,
}

struct CachedToken {
    value: String,
    expires_at: DateTime<Utc>,
}

#[derive(Serialize)]
struct Claims {
    iat: i64,
    exp: i64,
    iss: String,
}

/// One page of App webhook deliveries for ingress recovery.
#[derive(Serialize)]
pub struct AppDeliveryPage {
    /// GitHub's original delivery records.
    pub deliveries: Vec<Value>,
    /// Opaque cursor of the next page, when present.
    pub next_cursor: Option<String>,
}

#[derive(Deserialize)]
struct Installation {
    id: i64,
    repository_selection: String,
    account: InstallationAccount,
}

#[derive(Deserialize)]
struct InstallationAccount {
    login: String,
}

#[derive(Deserialize)]
struct MintedToken {
    token: String,
    expires_at: DateTime<Utc>,
    permissions: BTreeMap<String, String>,
}

#[derive(Deserialize)]
struct InstallationRepositories {
    total_count: u64,
    repositories: Vec<InstallationRepository>,
}

#[derive(Deserialize)]
struct InstallationRepository {
    full_name: String,
}

#[derive(Deserialize)]
struct IssueState {
    state: String,
}

#[derive(Deserialize)]
struct CollaboratorPermission {
    permission: String,
}

/// Default branch name returned by a repository read.
#[derive(Deserialize)]
struct RepositoryMetadata {
    default_branch: String,
}

/// Numeric target and owner identity used for runner OIDC claims.
#[derive(Deserialize)]
struct RunnerRepository {
    id: i64,
    owner: RunnerOwner,
    default_branch: String,
}

/// Numeric owner identity of a runner repository.
#[derive(Deserialize)]
struct RunnerOwner {
    id: i64,
}

/// Public bot identity expected to dispatch a runner.
#[derive(Deserialize)]
struct RunnerBot {
    id: i64,
    login: String,
}

/// Exact commit pointed to by a Git reference.
#[derive(Deserialize)]
struct GitRef {
    object: GitRefObject,
}

/// Commit identity within a Git reference response.
#[derive(Deserialize)]
struct GitRefObject {
    sha: String,
}

/// Base64 file body returned by the Contents API.
#[derive(Deserialize)]
struct ContentFile {
    #[serde(rename = "type")]
    kind: String,
    encoding: String,
    path: String,
    size: usize,
    content: String,
}

#[derive(Debug, thiserror::Error)]
enum ApiError {
    #[error("GitHub request failed (HTTP {0})")]
    Status(StatusCode),
    #[error("GitHub rate limited request (HTTP {0})")]
    Throttled(StatusCode, u64),
}

/// Return a GitHub HTTP failure code without exposing token or response content.
pub fn response_status(error: &anyhow::Error) -> Option<StatusCode> {
    match error.downcast_ref::<ApiError>() {
        Some(ApiError::Status(status)) => Some(*status),
        Some(ApiError::Throttled(status, _)) => Some(*status),
        None => None,
    }
}

/// Return GitHub's retry delay only for a response proven to be rate limited.
pub fn response_retry_delay(error: &anyhow::Error) -> Option<u64> {
    match error.downcast_ref::<ApiError>() {
        Some(ApiError::Throttled(_, seconds)) => Some(*seconds),
        _ => None,
    }
}

/// Whether a failed GitHub write was rejected, can be retried, or may have applied.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum WriteFailure {
    /// The response proves the requested write did not apply.
    Definitive(StatusCode),
    /// The request was rejected before applying and can be sent again after this delay.
    Retryable(StatusCode, u64),
    /// A transport failure or ambiguous response requires readback without resending.
    Unknown,
}

/// Classify a GitHub mutation using the same rules on every publication surface.
pub fn classify_write_failure(error: &anyhow::Error) -> WriteFailure {
    let Some(status) = response_status(error) else {
        return WriteFailure::Unknown;
    };
    if let Some(delay) = response_retry_delay(error) {
        return WriteFailure::Retryable(status, delay);
    }
    if status == StatusCode::UNAUTHORIZED {
        return WriteFailure::Retryable(status, 60);
    }
    if status == StatusCode::TOO_MANY_REQUESTS {
        return WriteFailure::Retryable(status, 60);
    }
    if status.is_client_error() && status != StatusCode::REQUEST_TIMEOUT {
        return WriteFailure::Definitive(status);
    }
    WriteFailure::Unknown
}

impl AppClient {
    /// Identify the selected App installation serving one configured repository.
    pub async fn repository_installation_id(&self, repository: &str) -> Result<i64> {
        Ok(self.repository_installation(repository).await?.id)
    }

    /// Read and verify the repository's App installation without minting a token.
    async fn repository_installation(&self, repository: &str) -> Result<Installation> {
        let (owner, name) = repository
            .split_once('/')
            .context("GitHub repository must be owner/name")?;
        let installation: Installation = self
            .api(
                Method::GET,
                &["repos", owner, name, "installation"],
                &self.app_jwt()?,
                None,
                &[],
            )
            .await?;
        ensure!(
            installation.repository_selection == "selected",
            "GitHub App installation must select repositories"
        );
        ensure!(
            installation.account.login.eq_ignore_ascii_case(owner),
            "GitHub App installation owner differs from repository owner"
        );
        Ok(installation)
    }

    /// List retained App webhook deliveries with an App JWT, preserving GitHub cursors.
    pub async fn app_deliveries(&self, cursor: Option<&str>) -> Result<AppDeliveryPage> {
        let mut url = self.url(&["app", "hook", "deliveries"])?;
        url.query_pairs_mut().append_pair("per_page", "100");
        if let Some(cursor) = cursor {
            url.query_pairs_mut().append_pair("cursor", cursor);
        }
        let response = self
            .request(Method::GET, url, &self.app_jwt()?)
            .send()
            .await
            .map_err(|_| anyhow::anyhow!("GitHub delivery listing failed"))?;
        ensure!(
            response.status().is_success(),
            "GitHub delivery listing failed"
        );
        let next_cursor = response
            .headers()
            .get(reqwest::header::LINK)
            .and_then(|value| value.to_str().ok())
            .map(|value| self.delivery_next_cursor(value))
            .transpose()?
            .flatten();
        let deliveries = response
            .json::<Vec<Value>>()
            .await
            .map_err(|_| anyhow::anyhow!("invalid GitHub delivery listing"))?;
        Ok(AppDeliveryPage {
            deliveries,
            next_cursor,
        })
    }

    /// Ask GitHub to redeliver one retained App delivery to ingress.
    pub async fn redeliver_app_delivery(&self, id: u64) -> Result<()> {
        let id = id.to_string();
        let response = self
            .request(
                Method::POST,
                self.url(&["app", "hook", "deliveries", &id, "attempts"])?,
                &self.app_jwt()?,
            )
            .send()
            .await
            .map_err(|_| anyhow::anyhow!("GitHub delivery redelivery failed"))?;
        ensure!(
            response.status().is_success(),
            "GitHub delivery redelivery failed"
        );
        Ok(())
    }

    /// Accept only an opaque cursor on this client's own delivery endpoint.
    fn delivery_next_cursor(&self, link: &str) -> Result<Option<String>> {
        for part in link.split(',') {
            let mut fields = part.trim().split(';');
            let Some(target) = fields.next() else {
                continue;
            };
            if !fields.any(|field| field.trim() == "rel=\"next\"") {
                continue;
            }
            let url = Url::parse(target.trim().trim_start_matches('<').trim_end_matches('>'))?;
            let expected = self.url(&["app", "hook", "deliveries"])?;
            ensure!(
                url.origin() == expected.origin() && url.path() == expected.path(),
                "invalid GitHub delivery pagination"
            );
            return Ok(url
                .query_pairs()
                .find_map(|(key, value)| (key == "cursor").then(|| value.into_owned())));
        }
        Ok(None)
    }

    /// Sign one short-lived App JWT without exposing its key to callers.
    fn app_jwt(&self) -> Result<String> {
        let now = Utc::now().timestamp();
        jsonwebtoken::encode(
            &Header::new(Algorithm::RS256),
            &Claims {
                iat: now - 60,
                exp: now + 540,
                iss: self.inner.app_id.to_string(),
            },
            &self.inner.key,
        )
        .map_err(|_| anyhow::anyhow!("GitHub App authentication failed"))
    }

    /// Bind an App key and API endpoint; no credential is displayed in errors.
    pub fn new(
        client: Client,
        api_base: &str,
        app_id: i64,
        private_key_pem: &[u8],
    ) -> Result<Self> {
        let key = EncodingKey::from_rsa_pem(private_key_pem)
            .map_err(|_| anyhow::anyhow!("GitHub App key is invalid"))?;
        let base = Url::parse(api_base).context("GitHub API base URL is invalid")?;
        Ok(Self {
            inner: Arc::new(Inner {
                client,
                base,
                app_id,
                key,
                tokens: Mutex::new(BTreeMap::new()),
            }),
        })
    }

    /// Recheck issue state and actor permission at decision time.
    pub async fn issue_authority(
        &self,
        repository: &str,
        issue: i64,
        login: &str,
        read_permissions: &BTreeMap<String, String>,
    ) -> Result<IssueAuthority> {
        let open = self.issue_open(repository, issue, read_permissions).await?;
        let writer = self
            .writer_permission(repository, login, read_permissions)
            .await?;
        Ok(IssueAuthority { open, writer })
    }

    /// Recheck whether an issue currently accepts work.
    pub async fn issue_open(
        &self,
        repository: &str,
        issue: i64,
        read_permissions: &BTreeMap<String, String>,
    ) -> Result<bool> {
        let (owner, name) = repository
            .split_once('/')
            .context("GitHub repository must be owner/name")?;
        let issue_number = issue.to_string();
        let state: IssueState = self
            .read(
                repository,
                read_permissions,
                &["repos", owner, name, "issues", &issue_number],
                &[],
            )
            .await?;
        ensure!(
            matches!(state.state.as_str(), "open" | "closed"),
            "GitHub issue state is invalid"
        );
        Ok(state.state == "open")
    }

    /// Check one actor's current write permission without rereading the issue.
    pub async fn writer_permission(
        &self,
        repository: &str,
        login: &str,
        read_permissions: &BTreeMap<String, String>,
    ) -> Result<bool> {
        let (owner, name) = repository
            .split_once('/')
            .context("GitHub repository must be owner/name")?;
        let path = &["repos", owner, name, "collaborators", login, "permission"];
        Ok(
            match self
                .read::<CollaboratorPermission>(repository, read_permissions, path, &[])
                .await
            {
                Ok(permission) => matches!(
                    permission.permission.as_str(),
                    "admin" | "maintain" | "write"
                ),
                Err(error)
                    if matches!(
                        error.downcast_ref::<ApiError>(),
                        Some(ApiError::Status(StatusCode::NOT_FOUND))
                    ) =>
                {
                    false
                }
                Err(error) => return Err(error),
            },
        )
    }

    /// Read a repository-scoped GitHub API resource with an exact permission set.
    pub async fn read<T: DeserializeOwned>(
        &self,
        repository: &str,
        permissions: &BTreeMap<String, String>,
        segments: &[&str],
        query: &[(&str, &str)],
    ) -> Result<T> {
        for attempt in 0..2 {
            let token = self.token(repository, permissions).await?;
            let result = self.api(Method::GET, segments, &token, None, query).await;
            if attempt == 0
                && matches!(
                    result
                        .as_ref()
                        .err()
                        .and_then(|error| error.downcast_ref::<ApiError>()),
                    Some(ApiError::Status(StatusCode::UNAUTHORIZED))
                )
            {
                self.drop_token(repository, permissions, &token).await?;
                continue;
            }
            return result;
        }
        unreachable!("second GitHub read returns its result")
    }

    /// Send one repository-scoped App mutation with an exact write permission set.
    pub async fn write<T: DeserializeOwned>(
        &self,
        repository: &str,
        permissions: &BTreeMap<String, String>,
        method: Method,
        segments: &[&str],
        body: Option<Value>,
    ) -> Result<T> {
        ensure!(
            method != Method::GET,
            "GitHub mutation requires a write method"
        );
        for attempt in 0..2 {
            let token = self.token(repository, permissions).await?;
            let result = self
                .api(method.clone(), segments, &token, body.clone(), &[])
                .await;
            if attempt == 0
                && result.as_ref().err().and_then(response_status) == Some(StatusCode::UNAUTHORIZED)
            {
                self.drop_token(repository, permissions, &token).await?;
                continue;
            }
            return result;
        }
        unreachable!("second GitHub write returns its result")
    }

    /// Send with an already scoped token, without preparing credentials after write claim.
    pub async fn write_prepared<T: DeserializeOwned>(
        &self,
        repository: &str,
        permissions: &BTreeMap<String, String>,
        token: &str,
        method: Method,
        segments: &[&str],
        body: Option<Value>,
    ) -> Result<T> {
        ensure!(
            method != Method::GET,
            "GitHub mutation requires a write method"
        );
        let result = self.api(method, segments, token, body, &[]).await;
        if result.as_ref().err().and_then(response_status) == Some(StatusCode::UNAUTHORIZED) {
            self.drop_token(repository, permissions, token).await?;
        }
        result
    }

    /// Read reviewer-visible GitHub state with the bound user grant.
    pub async fn user_read<T: DeserializeOwned>(
        &self,
        grant: &UserGrant,
        segments: &[&str],
        query: &[(&str, &str)],
    ) -> Result<T> {
        for attempt in 0..2 {
            let token = grant.token().await?;
            let result = self.api(Method::GET, segments, &token, None, query).await;
            if attempt == 0
                && result.as_ref().err().and_then(response_status) == Some(StatusCode::UNAUTHORIZED)
            {
                grant.drop_token(&token).await;
                continue;
            }
            return result;
        }
        unreachable!("second reviewer read returns its result")
    }

    /// Read with a session-held OAuth bearer that has no refresh grant.
    pub async fn user_read_bearer<T: DeserializeOwned>(
        &self,
        token: &str,
        segments: &[&str],
        query: &[(&str, &str)],
    ) -> Result<T> {
        self.api(Method::GET, segments, token, None, query).await
    }

    /// Send one fenced reviewer-account mutation with its prepared token.
    pub async fn user_write_prepared<T: DeserializeOwned>(
        &self,
        grant: &UserGrant,
        token: &str,
        method: Method,
        segments: &[&str],
        body: Value,
    ) -> Result<T> {
        ensure!(
            method != Method::GET,
            "reviewer mutation requires a write method"
        );
        let result = self.api(method, segments, token, Some(body), &[]).await;
        if result.as_ref().err().and_then(response_status) == Some(StatusCode::UNAUTHORIZED) {
            grant.drop_token(token).await;
        }
        result
    }

    /// Resolve a repository's current default branch to its exact commit.
    pub async fn default_head(
        &self,
        repository: &str,
        permissions: &BTreeMap<String, String>,
    ) -> Result<(String, String)> {
        let (owner, name) = repository
            .split_once('/')
            .context("GitHub repository must be owner/name")?;
        let metadata: RepositoryMetadata = self
            .read(repository, permissions, &["repos", owner, name], &[])
            .await?;
        ensure!(
            !metadata.default_branch.is_empty(),
            "GitHub default branch is empty"
        );
        let sha = self
            .branch_head(repository, permissions, &metadata.default_branch)
            .await?;
        Ok((metadata.default_branch, sha))
    }

    /// Read the current repository and bot identities for an expected runner claim.
    pub async fn runner_claims(
        &self,
        repository: &str,
        permissions: &BTreeMap<String, String>,
        workflow_ref: &str,
        bot_login: &str,
    ) -> Result<Value> {
        let (owner, name) = repository
            .split_once('/')
            .context("GitHub repository must be owner/name")?;
        let target: RunnerRepository = self
            .read(repository, permissions, &["repos", owner, name], &[])
            .await?;
        let bot_id = self.bot_user_id(repository, permissions, bot_login).await?;
        ensure!(
            target.id > 0 && target.owner.id > 0 && !target.default_branch.is_empty(),
            "cannot establish target runner identity"
        );
        Ok(json!({
            "repository_id": target.id.to_string(),
            "repository_owner_id": target.owner.id.to_string(),
            "job_workflow_ref": workflow_ref,
            "event_name": "workflow_dispatch",
            "ref": "",
            "actor_id": bot_id.to_string(),
        }))
    }

    /// Resolve the exact public bot account ID for commit and runner attribution.
    pub async fn bot_user_id(
        &self,
        repository: &str,
        permissions: &BTreeMap<String, String>,
        bot_login: &str,
    ) -> Result<i64> {
        self.public_user_id(repository, permissions, bot_login)
            .await
            .context("cannot establish target bot identity")
    }

    /// Resolve a public account's stable numeric ID from its current login.
    pub async fn public_user_id(
        &self,
        repository: &str,
        permissions: &BTreeMap<String, String>,
        login: &str,
    ) -> Result<i64> {
        let bot: RunnerBot = self
            .read(repository, permissions, &["users", login], &[])
            .await?;
        ensure!(
            bot.id > 0 && bot.login.eq_ignore_ascii_case(login),
            "cannot establish source actor identity"
        );
        Ok(bot.id)
    }

    /// Resolve the current login of one durable account ID, including after a rename.
    pub async fn public_user_login_by_id(
        &self,
        repository: &str,
        permissions: &BTreeMap<String, String>,
        id: i64,
    ) -> Result<Option<String>> {
        ensure!(id > 0, "source actor ID is invalid");
        let account = self
            .read::<RunnerBot>(repository, permissions, &["user", &id.to_string()], &[])
            .await;
        match account {
            Ok(account) if account.id == id && !account.login.is_empty() => Ok(Some(account.login)),
            Ok(_) => Ok(None),
            Err(error) if response_status(&error) == Some(StatusCode::NOT_FOUND) => Ok(None),
            Err(error) => Err(error),
        }
    }

    /// Resolve one named branch to its exact current commit.
    pub async fn branch_head(
        &self,
        repository: &str,
        permissions: &BTreeMap<String, String>,
        branch: &str,
    ) -> Result<String> {
        let (owner, name) = repository
            .split_once('/')
            .context("GitHub repository must be owner/name")?;
        ensure!(
            !branch.is_empty()
                && branch
                    .split('/')
                    .all(|part| !part.is_empty() && part != ".."),
            "GitHub branch is invalid"
        );
        let mut path = vec!["repos", owner, name, "git", "ref", "heads"];
        path.extend(branch.split('/'));
        let reference: GitRef = self.read(repository, permissions, &path, &[]).await?;
        ensure!(
            reference.object.sha.len() == 40
                && reference
                    .object
                    .sha
                    .bytes()
                    .all(|byte| byte.is_ascii_hexdigit()),
            "GitHub reference has an invalid commit"
        );
        Ok(reference.object.sha)
    }

    /// Read a regular UTF-8 file at one pinned commit, returning None for a missing path.
    pub async fn file_text(
        &self,
        repository: &str,
        permissions: &BTreeMap<String, String>,
        path: &str,
        commit: &str,
    ) -> Result<Option<String>> {
        let (owner, name) = repository
            .split_once('/')
            .context("GitHub repository must be owner/name")?;
        ensure!(
            !path.is_empty() && path.split('/').all(|part| !part.is_empty() && part != ".."),
            "GitHub file path is invalid"
        );
        let mut segments = vec!["repos", owner, name, "contents"];
        segments.extend(path.split('/'));
        let file: ContentFile = match self
            .read(repository, permissions, &segments, &[("ref", commit)])
            .await
        {
            Ok(file) => file,
            Err(error)
                if matches!(
                    error.downcast_ref::<ApiError>(),
                    Some(ApiError::Status(StatusCode::NOT_FOUND))
                ) =>
            {
                return Ok(None);
            }
            Err(error) => return Err(error),
        };
        ensure!(
            file.kind == "file" && file.encoding == "base64" && file.path == path,
            "GitHub content is not the requested regular file"
        );
        let encoded: Vec<u8> = file
            .content
            .bytes()
            .filter(|byte| !byte.is_ascii_whitespace())
            .collect();
        let bytes = STANDARD
            .decode(&encoded)
            .map_err(|_| anyhow::anyhow!("GitHub file content has invalid base64"))?;
        ensure!(
            bytes.len() == file.size,
            "GitHub file size differs from response"
        );
        Ok(Some(
            String::from_utf8(bytes).context("GitHub file is not UTF-8")?,
        ))
    }

    /// Mint or reuse an exactly scoped installation token for one repository.
    pub async fn token(
        &self,
        repository: &str,
        permissions: &BTreeMap<String, String>,
    ) -> Result<String> {
        let scope = serde_json::to_string(permissions)?;
        let cache_key = (repository.to_ascii_lowercase(), scope);
        if let Some(cached) = self.inner.tokens.lock().await.get(&cache_key)
            && cached.expires_at.timestamp() - Utc::now().timestamp() > TOKEN_MARGIN_SECONDS
        {
            return Ok(cached.value.clone());
        }
        let (owner, name) = repository
            .split_once('/')
            .context("GitHub repository must be owner/name")?;
        ensure!(
            !owner.is_empty() && !name.is_empty() && !name.contains('/'),
            "GitHub repository must be owner/name"
        );
        let installation = self.repository_installation(repository).await?;
        let jwt = self.app_jwt()?;
        let installation_id = installation.id.to_string();
        let minted: MintedToken = self
            .api(
                Method::POST,
                &["app", "installations", &installation_id, "access_tokens"],
                &jwt,
                Some(json!({"repositories": [name], "permissions": permissions})),
                &[],
            )
            .await?;
        let mut granted = minted.permissions;
        granted.remove("metadata");
        let scoped = if granted == *permissions && !minted.token.is_empty() {
            self.api::<InstallationRepositories>(
                Method::GET,
                &["installation", "repositories"],
                &minted.token,
                None,
                &[],
            )
            .await
            .is_ok_and(|listing| {
                listing.total_count == 1
                    && listing.repositories.len() == 1
                    && listing.repositories[0]
                        .full_name
                        .eq_ignore_ascii_case(repository)
            })
        } else {
            false
        };
        if !scoped {
            self.revoke(&minted.token).await;
            bail!("GitHub App token scope differs from requested repository and permissions");
        }
        self.inner.tokens.lock().await.insert(
            cache_key,
            CachedToken {
                value: minted.token.clone(),
                expires_at: minted.expires_at,
            },
        );
        Ok(minted.token)
    }

    /// Forget a refused cached token so its next use mints a fresh one.
    pub async fn drop_token(
        &self,
        repository: &str,
        permissions: &BTreeMap<String, String>,
        token: &str,
    ) -> Result<()> {
        let key = (
            repository.to_ascii_lowercase(),
            serde_json::to_string(permissions)?,
        );
        let mut tokens = self.inner.tokens.lock().await;
        if tokens.get(&key).is_some_and(|cached| cached.value == token) {
            tokens.remove(&key);
        }
        Ok(())
    }

    async fn revoke(&self, token: &str) {
        let Ok(url) = self.url(&["installation", "token"]) else {
            return;
        };
        let _ = self.request(Method::DELETE, url, token).send().await;
    }

    /// Apply the same identity and timeout headers to every GitHub API request.
    fn request(&self, method: Method, url: Url, token: &str) -> reqwest::RequestBuilder {
        self.inner
            .client
            .request(method, url)
            .timeout(Duration::from_secs(REQUEST_TIMEOUT_SECONDS))
            .bearer_auth(token)
            .header(USER_AGENT, "concurro-service")
            .header("Accept", "application/vnd.github+json")
            .header("X-GitHub-Api-Version", "2022-11-28")
    }

    fn url(&self, segments: &[&str]) -> Result<Url> {
        let mut url = self.inner.base.clone();
        url.path_segments_mut()
            .map_err(|_| anyhow::anyhow!("GitHub API base URL cannot take paths"))?
            .pop_if_empty()
            .extend(segments.iter().copied());
        Ok(url)
    }

    async fn api<T: DeserializeOwned>(
        &self,
        method: Method,
        segments: &[&str],
        token: &str,
        body: Option<Value>,
        query: &[(&str, &str)],
    ) -> Result<T> {
        let mut url = self.url(segments)?;
        if !query.is_empty() {
            url.query_pairs_mut().extend_pairs(query.iter().copied());
        }
        let mut request = self.request(method, url, token);
        if let Some(body) = body {
            request = request.json(&body);
        }
        let response = request
            .send()
            .await
            .map_err(|_| anyhow::anyhow!("GitHub request failed"))?;
        if !response.status().is_success() {
            let status = response.status();
            if status == StatusCode::FORBIDDEN || status == StatusCode::TOO_MANY_REQUESTS {
                let headers = response.headers();
                let retry_after = headers
                    .get("retry-after")
                    .and_then(|value| value.to_str().ok())
                    .filter(|value| !value.trim().is_empty())
                    .map(str::to_owned);
                let exhausted = headers
                    .get("x-ratelimit-remaining")
                    .and_then(|value| value.to_str().ok())
                    == Some("0");
                let reset = headers
                    .get("x-ratelimit-reset")
                    .and_then(|value| value.to_str().ok())
                    .and_then(|value| value.parse::<i64>().ok());
                let retry_seconds = retry_after
                    .as_deref()
                    .and_then(|value| value.parse::<u64>().ok());
                let body_limited = if status == StatusCode::FORBIDDEN
                    && retry_after.is_none()
                    && !exhausted
                {
                    response
                        .json::<Value>()
                        .await
                        .ok()
                        .and_then(|value| {
                            value
                                .get("message")
                                .and_then(Value::as_str)
                                .map(str::to_owned)
                        })
                        .is_some_and(|message| message.to_ascii_lowercase().contains("rate limit"))
                } else {
                    false
                };
                if status == StatusCode::TOO_MANY_REQUESTS
                    || retry_after.is_some()
                    || exhausted
                    || body_limited
                {
                    let seconds = retry_seconds
                        .or_else(|| {
                            if exhausted {
                                reset.map(|value| {
                                    value.saturating_sub(Utc::now().timestamp()).max(1) as u64
                                })
                            } else {
                                None
                            }
                        })
                        .unwrap_or(60)
                        .max(1);
                    return Err(ApiError::Throttled(status, seconds).into());
                }
            }
            return Err(ApiError::Status(status).into());
        }
        let bytes = response
            .bytes()
            .await
            .map_err(|_| anyhow::anyhow!("GitHub response unreadable"))?;
        if bytes.is_empty() {
            serde_json::from_value(Value::Null)
                .map_err(|_| anyhow::anyhow!("GitHub response unreadable"))
        } else {
            serde_json::from_slice(&bytes)
                .map_err(|_| anyhow::anyhow!("GitHub response unreadable"))
        }
    }
}

#[cfg(test)]
mod tests {
    use std::{
        collections::BTreeMap,
        process::Command,
        sync::{
            Arc,
            atomic::{AtomicBool, AtomicUsize, Ordering},
        },
    };

    use axum::{
        Json, Router,
        http::{HeaderMap, Uri},
        routing::{get, post},
    };
    use chrono::{Duration, Utc};
    use reqwest::StatusCode;
    use serde_json::{Value, json};

    use super::{
        ApiError, AppClient, IssueAuthority, WriteFailure, classify_write_failure, response_status,
    };

    #[test]
    fn write_rejections_share_one_outcome_classification() {
        assert_eq!(
            classify_write_failure(&ApiError::Status(StatusCode::UNAUTHORIZED).into()),
            WriteFailure::Retryable(StatusCode::UNAUTHORIZED, 60)
        );
        assert_eq!(
            classify_write_failure(&ApiError::Throttled(StatusCode::FORBIDDEN, 120).into()),
            WriteFailure::Retryable(StatusCode::FORBIDDEN, 120)
        );
        assert_eq!(
            classify_write_failure(&ApiError::Status(StatusCode::TOO_MANY_REQUESTS).into()),
            WriteFailure::Retryable(StatusCode::TOO_MANY_REQUESTS, 60)
        );
        assert_eq!(
            classify_write_failure(&ApiError::Status(StatusCode::FORBIDDEN).into()),
            WriteFailure::Definitive(StatusCode::FORBIDDEN)
        );
        assert_eq!(
            classify_write_failure(&ApiError::Status(StatusCode::UNPROCESSABLE_ENTITY).into()),
            WriteFailure::Definitive(StatusCode::UNPROCESSABLE_ENTITY)
        );
    }

    #[tokio::test]
    async fn scopes_and_caches_read_tokens_then_revokes_a_scope_mismatch() {
        let minted = Arc::new(AtomicUsize::new(0));
        let revoked = Arc::new(AtomicUsize::new(0));
        let bad_scope = Arc::new(AtomicBool::new(false));
        let unauthorized = Arc::new(AtomicBool::new(false));
        let app = Router::new()
            .route(
                "/repos/acme/demo/installation",
                get(|headers: HeaderMap| async move {
                    assert_eq!(headers["user-agent"], "concurro-service");
                    Json(json!({"id": 7, "repository_selection": "selected", "account": {"login": "acme"}}))
                }),
            )
            .route(
                "/app/installations/7/access_tokens",
                post({
                    let minted = Arc::clone(&minted);
                    let bad_scope = Arc::clone(&bad_scope);
                    move |headers: HeaderMap, Json(body): Json<Value>| {
                        let minted = Arc::clone(&minted);
                        let bad_scope = Arc::clone(&bad_scope);
                        async move {
                            assert_eq!(headers["user-agent"], "concurro-service");
                            assert_eq!(body["repositories"], json!(["demo"]));
                            assert_eq!(body["permissions"]["issues"], "read");
                            minted.fetch_add(1, Ordering::SeqCst);
                            let permissions = if bad_scope.load(Ordering::SeqCst) {
                                json!({"issues": "write"})
                            } else {
                                body["permissions"].clone()
                            };
                            Json(json!({"token": "test-installation-token", "permissions": permissions,
                                "expires_at": (Utc::now() + Duration::minutes(55)).to_rfc3339()}))
                        }
                    }
                }),
            )
            .route(
                "/installation/repositories",
                get(|| async {
                    Json(json!({"total_count": 1, "repositories": [{"full_name": "acme/demo"}]}))
                }),
            )
            .route(
                "/installation/token",
                axum::routing::delete({
                    let revoked = Arc::clone(&revoked);
                    move |headers: HeaderMap| {
                        let revoked = Arc::clone(&revoked);
                        async move {
                            assert_eq!(headers["user-agent"], "concurro-service");
                            revoked.fetch_add(1, Ordering::SeqCst);
                        }
                    }
                }),
            )
            .route(
                "/app/hook/deliveries",
                get(|headers: HeaderMap, uri: Uri| async move {
                    assert!(headers["authorization"].to_str().unwrap().starts_with("Bearer ey"));
                    assert_eq!(uri.query(), Some("per_page=100"));
                    Json(json!([{"id": 41, "guid": "one", "status": "OK"}]))
                }),
            )
            .route(
                "/app/hook/deliveries/41/attempts",
                post(|headers: HeaderMap| async move {
                    assert!(headers["authorization"].to_str().unwrap().starts_with("Bearer ey"));
                    axum::http::StatusCode::ACCEPTED
                }),
            )
            .route(
                "/repos/acme/demo/issues/3",
                get({
                    let unauthorized = Arc::clone(&unauthorized);
                    move || {
                        let unauthorized = Arc::clone(&unauthorized);
                        async move {
                            if unauthorized.swap(false, Ordering::SeqCst) {
                                (axum::http::StatusCode::UNAUTHORIZED, Json(json!({"message": "expired"})))
                            } else {
                                (axum::http::StatusCode::OK, Json(json!({"state": "open"})))
                            }
                        }
                    }
                }),
            )
            .route(
                "/repos/acme/demo/collaborators/writer/permission",
                get(|headers: HeaderMap| async move {
                    assert_eq!(headers["authorization"], "Bearer test-installation-token");
                    Json(json!({"permission": "write"}))
                }),
            )
            .route(
                "/repos/acme/demo",
                get(|| async { Json(json!({"id": 17, "owner": {"id": 18}, "default_branch": "release/main"})) }),
            )
            .route(
                "/users/concurro[bot]",
                get(|| async { Json(json!({"id": 19, "login": "concurro[bot]"})) }),
            )
            .route(
                "/user/2",
                get(|| async { Json(json!({"id": 2, "login": "renamed-writer"})) }),
            )
            .route(
                "/repos/acme/demo/git/ref/heads/release/main",
                get(|| async { Json(json!({"object": {"sha": "a".repeat(40)}})) }),
            )
            .route(
                "/repos/acme/demo/contents/.concurro/config.json",
                get(|uri: Uri| async move {
                    assert_eq!(uri.query(), Some(format!("ref={}", "a".repeat(40)).as_str()));
                    Json(json!({"type": "file", "encoding": "base64",
                        "path": ".concurro/config.json", "size": 11,
                        "content": "eyJvayI6\ndHJ1ZX0="}))
                }),
            );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let address = listener.local_addr().expect("address");
        let server = tokio::spawn(async move { axum::serve(listener, app).await });
        let key = Command::new("openssl")
            .args([
                "genpkey",
                "-algorithm",
                "RSA",
                "-pkeyopt",
                "rsa_keygen_bits:2048",
            ])
            .output()
            .expect("generate test key");
        assert!(key.status.success());
        let identity = AppClient::new(
            reqwest::Client::new(),
            &format!("http://{address}"),
            42,
            &key.stdout,
        )
        .expect("App client");
        let deliveries = identity.app_deliveries(None).await.expect("deliveries");
        assert_eq!(deliveries.deliveries[0]["id"], 41);
        assert_eq!(deliveries.next_cursor, None);
        identity
            .redeliver_app_delivery(41)
            .await
            .expect("redelivery");
        assert_eq!(
            identity
                .delivery_next_cursor(&format!(
                    "<http://{address}/app/hook/deliveries?cursor=next>; rel=\"next\""
                ))
                .expect("next cursor"),
            Some("next".to_owned())
        );
        assert!(
            identity
                .delivery_next_cursor(
                    "<https://elsewhere.invalid/app/hook/deliveries?cursor=bad>; rel=\"next\""
                )
                .is_err()
        );
        let permissions = BTreeMap::from([
            ("administration".to_owned(), "read".to_owned()),
            ("checks".to_owned(), "read".to_owned()),
            ("contents".to_owned(), "read".to_owned()),
            ("issues".to_owned(), "read".to_owned()),
            ("pull_requests".to_owned(), "read".to_owned()),
        ]);
        for _ in 0..2 {
            assert_eq!(
                identity
                    .issue_authority("acme/demo", 3, "writer", &permissions)
                    .await
                    .expect("authority"),
                IssueAuthority {
                    open: true,
                    writer: true,
                }
            );
        }
        assert_eq!(minted.load(Ordering::SeqCst), 1);
        assert_eq!(
            identity
                .default_head("acme/demo", &permissions)
                .await
                .expect("head"),
            ("release/main".to_owned(), "a".repeat(40))
        );
        assert_eq!(
            identity
                .runner_claims(
                    "acme/demo",
                    &permissions,
                    "acme/demo/.github/workflows/agent.yml@refs/heads/main",
                    "concurro[bot]",
                )
                .await
                .expect("runner claims"),
            json!({
                "repository_id": "17",
                "repository_owner_id": "18",
                "job_workflow_ref": "acme/demo/.github/workflows/agent.yml@refs/heads/main",
                "event_name": "workflow_dispatch",
                "ref": "",
                "actor_id": "19",
            })
        );
        assert_eq!(
            identity
                .public_user_id("acme/demo", &permissions, "concurro[bot]")
                .await
                .expect("public account"),
            19
        );
        assert_eq!(
            response_status(
                &identity
                    .public_user_id("acme/demo", &permissions, "former-writer")
                    .await
                    .expect_err("old login no longer resolves")
            ),
            Some(StatusCode::NOT_FOUND)
        );
        assert_eq!(
            identity
                .public_user_login_by_id("acme/demo", &permissions, 2)
                .await
                .expect("stable numeric account"),
            Some("renamed-writer".to_owned())
        );
        assert_eq!(
            identity
                .public_user_login_by_id("acme/demo", &permissions, 999)
                .await
                .expect("removed account"),
            None
        );
        assert_eq!(
            identity
                .file_text(
                    "acme/demo",
                    &permissions,
                    ".concurro/config.json",
                    &"a".repeat(40)
                )
                .await
                .expect("pinned file"),
            Some("{\"ok\":true}".to_owned())
        );
        unauthorized.store(true, Ordering::SeqCst);
        identity
            .issue_authority("acme/demo", 3, "writer", &permissions)
            .await
            .expect("refreshed authority");
        assert_eq!(minted.load(Ordering::SeqCst), 2);
        identity
            .drop_token("acme/demo", &permissions, "test-installation-token")
            .await
            .expect("drop refused token");
        bad_scope.store(true, Ordering::SeqCst);
        assert!(identity.token("acme/demo", &permissions).await.is_err());
        assert_eq!(revoked.load(Ordering::SeqCst), 1);
        server.abort();
    }
}
