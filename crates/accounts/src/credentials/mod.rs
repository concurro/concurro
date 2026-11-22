//! Owner-scoped credential and relay browser routes, served by the Python vault or by
//! accounts' native store.

mod jobs;
mod native;
#[cfg(test)]
pub mod tests;
mod vault;
pub mod view;

use axum::{
    Json,
    body::Bytes,
    extract::State,
    http::{HeaderMap, Method, StatusCode, Uri},
    response::{IntoResponse, Response},
};
use axum_extra::extract::CookieJar;
use reqwest::Client;
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use serde_json::{Value, json};

pub use jobs::{DestroyMaterialJob, UsageHandler, run_maintenance};
pub use native::{Clock, Native, Outcome, Verifier, material_path, read_metadata};
pub use vault::Vault;

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

/// Which store serves the browser's credential routes.
#[derive(Clone)]
pub enum Backend {
    /// The Python vault's owner API.
    Vault(Vault),
    /// Accounts' native store.
    Native(Native),
}

/// The credential store and public relay setup values.
#[derive(Clone)]
pub struct Credentials {
    backend: Backend,
    /// Public Sponsor setup fields.
    setup: Value,
}

impl Credentials {
    /// Serve the browser routes from the vault at `vault_url`.
    pub fn new(
        client: Client,
        vault_url: Option<String>,
        relay_server: Option<String>,
        relay_download_url: Option<String>,
    ) -> anyhow::Result<Self> {
        Ok(Self::with_backend(
            Backend::Vault(Vault::new(client, vault_url)?),
            relay_server,
            relay_download_url,
        ))
    }

    /// Serve the browser routes from `backend`, with relay setup fields.
    pub fn with_backend(
        backend: Backend,
        relay_server: Option<String>,
        relay_download_url: Option<String>,
    ) -> Self {
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
        Self {
            backend,
            setup: json!({"server":relay_server,"download":relay_download_url,
                "command":command,"login":login}),
        }
    }

    /// Run one owner operation against the configured store.
    async fn run(&self, session: &Session, operation: Operation) -> Result<Value, CredentialError> {
        match &self.backend {
            Backend::Vault(vault) => vault.run(session, operation).await,
            Backend::Native(native) => native.run(&session.user, operation).await,
        }
    }
}

/// One owner operation, decoded from a browser route.
pub enum Operation {
    List,
    Add { route: &'static str, body: AddBody },
    RelayPending(CodeBody),
    Relay(RelayBody),
    Device(DeviceBody),
    Reorder { provider: String, body: OrderBody },
    Edit { entry_id: String, body: EditBody },
    Replace { entry_id: String, body: SecretBody },
    Remove { entry_id: String, revision: i64 },
    RelayStatus,
}

impl Operation {
    /// The browser mapping of this operation's answer.
    fn mapping(&self) -> &'static str {
        match self {
            Self::List => "listing",
            Self::Add { .. } | Self::Edit { .. } | Self::Replace { .. } | Self::Remove { .. } => {
                "entry"
            }
            Self::Reorder { .. } => "group",
            Self::Relay(_) | Self::Device(_) => "entries",
            Self::RelayPending(_) | Self::RelayStatus => "raw",
        }
    }
}

/// Safe browser error; vault errors expose only their declared message.
#[derive(Debug)]
pub struct CredentialError(StatusCode, String);

impl CredentialError {
    /// Construct a browser error without retaining a request body or token.
    pub fn new(status: StatusCode, detail: impl Into<String>) -> Self {
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
#[derive(Clone, Deserialize, PartialEq, Serialize)]
pub struct ProfileFilter {
    /// Target repository name.
    target: String,
    /// Selected profile key.
    profile: String,
}

/// Browser fields for adding a write-only credential.
#[derive(Deserialize, Serialize)]
pub struct AddBody {
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
pub struct EditBody {
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
pub struct SecretBody {
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
pub struct OrderBody {
    /// Provider list revision last read.
    revision: i64,
    /// Entry IDs in requested order.
    entries: Vec<String>,
    /// Vault-owned idempotency key.
    mutation_id: String,
}

/// One Sponsor code used for a relay preview.
#[derive(Deserialize, Serialize)]
pub struct CodeBody {
    /// Displayed Sponsor device code.
    code: String,
}

/// Browser approval of a Sponsor device and route set.
#[derive(Deserialize, Serialize)]
pub struct RelayBody {
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
pub struct DeviceEntry {
    /// Existing browser entry ID.
    id: String,
    /// Entry revision last read.
    revision: i64,
}

/// Browser device replacement with one entry per route.
#[derive(Deserialize)]
pub struct DeviceBody {
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

/// Run one operation, ending the local session if the vault refused the owner token.
async fn call(
    state: &AccountState,
    session: &Session,
    operation: Operation,
) -> Result<Value, CredentialError> {
    let credentials = state
        .credentials
        .as_ref()
        .ok_or_else(CredentialError::unavailable)?;
    let result = credentials.run(session, operation).await;
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
    let operation = match (method, segments.as_slice()) {
        (Method::GET, [""]) => Operation::List,
        (Method::POST, [""]) => {
            let body: AddBody = parse(&body)?;
            policy(Some(&body.roles))?;
            let route = TYPES
                .iter()
                .find(|(kind, _)| *kind == body.kind)
                .map(|(_, route)| *route)
                .ok_or_else(|| {
                    CredentialError::new(
                        StatusCode::UNPROCESSABLE_ENTITY,
                        "this credential type can't be stored",
                    )
                })?;
            Operation::Add { route, body }
        }
        (Method::POST, ["relay", "pending"]) => Operation::RelayPending(parse(&body)?),
        (Method::POST, ["relay"]) => {
            let body: RelayBody = parse(&body)?;
            if !(1..=2).contains(&body.routes.len()) {
                return Err(CredentialError::malformed());
            }
            policy(Some(&body.roles))?;
            Operation::Relay(body)
        }
        (Method::PUT, ["relay", "device"]) => {
            let body: DeviceBody = parse(&body)?;
            if !(1..=2).contains(&body.entries.len()) {
                return Err(CredentialError::malformed());
            }
            Operation::Device(body)
        }
        (Method::PUT, ["order", provider]) => Operation::Reorder {
            provider: (*provider).to_owned(),
            body: parse(&body)?,
        },
        (Method::PATCH, [entry_id]) => {
            let body: EditBody = parse(&body)?;
            policy(body.roles.as_deref())?;
            Operation::Edit {
                entry_id: (*entry_id).to_owned(),
                body,
            }
        }
        (Method::PUT, [entry_id, "secret"]) => Operation::Replace {
            entry_id: (*entry_id).to_owned(),
            body: parse(&body)?,
        },
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
            Operation::Remove {
                entry_id: (*entry_id).to_owned(),
                revision,
            }
        }
        _ => return Err(CredentialError::new(StatusCode::NOT_FOUND, "not found")),
    };
    let mapping = operation.mapping();
    let value = call(&state, &session, operation).await?;
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
    let status = match call(&state, &session, Operation::RelayStatus).await {
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
