//! Reviewer-account user token retained across task-service restarts.

use std::{
    fs::{self, OpenOptions},
    io::Write,
    os::unix::fs::{OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};

use anyhow::{Context, Result, bail, ensure};
use chrono::Utc;
use reqwest::{Client, StatusCode, Url, header::USER_AGENT};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::sync::Mutex;
use tokio::time::Instant;
use uuid::Uuid;

const REFRESH_MARGIN_SECONDS: f64 = 300.0;

/// A stored GitHub user-to-server token bound to one numeric reviewer account.
#[derive(Clone)]
pub struct UserGrant {
    inner: Arc<GrantInner>,
}

struct GrantInner {
    client: Client,
    oauth_base: Url,
    path: PathBuf,
    client_id: String,
    client_secret_file: PathBuf,
    expected_user_id: i64,
    current: Mutex<StoredGrant>,
}

/// The retained Python grant format, including an absent expiry for legacy tokens.
#[derive(Clone, Deserialize, Serialize)]
struct StoredGrant {
    token: String,
    #[serde(default)]
    expires: Option<f64>,
    #[serde(default)]
    refresh_token: Option<String>,
    #[serde(default)]
    refresh_expires: Option<f64>,
    actor: Value,
}

/// GitHub's OAuth refresh reply; absent expiry remains absent.
#[derive(Deserialize)]
struct TokenReply {
    error: Option<String>,
    access_token: Option<String>,
    expires_in: Option<i64>,
    refresh_token: Option<String>,
    refresh_token_expires_in: Option<i64>,
}

/// Device-flow code and polling schedule returned by GitHub.
#[derive(Deserialize)]
struct DeviceReply {
    device_code: String,
    user_code: String,
    verification_uri: String,
    expires_in: Option<u64>,
    interval: Option<u64>,
}

impl TokenReply {
    /// Validate an OAuth reply without inventing absent token lifetimes.
    fn into_grant(self, actor: Value, now: f64, require_refresh: bool) -> Result<StoredGrant> {
        let token = self
            .access_token
            .filter(|value| !value.is_empty())
            .context("reviewer authorization was incomplete")?;
        ensure!(
            self.expires_in.is_none_or(|seconds| seconds > 0),
            "reviewer authorization was incomplete"
        );
        if require_refresh {
            ensure!(
                self.refresh_token
                    .as_ref()
                    .is_some_and(|value| !value.is_empty())
                    && self
                        .refresh_token_expires_in
                        .is_some_and(|seconds| seconds > 0)
                    && self.expires_in.is_some(),
                "reviewer grant refresh was incomplete"
            );
        } else if self.refresh_token.is_some() {
            ensure!(
                self.refresh_token
                    .as_ref()
                    .is_some_and(|value| !value.is_empty())
                    && self
                        .refresh_token_expires_in
                        .is_some_and(|seconds| seconds > 0)
                    && self.expires_in.is_some(),
                "reviewer authorization was incomplete"
            );
        }
        Ok(StoredGrant {
            token,
            expires: self.expires_in.map(|seconds| now + seconds as f64),
            refresh_token: self.refresh_token,
            refresh_expires: self
                .refresh_token_expires_in
                .map(|seconds| now + seconds as f64),
            actor,
        })
    }
}

impl UserGrant {
    /// Authorize the configured account through GitHub's device flow and save its grant.
    #[allow(clippy::too_many_arguments)]
    pub async fn authorize_device(
        client: Client,
        oauth_base: &str,
        api_base: &str,
        path: impl Into<PathBuf>,
        client_id: impl Into<String>,
        client_secret_file: impl Into<PathBuf>,
        expected_user_id: i64,
        show_code: impl FnOnce(&str, &str),
    ) -> Result<Self> {
        ensure!(expected_user_id > 0, "reviewer account ID is invalid");
        let mut oauth = Url::parse(oauth_base).context("reviewer OAuth URL is invalid")?;
        if !oauth.path().ends_with('/') {
            oauth.set_path(&format!("{}/", oauth.path()));
        }
        let mut api = Url::parse(api_base).context("reviewer API URL is invalid")?;
        if !api.path().ends_with('/') {
            api.set_path(&format!("{}/", api.path()));
        }
        let client_id = client_id.into();
        let device: DeviceReply = client
            .post(oauth.join("login/device/code")?)
            .header("Accept", "application/json")
            .header(USER_AGENT, "concurro-tasks")
            .form(&[("client_id", client_id.as_str())])
            .send()
            .await
            .map_err(|_| anyhow::anyhow!("reviewer device authorization failed"))?
            .error_for_status()
            .map_err(|_| anyhow::anyhow!("reviewer device authorization failed"))?
            .json()
            .await
            .context("reviewer device authorization was incomplete")?;
        ensure!(
            !device.device_code.is_empty()
                && !device.user_code.is_empty()
                && !device.verification_uri.is_empty()
                && device.expires_in.unwrap_or(900) > 0,
            "reviewer device authorization was incomplete"
        );
        show_code(&device.verification_uri, &device.user_code);
        let deadline = Instant::now() + Duration::from_secs(device.expires_in.unwrap_or(900));
        let mut interval = device.interval.unwrap_or(5).max(1);
        let reply = loop {
            tokio::time::sleep(Duration::from_secs(interval)).await;
            ensure!(Instant::now() < deadline, "reviewer authorization expired");
            let response = client
                .post(oauth.join("login/oauth/access_token")?)
                .header("Accept", "application/json")
                .header(USER_AGENT, "concurro-tasks")
                .form(&[
                    ("client_id", client_id.as_str()),
                    ("device_code", device.device_code.as_str()),
                    ("grant_type", "urn:ietf:params:oauth:grant-type:device_code"),
                ])
                .send()
                .await
                .map_err(|_| anyhow::anyhow!("reviewer authorization could not be confirmed"))?;
            if response.status() != StatusCode::OK {
                bail!("reviewer authorization was refused");
            }
            let payload: TokenReply = response
                .json()
                .await
                .context("reviewer authorization was incomplete")?;
            match payload.error.as_deref() {
                Some("authorization_pending") => continue,
                Some("slow_down") => {
                    interval += 5;
                    continue;
                }
                Some(_) => bail!("reviewer authorization was refused"),
                None => break payload,
            }
        };
        let token = reply
            .access_token
            .as_deref()
            .filter(|value| !value.is_empty())
            .context("reviewer authorization was incomplete")?;
        let response = client
            .get(api.join("user")?)
            .bearer_auth(token)
            .header(USER_AGENT, "concurro-tasks")
            .send()
            .await
            .map_err(|_| anyhow::anyhow!("GitHub did not confirm the reviewer account"))?;
        if response.status() != StatusCode::OK {
            bail!("GitHub did not confirm the reviewer account");
        }
        let account: Value = response
            .json()
            .await
            .context("GitHub did not confirm the reviewer account")?;
        ensure!(
            account["id"].as_i64() == Some(expected_user_id)
                && account["login"]
                    .as_str()
                    .is_some_and(|login| !login.is_empty()),
            "the authorizing account is not the configured reviewer"
        );
        let viewer = client
            .post(api.join("graphql")?)
            .bearer_auth(token)
            .header(USER_AGENT, "concurro-tasks")
            .json(&serde_json::json!({"query":"query { viewer { id } }"}))
            .send()
            .await;
        let node = if let Ok(response) = viewer {
            if response.status() == StatusCode::OK {
                response
                    .json::<Value>()
                    .await
                    .ok()
                    .and_then(|body| body["data"]["viewer"]["id"].as_str().map(str::to_owned))
            } else {
                None
            }
        } else {
            None
        };
        let actor = serde_json::json!({
            "id":expected_user_id,"login":account["login"],"node":node,
        });
        let grant = reply.into_grant(actor, Utc::now().timestamp() as f64, false)?;
        let path = path.into();
        save_grant(&path, &grant)?;
        Self::open(
            client,
            oauth_base,
            path,
            client_id,
            client_secret_file,
            expected_user_id,
        )
    }

    /// Load an owner-only grant and bind it to the configured reviewer ID.
    pub fn open(
        client: Client,
        oauth_base: &str,
        path: impl Into<PathBuf>,
        client_id: impl Into<String>,
        client_secret_file: impl Into<PathBuf>,
        expected_user_id: i64,
    ) -> Result<Self> {
        ensure!(expected_user_id > 0, "reviewer account ID is invalid");
        let path = path.into();
        let current = read_grant(&path, expected_user_id)?;
        let mut oauth_base = Url::parse(oauth_base).context("reviewer OAuth URL is invalid")?;
        if !oauth_base.path().ends_with('/') {
            oauth_base.set_path(&format!("{}/", oauth_base.path()));
        }
        Ok(Self {
            inner: Arc::new(GrantInner {
                client,
                oauth_base,
                path,
                client_id: client_id.into(),
                client_secret_file: client_secret_file.into(),
                expected_user_id,
                current: Mutex::new(current),
            }),
        })
    }

    /// Return a token, serializing refresh and saving a replacement before use.
    pub async fn token(&self) -> Result<String> {
        let mut current = self.inner.current.lock().await;
        let now = Utc::now().timestamp() as f64;
        if usable(&current, now) {
            return Ok(current.token.clone());
        }
        if let Ok(stored) = read_grant(&self.inner.path, self.inner.expected_user_id) {
            if stored.token != current.token {
                *current = stored;
                if usable(&current, now) {
                    return Ok(current.token.clone());
                }
            }
        }
        let refresh = current
            .refresh_token
            .as_deref()
            .filter(|value| !value.is_empty())
            .context("reviewer grant expired; authorize the reviewer again")?;
        ensure!(
            current.refresh_expires.is_some_and(|expiry| expiry > now),
            "reviewer grant expired; authorize the reviewer again"
        );
        let secret = fs::read_to_string(&self.inner.client_secret_file)
            .context("reviewer client secret is unavailable")?;
        let url = self
            .inner
            .oauth_base
            .join("login/oauth/access_token")
            .context("reviewer OAuth URL is invalid")?;
        let response = self
            .inner
            .client
            .post(url)
            .header("Accept", "application/json")
            .header(USER_AGENT, "concurro-tasks")
            .form(&[
                ("client_id", self.inner.client_id.as_str()),
                ("client_secret", secret.trim()),
                ("grant_type", "refresh_token"),
                ("refresh_token", refresh),
            ])
            .send()
            .await
            .map_err(|_| anyhow::anyhow!("reviewer grant could not be refreshed"))?;
        if response.status() != StatusCode::OK {
            bail!("reviewer grant could not be refreshed");
        }
        let reply: TokenReply = response
            .json()
            .await
            .map_err(|_| anyhow::anyhow!("reviewer grant refresh was incomplete"))?;
        let replacement = reply.into_grant(current.actor.clone(), now, true)?;
        save_grant(&self.inner.path, &replacement)?;
        *current = replacement;
        Ok(current.token.clone())
    }

    /// Force the next use to refresh a token GitHub definitely refused.
    pub async fn drop_token(&self, token: &str) {
        let mut current = self.inner.current.lock().await;
        if current.token == token && current.refresh_token.is_some() {
            current.expires = Some(0.0);
        }
    }
}

/// Read a grant only when its file mode and account binding are valid.
fn read_grant(path: &Path, expected_user_id: i64) -> Result<StoredGrant> {
    let metadata = fs::metadata(path).context("reviewer grant is unavailable")?;
    ensure!(
        metadata.permissions().mode() & 0o077 == 0,
        "reviewer grant must be owner-only"
    );
    let grant: StoredGrant =
        serde_json::from_slice(&fs::read(path)?).context("reviewer grant is invalid")?;
    ensure!(
        !grant.token.is_empty()
            && grant.actor["id"].as_i64() == Some(expected_user_id)
            && grant.actor["login"]
                .as_str()
                .is_some_and(|login| !login.is_empty()),
        "reviewer grant belongs to another account"
    );
    Ok(grant)
}

/// Keep non-expiring tokens and tokens outside the refresh margin.
fn usable(grant: &StoredGrant, now: f64) -> bool {
    grant
        .expires
        .is_none_or(|expiry| expiry - REFRESH_MARGIN_SECONDS > now)
}

/// Replace a refreshed grant atomically with owner-only permissions.
fn save_grant(path: &Path, grant: &StoredGrant) -> Result<()> {
    let temporary = path.with_extension(format!("new-{}", Uuid::now_v7()));
    let result = (|| -> Result<()> {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&temporary)
            .context("reviewer grant temporary file is unavailable")?;
        file.write_all(&serde_json::to_vec(grant)?)?;
        file.sync_all()?;
        fs::rename(&temporary, path).context("reviewer grant replacement failed")?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}

#[cfg(test)]
mod tests {
    use std::{
        fs,
        os::unix::fs::PermissionsExt,
        sync::{
            Arc,
            atomic::{AtomicI64, AtomicUsize, Ordering},
        },
    };

    use axum::{
        Json, Router,
        routing::{get, post},
    };
    use reqwest::Client;
    use serde_json::{Value, json};
    use uuid::Uuid;

    use super::UserGrant;

    #[tokio::test]
    async fn reviewer_grant_refreshes_once_and_keeps_account_binding() {
        let directory =
            std::env::temp_dir().join(format!("concurro-user-grant-{}", Uuid::now_v7()));
        fs::create_dir(&directory).expect("grant directory");
        let path = directory.join("reviewer-grant.json");
        let secret = directory.join("oauth-secret");
        fs::write(&secret, "private-client-secret\n").expect("client secret");
        fs::write(
            &path,
            serde_json::to_vec(&json!({
                "token":"old-token", "expires":1, "refresh_token":"old-refresh",
                "refresh_expires":4102444800.0,
                "actor":{"id":99,"login":"reviewer","node":"U_99"}
            }))
            .expect("grant JSON"),
        )
        .expect("stored grant");
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).expect("grant mode");
        let calls = Arc::new(AtomicUsize::new(0));
        let counter = calls.clone();
        let app = Router::new().route(
            "/login/oauth/access_token",
            post(move |body: String| {
                let counter = counter.clone();
                async move {
                    assert!(body.contains("grant_type=refresh_token"));
                    assert!(body.contains("refresh_token=old-refresh"));
                    counter.fetch_add(1, Ordering::SeqCst);
                    Json(json!({"access_token":"new-token","expires_in":3600,
                        "refresh_token":"new-refresh","refresh_token_expires_in":7200}))
                }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("OAuth listener");
        let address = listener.local_addr().expect("OAuth address");
        let server = tokio::spawn(async move { axum::serve(listener, app).await });
        let oauth = format!("http://{address}/");
        let grant = UserGrant::open(Client::new(), &oauth, &path, "client-id", &secret, 99)
            .expect("bound reviewer grant");
        let (first, second) = tokio::join!(grant.token(), grant.token());
        assert_eq!(first.expect("first token"), "new-token");
        assert_eq!(second.expect("second token"), "new-token");
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        let saved: Value =
            serde_json::from_slice(&fs::read(&path).expect("saved grant")).expect("saved JSON");
        assert_eq!(saved["token"], "new-token");
        assert_eq!(saved["actor"]["node"], "U_99");
        assert_eq!(
            fs::metadata(&path)
                .expect("saved metadata")
                .permissions()
                .mode()
                & 0o077,
            0
        );
        assert!(UserGrant::open(Client::new(), &oauth, &path, "client-id", &secret, 100).is_err());
        server.abort();
        fs::remove_dir_all(&directory).expect("grant cleanup");
    }

    #[tokio::test]
    async fn device_authorization_refuses_another_account_then_saves_nonexpiring_grant() {
        let directory =
            std::env::temp_dir().join(format!("concurro-device-grant-{}", Uuid::now_v7()));
        fs::create_dir(&directory).expect("grant directory");
        let path = directory.join("reviewer-grant.json");
        let secret = directory.join("oauth-secret");
        fs::write(&secret, "unused-secret").expect("client secret");
        let account_id = Arc::new(AtomicI64::new(100));
        let user_id = account_id.clone();
        let app =
            Router::new()
                .route(
                    "/login/device/code",
                    post(|| async {
                        Json(json!({"device_code":"device-code","user_code":"ABCD-EFGH",
                    "verification_uri":"https://github.test/login/device","expires_in":10,
                    "interval":1}))
                    }),
                )
                .route(
                    "/login/oauth/access_token",
                    post(|| async { Json(json!({"access_token":"authorized-token"})) }),
                )
                .route(
                    "/user",
                    get(move || {
                        let user_id = user_id.clone();
                        async move {
                            Json(json!({"id":user_id.load(Ordering::SeqCst),"login":"reviewer"}))
                        }
                    }),
                )
                .route(
                    "/graphql",
                    post(|| async { Json(json!({"data":{"viewer":{"id":"U_99"}}})) }),
                );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("OAuth listener");
        let address = listener.local_addr().expect("OAuth address");
        let server = tokio::spawn(async move { axum::serve(listener, app).await });
        let base = format!("http://{address}/");
        let wrong = UserGrant::authorize_device(
            Client::new(),
            &base,
            &base,
            &path,
            "client-id",
            &secret,
            99,
            |uri, code| {
                assert_eq!(uri, "https://github.test/login/device");
                assert_eq!(code, "ABCD-EFGH");
            },
        )
        .await;
        assert!(wrong.is_err());
        assert!(!path.exists());
        account_id.store(99, Ordering::SeqCst);
        let grant = UserGrant::authorize_device(
            Client::new(),
            &base,
            &base,
            &path,
            "client-id",
            &secret,
            99,
            |_, _| {},
        )
        .await
        .expect("bound device authorization");
        assert_eq!(
            grant.token().await.expect("nonexpiring token"),
            "authorized-token"
        );
        let stored: Value =
            serde_json::from_slice(&fs::read(&path).expect("stored grant")).expect("grant JSON");
        assert!(stored["expires"].is_null());
        assert_eq!(stored["actor"]["node"], "U_99");
        server.abort();
        fs::remove_dir_all(&directory).expect("grant cleanup");
    }
}
