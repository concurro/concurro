//! Nonsecret metadata client for the Python credential Vault during the Rust migration.

use std::{collections::BTreeMap, fmt, time::Duration};

use chrono::{DateTime, Utc};
use reqwest::{Client, StatusCode};
use serde::{Deserialize, de::DeserializeOwned};

/// The Vault's nonsecret view of one owner's credentials and list policies.
#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct Metadata {
    /// Whether all latest records have known historical status.
    pub history_complete: bool,
    /// Latest record for each entry at the requested boundary.
    pub credentials: Vec<Credential>,
    /// Provider list revisions and ordered entry IDs.
    pub lists: BTreeMap<String, ListPolicy>,
}

impl Metadata {
    /// Select the first currently usable list entry for one task role and profile.
    pub fn select(
        &self,
        provider: &str,
        role: &str,
        target: &str,
        profile: &str,
        routes: &[String],
        now: DateTime<Utc>,
    ) -> Option<&Credential> {
        self.credentials
            .iter()
            .filter(|record| {
                let kind = match record.route.as_str() {
                    "openai-api-key" if provider == "openai" => "api_key",
                    "chatgpt-subscription" if provider == "openai" => "subscription",
                    "anthropic-api-key" if provider == "anthropic" => "api_key",
                    "claude-subscription" if provider == "anthropic" => "subscription",
                    _ => return false,
                };
                record.provider.as_deref() == Some(provider)
                    && record.kind == kind
                    && record.status == "valid"
                    && record.enabled == Some(true)
                    && record.position.is_some()
                    && routes.contains(&record.route)
                    && record.roles.as_ref().is_none_or(|roles| {
                        roles.is_empty() || roles.iter().any(|item| item == role)
                    })
                    && record.profiles.as_ref().is_none_or(|profiles| {
                        profiles.is_empty()
                            || profiles
                                .iter()
                                .any(|item| item.target == target && item.profile == profile)
                    })
                    && (!record.expiry_known
                        || record
                            .expires_at
                            .as_deref()
                            .and_then(|at| DateTime::parse_from_rfc3339(at).ok())
                            .is_some_and(|at| at > now))
            })
            .min_by_key(|record| record.position.expect("listed entry has position"))
    }
}

/// One entry's current or historical credential version, without secret material.
#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct Credential {
    /// Stable version identity used for admission binding.
    pub record_id: String,
    /// GitHub actor ID, encoded as a decimal string by Vault.
    pub user_id: String,
    /// Exact Vault route.
    pub route: String,
    /// Version allocated on the owner and route.
    pub version: u64,
    /// Current or boundary-relative usability status.
    pub status: String,
    /// Historical model coverage, if known.
    pub models: Option<Vec<String>>,
    /// Version creation timestamp.
    pub created: String,
    /// Latest version update timestamp.
    pub updated: String,
    /// `api_key` or `subscription`.
    pub kind: String,
    /// Whether expiry evidence is known.
    pub expiry_known: bool,
    /// Known expiry, when present.
    pub expires_at: Option<String>,
    /// Stable list entry identity, absent only in legacy evidence.
    pub entry_id: Option<String>,
    /// Sponsorship provider of this entry.
    pub provider: Option<String>,
    /// Owner-supplied entry name.
    pub name: Option<String>,
    /// Whether the entry was enabled at the boundary.
    pub enabled: Option<bool>,
    /// Role keys allowed by the owner.
    pub roles: Option<Vec<String>>,
    /// Target and profile filters allowed by the owner.
    pub profiles: Option<Vec<ProfileFilter>>,
    /// Selection priority in the provider list.
    pub position: Option<usize>,
    /// Whether the entry was absent from the provider list.
    pub removed: Option<bool>,
    /// Governing list revision; zero means pre-list legacy evidence.
    #[serde(skip)]
    pub list_revision: Option<u64>,
}

/// One target-specific profile filter on an owner entry.
#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct ProfileFilter {
    /// Canonical upstream repository.
    pub target: String,
    /// Profile key within the target.
    pub profile: String,
}

/// A provider's ordering policy at the requested boundary.
#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct ListPolicy {
    /// Policy revision, with zero denoting adopted legacy state.
    pub revision: u64,
    /// Entry IDs in priority order.
    pub entries: Vec<String>,
}

/// A page of immutable, nonsecret credential lifecycle facts.
#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct EventPage {
    /// Events after the requested cursor, in source order.
    pub events: Vec<CredentialEvent>,
    /// Last event sequence returned, or the requested cursor for an empty page.
    pub next: u64,
    /// Whether another page was present when Vault read this page.
    pub more: bool,
}

/// One record or selection-policy event from the Vault metadata feed.
#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct CredentialEvent {
    /// Monotonic Vault source sequence.
    pub sequence: u64,
    /// Immutable event identity.
    pub id: String,
    /// Empty for policy events.
    pub record_id: String,
    /// GitHub actor ID, encoded as an integer in event rows.
    pub user_id: i64,
    /// Exact Vault route, empty for policy events.
    pub route: String,
    /// Record version, zero for policy events.
    pub version: u64,
    /// Lifecycle or `policy` event kind.
    #[serde(rename = "type")]
    pub kind: String,
    /// Record status, empty for policy events.
    pub status: String,
    /// Nonsecret reason when supplied.
    pub reason: Option<String>,
    /// Vault event timestamp.
    pub at: String,
    /// Entry identity for schema 6 events.
    pub entry_id: Option<String>,
    /// Provider identity for schema 6 events.
    pub provider: Option<String>,
    /// Provider list revision for policy events.
    pub revision: Option<u64>,
}

/// A metadata read failed without revealing the service bearer or response body.
#[derive(Debug, thiserror::Error, PartialEq)]
pub enum VaultError {
    /// Vault could not be reached or its body could not be read.
    #[error("vault metadata unavailable")]
    Unavailable,
    /// Vault refused the service metadata request.
    #[error("vault metadata refused: HTTP {0}")]
    Refused(StatusCode),
    /// Vault returned a response outside its nonsecret metadata contract.
    #[error("invalid vault metadata response")]
    InvalidResponse,
}

/// Authenticated read-only client for the Vault metadata listener.
#[derive(Clone)]
pub struct MetadataClient {
    client: Client,
    base: String,
    token: String,
}

impl fmt::Debug for MetadataClient {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("MetadataClient")
            .finish_non_exhaustive()
    }
}

impl MetadataClient {
    /// Bind the dedicated metadata bearer to a reusable HTTP client.
    pub fn new(client: Client, base: impl Into<String>, token: impl Into<String>) -> Self {
        Self {
            client,
            base: base.into().trim_end_matches('/').to_owned(),
            token: token.into(),
        }
    }

    /// Read one owner's nonsecret records at the current time or an exact boundary.
    pub async fn read(&self, owner: i64, at: Option<&str>) -> Result<Metadata, VaultError> {
        if owner < 1 {
            return Err(VaultError::InvalidResponse);
        }
        let mut request = self.request(&format!("metadata/{owner}"));
        if let Some(at) = at {
            request = request.query(&[("at", at)]);
        }
        let mut metadata: Metadata = self.fetch(request).await?;
        for credential in &mut metadata.credentials {
            if credential.user_id != owner.to_string() {
                return Err(VaultError::InvalidResponse);
            }
            credential.list_revision = credential
                .provider
                .as_deref()
                .and_then(|provider| metadata.lists.get(provider))
                .map(|list| list.revision)
                .filter(|revision| *revision > 0);
        }
        Ok(metadata)
    }

    /// Read and validate one page after a durable metadata event cursor.
    pub async fn events(&self, after: u64) -> Result<EventPage, VaultError> {
        let page: EventPage = self
            .fetch(self.request("metadata/events").query(&[("after", after)]))
            .await?;
        let mut previous = after;
        for event in &page.events {
            if event.sequence <= previous || event.user_id < 1 || !event.valid_kind() {
                return Err(VaultError::InvalidResponse);
            }
            previous = event.sequence;
        }
        if page.next != previous || (page.more && page.events.is_empty()) {
            return Err(VaultError::InvalidResponse);
        }
        Ok(page)
    }

    /// Apply the dedicated bearer and a bounded request timeout.
    fn request(&self, route: &str) -> reqwest::RequestBuilder {
        self.client
            .get(format!("{}/{route}", self.base))
            .bearer_auth(&self.token)
            .timeout(Duration::from_secs(10))
    }

    /// Decode an allow-listed response without retaining remote bodies in errors.
    async fn fetch<T: DeserializeOwned>(
        &self,
        request: reqwest::RequestBuilder,
    ) -> Result<T, VaultError> {
        let response = request.send().await.map_err(|_| VaultError::Unavailable)?;
        if !response.status().is_success() {
            return Err(VaultError::Refused(response.status()));
        }
        response
            .json()
            .await
            .map_err(|_| VaultError::InvalidResponse)
    }
}

impl CredentialEvent {
    /// Check the event kinds and record fields accepted by the legacy consumer.
    fn valid_kind(&self) -> bool {
        if self.kind == "policy" {
            return self.version == 0
                && self.record_id.is_empty()
                && matches!(self.provider.as_deref(), Some("openai" | "anthropic"))
                && self.revision.is_some();
        }
        self.version > 0
            && matches!(
                self.route.as_str(),
                "openai-api-key"
                    | "anthropic-api-key"
                    | "chatgpt-subscription"
                    | "claude-subscription"
            )
            && matches!(
                self.status.as_str(),
                "valid" | "invalid" | "revoked" | "retired"
            )
            && matches!(
                self.kind.as_str(),
                "stored"
                    | "validated"
                    | "retired"
                    | "revoked"
                    | "unusable"
                    | "rewrapped"
                    | "snapshot"
            )
    }
}

#[cfg(test)]
mod tests {
    use axum::{
        Json, Router,
        http::{HeaderMap, StatusCode, Uri},
        routing::get,
    };
    use chrono::DateTime;
    use serde_json::json;

    use super::{MetadataClient, VaultError};

    /// Read only nonsecret fields using the dedicated metadata credential.
    #[tokio::test]
    async fn reads_allowlisted_owner_metadata_and_cursor_events() {
        let app = Router::new()
            .route(
                "/metadata/42",
                get(|headers: HeaderMap, uri: Uri| async move {
                    assert_eq!(headers["authorization"], "Bearer metadata-only");
                    assert_eq!(uri.query(), Some("at=2026-11-20T10%3A00%3A00Z"));
                    Json(json!({
                        "history_complete": true,
                        "credentials": [{
                            "record_id": "42:openai-api-key:2", "user_id": "42",
                            "route": "openai-api-key", "version": 2, "status": "valid",
                            "models": null, "created": "2026-11-19T10:00:00Z",
                            "updated": "2026-11-19T10:00:00Z", "kind": "api_key",
                            "expiry_known": false, "expires_at": null,
                            "entry_id": "entry-a", "provider": "openai", "name": "mine",
                            "enabled": true, "roles": ["implementer"],
                            "profiles": [{"target": "org/repo", "profile": "default"}],
                            "position": 0, "removed": false,
                            "fingerprint": "must-not-cross", "secret": "must-not-cross"
                        }],
                        "lists": {"openai": {"revision": 3, "entries": ["entry-a"]}}
                    }))
                }),
            )
            .route(
                "/metadata/events",
                get(|headers: HeaderMap, uri: Uri| async move {
                    assert_eq!(headers["authorization"], "Bearer metadata-only");
                    assert_eq!(uri.query(), Some("after=7"));
                    Json(json!({
                        "events": [{
                            "sequence": 8, "id": "event-8", "record_id": "42:openai-api-key:2",
                            "user_id": 42, "route": "openai-api-key", "version": 2,
                            "type": "stored", "status": "valid", "reason": null,
                            "at": "2026-11-20T10:00:00Z", "entry_id": "entry-a",
                            "provider": "openai", "revision": null,
                            "secret": "must-not-cross"
                        }],
                        "next": 8, "more": false
                    }))
                }),
            );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let address = listener.local_addr().expect("address");
        let server = tokio::spawn(async move { axum::serve(listener, app).await });
        let client = MetadataClient::new(
            reqwest::Client::new(),
            format!("http://{address}/"),
            "metadata-only",
        );
        let metadata = client
            .read(42, Some("2026-11-20T10:00:00Z"))
            .await
            .expect("metadata");
        assert_eq!(metadata.credentials[0].list_revision, Some(3));
        assert!(!format!("{metadata:?}").contains("must-not-cross"));
        let now = DateTime::parse_from_rfc3339("2026-11-20T10:00:00Z")
            .expect("time")
            .to_utc();
        let routes = vec!["openai-api-key".to_owned()];
        assert_eq!(
            metadata
                .select("openai", "implementer", "org/repo", "default", &routes, now)
                .map(|entry| entry.record_id.as_str()),
            Some("42:openai-api-key:2")
        );
        assert!(
            metadata
                .select("openai", "planner", "org/repo", "default", &routes, now)
                .is_none()
        );
        assert!(
            metadata
                .select(
                    "openai",
                    "implementer",
                    "other/repo",
                    "default",
                    &routes,
                    now
                )
                .is_none()
        );
        let page = client.events(7).await.expect("events");
        assert_eq!(page.next, 8);
        assert!(!format!("{page:?}").contains("must-not-cross"));
        assert!(!format!("{client:?}").contains("metadata-only"));
        server.abort();
    }

    /// Refuse a mismatched owner, inconsistent cursor or secret-bearing error body.
    #[tokio::test]
    async fn rejects_wrong_owner_bad_cursor_and_remote_error_body() {
        let app = Router::new()
            .route(
                "/metadata/42",
                get(|| async {
                    Json(json!({
                        "history_complete": true,
                        "credentials": [{
                            "record_id": "wrong", "user_id": "43", "route": "openai-api-key",
                            "version": 1, "status": "valid", "models": null,
                            "created": "now", "updated": "now", "kind": "api_key",
                            "expiry_known": false, "expires_at": null
                        }],
                        "lists": {}
                    }))
                }),
            )
            .route(
                "/metadata/events",
                get(|| async { Json(json!({"events": [], "next": 10, "more": true})) }),
            )
            .route(
                "/metadata/44",
                get(|| async { (StatusCode::FORBIDDEN, Json(json!({"error": "secret-body"}))) }),
            );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let address = listener.local_addr().expect("address");
        let server = tokio::spawn(async move { axum::serve(listener, app).await });
        let client = MetadataClient::new(
            reqwest::Client::new(),
            format!("http://{address}"),
            "secret-token",
        );
        assert_eq!(
            client.read(42, None).await,
            Err(VaultError::InvalidResponse)
        );
        assert_eq!(client.events(7).await, Err(VaultError::InvalidResponse));
        let error = client.read(44, None).await.expect_err("refusal");
        assert_eq!(error, VaultError::Refused(StatusCode::FORBIDDEN));
        assert!(!format!("{error:?} {error}").contains("secret"));
        server.abort();
    }
}
