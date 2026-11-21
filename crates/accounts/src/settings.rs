//! File-backed accounts settings and public URL conventions.

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
    /// GitHub REST API origin.
    pub github_api_base: String,
    /// Read-only Backoffice App ID.
    pub reader_app_id: i64,
    /// Read-only Backoffice App private key file.
    pub reader_private_key_file: String,
    /// Reserved for the command slice's proof signer.
    pub command_proof_private_key_file: Option<String>,
    /// Service bearer file for the internal viewer route.
    pub viewer_authority_bearer_file: String,
    /// Private task application origin for target snapshots.
    pub task_api_url: String,
    /// Reserved for the credentials slice's vault owner calls.
    pub vault_url: Option<String>,
    /// Optional relay setup hostname for the credentials slice.
    pub relay_server: Option<String>,
    /// Optional relay download URL for the credentials slice.
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
        Ok(())
    }
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
