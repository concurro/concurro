//! `concurro-secrets` against OpenBao 2.7.3 configured by `deploy/openbao/openbao.hcl`,
//! so the policies tested are the ones deployed.

use std::{assert_matches, collections::BTreeMap, path::PathBuf, time::Duration};

use concurro_secrets::{
    Error, ExposeSecret, OpenBao, OpenBaoSettings, SecretRef, SecretString, Secrets,
};
use concurro_testkit::OpenBaoContainer;
use reqwest::{Method, StatusCode};
use serde_json::{Value, json};

const KNOWN: &str = "sk-known-secret-value-4c1e";
const PERSONAL: &str = "personal/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

fn config() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../deploy/openbao/openbao.hcl")
}

async fn openbao_with(config: &std::path::Path) -> OpenBaoContainer {
    OpenBaoContainer::start(
        config,
        &[
            ("INITIAL_ADMIN_PASSWORD", "admin-pass"),
            ("INITIAL_ADMIN_RECOVERY_PASSWORD", "recovery-pass"),
            ("INITIAL_OPERATOR_PASSWORD", "operator-pass"),
        ],
    )
    .await
    .expect("OpenBao starts and self-initializes")
}

/// Raw HTTP for the people's identities and checks the client doesn't cover.
async fn call(
    url: &str,
    token: Option<&str>,
    method: Method,
    path: &str,
    body: Option<Value>,
) -> (StatusCode, Value) {
    let mut request = reqwest::Client::new().request(method.clone(), format!("{url}/v1/{path}"));
    if let Some(token) = token {
        request = request.header("X-Vault-Token", token);
    }
    if let Some(body) = body {
        let content = if method == Method::PATCH {
            "application/merge-patch+json"
        } else {
            "application/json"
        };
        request = request
            .header("Content-Type", content)
            .body(body.to_string());
    }
    let response = request.send().await.expect("OpenBao answers");
    let status = response.status();
    let text = response.text().await.unwrap_or_default();
    (status, serde_json::from_str(&text).unwrap_or(Value::Null))
}

async fn person(url: &str, user: &str, password: &str) -> String {
    let (status, body) = call(
        url,
        None,
        Method::POST,
        &format!("auth/userpass/login/{user}"),
        Some(json!({"password": password})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{user} signs in");
    body["auth"]["client_token"].as_str().unwrap().to_owned()
}

/// A service client signed in with `role`, its secret ID issued by `admin`.
async fn service(
    url: &str,
    admin: &str,
    role: &str,
    refresh_seconds: u64,
) -> (OpenBao, tempfile_path::Kept) {
    let (_, body) = call(
        url,
        Some(admin),
        Method::GET,
        &format!("auth/approle/role/{role}/role-id"),
        None,
    )
    .await;
    let role_id = body["data"]["role_id"]
        .as_str()
        .expect("role ID")
        .to_owned();
    let (_, body) = call(
        url,
        Some(admin),
        Method::POST,
        &format!("auth/approle/role/{role}/secret-id"),
        Some(json!({})),
    )
    .await;
    let file = tempfile_path::Kept::write(body["data"]["secret_id"].as_str().expect("secret ID"));
    let client = OpenBao::connect(OpenBaoSettings {
        url: url.to_owned(),
        role_id,
        secret_id_file: file.path.clone(),
        mount: "concurro".to_owned(),
        refresh_seconds,
    })
    .await
    .expect("service signs in");
    (client, file)
}

/// A secret ID file removed when the test ends.
mod tempfile_path {
    use std::{
        path::PathBuf,
        sync::atomic::{AtomicU64, Ordering},
    };

    static NEXT: AtomicU64 = AtomicU64::new(0);

    pub struct Kept {
        pub path: PathBuf,
    }

    impl Kept {
        pub fn write(value: &str) -> Self {
            let path = std::env::temp_dir().join(format!(
                "concurro-secret-id-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::SeqCst)
            ));
            std::fs::write(&path, value).unwrap();
            Self { path }
        }
    }

    impl Drop for Kept {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.path);
        }
    }
}

fn value(secret: &str) -> BTreeMap<String, SecretString> {
    BTreeMap::from([
        ("secret".to_owned(), SecretString::from(secret.to_owned())),
        (
            "account".to_owned(),
            SecretString::from("acct-1".to_owned()),
        ),
    ])
}

#[tokio::test]
async fn accounts_writes_once_and_cannot_read_back() {
    let openbao = openbao_with(&config()).await;
    let url = openbao.url();
    let admin = person(url, "admin", "admin-pass").await;
    let (accounts, _a) = service(url, &admin, "accounts", 60).await;
    let (gateway, _g) = service(url, &admin, "gateway", 60).await;

    assert_eq!(accounts.create(PERSONAL, &value(KNOWN)).await.unwrap(), 1);
    let again = accounts.create(PERSONAL, &value(KNOWN)).await;
    assert_matches!(again, Err(Error::Refused { .. }));
    let message = format!("{again:?} {}", again.as_ref().unwrap_err());
    assert!(!message.contains(KNOWN), "errors carry no secret value");
    assert_matches!(accounts.refresh(PERSONAL).await, Err(Error::Refused { .. }));
    let metadata = accounts
        .metadata(PERSONAL)
        .await
        .unwrap()
        .expect("metadata");
    assert_eq!((metadata.current_version, metadata.versions), (1, vec![1]));

    let read = gateway.read(PERSONAL).await.unwrap();
    assert_eq!(read.field("secret").unwrap().expose_secret(), KNOWN);
    assert!(
        !format!("{read:?}").contains(KNOWN),
        "debug output carries no value"
    );
    assert_matches!(
        gateway.create("personal/other", &value("x")).await,
        Err(Error::Refused { .. })
    );
    assert_matches!(gateway.destroy(PERSONAL).await, Err(Error::Refused { .. }));

    for (user, password) in [
        ("operator", "operator-pass"),
        ("admin-recovery", "recovery-pass"),
    ] {
        let token = person(url, user, password).await;
        let (status, _) = call(
            url,
            Some(&token),
            Method::GET,
            &format!("concurro/data/{PERSONAL}"),
            None,
        )
        .await;
        assert_eq!(
            status,
            StatusCode::FORBIDDEN,
            "{user} can't read personal material"
        );
    }

    accounts.destroy(PERSONAL).await.unwrap();
    assert_eq!(accounts.metadata(PERSONAL).await.unwrap(), None);
    assert_matches!(gateway.refresh(PERSONAL).await, Err(Error::NotFound { .. }));
    accounts
        .destroy(PERSONAL)
        .await
        .expect("an absent path is already destroyed");

    let audit = String::from_utf8(
        openbao
            .exec(&["cat", "/openbao/logs/audit.log"])
            .await
            .unwrap(),
    )
    .unwrap();
    assert!(audit.contains("hmac-sha256:"), "audit entries HMAC values");
    assert!(
        !audit.contains(KNOWN),
        "the audit log holds no plaintext secret"
    );
}

#[tokio::test]
async fn operator_rotation_reaches_readers() {
    let openbao = openbao_with(&config()).await;
    let url = openbao.url();
    let admin = person(url, "admin", "admin-pass").await;
    let operator = person(url, "operator", "operator-pass").await;
    let path = "concurro/data/ops/gateway/providers";
    let (status, _) = call(
        url,
        Some(&operator),
        Method::POST,
        path,
        Some(json!({"data": {"chatgpt_access_token": "tok-1", "chatgpt_account": "account-1"}})),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let (gateway, _g) = service(url, &admin, "gateway", 2).await;
    let secrets = Secrets::new(Some(gateway), Duration::from_secs(2));
    let reference: SecretRef = "openbao:ops/gateway/providers#chatgpt_access_token"
        .parse()
        .unwrap();
    let account: SecretRef = "openbao:ops/gateway/providers#chatgpt_account"
        .parse()
        .unwrap();
    let (cached_token, cached_account) =
        secrets.get_pair(&reference, Some(&account)).await.unwrap();
    assert_eq!(cached_token.expose_secret(), "tok-1");
    assert_eq!(cached_account.unwrap().expose_secret(), "account-1");
    assert_eq!(
        secrets.get(&reference).await.unwrap().expose_secret(),
        "tok-1"
    );

    // Routine rotation patches without naming a version.
    let (status, _) = call(
        url,
        Some(&operator),
        Method::PATCH,
        path,
        Some(json!({"data": {"chatgpt_access_token": "tok-2", "chatgpt_account": "account-2"}})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "operator patch without cas");
    assert_eq!(
        secrets.get(&reference).await.unwrap().expose_secret(),
        "tok-1",
        "served from memory"
    );
    assert_eq!(
        secrets.refresh(&reference).await.unwrap().expose_secret(),
        "tok-2",
        "seen at once on refresh"
    );
    let (token, account_value) = secrets
        .refresh_pair(&reference, Some(&account))
        .await
        .unwrap();
    assert_eq!(token.expose_secret(), "tok-2");
    assert_eq!(account_value.unwrap().expose_secret(), "account-2");
    call(
        url,
        Some(&operator),
        Method::PATCH,
        path,
        Some(json!({"data": {"chatgpt_access_token": "tok-3"}})),
    )
    .await;
    tokio::time::sleep(Duration::from_millis(2100)).await;
    assert_eq!(
        secrets.get(&reference).await.unwrap().expose_secret(),
        "tok-3",
        "seen within the interval"
    );
    let missing: SecretRef = "openbao:ops/gateway/providers#absent".parse().unwrap();
    assert_matches!(secrets.get(&missing).await, Err(Error::MissingField { .. }));
}

#[tokio::test]
async fn admin_manages_roles_and_operator_cannot() {
    let openbao = openbao_with(&config()).await;
    let url = openbao.url();
    for admin in [
        person(url, "admin", "admin-pass").await,
        person(url, "admin-recovery", "recovery-pass").await,
    ] {
        let (status, _) = call(
            url,
            Some(&admin),
            Method::POST,
            "auth/approle/role/migrator",
            Some(json!({"token_policies": "migrator"})),
        )
        .await;
        assert_eq!(
            status,
            StatusCode::NO_CONTENT,
            "admin creates the migrator role"
        );
        let (status, body) = call(
            url,
            Some(&admin),
            Method::POST,
            "auth/approle/role/accounts/secret-id",
            Some(json!({})),
        )
        .await;
        assert_eq!(
            status,
            StatusCode::OK,
            "admin issues a replacement secret ID"
        );
        assert!(body["data"]["secret_id"].is_string());
        let (status, _) = call(
            url,
            Some(&admin),
            Method::DELETE,
            "auth/approle/role/migrator",
            None,
        )
        .await;
        assert_eq!(
            status,
            StatusCode::NO_CONTENT,
            "admin deletes the migrator role"
        );
    }
    let operator = person(url, "operator", "operator-pass").await;
    let (status, _) = call(
        url,
        Some(&operator),
        Method::POST,
        "auth/approle/role/accounts/secret-id",
        Some(json!({})),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN);
    let (status, _) = call(
        url,
        Some(&operator),
        Method::DELETE,
        "auth/approle/role/accounts",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN);
}

#[tokio::test]
async fn the_import_cleanup_revokes_migrator_tokens() {
    let openbao = openbao_with(&config()).await;
    let url = openbao.url();
    let admin = person(url, "admin", "admin-pass").await;
    // As the platform guide creates it for the import: a short token TTL.
    let (status, _) = call(
        url,
        Some(&admin),
        Method::POST,
        "auth/approle/role/migrator",
        Some(json!({"token_policies": "migrator", "token_ttl": "1h", "token_max_ttl": "4h"})),
    )
    .await;
    assert_eq!(status, StatusCode::NO_CONTENT);
    let (_, body) = call(
        url,
        Some(&admin),
        Method::GET,
        "auth/approle/role/migrator/role-id",
        None,
    )
    .await;
    let role_id = body["data"]["role_id"].as_str().unwrap().to_owned();
    let (_, body) = call(
        url,
        Some(&admin),
        Method::POST,
        "auth/approle/role/migrator/secret-id",
        Some(json!({})),
    )
    .await;
    let secret_id = body["data"]["secret_id"].as_str().unwrap().to_owned();
    let (_, body) = call(
        url,
        None,
        Method::POST,
        "auth/approle/login",
        Some(json!({"role_id": role_id, "secret_id": secret_id})),
    )
    .await;
    let migrator = body["auth"]["client_token"]
        .as_str()
        .expect("migrator signs in")
        .to_owned();
    let path = format!("concurro/data/{PERSONAL}");
    let (status, _) = call(
        url,
        Some(&migrator),
        Method::POST,
        &path,
        Some(json!({"options": {"cas": 0}, "data": {"secret": KNOWN}})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "the migrator writes material");
    let (status, _) = call(url, Some(&migrator), Method::GET, &path, None).await;
    assert_eq!(status, StatusCode::OK, "and reads it back to verify");

    // Cleanup: deleting the role alone leaves its issued token working.
    call(
        url,
        Some(&admin),
        Method::DELETE,
        "auth/approle/role/migrator",
        None,
    )
    .await;
    let (status, _) = call(url, Some(&migrator), Method::GET, &path, None).await;
    assert_eq!(
        status,
        StatusCode::OK,
        "a deleted role's token still reads, so cleanup must revoke it"
    );
    let (status, _) = call(
        url,
        Some(&admin),
        Method::PUT,
        "sys/leases/revoke-prefix/auth/approle/login",
        Some(json!({})),
    )
    .await;
    assert_eq!(status, StatusCode::NO_CONTENT);
    let (status, _) = call(url, Some(&migrator), Method::GET, &path, None).await;
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "the revoked migrator token reads nothing"
    );
    let (status, _) = call(
        url,
        None,
        Method::POST,
        "auth/approle/login",
        Some(json!({"role_id": role_id, "secret_id": secret_id})),
    )
    .await;
    assert_ne!(status, StatusCode::OK, "and can't sign in again");
}

#[tokio::test]
async fn an_expired_token_signs_in_again() {
    let openbao = openbao_with(&config()).await;
    let url = openbao.url();
    let admin = person(url, "admin", "admin-pass").await;
    let operator = person(url, "operator", "operator-pass").await;
    call(
        url,
        Some(&admin),
        Method::POST,
        "auth/approle/role/short",
        Some(json!({"token_policies": "gateway", "token_ttl": "3s", "token_max_ttl": "4s"})),
    )
    .await;
    call(
        url,
        Some(&operator),
        Method::POST,
        "concurro/data/ops/gateway/providers",
        Some(json!({"data": {"openai_api_key": "key-1"}})),
    )
    .await;
    let (client, _f) = service(url, &admin, "short", 60).await;
    client.refresh("ops/gateway/providers").await.unwrap();
    tokio::time::sleep(Duration::from_secs(5)).await;
    let read = client
        .refresh("ops/gateway/providers")
        .await
        .expect("signs in again after expiry");
    assert_eq!(
        read.field("openai_api_key").unwrap().expose_secret(),
        "key-1"
    );
}

#[tokio::test]
async fn a_revoked_token_is_replaced_by_signing_in_again() {
    let openbao = openbao_with(&config()).await;
    let url = openbao.url();
    let admin = person(url, "admin", "admin-pass").await;
    let operator = person(url, "operator", "operator-pass").await;
    call(
        url,
        Some(&operator),
        Method::POST,
        "concurro/data/ops/gateway/providers",
        Some(json!({"data": {"openai_api_key": "key-1"}})),
    )
    .await;
    let (gateway, _g) = service(url, &admin, "gateway", 60).await;
    gateway.refresh("ops/gateway/providers").await.unwrap();
    // Admin revokes every AppRole sign-in, as after a leaked secret ID.
    let (status, _) = call(
        url,
        Some(&admin),
        Method::PUT,
        "sys/leases/revoke-prefix/auth/approle/login",
        Some(json!({})),
    )
    .await;
    assert_eq!(
        status,
        StatusCode::NO_CONTENT,
        "admin revokes AppRole sign-ins"
    );
    let (status, _) = call(
        url,
        Some(&operator),
        Method::PUT,
        "sys/leases/revoke-prefix/auth/approle/login",
        Some(json!({})),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN, "the operator can't");
    // The client's token is refused before its renewal deadline; it signs in again once.
    let read = gateway
        .refresh("ops/gateway/providers")
        .await
        .expect("signs in again after revocation");
    assert_eq!(
        read.field("openai_api_key").unwrap().expose_secret(),
        "key-1"
    );
}

#[tokio::test]
async fn a_cold_restart_unseals_without_help() {
    let mut openbao = openbao_with(&config()).await;
    let admin = person(openbao.url(), "admin", "admin-pass").await;
    let (accounts, _a) = service(openbao.url(), &admin, "accounts", 60).await;
    accounts.create(PERSONAL, &value(KNOWN)).await.unwrap();
    openbao
        .restart()
        .await
        .expect("PostgreSQL then OpenBao restart and unseal");
    let url = openbao.url();
    let admin = person(url, "admin", "admin-pass").await;
    let (gateway, _g) = service(url, &admin, "gateway", 60).await;
    assert_eq!(
        gateway
            .read(PERSONAL)
            .await
            .unwrap()
            .field("secret")
            .unwrap()
            .expose_secret(),
        KNOWN
    );
}

#[tokio::test]
async fn a_recovery_key_yields_root_only_while_the_legacy_endpoint_is_enabled() {
    // The deployed configuration keeps the unauthenticated endpoint disabled.
    let normal = openbao_with(&config()).await;
    let (status, _) = call(
        normal.url(),
        None,
        Method::GET,
        "sys/generate-root/attempt",
        None,
    )
    .await;
    assert_ne!(
        status,
        StatusCode::OK,
        "legacy root generation is disabled by default"
    );
    drop(normal);

    // Recovery enables it temporarily on the listener.
    let deployed = std::fs::read_to_string(config()).unwrap();
    let recovery = deployed.replacen(
        "  tls_disable = true\n",
        "  tls_disable = true\n  disable_unauthed_generate_root_endpoints = false\n",
        1,
    );
    assert_ne!(recovery, deployed);
    let path = std::env::temp_dir().join(format!(
        "concurro-openbao-recovery-{}.hcl",
        std::process::id()
    ));
    std::fs::write(&path, recovery).unwrap();
    let openbao = openbao_with(&path).await;
    let url = openbao.url();
    let admin = person(url, "admin", "admin-pass").await;
    let (status, body) = call(
        url,
        Some(&admin),
        Method::POST,
        "sys/rotate/recovery/init",
        Some(json!({"secret_shares": 1, "secret_threshold": 1})),
    )
    .await;
    assert_eq!(
        status,
        StatusCode::OK,
        "admin creates a recovery key: {body}"
    );
    let key = body["data"]["keys_base64"][0]
        .as_str()
        .unwrap_or_else(|| panic!("recovery key in {body}"))
        .to_owned();

    let (_, attempt) = call(url, None, Method::GET, "sys/generate-root/attempt", None).await;
    let length = attempt["otp_length"].as_u64().expect("otp length") as usize;
    let otp: String = (0..length)
        .map(|i| (b'a' + (i % 26) as u8) as char)
        .collect();
    let (status, started) = call(
        url,
        None,
        Method::PUT,
        "sys/generate-root/attempt",
        Some(json!({"otp": otp})),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let (_, finished) = call(
        url,
        None,
        Method::PUT,
        "sys/generate-root/update",
        Some(json!({"key": key, "nonce": started["nonce"]})),
    )
    .await;
    let encoded = finished["encoded_token"]
        .as_str()
        .expect("encoded root token");
    let decoded = base64_decode(encoded);
    let root: String = decoded
        .iter()
        .zip(otp.bytes())
        .map(|(a, b)| (a ^ b) as char)
        .collect();
    let (status, lookup) = call(
        url,
        Some(&root),
        Method::GET,
        "auth/token/lookup-self",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(lookup["data"]["policies"], json!(["root"]));
    call(
        url,
        Some(&root),
        Method::POST,
        "auth/token/revoke-self",
        Some(json!({})),
    )
    .await;
    let (status, _) = call(
        url,
        Some(&root),
        Method::GET,
        "auth/token/lookup-self",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN, "the root token is revoked");
    let _ = std::fs::remove_file(path);
}

/// Standard or unpadded base64, as OpenBao encodes the root token.
fn base64_decode(value: &str) -> Vec<u8> {
    const ALPHABET: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut bits = 0_u32;
    let mut count = 0;
    let mut out = Vec::new();
    for byte in value.bytes().filter(|b| *b != b'=') {
        let index = ALPHABET.iter().position(|a| *a == byte).expect("base64") as u32;
        bits = (bits << 6) | index;
        count += 6;
        if count >= 8 {
            count -= 8;
            out.push((bits >> count) as u8);
            bits &= (1 << count) - 1;
        }
    }
    out
}
