//! In-memory browser sessions and the private viewer authority.

mod browser;
pub mod github;
mod internal;
pub mod logout;
pub mod oauth;
pub mod sessions;
pub mod settings;
pub mod targets;

use std::{
    collections::HashMap,
    sync::Arc,
    time::{Duration, Instant},
};

use reqwest::Client;
use tokio::sync::Mutex;

use github::Access;
use logout::Logout;
use oauth::OAuth;
use sessions::Sessions;
use targets::Targets;

/// Session, target, issue and tier entries held for one minute.
type TierCache = HashMap<(String, String, String, i64), (Instant, &'static str)>;

/// Listener and credential values shared by the authority routers.
pub struct AuthorityConfig {
    /// Private service bearer for viewer lookups.
    pub bearer: String,
    /// Public URL path prepended to browser redirects.
    pub base_path: String,
    /// Issue Secure browser cookies under HTTPS.
    pub secure_cookies: bool,
}

/// Separate browser and platform routers share only this authority state.
#[derive(Clone)]
pub struct AccountState {
    sessions: Sessions,
    oauth: OAuth,
    github: Access,
    targets: Targets,
    logout: Logout,
    authority_bearer: String,
    base_path: String,
    secure_cookies: bool,
    tier_cache: Arc<Mutex<TierCache>>,
}

impl AccountState {
    /// Bind the browser, GitHub and private viewer dependencies.
    pub fn new(
        sessions: Sessions,
        oauth: OAuth,
        github: Access,
        targets: Targets,
        logout: Logout,
        config: AuthorityConfig,
    ) -> Self {
        Self {
            sessions,
            oauth,
            github,
            targets,
            logout,
            authority_bearer: config.bearer,
            base_path: config.base_path,
            secure_cookies: config.secure_cookies,
            tier_cache: Arc::new(Mutex::new(HashMap::new())),
        }
    }
}

/// Build the shared HTTP client used by OAuth, GitHub reads and target snapshots.
pub fn http_client() -> anyhow::Result<Client> {
    Ok(Client::builder()
        .timeout(Duration::from_secs(20))
        .redirect(reqwest::redirect::Policy::none())
        .user_agent("concurro-accounts")
        .build()?)
}

/// Serve only browser sign-in, logout and current-user routes.
pub fn browser_router(state: AccountState) -> axum::Router {
    browser::router(state)
}

/// Serve only the platform viewer-authority route.
pub fn internal_router(state: AccountState) -> axum::Router {
    internal::router(state)
}
