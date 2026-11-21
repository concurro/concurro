//! Shared authenticated NATS connection setup.

use anyhow::Result;
use async_nats::{Client, ConnectOptions};
use concurro_config::read_secret;
use serde::Deserialize;

/// NATS user credentials loaded from a private JSON file.
#[derive(Deserialize)]
struct NatsCredentials {
    /// Account user name.
    user: String,
    /// Account password.
    password: String,
}

/// Connect with the configured private credentials, or anonymously for local development.
pub async fn connect(url: &str, credentials_file: Option<&str>) -> Result<Client> {
    let options = match credentials_file {
        Some(path) => {
            let credentials: NatsCredentials = serde_json::from_str(&read_secret(path)?)?;
            ConnectOptions::with_user_and_password(credentials.user, credentials.password)
        }
        None => ConnectOptions::new(),
    };
    Ok(options.connect(url).await?)
}
