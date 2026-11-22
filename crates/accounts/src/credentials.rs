//! Owner-scoped credential and relay browser calls to the vault.

use std::time::Duration;

use axum::{
    Json,
    body::Bytes,
    extract::State,
    http::{HeaderMap, Method, StatusCode, Uri},
    response::{IntoResponse, Response},
};
use axum_extra::extract::CookieJar;
use reqwest::{Client, Url};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use serde_json::{Value, json};

use crate::{
    AccountState,
    browser::{SESSION, csrf},
    sessions::Session,
};

const STALE: &str = "Credentials are now managed as a list. Reload the page to continue.";
const ROLES: [(&str, &str); 5] = [
    ("triage", "Triage / intake"),
    ("planner", "Planning"),
    ("plan_reviewers", "Plan review"),
    ("implementer", "Implementation"),
    ("pr_reviewers", "PR review"),
];
const TYPES: [(&str, &str); 4] = [
    ("anthropic_api", "anthropic-api-key"),
    ("claude_subscription", "claude-subscription"),
    ("openai_api", "openai-api-key"),
    ("chatgpt_subscription", "chatgpt-subscription"),
];

/// HTTP vault owner adapter and public relay setup values.
#[derive(Clone)]
pub struct Credentials {
    /// Shared accounts HTTP client.
    client: Client,
    /// Owner API origin, if configured.
    url: Option<Url>,
    /// Public Sponsor setup fields.
    setup: Value,
}

impl Credentials {
    /// Validate the optional vault origin and prepare relay setup fields.
    pub fn new(
        client: Client,
        vault_url: Option<String>,
        relay_server: Option<String>,
        relay_download_url: Option<String>,
    ) -> anyhow::Result<Self> {
        let url = vault_url
            .map(|value| Url::parse(&format!("{}/", value.trim_end_matches('/'))))
            .transpose()?;
        let argument = relay_server.as_ref().map(|server| {
            if server.starts_with('[') {
                format!("'{server}'")
            } else {
                server.clone()
            }
        });
        let command = argument
            .as_ref()
            .map(|server| format!("concurro-relay --server {server}"));
        let login = argument
            .as_ref()
            .map(|server| format!("concurro-relay login --server {server}"));
        Ok(Self {
            client,
            url,
            setup: json!({"server":relay_server,"download":relay_download_url,
                "command":command,"login":login}),
        })
    }

    /// Apply the owner's token and operation-specific request deadline.
    fn request(
        &self,
        session: &Session,
        method: Method,
        path: &str,
        approval: bool,
    ) -> Result<reqwest::RequestBuilder, CredentialError> {
        let base = self.url.as_ref().ok_or_else(|| {
            CredentialError::new(
                StatusCode::SERVICE_UNAVAILABLE,
                "the credentials vault is not configured",
            )
        })?;
        let url = base
            .join(path.trim_start_matches('/'))
            .map_err(|_| CredentialError::unavailable())?;
        Ok(self
            .client
            .request(method, url)
            .bearer_auth(&session.token)
            .timeout(Duration::from_secs(if approval { 60 } else { 30 })))
    }

    /// Forward the user's bearer and JSON unchanged within a bounded owner call.
    async fn call(
        &self,
        session: &Session,
        method: Method,
        path: &str,
        body: Option<Value>,
        revision: Option<i64>,
        approval: bool,
    ) -> Result<Value, CredentialError> {
        let mut request = self.request(session, method, path, approval)?;
        if let Some(revision) = revision {
            request = request.query(&[("revision", revision)]);
        }
        if let Some(body) = body {
            request = request.json(&body);
        }
        let response = request
            .send()
            .await
            .map_err(|_| CredentialError::unavailable())?;
        let status = response.status();
        let value: Value = response.json().await.unwrap_or(Value::Null);
        if status.is_client_error() || status.is_server_error() {
            let detail = value
                .get("error")
                .and_then(Value::as_str)
                .unwrap_or("vault request failed");
            return Err(CredentialError::new(status, detail));
        }
        if value.is_null() {
            return Err(CredentialError::unavailable());
        }
        Ok(value)
    }
}

/// Safe browser error; vault errors expose only their declared message.
#[derive(Debug)]
pub struct CredentialError(StatusCode, String);

impl CredentialError {
    /// Construct a browser error without retaining a request body or token.
    fn new(status: StatusCode, detail: impl Into<String>) -> Self {
        Self(status, detail.into())
    }

    /// Represent a transport or unreadable vault response.
    fn unavailable() -> Self {
        Self::new(
            StatusCode::SERVICE_UNAVAILABLE,
            "the credentials vault is unavailable",
        )
    }

    /// Return the fixed malformed-body response.
    fn malformed() -> Self {
        Self::new(StatusCode::UNPROCESSABLE_ENTITY, "malformed request")
    }
}

impl IntoResponse for CredentialError {
    fn into_response(self) -> Response {
        (self.0, Json(json!({"detail":self.1}))).into_response()
    }
}

/// Resolve a browser session before any credential body is parsed.
fn session(state: &AccountState, jar: &CookieJar) -> Result<Session, CredentialError> {
    state
        .sessions
        .get(jar.get(SESSION).map(|cookie| cookie.value()))
        .ok_or_else(|| CredentialError::new(StatusCode::UNAUTHORIZED, "sign in with GitHub"))
}

/// Apply the existing browser CSRF check to a vault mutation.
fn write_header(headers: &HeaderMap) -> Result<(), CredentialError> {
    if csrf(headers) {
        Ok(())
    } else {
        Err(CredentialError::new(
            StatusCode::FORBIDDEN,
            "missing request header",
        ))
    }
}

/// Parse a typed browser body without returning supplied fields in failures.
fn parse<T: DeserializeOwned>(bytes: &[u8]) -> Result<T, CredentialError> {
    serde_json::from_slice(bytes).map_err(|_| CredentialError::malformed())
}

/// Convert a typed body to the vault's JSON wire shape.
fn wire<T: Serialize>(value: T) -> Result<Value, CredentialError> {
    serde_json::to_value(value).map_err(|_| CredentialError::malformed())
}

/// Reject unknown role filters before a write could silently match no work.
fn policy(roles: Option<&[String]>) -> Result<(), CredentialError> {
    if roles.is_some_and(|roles| {
        roles
            .iter()
            .any(|role| !ROLES.iter().any(|(key, _)| key == role))
    }) {
        Err(CredentialError::new(
            StatusCode::UNPROCESSABLE_ENTITY,
            "unknown role filter",
        ))
    } else {
        Ok(())
    }
}

/// One target and profile filter sent to the vault.
#[derive(Deserialize, Serialize)]
struct ProfileFilter {
    /// Target repository name.
    target: String,
    /// Selected profile key.
    profile: String,
}

/// Browser fields for adding a write-only credential.
#[derive(Deserialize, Serialize)]
struct AddBody {
    /// Frontend type identifier.
    #[serde(rename = "type")]
    kind: String,
    /// Owner-visible entry name.
    name: String,
    /// Optional role filters.
    #[serde(default)]
    roles: Vec<String>,
    /// Optional target profile filters.
    #[serde(default)]
    profiles: Vec<ProfileFilter>,
    /// Write-only candidate.
    secret: String,
    /// Optional supplied account header.
    #[serde(skip_serializing_if = "Option::is_none")]
    account_id: Option<String>,
    /// Vault-owned idempotency key.
    mutation_id: String,
}

/// Browser changes to one existing credential entry.
#[derive(Deserialize, Serialize)]
struct EditBody {
    /// Entry revision last read by the browser.
    revision: i64,
    /// Vault-owned idempotency key.
    mutation_id: String,
    /// Optional replacement name.
    #[serde(skip_serializing_if = "Option::is_none")]
    name: Option<String>,
    /// Optional enabled state.
    #[serde(skip_serializing_if = "Option::is_none")]
    enabled: Option<bool>,
    /// Optional replacement role filters.
    #[serde(skip_serializing_if = "Option::is_none")]
    roles: Option<Vec<String>>,
    /// Optional replacement target profile filters.
    #[serde(skip_serializing_if = "Option::is_none")]
    profiles: Option<Vec<ProfileFilter>>,
}

/// Browser's replacement secret and current entry revision.
#[derive(Deserialize, Serialize)]
struct SecretBody {
    /// Entry revision last read by the browser.
    revision: i64,
    /// Write-only replacement candidate.
    secret: String,
    /// Optional supplied account header.
    #[serde(skip_serializing_if = "Option::is_none")]
    account_id: Option<String>,
    /// Vault-owned idempotency key.
    mutation_id: String,
}

/// Browser's exact ordered entry IDs at one provider revision.
#[derive(Deserialize, Serialize)]
struct OrderBody {
    /// Provider list revision last read.
    revision: i64,
    /// Entry IDs in requested order.
    entries: Vec<String>,
    /// Vault-owned idempotency key.
    mutation_id: String,
}

/// One Sponsor code used for a relay preview.
#[derive(Deserialize, Serialize)]
struct CodeBody {
    /// Displayed Sponsor device code.
    code: String,
}

/// Browser approval of a Sponsor device and route set.
#[derive(Deserialize, Serialize)]
struct RelayBody {
    /// Displayed Sponsor device code.
    code: String,
    /// Exact registered routes to approve.
    routes: Vec<String>,
    /// Owner-visible device name.
    name: String,
    /// Optional role filters.
    #[serde(default)]
    roles: Vec<String>,
    /// Optional target profile filters.
    #[serde(default)]
    profiles: Vec<ProfileFilter>,
    /// Vault-owned idempotency key.
    mutation_id: String,
}

/// One previously read entry to rebind to a Sponsor device.
#[derive(Deserialize)]
struct DeviceEntry {
    /// Existing browser entry ID.
    id: String,
    /// Entry revision last read.
    revision: i64,
}

/// Browser device replacement with one entry per route.
#[derive(Deserialize)]
struct DeviceBody {
    /// New displayed Sponsor device code.
    code: String,
    /// Existing entry for each registered route.
    entries: Vec<DeviceEntry>,
    /// Vault-owned idempotency key.
    mutation_id: String,
}

/// Map one vault entry to the frontend's stable credential shape.
fn entry(value: &Value) -> Value {
    let record = &value["record"];
    let kind = TYPES
        .iter()
        .find(|(_, route)| Some(*route) == value["route"].as_str())
        .map(|(kind, _)| *kind);
    let status = if record["status_reason"] == "expired" {
        "expired"
    } else {
        match record["status"].as_str() {
            Some("valid") => "valid",
            Some("revoked") => "revoked",
            _ => "invalid",
        }
    };
    json!({"id":value["entry_id"],"provider":value["provider"],"type":kind,
        "kind":value["kind"],"name":value["name"],"enabled":value["enabled"],
        "roles":value["roles"],"profiles":value["profiles"],"revision":value["revision"],
        "removed":!value["removed"].is_null(),"created":value["created"],"status":status,
        "statusReason":record["status_reason"],"fingerprint":record["fingerprint"],
        "version":record["version"],"lastUpdated":record["updated"],"lastUsed":record["last_used"],
        "expiresAt":record["expires_at"],"expiryKnown":record["expiry_known"].as_bool().unwrap_or(false),
        "accountLabel":record["account_label"],"accountVerified":record["verified_account"].as_bool().unwrap_or(false),
        "custody":record.get("custody").unwrap_or(&json!("vault")),"relay":record["relay_id"]})
}

/// Map one provider's ordered list and revision to the frontend.
fn group(value: &Value) -> Value {
    let label = match value["provider"].as_str() {
        Some("anthropic") => "Anthropic",
        Some("openai") => "OpenAI",
        _ => "",
    };
    let entries = value["entries"]
        .as_array()
        .map(|items| items.iter().map(entry).collect::<Vec<_>>())
        .unwrap_or_default();
    json!({"provider":value["provider"],"label":label,"revision":value["revision"],"entries":entries})
}

/// Map vault type and role choices alongside both provider lists.
fn listing(value: &Value) -> Value {
    let groups = value["providers"]
        .as_array()
        .map(|groups| groups.iter().map(group).collect::<Vec<_>>())
        .unwrap_or_default();
    let types = TYPES
        .iter()
        .flat_map(|(kind, route)| {
            value["types"]
                .as_array()
                .into_iter()
                .flatten()
                .filter(move |item| item["route"] == *route)
                .map(move |item| {
                    json!({"id":kind,"provider":item["provider"],"kind":item["kind"],
                "label":item["label"],"accountId":*route == "chatgpt-subscription"})
                })
        })
        .collect::<Vec<_>>();
    let roles = ROLES
        .iter()
        .map(|(key, label)| json!({"key":key,"label":label}))
        .collect::<Vec<_>>();
    json!({"providers":groups,"types":types,"roles":roles})
}

/// Map relay lease task references and telemetry to the frontend.
fn relay_status(value: &Value) -> Value {
    let relays = value["relays"]
        .as_array()
        .map(|relays| {
            relays.iter().map(|relay| {
        let active = relay["active"].as_array().map(|leases| leases.iter().map(|lease| {
            let task = lease["task"].as_object().and_then(|task| {
                task.get("repository")?.as_str()?.split_once('/').map(|(owner, repo)|
                    json!({"owner":owner,"repo":repo,"issue":task.get("issue")}))
            });
            json!({"slot":lease["slot"],"entry":lease["entry_id"],"route":lease["route"],
                "version":lease["version"],"since":lease["since"],"task":task})
        }).collect::<Vec<_>>()).unwrap_or_default();
        json!({"relay":relay["relay"],"idle":relay["idle"],"lastSeen":relay["last_seen"],
            "active":active})
    }).collect::<Vec<_>>()
        })
        .unwrap_or_default();
    json!({"service":value["service"],"started":value.get("started"),
        "checked":value.get("checked"),"relays":relays})
}

/// Convert one vault call, ending the local session if the owner token was refused.
async fn call(
    state: &AccountState,
    session: &Session,
    method: Method,
    path: &str,
    body: Option<Value>,
    revision: Option<i64>,
    approval: bool,
) -> Result<Value, CredentialError> {
    let credentials = state
        .credentials
        .as_ref()
        .ok_or_else(CredentialError::unavailable)?;
    let result = credentials
        .call(session, method, path, body, revision, approval)
        .await;
    if matches!(&result, Err(CredentialError(StatusCode::UNAUTHORIZED, _))) {
        state.sessions.end(Some(&session.id));
    }
    result
}

/// Serve the supported browser credential list and mutation routes.
pub async fn credentials(
    State(state): State<AccountState>,
    method: Method,
    uri: Uri,
    headers: HeaderMap,
    jar: CookieJar,
    body: Bytes,
) -> Result<Json<Value>, CredentialError> {
    let session = session(&state, &jar)?;
    if method != Method::GET {
        write_header(&headers)?;
    }
    let path = uri.path().strip_prefix("/api/me/credentials").unwrap_or("");
    let segments = path.trim_start_matches('/').split('/').collect::<Vec<_>>();
    let (vault_method, vault_path, vault_body, revision, approval, mapping) = match (
        method,
        segments.as_slice(),
    ) {
        (Method::GET, [""]) => (
            Method::GET,
            "/credentials".to_owned(),
            None,
            None,
            false,
            "listing",
        ),
        (Method::POST, [""]) => {
            let input: AddBody = parse(&body)?;
            policy(Some(&input.roles))?;
            let route = TYPES
                .iter()
                .find(|(kind, _)| *kind == input.kind)
                .map(|(_, route)| *route)
                .ok_or_else(|| {
                    CredentialError::new(
                        StatusCode::UNPROCESSABLE_ENTITY,
                        "this credential type can't be stored",
                    )
                })?;
            let mut value = wire(input)?;
            value.as_object_mut().unwrap().remove("type");
            value["route"] = json!(route);
            (
                Method::POST,
                "/credentials".to_owned(),
                Some(value),
                None,
                false,
                "entry",
            )
        }
        (Method::POST, ["relay", "pending"]) => {
            let input: CodeBody = parse(&body)?;
            (
                Method::POST,
                "/credentials/relay/pending".to_owned(),
                Some(wire(input)?),
                None,
                false,
                "raw",
            )
        }
        (Method::POST, ["relay"]) => {
            let input: RelayBody = parse(&body)?;
            if !(1..=2).contains(&input.routes.len()) {
                return Err(CredentialError::malformed());
            }
            policy(Some(&input.roles))?;
            (
                Method::POST,
                "/credentials/relay".to_owned(),
                Some(wire(input)?),
                None,
                true,
                "entries",
            )
        }
        (Method::PUT, ["relay", "device"]) => {
            let input: DeviceBody = parse(&body)?;
            if !(1..=2).contains(&input.entries.len()) {
                return Err(CredentialError::malformed());
            }
            let entries = input
                .entries
                .into_iter()
                .map(|item| json!({"entry_id":item.id,"revision":item.revision}))
                .collect::<Vec<_>>();
            (
                Method::PUT,
                "/credentials/relay/device".to_owned(),
                Some(json!({"code":input.code,"entries":entries,"mutation_id":input.mutation_id})),
                None,
                true,
                "entries",
            )
        }
        (Method::PUT, ["order", provider]) => {
            let input: OrderBody = parse(&body)?;
            (
                Method::PUT,
                format!("/credentials/order/{provider}"),
                Some(wire(input)?),
                None,
                false,
                "group",
            )
        }
        (Method::PATCH, [entry_id]) => {
            let input: EditBody = parse(&body)?;
            policy(input.roles.as_deref())?;
            (
                Method::PATCH,
                format!("/credentials/{entry_id}"),
                Some(wire(input)?),
                None,
                false,
                "entry",
            )
        }
        (Method::PUT, [entry_id, "secret"]) => {
            let input: SecretBody = parse(&body)?;
            (
                Method::PUT,
                format!("/credentials/{entry_id}/secret"),
                Some(wire(input)?),
                None,
                false,
                "entry",
            )
        }
        (Method::DELETE, [entry_id]) => {
            let revision = uri
                .query()
                .and_then(|query| {
                    query
                        .split('&')
                        .find_map(|pair| pair.strip_prefix("revision="))
                })
                .and_then(|value| value.parse::<i64>().ok())
                .ok_or_else(|| CredentialError::new(StatusCode::GONE, STALE))?;
            (
                Method::DELETE,
                format!("/credentials/{entry_id}"),
                None,
                Some(revision),
                false,
                "entry",
            )
        }
        _ => return Err(CredentialError::new(StatusCode::NOT_FOUND, "not found")),
    };
    let value = call(
        &state,
        &session,
        vault_method,
        &vault_path,
        vault_body,
        revision,
        approval,
    )
    .await?;
    let value = match mapping {
        "listing" => listing(&value),
        "entry" => entry(&value),
        "group" => group(&value),
        "entries" => {
            let entries = if let Some(entries) = value.get("entries").and_then(Value::as_object) {
                entries
                    .iter()
                    .map(|(route, value)| (route.clone(), entry(value)))
                    .collect::<serde_json::Map<_, _>>()
            } else if let Some(route) = value.get("route").and_then(Value::as_str) {
                serde_json::Map::from_iter([(route.to_owned(), entry(&value))])
            } else {
                serde_json::Map::new()
            };
            json!({"entries":entries})
        }
        _ => value,
    };
    Ok(Json(value))
}

/// Return relay setup and owner link telemetry, including offline status.
pub async fn relays(
    State(state): State<AccountState>,
    jar: CookieJar,
) -> Result<Json<Value>, CredentialError> {
    let session = session(&state, &jar)?;
    let credentials = state
        .credentials
        .as_ref()
        .ok_or_else(CredentialError::unavailable)?;
    let status = match call(
        &state,
        &session,
        Method::GET,
        "/credentials/relay/status",
        None,
        None,
        false,
    )
    .await
    {
        Ok(value) => relay_status(&value),
        Err(CredentialError(StatusCode::SERVICE_UNAVAILABLE, _)) => {
            json!({"service":"unavailable","relays":[]})
        }
        Err(error) => return Err(error),
    };
    let mut result = status.as_object().cloned().unwrap_or_default();
    result.insert("setup".to_owned(), credentials.setup.clone());
    Ok(Json(Value::Object(result)))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{github::User, sessions::Sessions};

    /// Relay approvals and device replacements retain Python's 60-second deadline.
    #[test]
    fn approval_deadline() {
        let credentials = Credentials::new(
            Client::new(),
            Some("http://vault.invalid".to_owned()),
            None,
            None,
        )
        .unwrap();
        let session = Sessions::new().create(
            "synthetic-token".to_owned(),
            User {
                id: 1,
                login: "owner".to_owned(),
                name: None,
                avatar_url: None,
            },
            3600,
        );
        for (path, approval, seconds) in [
            ("/credentials", false, 30),
            ("/credentials/relay", true, 60),
            ("/credentials/relay/device", true, 60),
        ] {
            let request = credentials
                .request(&session, Method::POST, path, approval)
                .unwrap()
                .build()
                .unwrap();
            assert_eq!(request.timeout(), Some(&Duration::from_secs(seconds)));
            assert_eq!(request.headers()["authorization"], "Bearer synthetic-token");
        }
        let fixture: Value =
            serde_json::from_str(include_str!("../tests/credentials_python.json")).unwrap();
        let ipv6 = Credentials::new(
            Client::new(),
            Some("http://vault.invalid".to_owned()),
            Some("[::1]:80".to_owned()),
            Some("https://example.invalid/relay".to_owned()),
        )
        .unwrap();
        assert_eq!(ipv6.setup, fixture["relay_setup_ipv6"]);
    }
}
