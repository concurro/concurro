//! Accounts browser service and isolated platform viewer authority.

use std::{sync::Arc, time::Duration};

use anyhow::{Context, Result, ensure};
use async_nats::jetstream::stream;
use chrono::Utc;
use concurro_accounts::{
    AccountState, AuthorityConfig, Credentials, browser_router,
    commands::Commands,
    credentials::{
        Backend, DestroyMaterialJob, Native, UsageHandler, Vault, Verifier, run_maintenance,
    },
    github::Access,
    http_client,
    import::{self, ImportOptions},
    internal_router,
    logout::{Logout, RevokeTokenJob},
    oauth::OAuth,
    sessions::Sessions,
    settings::Settings,
    targets::Targets,
};
use concurro_accounts_postgres as storage;
use concurro_config::{load, read_secret};
use concurro_contracts::InferenceUsage;
use concurro_db::Database;
use concurro_events_nats::{Producer, Subscription, run_producer, run_subscription};
use concurro_events_postgres::Outbox;
use concurro_github_v1::AppClient;
use concurro_secrets::{OpenBao, OpenBaoSettings};
use graphile_worker::WorkerOptions;

/// Start the two listeners and durable workers, or run a vault import command.
#[tokio::main]
async fn main() -> Result<()> {
    concurro_telemetry::init("info").map_err(|error| anyhow::anyhow!(error.to_string()))?;
    let arguments = std::env::args().skip(1).collect::<Vec<_>>();
    if arguments.first().map(String::as_str) == Some("capture-vault") {
        return capture_vault(&arguments[1..]).await;
    }
    let settings: Settings = load("CONCURRO_ACCOUNTS_CONFIG")?;
    let database = Database::connect(&read_secret(&settings.database_url_file)?, 8).await?;
    storage::migrate(&database).await?;
    match arguments.first().map(String::as_str) {
        Some("import-vault") => return import_vault(&arguments[1..], &settings, &database).await,
        Some(other) => anyhow::bail!("unknown command {other}"),
        None => {}
    }
    let key =
        hex::decode(read_secret(&settings.logout_key_file)?).context("logout key must be hex")?;
    ensure!(key.len() == 32, "logout key must be 64 hex characters");
    let client = http_client()?;
    let oauth = OAuth::new(
        client.clone(),
        settings.oauth_client_id.clone(),
        read_secret(&settings.oauth_client_secret_file)?,
        settings.callback_url(),
        settings
            .oauth_authorize_url
            .clone()
            .unwrap_or_else(|| "https://github.com/login/oauth/authorize".to_owned()),
        settings
            .oauth_token_url
            .clone()
            .unwrap_or_else(|| "https://github.com/login/oauth/access_token".to_owned()),
        settings.github_api_base.clone(),
    );
    let app = AppClient::new(
        client.clone(),
        &settings.github_api_base,
        settings.reader_app_id,
        read_secret(&settings.reader_private_key_file)?.as_bytes(),
    )?;
    let logout = Logout::new(database.clone(), &key, oauth.clone())?;
    let commands = Commands::new(
        client.clone(),
        settings.task_api_url.clone(),
        read_secret(&settings.command_proof_private_key_file)?.as_bytes(),
    )?;
    let native = match (&settings.credentials, &settings.events) {
        (Some(credentials), Some(events)) => {
            concurro_events_postgres::migrate(&database).await?;
            let openbao = OpenBao::connect(credentials.openbao.clone()).await?;
            let outbox = Outbox::new(events.max_message_bytes, vec!["credential.>".to_owned()]);
            let native = Native::new(database.clone(), openbao, outbox, Arc::new(Utc::now));
            Some(match &credentials.verifier {
                Some(verifier) => native.with_verifier(Verifier::new(
                    client.clone(),
                    &verifier.url,
                    read_secret(&verifier.owner_token_file)?,
                )),
                None => native,
            })
        }
        _ => None,
    };
    let backend = match &native {
        Some(native) if settings.native_store_active() => Backend::Native(native.clone()),
        _ => Backend::Vault(Vault::new(
            client.clone(),
            settings.vault_url.clone().filter(|value| !value.is_empty()),
        )?),
    };
    let credentials = Credentials::with_backend(
        backend,
        settings
            .relay_server
            .clone()
            .filter(|value| !value.is_empty()),
        settings
            .relay_download_url
            .clone()
            .filter(|value| !value.is_empty()),
    );
    let targets = Targets::new(client, settings.task_api_url.clone());
    if let Err(error) = targets.refresh().await {
        tracing::warn!(%error, "task target snapshot unavailable at startup");
    }
    let mut state = AccountState::new(
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
    )
    .with_commands(commands)
    .with_credentials(credentials);
    let mut worker = WorkerOptions::default()
        .pg_pool(database.pool().clone())
        .schema("graphile_worker")
        .define_job::<RevokeTokenJob>()
        .define_job::<DestroyMaterialJob>()
        .add_extension(logout);
    if let (Some(native), Some(credentials), Some(events)) =
        (&native, &settings.credentials, &settings.events)
    {
        state = state.with_credential_metadata(
            native.clone(),
            read_secret(&credentials.metadata_bearer_file)?,
        );
        worker = worker.add_extension(native.clone());
        if settings.native_store_active() {
            tokio::spawn(run_maintenance(native.clone()));
        }
        tokio::spawn(run_producer(
            Producer {
                nats_url: events.nats_url.clone(),
                credentials_file: events.nats_credentials_file.clone(),
                stream: stream::Config {
                    name: "ACCOUNTS".to_owned(),
                    subjects: vec!["credential.>".to_owned()],
                    ..Default::default()
                },
                poll_interval: Duration::from_millis(events.forwarder_poll_ms),
                published_retention: Duration::from_secs(events.published_retention_seconds),
                max_message_bytes: events.max_message_bytes,
            },
            database.clone(),
        ));
        tokio::spawn(run_subscription::<InferenceUsage, _>(
            Subscription {
                nats_url: events.nats_url.clone(),
                credentials_file: events.nats_credentials_file.clone(),
                stream: "EXCHANGE".to_owned(),
                consumer: "accounts-usage".to_owned(),
                subject: "inference.usage".to_owned(),
                max_attempts: events.consumer_max_attempts,
                held_poll_interval: Duration::from_millis(events.consumer_poll_ms),
            },
            database.clone(),
            UsageHandler,
        ));
    }
    let worker = worker.init().await?;
    tokio::spawn(async move {
        if let Err(error) = worker.run().await {
            tracing::error!(%error, "accounts worker stopped");
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

/// One `--name value` argument.
fn argument(arguments: &[String], name: &str) -> Option<String> {
    arguments
        .iter()
        .position(|argument| argument == name)
        .and_then(|index| arguments.get(index + 1))
        .cloned()
}

/// A required `--name value` argument.
fn required(arguments: &[String], name: &str) -> Result<String> {
    argument(arguments, name).with_context(|| format!("{name} is required"))
}

/// `capture-vault --vault-url URL --service-token-file FILE --vault-db COPY --out FILE`:
/// record the live vault's metadata answers for every owner in the copy.
async fn capture_vault(arguments: &[String]) -> Result<()> {
    let token = read_secret(&required(arguments, "--service-token-file")?)?
        .trim()
        .to_ascii_lowercase();
    let capture = import::capture(
        &required(arguments, "--vault-url")?,
        &token,
        required(arguments, "--vault-db")?.as_ref(),
    )
    .await?;
    std::fs::write(
        required(arguments, "--out")?,
        serde_json::to_vec_pretty(&capture)?,
    )?;
    println!(
        "{}",
        serde_json::json!({"reads": capture.reads.len(), "now": capture.now})
    );
    Ok(())
}

/// `import-vault --vault-db COPY --master-key FILE [--keyring FILE] --capture FILE
/// --migrator-role-id ID --migrator-secret-id-file FILE`: import the stopped vault's copy
/// and print the report.
async fn import_vault(
    arguments: &[String],
    settings: &Settings,
    database: &Database,
) -> Result<()> {
    let openbao = &settings
        .credentials
        .as_ref()
        .context("the import needs the credentials settings")?
        .openbao;
    let migrator = OpenBao::connect(OpenBaoSettings {
        role_id: required(arguments, "--migrator-role-id")?,
        secret_id_file: required(arguments, "--migrator-secret-id-file")?.into(),
        ..openbao.clone()
    })
    .await?;
    let options = ImportOptions {
        vault_db: required(arguments, "--vault-db")?.into(),
        master_key: required(arguments, "--master-key")?.into(),
        keyring: argument(arguments, "--keyring").map(Into::into),
        capture: required(arguments, "--capture")?.into(),
    };
    let report = import::import(database, &migrator, &options).await?;
    println!("{}", serde_json::to_string_pretty(&report)?);
    ensure!(report.result == "pass", "the import did not verify");
    Ok(())
}
