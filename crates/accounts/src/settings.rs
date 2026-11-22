//! File-backed accounts settings and public URL conventions.

use std::net::Ipv6Addr;

use anyhow::{Context, Result, ensure};
use concurro_config::Validate;
use concurro_secrets::OpenBaoSettings;
use reqwest::Url;
use serde::Deserialize;

/// Accounts listener, OAuth, GitHub App and logout storage configuration.
#[derive(Deserialize)]
pub struct Settings {
    /// Browser listener on the edge network.
    pub listen: String,
    /// Viewer-only listener on the platform network.
    pub internal_listen: String,
    /// Accounts PostgreSQL URL file.
    pub database_url_file: String,
    /// AES-256 logout key file, encoded as 64 hex characters.
    pub logout_key_file: String,
    /// Externally routed base URL, including its path.
    pub public_url: String,
    /// Sign-in OAuth App client ID.
    pub oauth_client_id: String,
    /// Sign-in OAuth App client secret file.
    pub oauth_client_secret_file: String,
    /// Optional OAuth authorization URL for local integration tests.
    pub oauth_authorize_url: Option<String>,
    /// Optional OAuth token URL for local integration tests.
    pub oauth_token_url: Option<String>,
    /// GitHub REST API origin.
    pub github_api_base: String,
    /// Read-only Backoffice App ID.
    pub reader_app_id: i64,
    /// Read-only Backoffice App private key file.
    pub reader_private_key_file: String,
    /// Private RSA PEM for browser command proofs.
    pub command_proof_private_key_file: String,
    /// Service bearer file for the internal viewer route.
    pub viewer_authority_bearer_file: String,
    /// Private task application origin for target snapshots.
    pub task_api_url: String,
    /// Private vault owner API for browser credential and relay calls.
    pub vault_url: Option<String>,
    /// Optional public host:port advertised to Sponsor relays.
    pub relay_server: Option<String>,
    /// Optional HTTPS download URL shown to owners.
    pub relay_download_url: Option<String>,
    /// The native credential store, its metadata API and maintenance; absent until it is
    /// deployed.
    pub credentials: Option<CredentialSettings>,
    /// Event publication for credential change facts; required with `credentials`.
    pub events: Option<EventSettings>,
}

/// Native credential store settings.
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CredentialSettings {
    /// Which store serves the browser's credential routes.
    #[serde(default)]
    pub store: CredentialStore,
    /// OpenBao, where version material is created.
    pub openbao: OpenBaoSettings,
    /// Bearer file the task application presents to read credential metadata.
    pub metadata_bearer_file: String,
    /// The relay verifier's owner API, for Sponsor approval and status.
    pub verifier: Option<VerifierSettings>,
}

/// The relay verifier's owner API.
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VerifierSettings {
    /// Owner API origin on the credential-private network.
    pub url: String,
    /// File holding the `owner` bearer.
    pub owner_token_file: String,
}

/// The store behind the browser's credential routes.
#[derive(Clone, Copy, Debug, Default, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum CredentialStore {
    /// The Python vault's owner API, at `vault_url`.
    #[default]
    Vault,
    /// Accounts' native store.
    Native,
}

/// The accounts event stream's NATS connection and outbox limits.
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EventSettings {
    /// NATS server URL.
    pub nats_url: String,
    /// Optional NATS credentials file.
    pub nats_credentials_file: Option<String>,
    /// Maximum event size accepted to the outbox.
    #[serde(default = "default_max_message_bytes")]
    pub max_message_bytes: usize,
    /// How long published outbox rows are kept.
    #[serde(default = "default_published_retention_seconds")]
    pub published_retention_seconds: u64,
    /// Idle outbox poll interval.
    #[serde(default = "default_forwarder_poll_ms")]
    pub forwarder_poll_ms: u64,
    /// Failed applications of a consumed event before it is parked.
    #[serde(default = "default_consumer_max_attempts")]
    pub consumer_max_attempts: i32,
    /// How often held consumed events are retried.
    #[serde(default = "default_consumer_poll_ms")]
    pub consumer_poll_ms: u64,
}

fn default_consumer_max_attempts() -> i32 {
    25
}

fn default_consumer_poll_ms() -> u64 {
    1000
}

fn default_max_message_bytes() -> usize {
    1024 * 1024
}

fn default_published_retention_seconds() -> u64 {
    7 * 24 * 3600
}

fn default_forwarder_poll_ms() -> u64 {
    1000
}

impl Validate for Settings {
    /// Require a parseable external URL and two distinct listener addresses.
    fn validate(&self) -> Result<()> {
        let url = Url::parse(&self.public_url).context("accounts public_url is invalid")?;
        ensure!(
            matches!(url.scheme(), "http" | "https") && url.host().is_some(),
            "accounts public_url must be HTTP(S)"
        );
        ensure!(
            self.listen != self.internal_listen,
            "accounts listeners must be separate"
        );
        ensure!(
            self.credentials.is_some() == self.events.is_some(),
            "accounts credentials and events must be configured together"
        );
        if let Some(events) = &self.events {
            ensure!(
                events.max_message_bytes > 0
                    && events.forwarder_poll_ms > 0
                    && events.consumer_max_attempts > 0
                    && events.consumer_poll_ms > 0,
                "accounts event limits must be positive"
            );
        }
        if let Some(server) = self
            .relay_server
            .as_deref()
            .filter(|value| !value.is_empty())
        {
            ensure!(
                relay_address(server),
                "accounts relay_server must be host:port"
            );
        }
        if let Some(download) = self
            .relay_download_url
            .as_deref()
            .filter(|value| !value.is_empty())
        {
            let url = Url::parse(download).context("accounts relay_download_url is invalid")?;
            ensure!(
                url.scheme() == "https"
                    && url.host().is_some()
                    && !download.chars().any(char::is_whitespace),
                "accounts relay_download_url must be HTTPS"
            );
        }
        Ok(())
    }
}

/// Match the portal's DNS/IPv4 or bracketed IPv6 address with an explicit port.
fn relay_address(value: &str) -> bool {
    let Some((host, port)) = value.rsplit_once(':') else {
        return false;
    };
    if port.is_empty()
        || !port.bytes().all(|byte| byte.is_ascii_digit())
        || !port.parse::<u16>().is_ok_and(|port| port > 0)
    {
        return false;
    }
    if let Some(ipv6) = host
        .strip_prefix('[')
        .and_then(|host| host.strip_suffix(']'))
    {
        return ipv6.parse::<Ipv6Addr>().is_ok();
    }
    !host.is_empty()
        && host.len() <= 253
        && host.split('.').all(|label| {
            !label.is_empty()
                && label.len() <= 63
                && label
                    .as_bytes()
                    .first()
                    .is_some_and(u8::is_ascii_alphanumeric)
                && label
                    .as_bytes()
                    .last()
                    .is_some_and(u8::is_ascii_alphanumeric)
                && label
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
        })
}

impl Settings {
    /// Public application path used as the cookie path and redirect prefix.
    pub fn base_path(&self) -> String {
        Url::parse(&self.public_url)
            .expect("validated public URL")
            .path()
            .trim_end_matches('/')
            .to_owned()
    }

    /// Exact OAuth callback URL under the public base.
    pub fn callback_url(&self) -> String {
        format!("{}/auth/callback", self.public_url.trim_end_matches('/'))
    }

    /// Whether the native credential store serves the browser routes and runs its
    /// maintenance (settlement and expiry). Until then a vault import may be in
    /// progress, which maintenance must not change.
    pub fn native_store_active(&self) -> bool {
        self.credentials
            .as_ref()
            .is_some_and(|credentials| credentials.store == CredentialStore::Native)
    }

    /// Whether browsers require TLS for issued cookies.
    pub fn secure_cookies(&self) -> bool {
        self.public_url.starts_with("https://")
    }
}

#[cfg(test)]
mod tests {
    use super::{Settings, relay_address};

    /// Settings with optional credential and event sections.
    fn settings(credentials: Option<serde_json::Value>) -> Settings {
        let mut value = serde_json::json!({
            "listen": "0.0.0.0:1", "internal_listen": "0.0.0.0:2", "database_url_file": "x",
            "logout_key_file": "x", "public_url": "https://example.invalid",
            "oauth_client_id": "x", "oauth_client_secret_file": "x",
            "github_api_base": "https://api.github.com", "reader_app_id": 1,
            "reader_private_key_file": "x", "command_proof_private_key_file": "x",
            "viewer_authority_bearer_file": "x", "task_api_url": "http://tasks.invalid",
        });
        if let Some(mut credentials) = credentials {
            credentials["metadata_bearer_file"] = "x".into();
            credentials["openbao"] = serde_json::json!({"url": "http://openbao.invalid",
                "role_id": "r", "secret_id_file": "s"});
            value["credentials"] = credentials;
            value["events"] = serde_json::json!({"nats_url": "nats://nats.invalid"});
        }
        serde_json::from_value(value).unwrap()
    }

    /// The native store, and its maintenance, stay inactive unless selected, so a vault
    /// import in progress isn't changed by settlement or expiry.
    #[test]
    fn native_store_activation() {
        use serde_json::json;
        assert!(!settings(None).native_store_active());
        assert!(!settings(Some(json!({}))).native_store_active());
        assert!(!settings(Some(json!({"store": "vault"}))).native_store_active());
        assert!(settings(Some(json!({"store": "native"}))).native_store_active());
    }

    /// Match Python's configured relay address grammar, including default HTTP port 80.
    #[test]
    fn relay_address_parity() {
        for value in [
            "relay.invalid:7443",
            "relay.invalid:80",
            "127.0.0.1:80",
            "[::1]:80",
            "[2001:db8::1]:7443",
            "relay.invalid:00080",
        ] {
            assert!(relay_address(value), "{value}");
        }
        for value in [
            "relay.invalid",
            "relay.invalid:0",
            "relay.invalid:65536",
            " relay.invalid:80",
            "relay.invalid:80 ",
            "relay_name:80",
            "relay;bad:80",
            "relay$(bad):80",
            "::1:80",
            "[::1]:+80",
        ] {
            assert!(!relay_address(value), "{value}");
        }
    }
}
