//! The Python vault's owner API, reached with the owner's GitHub token.

use std::time::Duration;

use axum::http::{Method, StatusCode};
use reqwest::{Client, Url};
use serde_json::{Value, json};

use super::{CredentialError, Operation, wire};
use crate::sessions::Session;

/// HTTP adapter for the vault owner API.
#[derive(Clone)]
pub struct Vault {
    /// Shared accounts HTTP client.
    client: Client,
    /// Owner API origin, if configured.
    url: Option<Url>,
}

/// One vault request: method, path, body, `revision` query and approval deadline.
struct Request {
    method: Method,
    path: String,
    body: Option<Value>,
    revision: Option<i64>,
    approval: bool,
}

impl Vault {
    /// Validate the optional vault origin.
    pub fn new(client: Client, url: Option<String>) -> anyhow::Result<Self> {
        let url = url
            .map(|value| Url::parse(&format!("{}/", value.trim_end_matches('/'))))
            .transpose()?;
        Ok(Self { client, url })
    }

    /// Run one owner operation through the vault.
    pub async fn run(
        &self,
        session: &Session,
        operation: Operation,
    ) -> Result<Value, CredentialError> {
        let request = translate(operation)?;
        self.call(session, request).await
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
    async fn call(&self, session: &Session, request: Request) -> Result<Value, CredentialError> {
        let mut builder = self.request(session, request.method, &request.path, request.approval)?;
        if let Some(revision) = request.revision {
            builder = builder.query(&[("revision", revision)]);
        }
        if let Some(body) = request.body {
            builder = builder.json(&body);
        }
        let response = builder
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

/// The vault request for one operation.
fn translate(operation: Operation) -> Result<Request, CredentialError> {
    let request = |method, path: String, body, approval| Request {
        method,
        path,
        body,
        revision: None,
        approval,
    };
    Ok(match operation {
        Operation::List => request(Method::GET, "/credentials".to_owned(), None, false),
        Operation::RelayStatus => request(
            Method::GET,
            "/credentials/relay/status".to_owned(),
            None,
            false,
        ),
        Operation::Add { route, body } => {
            let mut value = wire(body)?;
            value.as_object_mut().expect("object body").remove("type");
            value["route"] = json!(route);
            request(Method::POST, "/credentials".to_owned(), Some(value), false)
        }
        Operation::RelayPending(body) => request(
            Method::POST,
            "/credentials/relay/pending".to_owned(),
            Some(wire(body)?),
            false,
        ),
        Operation::Relay(body) => request(
            Method::POST,
            "/credentials/relay".to_owned(),
            Some(wire(body)?),
            true,
        ),
        Operation::Device(body) => {
            let entries = body
                .entries
                .into_iter()
                .map(|item| json!({"entry_id":item.id,"revision":item.revision}))
                .collect::<Vec<_>>();
            request(
                Method::PUT,
                "/credentials/relay/device".to_owned(),
                Some(json!({"code":body.code,"entries":entries,"mutation_id":body.mutation_id})),
                true,
            )
        }
        Operation::Reorder { provider, body } => request(
            Method::PUT,
            format!("/credentials/order/{provider}"),
            Some(wire(body)?),
            false,
        ),
        Operation::Edit { entry_id, body } => request(
            Method::PATCH,
            format!("/credentials/{entry_id}"),
            Some(wire(body)?),
            false,
        ),
        Operation::Replace { entry_id, body } => request(
            Method::PUT,
            format!("/credentials/{entry_id}/secret"),
            Some(wire(body)?),
            false,
        ),
        Operation::Remove { entry_id, revision } => Request {
            revision: Some(revision),
            ..request(
                Method::DELETE,
                format!("/credentials/{entry_id}"),
                None,
                false,
            )
        },
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{github::User, sessions::Sessions};

    /// Relay approvals and device replacements retain Python's 60-second deadline.
    #[test]
    fn approval_deadline() {
        let vault = Vault::new(Client::new(), Some("http://vault.invalid".to_owned())).unwrap();
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
            let request = vault
                .request(&session, Method::POST, path, approval)
                .unwrap()
                .build()
                .unwrap();
            assert_eq!(request.timeout(), Some(&Duration::from_secs(seconds)));
            assert_eq!(request.headers()["authorization"], "Bearer synthetic-token");
        }
    }
}
