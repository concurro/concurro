//! Accounts browser service and isolated platform viewer authority.

use std::time::Duration;

use anyhow::{Context, Result, ensure};
use concurro_accounts::{
    AccountState, AuthorityConfig, browser_router,
    github::Access,
    http_client, internal_router,
    logout::{Logout, RevokeTokenJob},
    oauth::OAuth,
    sessions::Sessions,
    settings::Settings,
    targets::Targets,
};
use concurro_accounts_postgres as storage;
use concurro_config::{load, read_secret};
use concurro_db::Database;
use concurro_github_v1::AppClient;
use graphile_worker::WorkerOptions;

/// Start the two listeners and durable logout worker.
#[tokio::main]
async fn main() -> Result<()> {
    concurro_telemetry::init("info").map_err(|error| anyhow::anyhow!(error.to_string()))?;
    let settings: Settings = load("CONCURRO_ACCOUNTS_CONFIG")?;
    let database = Database::connect(&read_secret(&settings.database_url_file)?, 8).await?;
    storage::migrate(&database).await?;
    let key =
        hex::decode(read_secret(&settings.logout_key_file)?).context("logout key must be hex")?;
    ensure!(key.len() == 32, "logout key must be 64 hex characters");
    let client = http_client()?;
    let oauth = OAuth::new(
        client.clone(),
        settings.oauth_client_id.clone(),
        read_secret(&settings.oauth_client_secret_file)?,
        settings.callback_url(),
        "https://github.com/login/oauth/authorize".to_owned(),
        "https://github.com/login/oauth/access_token".to_owned(),
        settings.github_api_base.clone(),
    );
    let app = AppClient::new(
        client.clone(),
        &settings.github_api_base,
        settings.reader_app_id,
        read_secret(&settings.reader_private_key_file)?.as_bytes(),
    )?;
    let logout = Logout::new(database.clone(), &key, oauth.clone())?;
    let targets = Targets::new(client, settings.task_api_url.clone());
    if let Err(error) = targets.refresh().await {
        tracing::warn!(%error, "task target snapshot unavailable at startup");
    }
    let state = AccountState::new(
        Sessions::new(),
        oauth,
        Access::new(app),
        targets.clone(),
        logout.clone(),
        AuthorityConfig {
            bearer: read_secret(&settings.viewer_authority_bearer_file)?,
            base_path: settings.base_path(),
            secure_cookies: settings.secure_cookies(),
        },
    );
    let worker = WorkerOptions::default()
        .pg_pool(database.pool().clone())
        .schema("graphile_worker")
        .define_job::<RevokeTokenJob>()
        .add_extension(logout)
        .init()
        .await?;
    tokio::spawn(async move {
        if let Err(error) = worker.run().await {
            tracing::error!(%error, "logout worker stopped");
        }
    });
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(Duration::from_secs(60));
        loop {
            interval.tick().await;
            if let Err(error) = targets.refresh().await {
                tracing::warn!(%error, "task target refresh unavailable");
            }
        }
    });
    let browser = tokio::net::TcpListener::bind(&settings.listen).await?;
    let internal = tokio::net::TcpListener::bind(&settings.internal_listen).await?;
    tracing::info!(browser = %settings.listen, internal = %settings.internal_listen,
        "accounts listeners ready");
    tokio::try_join!(
        axum::serve(browser, browser_router(state.clone())),
        axum::serve(internal, internal_router(state)),
    )?;
    Ok(())
}
