//! An OpenBao KV v2 client signed in with an AppRole.

use std::{
    collections::{BTreeMap, HashMap},
    fmt,
    path::PathBuf,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

use secrecy::{ExposeSecret, SecretString};
use serde::Deserialize;
use tokio::sync::RwLock;
use vaultrs::{
    api::kv2::requests::SetSecretRequestOptions,
    auth::approle,
    client::{VaultClient, VaultClientSettingsBuilder},
    error::ClientError,
    kv2, token,
};

use crate::Error;

/// How a service reaches OpenBao.
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OpenBaoSettings {
    /// OpenBao's address on its private network, such as `http://concurro-openbao:8200`.
    pub url: String,
    /// The service's AppRole role ID; not secret.
    pub role_id: String,
    /// File holding the AppRole secret ID, the service's one piece of OpenBao bootstrap
    /// material.
    pub secret_id_file: PathBuf,
    /// The KV v2 mount.
    #[serde(default = "default_mount")]
    pub mount: String,
    /// How long a read is served from memory before OpenBao is asked again.
    #[serde(default = "default_refresh_seconds")]
    pub refresh_seconds: u64,
}

fn default_mount() -> String {
    "concurro".to_owned()
}

fn default_refresh_seconds() -> u64 {
    60
}

/// One secret's fields, read together.
#[derive(Clone)]
pub struct Secret {
    path: String,
    fields: Arc<BTreeMap<String, SecretString>>,
}

impl Secret {
    /// One field's value.
    pub fn field(&self, name: &str) -> Result<&SecretString, Error> {
        self.fields.get(name).ok_or_else(|| Error::MissingField {
            path: self.path.clone(),
            field: name.to_owned(),
        })
    }

    /// The field names, which aren't secret.
    pub fn field_names(&self) -> impl Iterator<Item = &str> {
        self.fields.keys().map(String::as_str)
    }
}

impl fmt::Debug for Secret {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("Secret")
            .field("path", &self.path)
            .field("fields", &self.fields.keys().collect::<Vec<_>>())
            .finish()
    }
}

/// A secret's version metadata; reading it needs no access to the values.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Metadata {
    /// The latest version number.
    pub current_version: u64,
    /// Every version OpenBao still records, destroyed ones included.
    pub versions: Vec<u64>,
}

/// A renewal granting less than this is treated as exhausted.
const MIN_RENEWED_LEASE_SECONDS: u64 = 60;

/// The current token and when to renew it.
struct Session {
    renew_at: Instant,
    renewable: bool,
}

/// A read kept in memory until the refresh interval passes.
struct Cached {
    fetched: Instant,
    secret: Secret,
}

struct Inner {
    settings: OpenBaoSettings,
    /// The signed-in client; signing in again swaps in a new one.
    client: RwLock<Arc<VaultClient>>,
    session: tokio::sync::Mutex<Session>,
    cache: Mutex<HashMap<String, Cached>>,
}

/// A shared OpenBao client; clones share one token and one cache.
#[derive(Clone)]
pub struct OpenBao {
    inner: Arc<Inner>,
}

impl fmt::Debug for OpenBao {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("OpenBao")
            .field("url", &self.inner.settings.url)
            .field("mount", &self.inner.settings.mount)
            .finish_non_exhaustive()
    }
}

impl OpenBao {
    /// Sign in with the service's AppRole.
    pub async fn connect(settings: OpenBaoSettings) -> Result<Self, Error> {
        let client = build_client(&settings.url, None)?;
        let openbao = Self {
            inner: Arc::new(Inner {
                settings,
                client: RwLock::new(Arc::new(client)),
                session: tokio::sync::Mutex::new(Session {
                    renew_at: Instant::now(),
                    renewable: false,
                }),
                cache: Mutex::new(HashMap::new()),
            }),
        };
        let mut session = openbao.inner.session.lock().await;
        openbao.login(&mut session).await?;
        drop(session);
        Ok(openbao)
    }

    /// Read a secret, from memory if it was read within the refresh interval.
    pub async fn read(&self, path: &str) -> Result<Secret, Error> {
        let interval = Duration::from_secs(self.inner.settings.refresh_seconds);
        if let Some(cached) = self.inner.cache.lock().expect("cache lock").get(path)
            && cached.fetched.elapsed() < interval
        {
            return Ok(cached.secret.clone());
        }
        self.refresh(path).await
    }

    /// Read a secret from OpenBao now, for a caller that has just had it refused.
    pub async fn refresh(&self, path: &str) -> Result<Secret, Error> {
        let fields: HashMap<String, String> = self
            .call(path, |client, mount| async move {
                kv2::read(&*client, &mount, path).await
            })
            .await?;
        let secret = Secret {
            path: path.to_owned(),
            fields: Arc::new(
                fields
                    .into_iter()
                    .map(|(name, value)| (name, SecretString::from(value)))
                    .collect(),
            ),
        };
        self.inner.cache.lock().expect("cache lock").insert(
            path.to_owned(),
            Cached {
                fetched: Instant::now(),
                secret: secret.clone(),
            },
        );
        Ok(secret)
    }

    /// Create a secret that must not exist yet (`cas=0`).
    ///
    /// A path that already exists, in any version, is refused.
    pub async fn create(
        &self,
        path: &str,
        value: &BTreeMap<String, SecretString>,
    ) -> Result<u64, Error> {
        let created = self
            .call(path, |client, mount| async move {
                let exposed: BTreeMap<&str, &str> = value
                    .iter()
                    .map(|(name, secret)| (name.as_str(), secret.expose_secret()))
                    .collect();
                kv2::set_with_options(
                    &*client,
                    &mount,
                    path,
                    &exposed,
                    SetSecretRequestOptions { cas: 0 },
                )
                .await
            })
            .await?;
        Ok(created.version)
    }

    /// A secret's version metadata, or `None` if the path doesn't exist.
    pub async fn metadata(&self, path: &str) -> Result<Option<Metadata>, Error> {
        let found = self
            .call(path, |client, mount| async move {
                kv2::read_metadata(&*client, &mount, path).await
            })
            .await;
        match found {
            Ok(metadata) => {
                let mut versions: Vec<u64> = metadata
                    .versions
                    .keys()
                    .filter_map(|version| version.parse().ok())
                    .collect();
                versions.sort_unstable();
                Ok(Some(Metadata {
                    current_version: metadata.current_version,
                    versions,
                }))
            }
            Err(Error::NotFound { .. }) => Ok(None),
            Err(error) => Err(error),
        }
    }

    /// Destroy every version of a secret, then delete its metadata. An absent path is
    /// already destroyed.
    pub async fn destroy(&self, path: &str) -> Result<(), Error> {
        let Some(metadata) = self.metadata(path).await? else {
            return Ok(());
        };
        if !metadata.versions.is_empty() {
            let versions = metadata.versions.clone();
            self.call(path, |client, mount| {
                let versions = versions.clone();
                async move { kv2::destroy_versions(&*client, &mount, path, versions).await }
            })
            .await?;
        }
        match self
            .call(path, |client, mount| async move {
                kv2::delete_metadata(&*client, &mount, path).await
            })
            .await
        {
            Ok(()) | Err(Error::NotFound { .. }) => Ok(()),
            Err(error) => Err(error),
        }
        .inspect(|()| {
            self.inner.cache.lock().expect("cache lock").remove(path);
        })
    }

    /// Run one request with a current token. A refusal is retried once after signing in
    /// again, since an expired or revoked token is also refused.
    async fn call<T, F, Fut>(&self, path: &str, request: F) -> Result<T, Error>
    where
        F: Fn(Arc<VaultClient>, String) -> Fut,
        Fut: Future<Output = Result<T, ClientError>>,
    {
        self.keep_token().await?;
        let mount = self.inner.settings.mount.clone();
        let first = request(self.current().await, mount.clone())
            .await
            .map_err(|error| api_error(path, &error));
        match first {
            Err(error) if is_refused(&error) => {
                let mut session = self.inner.session.lock().await;
                self.login(&mut session).await?;
                drop(session);
                request(self.current().await, mount)
                    .await
                    .map_err(|error| api_error(path, &error))
            }
            other => other,
        }
    }

    /// The current signed-in client.
    async fn current(&self) -> Arc<VaultClient> {
        self.inner.client.read().await.clone()
    }

    /// Renew the token once two thirds of its lease have passed, or sign in again.
    async fn keep_token(&self) -> Result<(), Error> {
        let mut session = self.inner.session.lock().await;
        if Instant::now() < session.renew_at {
            return Ok(());
        }
        if session.renewable {
            let renewed = token::renew_self(&*self.current().await, None).await;
            // Near the role's maximum TTL a renewal grants little; sign in again instead.
            if let Ok(auth) = renewed
                && auth.lease_duration >= MIN_RENEWED_LEASE_SECONDS
            {
                session.renew_at = renew_at(auth.lease_duration);
                session.renewable = auth.renewable;
                return Ok(());
            }
            tracing::info!("OpenBao token not renewable further; signing in again");
        }
        self.login(&mut session).await
    }

    /// Sign in with the AppRole, reading the secret ID file each time so a replaced
    /// secret ID is picked up.
    async fn login(&self, session: &mut Session) -> Result<(), Error> {
        let settings = &self.inner.settings;
        let secret_id = SecretString::from(
            concurro_config::read_secret(&settings.secret_id_file)
                .map_err(|_| Error::File(settings.secret_id_file.display().to_string()))?,
        );
        let anonymous = build_client(&settings.url, None)?;
        let auth = approle::login(
            &anonymous,
            "approle",
            &settings.role_id,
            secret_id.expose_secret(),
        )
        .await
        .map_err(|error| api_error("auth", &error))?;
        let token = SecretString::from(auth.client_token);
        *self.inner.client.write().await = Arc::new(build_client(&settings.url, Some(&token))?);
        session.renew_at = renew_at(auth.lease_duration);
        session.renewable = auth.renewable;
        Ok(())
    }
}

/// A client for OpenBao's address, signed in with `token` if given.
fn build_client(url: &str, token: Option<&SecretString>) -> Result<VaultClient, Error> {
    let mut settings = VaultClientSettingsBuilder::default();
    settings.address(url).timeout(Some(Duration::from_secs(10)));
    if let Some(token) = token {
        settings.token(token.expose_secret());
    }
    let settings = settings
        .build()
        .map_err(|_| Error::Settings("OpenBao address".to_owned()))?;
    VaultClient::new(settings).map_err(|_| Error::Settings("OpenBao address".to_owned()))
}

/// When to renew a token with this lease, in seconds.
fn renew_at(lease_seconds: u64) -> Instant {
    Instant::now() + Duration::from_secs(lease_seconds.saturating_mul(2) / 3)
}

/// Map a `vaultrs` error to one that carries only the path and status.
fn api_error(path: &str, error: &ClientError) -> Error {
    let path = path.to_owned();
    match error {
        ClientError::APIError { code: 404, .. } => Error::NotFound { path },
        // 400 is a check-and-set mismatch on create; 403 a policy or token refusal.
        ClientError::APIError {
            code: 400 | 403, ..
        } => Error::Refused { path },
        ClientError::APIError { code, .. } => Error::Unavailable {
            path,
            status: Some(*code),
        },
        _ => Error::Unavailable { path, status: None },
    }
}

/// Whether a fresh token might clear the error.
fn is_refused(error: &Error) -> bool {
    matches!(error, Error::Refused { .. })
}
