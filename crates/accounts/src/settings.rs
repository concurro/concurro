//! File-backed accounts settings and public URL conventions.

use std::net::Ipv6Addr;

use anyhow::{Context, Result, ensure};
use concurro_config::Validate;
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

    /// Whether browsers require TLS for issued cookies.
    pub fn secure_cookies(&self) -> bool {
        self.public_url.starts_with("https://")
    }
}

#[cfg(test)]
mod tests {
    use super::relay_address;

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
