//! The native store against PostgreSQL and OpenBao configured by
//! `deploy/openbao/openbao.hcl`, checked against answers recorded from the Python vault
//! (`tests/record_vault_history.py`).

use std::{
    assert_matches,
    path::PathBuf,
    sync::{Arc, Mutex},
};

use chrono::{DateTime, Utc};
use concurro_accounts_postgres::credentials as storage;
use concurro_db::Database;
use concurro_events_postgres::Outbox;
use concurro_secrets::{Error, OpenBao, OpenBaoSettings};
use concurro_testkit::{OpenBaoContainer, PostgresContainer};
use reqwest::Method;
use serde_json::{Value, json};

use super::{Native, Operation, Outcome, material_path, view};
use crate::github::User;

/// Record IDs of queued destroy jobs, oldest first.
const QUEUED: &str = "SELECT j.payload->>'record_id' FROM graphile_worker._private_jobs j \
    JOIN graphile_worker._private_tasks t ON t.id=j.task_id \
    WHERE t.identifier='destroy_credential_material' ORDER BY j.id";

/// The paths at which two JSON values differ.
pub fn differences(actual: &Value, expected: &Value, path: &str, found: &mut Vec<String>) {
    match (actual, expected) {
        (Value::Object(a), Value::Object(b)) => {
            for key in a.keys().chain(b.keys().filter(|key| !a.contains_key(*key))) {
                let (left, right) = (
                    a.get(key).unwrap_or(&Value::Null),
                    b.get(key).unwrap_or(&Value::Null),
                );
                if a.contains_key(key) != b.contains_key(key) {
                    found.push(format!(
                        "{path}.{key}: present {} vs {}",
                        a.contains_key(key),
                        b.contains_key(key)
                    ));
                } else {
                    differences(left, right, &format!("{path}.{key}"), found);
                }
            }
        }
        (Value::Array(a), Value::Array(b)) if a.len() == b.len() => {
            for (index, (left, right)) in a.iter().zip(b).enumerate() {
                differences(left, right, &format!("{path}[{index}]"), found);
            }
        }
        _ if actual != expected => found.push(format!("{path}: {actual} vs {expected}")),
        _ => {}
    }
}

/// Assert two JSON values are equal, naming the paths that differ.
pub fn assert_same(actual: &Value, expected: &Value, context: &str) {
    let mut found = Vec::new();
    differences(actual, expected, "", &mut found);
    assert!(found.is_empty(), "{context}:\n{}", found.join("\n"));
}

/// A settable store clock.
#[derive(Clone)]
pub struct TestClock(Arc<Mutex<DateTime<Utc>>>);

impl TestClock {
    pub fn set(&self, at: &str) {
        *self.0.lock().unwrap() = view::parse(at).expect("instant");
    }

    pub fn clock(&self) -> super::Clock {
        let now = self.0.clone();
        Arc::new(move || *now.lock().unwrap())
    }
}

/// PostgreSQL, OpenBao and stores signed in with the `accounts` and `gateway` roles.
pub struct Harness {
    pub _postgres: PostgresContainer,
    pub openbao: OpenBaoContainer,
    pub database: Database,
    pub native: Native,
    /// The same store with a client that may not create or destroy material.
    pub refusing: Native,
    pub accounts: OpenBao,
    pub admin: String,
    pub clock: TestClock,
    _files: Vec<PathBuf>,
}

impl Harness {
    pub async fn start() -> Self {
        let _ = tracing_subscriber::fmt().with_test_writer().try_init();
        let postgres = PostgresContainer::start().await.expect("PostgreSQL starts");
        let database = Database::connect(postgres.url(), 4).await.unwrap();
        concurro_accounts_postgres::migrate(&database)
            .await
            .unwrap();
        concurro_events_postgres::migrate(&database).await.unwrap();
        let config =
            PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../deploy/openbao/openbao.hcl");
        let openbao = OpenBaoContainer::start(
            &config,
            &[
                ("INITIAL_ADMIN_PASSWORD", "admin-pass"),
                ("INITIAL_ADMIN_RECOVERY_PASSWORD", "recovery-pass"),
                ("INITIAL_OPERATOR_PASSWORD", "operator-pass"),
            ],
        )
        .await
        .expect("OpenBao starts");
        let (_, login) = call(
            openbao.url(),
            None,
            Method::POST,
            "auth/userpass/login/admin",
            Some(json!({"password":"admin-pass"})),
        )
        .await;
        let admin = login["auth"]["client_token"].as_str().unwrap().to_owned();
        let mut files = Vec::new();
        let accounts = service(openbao.url(), &admin, "accounts", &mut files).await;
        let gateway = service(openbao.url(), &admin, "gateway", &mut files).await;
        let clock = TestClock(Arc::new(Mutex::new(Utc::now())));
        let outbox = Outbox::new(1024 * 1024, vec!["credential.>".to_owned()]);
        Self {
            native: Native::new(
                database.clone(),
                accounts.clone(),
                outbox.clone(),
                clock.clock(),
            ),
            refusing: Native::new(database.clone(), gateway, outbox, clock.clock()),
            _postgres: postgres,
            openbao,
            database,
            accounts,
            admin,
            clock,
            _files: files,
        }
    }

    /// A store client signed in with `role`.
    pub async fn service(&mut self, role: &str) -> OpenBao {
        service(self.openbao.url(), &self.admin, role, &mut self._files).await
    }

    /// Committed change facts' data, in order.
    pub async fn facts(&self) -> Vec<Value> {
        sqlx::query_scalar::<_, Value>(
            "SELECT message->'data' FROM events.outbox ORDER BY sequence",
        )
        .fetch_all(self.database.pool())
        .await
        .unwrap()
    }

    /// Every version row of an owner.
    pub async fn versions(&self, user_id: i64) -> Vec<storage::VersionRow> {
        let mut work = self.database.begin().await.unwrap();
        let versions = storage::versions(&mut work, user_id).await.unwrap();
        work.commit().await.unwrap();
        versions
    }

    /// Run every queued destroy job inline.
    pub async fn destroy_queued(&self, native: &Native) {
        let jobs: Vec<String> = sqlx::query_scalar(QUEUED)
            .fetch_all(self.database.pool())
            .await
            .unwrap();
        for record_id in jobs {
            native.destroy(&record_id).await.unwrap();
        }
    }
}

impl Drop for Harness {
    fn drop(&mut self) {
        for file in &self._files {
            let _ = std::fs::remove_file(file);
        }
    }
}

/// Raw OpenBao HTTP.
pub async fn call(
    url: &str,
    token: Option<&str>,
    method: Method,
    path: &str,
    body: Option<Value>,
) -> (reqwest::StatusCode, Value) {
    let mut request = reqwest::Client::new().request(method, format!("{url}/v1/{path}"));
    if let Some(token) = token {
        request = request.header("X-Vault-Token", token);
    }
    if let Some(body) = body {
        request = request.json(&body);
    }
    let response = request.send().await.expect("OpenBao answers");
    let status = response.status();
    (status, response.json().await.unwrap_or(Value::Null))
}

/// A client signed in with `role`, its secret ID issued by `admin`.
async fn service(url: &str, admin: &str, role: &str, files: &mut Vec<PathBuf>) -> OpenBao {
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
    let path = std::env::temp_dir().join(format!(
        "concurro-accounts-secret-id-{}-{}-{role}",
        std::process::id(),
        files.len()
    ));
    std::fs::write(
        &path,
        body["data"]["secret_id"].as_str().expect("secret ID"),
    )
    .unwrap();
    files.push(path.clone());
    OpenBao::connect(OpenBaoSettings {
        url: url.to_owned(),
        role_id,
        secret_id_file: path,
        mount: "concurro".to_owned(),
        refresh_seconds: 60,
    })
    .await
    .expect("service signs in")
}

fn alice() -> User {
    User {
        id: 101,
        login: "alice".to_owned(),
        name: None,
        avatar_url: None,
    }
}

fn add(route: &'static str, name: &str, secret: &str, mutation: &str) -> Operation {
    Operation::Add {
        route,
        body: serde_json::from_value(json!({"type":"unused","name":name,"secret":secret,
            "mutation_id":mutation}))
        .unwrap(),
    }
}

fn replace(entry_id: &str, revision: i64, secret: &str, mutation: &str) -> Operation {
    Operation::Replace {
        entry_id: entry_id.to_owned(),
        body: serde_json::from_value(json!({"revision":revision,"secret":secret,
            "mutation_id":mutation}))
        .unwrap(),
    }
}

/// Replace each recorded vault entry ID with the native store's.
fn translate(value: &Value, ids: &[(String, String)]) -> Value {
    let mut text = value.to_string();
    for (vault, native) in ids {
        text = text.replace(vault, native);
    }
    serde_json::from_str(&text).unwrap()
}

/// The recorded owner writes, replayed, answer as the vault did, with the same policy
/// history, versions and one fact per committed change.
#[tokio::test]
async fn replay_matches_vault() {
    let harness = Harness::start().await;
    let recorded: Value =
        serde_json::from_str(include_str!("../../tests/vault_history/writes.json")).unwrap();
    let user = alice();
    let mut ids: Vec<(String, String)> = Vec::new();
    let entry =
        |ids: &[(String, String)], index: &Value| ids[index.as_u64().unwrap() as usize].1.clone();
    for step in recorded["steps"].as_array().unwrap() {
        harness.clock.set(step["at"].as_str().unwrap());
        let request = &step["request"];
        let operation = match step["operation"].as_str().unwrap() {
            "add" => {
                let route = view::ROUTES
                    .iter()
                    .find(|(route, ..)| Some(*route) == request["route"].as_str())
                    .unwrap()
                    .0;
                let mut body = request.clone();
                body["type"] = json!("unused");
                Operation::Add {
                    route,
                    body: serde_json::from_value(body).unwrap(),
                }
            }
            "replace" => Operation::Replace {
                entry_id: entry(&ids, &request["entry"]),
                body: serde_json::from_value(request.clone()).unwrap(),
            },
            "edit" => Operation::Edit {
                entry_id: entry(&ids, &request["entry"]),
                body: serde_json::from_value(request.clone()).unwrap(),
            },
            "reorder" => {
                let mut body = request.clone();
                body["entries"] = request["entries"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|index| json!(entry(&ids, index)))
                    .collect();
                Operation::Reorder {
                    provider: request["provider"].as_str().unwrap().to_owned(),
                    body: serde_json::from_value(body).unwrap(),
                }
            }
            "remove" => Operation::Remove {
                entry_id: entry(&ids, &request["entry"]),
                revision: request["revision"].as_i64().unwrap(),
            },
            "listing" => Operation::List,
            other => panic!("unknown operation {other}"),
        };
        let actual = match harness.native.run(&user, operation).await {
            Ok(value) => (200, value),
            Err(super::CredentialError(status, detail)) => {
                (status.as_u16(), json!({"error":detail}))
            }
        };
        if step["operation"] == "add" {
            ids.push((
                step["response"]["entry_id"].as_str().unwrap().to_owned(),
                actual.1["entry_id"].as_str().unwrap().to_owned(),
            ));
        }
        let expected = (
            step["status"].as_u64().unwrap() as u16,
            translate(&step["response"], &ids),
        );
        assert_eq!(actual, expected, "{} at {}", step["operation"], step["at"]);
    }
    for read in recorded["reads"].as_array().unwrap() {
        let actual = harness
            .native
            .metadata(101, read["at"].as_str())
            .await
            .unwrap();
        assert_same(
            &actual,
            &translate(&read["response"], &ids),
            &format!("metadata at {}", read["at"]),
        );
    }
    let facts = harness.facts().await;
    let changes = facts
        .iter()
        .map(|fact| fact["change"].as_str().unwrap())
        .collect::<Vec<_>>();
    assert_eq!(
        changes,
        [
            "added",
            "added",
            "added",
            "replaced",
            "edited",
            "reordered",
            "removed"
        ]
    );
    let replaced = &facts[3]["revoked"].as_array().unwrap();
    assert_eq!(replaced.len(), 1);
    assert_eq!(replaced[0]["version"], 1);
    assert_eq!(replaced[0]["route"], "anthropic-api-key");
    let removed = facts[6]["revoked"].as_array().unwrap();
    assert_eq!(
        removed
            .iter()
            .map(|v| v["version"].as_i64().unwrap())
            .collect::<Vec<_>>(),
        [1, 2]
    );
    assert_eq!(facts[5]["entry_id"], Value::Null);
    assert!(
        facts
            .windows(2)
            .all(|pair| pair[0]["sequence"].as_i64() < pair[1]["sequence"].as_i64())
    );
    // The replaced and removed versions' material is queued for destruction, and goes.
    harness.destroy_queued(&harness.native).await;
    let versions = harness.versions(101).await;
    let anthropic = versions
        .iter()
        .filter(|version| version.route == "anthropic-api-key")
        .map(|version| version.material.as_str())
        .collect::<Vec<_>>();
    assert_eq!(anthropic, ["destroyed", "destroyed"]);
    for version in versions
        .iter()
        .filter(|version| version.route == "anthropic-api-key")
    {
        let path = material_path(&version.record_id);
        assert_eq!(harness.accounts.metadata(&path).await.unwrap(), None);
    }
}

/// Two replaces of one entry, and a replace racing a remove: one activates, the other is
/// superseded, answers `409` and its material is destroyed.
#[tokio::test]
async fn overlapping_writes_supersede() {
    let harness = Harness::start().await;
    let user = alice();
    harness.clock.set("2027-01-01T00:00:00+00:00");
    let added = harness
        .native
        .run(
            &user,
            add(
                "anthropic-api-key",
                "Key",
                "sk-ant-first-AAAA",
                "overlap-add-000001",
            ),
        )
        .await
        .unwrap();
    let entry_id = added["entry_id"].as_str().unwrap();
    let revision = added["revision"].as_i64().unwrap();

    // Both reserve against the same revision before either activates.
    let first = reserve(
        &harness,
        entry_id,
        revision,
        "sk-ant-second-BBBB",
        "overlap-replace-001",
    )
    .await;
    let second = reserve(
        &harness,
        entry_id,
        revision,
        "sk-ant-third-CCCC",
        "overlap-replace-002",
    )
    .await;
    for (record_id, secret) in [
        (&first, "sk-ant-second-BBBB"),
        (&second, "sk-ant-third-CCCC"),
    ] {
        harness
            .accounts
            .create(
                &material_path(record_id),
                &super::native::test_fields(secret),
            )
            .await
            .unwrap();
    }
    assert_matches!(
        harness.native.activate(&first).await.unwrap(),
        Outcome::Activated(_)
    );
    assert_matches!(
        harness.native.activate(&second).await.unwrap(),
        Outcome::Superseded(None)
    );
    // A retry of the superseded mutation answers 409 against the new revision.
    let retried = harness
        .native
        .run(
            &user,
            replace(
                entry_id,
                revision,
                "sk-ant-third-CCCC",
                "overlap-replace-002",
            ),
        )
        .await;
    assert_matches!(
        retried,
        Err(super::CredentialError(reqwest::StatusCode::CONFLICT, _))
    );

    // A replace whose create lands after the entry was removed is superseded, never listed.
    let current = harness.native.run(&user, Operation::List).await.unwrap();
    let revision = current["providers"][0]["entries"][0]["revision"]
        .as_i64()
        .unwrap();
    let late = reserve(
        &harness,
        entry_id,
        revision,
        "sk-ant-late-DDDD",
        "overlap-replace-003",
    )
    .await;
    harness
        .native
        .run(
            &user,
            Operation::Remove {
                entry_id: entry_id.to_owned(),
                revision,
            },
        )
        .await
        .unwrap();
    harness
        .accounts
        .create(
            &material_path(&late),
            &super::native::test_fields("sk-ant-late-DDDD"),
        )
        .await
        .unwrap();
    assert_matches!(
        harness.native.activate(&late).await.unwrap(),
        Outcome::Superseded(None)
    );

    harness.destroy_queued(&harness.native).await;
    let versions = harness.versions(101).await;
    let states = versions
        .iter()
        .map(|version| {
            (
                version.version,
                version.material.as_str(),
                version.activated,
            )
        })
        .collect::<Vec<_>>();
    assert_eq!(
        states,
        [
            (1, "destroyed", true),
            (2, "destroyed", true),
            (3, "destroyed", false),
            (4, "destroyed", false)
        ]
    );
    let metadata = harness.native.metadata(101, None).await.unwrap();
    let listed = metadata["credentials"]
        .as_array()
        .unwrap()
        .iter()
        .map(|credential| credential["version"].as_i64().unwrap())
        .collect::<Vec<_>>();
    assert_eq!(listed, [2], "superseded versions are never listed");
    let changes = harness
        .facts()
        .await
        .iter()
        .map(|fact| fact["change"].as_str().unwrap().to_owned())
        .collect::<Vec<_>>();
    assert_eq!(changes, ["added", "replaced", "removed"]);
}

/// Reserve a pending replace of `entry_id` without creating its material.
async fn reserve(
    harness: &Harness,
    entry_id: &str,
    revision: i64,
    secret: &str,
    mutation: &str,
) -> String {
    harness
        .native
        .reserve_replace(&alice(), entry_id, revision, secret, mutation)
        .await
        .unwrap()
}

/// A failed create leaves nothing visible and its retry adds once; a crash after the
/// create settles to stored with one fact.
#[tokio::test]
async fn crash_recovery() {
    let harness = Harness::start().await;
    let user = alice();
    harness.clock.set("2027-01-01T00:00:00+00:00");
    let refused = harness
        .refusing
        .run(
            &user,
            add(
                "openai-api-key",
                "Key",
                "sk-proj-retried-AAAA",
                "recovery-add-00001",
            ),
        )
        .await;
    assert_matches!(
        refused,
        Err(super::CredentialError(
            reqwest::StatusCode::SERVICE_UNAVAILABLE,
            _
        ))
    );
    let listing = harness.native.run(&user, Operation::List).await.unwrap();
    assert_eq!(listing["providers"][1]["entries"], json!([]));
    assert_eq!(
        harness.native.metadata(101, None).await.unwrap()["credentials"],
        json!([])
    );
    let retried = harness
        .native
        .run(
            &user,
            add(
                "openai-api-key",
                "Key",
                "sk-proj-retried-AAAA",
                "recovery-add-00001",
            ),
        )
        .await
        .unwrap();
    let again = harness
        .native
        .run(
            &user,
            add(
                "openai-api-key",
                "Key",
                "sk-proj-retried-AAAA",
                "recovery-add-00001",
            ),
        )
        .await
        .unwrap();
    assert_eq!(retried, again, "a replay answers with the first result");
    let states = harness
        .versions(101)
        .await
        .iter()
        .map(|version| (version.version, version.material.clone()))
        .collect::<Vec<_>>();
    assert_eq!(states, [(1, "failed".to_owned()), (2, "stored".to_owned())]);
    assert_eq!(harness.facts().await.len(), 1);

    // Crash after the create, before activation: settlement activates it once.
    let entry_id = retried["entry_id"].as_str().unwrap();
    let revision = retried["revision"].as_i64().unwrap();
    let record_id = reserve(
        &harness,
        entry_id,
        revision,
        "sk-proj-crashed-BBBB",
        "recovery-replace-01",
    )
    .await;
    harness
        .accounts
        .create(
            &material_path(&record_id),
            &super::native::test_fields("sk-proj-crashed-BBBB"),
        )
        .await
        .unwrap();
    assert_matches!(
        harness.native.settle(&record_id).await.unwrap(),
        Outcome::Activated(_)
    );
    assert_matches!(
        harness.native.settle(&record_id).await.unwrap(),
        Outcome::Activated(_)
    );
    // Crash before the create: settlement fails it, and the retry succeeds.
    let entry =
        harness.native.run(&user, Operation::List).await.unwrap()["providers"][1]["entries"][0]
            .clone();
    let revision = entry["revision"].as_i64().unwrap();
    let lost = reserve(
        &harness,
        entry_id,
        revision,
        "sk-proj-lost-CCCC",
        "recovery-replace-02",
    )
    .await;
    assert_matches!(harness.native.settle(&lost).await.unwrap(), Outcome::Failed);
    harness
        .native
        .run(
            &user,
            replace(
                entry_id,
                revision,
                "sk-proj-lost-CCCC",
                "recovery-replace-02",
            ),
        )
        .await
        .unwrap();
    let changes = harness
        .facts()
        .await
        .iter()
        .map(|fact| fact["change"].as_str().unwrap().to_owned())
        .collect::<Vec<_>>();
    assert_eq!(changes, ["added", "replaced", "replaced"]);

    // A destroy refused by OpenBao is retried until it succeeds.
    let queued: String = sqlx::query_scalar(QUEUED)
        .fetch_all(harness.database.pool())
        .await
        .unwrap()
        .remove(0);
    assert!(harness.refusing.destroy(&queued).await.is_err());
    harness.native.destroy(&queued).await.unwrap();
    assert_eq!(
        harness
            .accounts
            .metadata(&material_path(&queued))
            .await
            .unwrap(),
        None
    );
}

/// A version past its known expiry becomes invalid, its material is destroyed and an
/// `unusable` fact appended; an as-of read before the expiry still answers `valid`.
#[tokio::test]
async fn expiry() {
    let harness = Harness::start().await;
    let user = alice();
    harness.clock.set("2027-01-01T00:00:00+00:00");
    let added = harness
        .native
        .run(
            &user,
            add(
                "claude-subscription",
                "Sub",
                "sk-ant-oat-expiring-AAAA",
                "expiry-add-0000001",
            ),
        )
        .await
        .unwrap();
    let record_id = added["record"]["record_id"].as_str().unwrap().to_owned();
    sqlx::query(
        "UPDATE accounts.credential_versions SET expires_at='2027-01-01T01:00:00+00:00', \
         expiry_known=true WHERE record_id=$1",
    )
    .bind(&record_id)
    .execute(harness.database.pool())
    .await
    .unwrap();
    harness.native.expire().await.unwrap();
    assert!(
        harness
            .facts()
            .await
            .iter()
            .all(|fact| fact["change"] != "unusable")
    );
    harness.clock.set("2027-01-01T02:00:00+00:00");
    harness.native.expire().await.unwrap();
    harness.native.expire().await.unwrap();
    let facts = harness.facts().await;
    let unusable = facts
        .iter()
        .filter(|fact| fact["change"] == "unusable")
        .collect::<Vec<_>>();
    assert_eq!(unusable.len(), 1);
    assert_eq!(unusable[0]["revoked"][0]["record_id"], record_id.as_str());
    harness.destroy_queued(&harness.native).await;
    let version = &harness.versions(101).await[0];
    assert_eq!(
        (
            version.status.as_str(),
            version.status_reason.as_deref(),
            version.material.as_str()
        ),
        ("invalid", Some("expired"), "destroyed")
    );
    let before = harness
        .native
        .metadata(101, Some("2027-01-01T00:30:00+00:00"))
        .await
        .unwrap();
    assert_eq!(before["credentials"][0]["status"], "valid");
    let now = harness.native.metadata(101, None).await.unwrap();
    assert_eq!(now["credentials"][0]["status"], "invalid");
}

/// The `accounts` policy can't read material back, and no answer carries a submitted
/// secret.
#[tokio::test]
async fn no_read_back() {
    let harness = Harness::start().await;
    let user = alice();
    harness.clock.set("2027-01-01T00:00:00+00:00");
    let secret = "sk-ant-never-echoed-ZZZZ";
    let added = harness
        .native
        .run(
            &user,
            add("anthropic-api-key", "Key", secret, "readback-add-00001"),
        )
        .await
        .unwrap();
    let path = material_path(added["record"]["record_id"].as_str().unwrap());
    assert_matches!(
        harness.accounts.refresh(&path).await,
        Err(Error::Refused { .. })
    );
    let listing = harness.native.run(&user, Operation::List).await.unwrap();
    let metadata = harness.native.metadata(101, None).await.unwrap();
    for value in [added, listing, metadata] {
        assert!(!value.to_string().contains(secret));
    }
    let journal: Vec<String> = sqlx::query_scalar(
        "SELECT digest || coalesce(response::text,'') FROM accounts.credential_mutations",
    )
    .fetch_all(harness.database.pool())
    .await
    .unwrap();
    assert!(
        journal
            .iter()
            .all(|row| !row.contains(secret) && !row.contains(&view::digest(secret)))
    );
}

/// A retry under the same `mutation_id` replays the first write even with a different
/// secret, since the journal digest leaves secrets out. The vault answered `409`.
#[tokio::test]
async fn replay_ignores_a_changed_secret() {
    let harness = Harness::start().await;
    let user = alice();
    harness.clock.set("2027-01-01T00:00:00+00:00");
    let first = harness
        .native
        .run(
            &user,
            add(
                "anthropic-api-key",
                "Key",
                "sk-ant-first-AAAA",
                "changed-secret-0001",
            ),
        )
        .await
        .unwrap();
    let retried = harness
        .native
        .run(
            &user,
            add(
                "anthropic-api-key",
                "Key",
                "sk-ant-other-BBBB",
                "changed-secret-0001",
            ),
        )
        .await
        .unwrap();
    assert_eq!(retried, first);
    assert!(
        first["record"]["fingerprint"]
            .as_str()
            .unwrap()
            .starts_with("AAAA:")
    );
    assert_eq!(harness.versions(101).await.len(), 1);
    assert_eq!(harness.facts().await.len(), 1);
}

/// An `inference.usage` fact advances `last_used` once, however often it is redelivered,
/// and an older fact arriving later never moves it back.
#[tokio::test]
async fn usage_advances_last_used() {
    let harness = Harness::start().await;
    let user = alice();
    harness.clock.set("2027-01-01T00:00:00+00:00");
    let added = harness
        .native
        .run(
            &user,
            add(
                "anthropic-api-key",
                "Key",
                "sk-ant-used-AAAA",
                "usage-add-00000001",
            ),
        )
        .await
        .unwrap();
    let record_id = added["record"]["record_id"].as_str().unwrap().to_owned();
    let fact = |id: &str, finished: &str| {
        serde_json::to_vec(&json!({
            "id": id, "version": 1, "producer": "exchange", "type": "inference.usage",
            "kind": "fact", "occurred_at": finished,
            "data": {"request_id": "00000000-0000-0000-0000-000000000003",
                "session_id": "00000000-0000-0000-0000-000000000002",
                "turn_id": "00000000-0000-0000-0000-000000000001", "grant_id": "personal-1",
                "record_id": record_id, "version": 1, "model": "claude-test", "category": "ok",
                "usage": {"input_tokens": 1, "output_tokens": 1,
                    "cache_creation_input_tokens": null, "cache_read_input_tokens": null},
                "usage_unavailable": null, "started": finished, "finished": finished}
        }))
        .unwrap()
    };
    let deliver = async |raw: Vec<u8>| {
        concurro_events_postgres::process_delivery::<concurro_contracts::InferenceUsage, _>(
            &harness.database,
            "accounts-usage",
            25,
            &raw,
            "fallback",
            &super::UsageHandler,
        )
        .await
        .unwrap();
    };
    let later = fact(
        "00000000-0000-0000-0000-0000000000a2",
        "2027-01-02T00:00:00Z",
    );
    deliver(later.clone()).await;
    deliver(later).await;
    deliver(fact(
        "00000000-0000-0000-0000-0000000000a1",
        "2027-01-01T12:00:00Z",
    ))
    .await;
    let listing = harness.native.run(&user, Operation::List).await.unwrap();
    assert_eq!(
        listing["providers"][0]["entries"][0]["record"]["last_used"],
        "2027-01-02T00:00:00+00:00"
    );
}

/// A fake verifier speaking owner API v1.
#[derive(Default)]
struct FakeVerifier {
    /// Approvals received, as JSON.
    approvals: Vec<Value>,
    /// Answer `503` to approvals while set.
    down: bool,
    /// Codes whose approval is refused with `409`.
    refused: Vec<String>,
}

/// Serve the fake verifier; codes map to relays `relay-<code>`.
async fn fake_verifier(state: Arc<Mutex<FakeVerifier>>) -> String {
    use axum::{
        Json, Router,
        extract::{Path, Query, State},
        http::{HeaderMap, StatusCode},
        routing::{get, post},
    };
    type Shared = State<Arc<Mutex<FakeVerifier>>>;
    let authorized = |headers: &HeaderMap| headers["authorization"] == "Bearer owner-test";
    let app = Router::new()
        .route(
            "/owner/v1/registrations/{code}",
            get(move |headers: HeaderMap, Path(code): Path<String>| async move {
                assert!(authorized(&headers));
                if code == "BCDF-GHJK" || code == "LMNP-QRST" {
                    (StatusCode::OK, Json(json!({"name":"Desk",
                        "routes":["claude-subscription","chatgpt-subscription"],
                        "expires_at":"2027-01-01T00:10:00Z"})))
                } else {
                    (StatusCode::NOT_FOUND, Json(json!({"error":{"code":"not_found","message":"no"}})))
                }
            }),
        )
        .route(
            "/owner/v1/approvals",
            post(move |State(state): Shared, headers: HeaderMap, Json(body): Json<Value>| async move {
                assert!(authorized(&headers));
                let mut state = state.lock().unwrap();
                let code = body["code"].as_str().unwrap().to_owned();
                if state.down {
                    return (StatusCode::SERVICE_UNAVAILABLE, Json(json!({})));
                }
                if state.refused.contains(&code) {
                    return (StatusCode::CONFLICT, Json(json!({"error":{"code":"conflict","message":"no"}})));
                }
                state.approvals.push(body);
                (StatusCode::OK, Json(json!({"relay_id": format!("relay-{code}")})))
            }),
        )
        .route(
            "/owner/v1/relays",
            get(move |headers: HeaderMap, Query(query): Query<std::collections::HashMap<String, String>>| async move {
                assert!(authorized(&headers));
                assert_eq!(query["user_id"], "101");
                Json(json!({"started":"2027-01-01T00:00:00Z","checked":"2027-01-01T01:00:00Z",
                    "relays":[
                        {"relay_id":"relay-LMNP-QRST","idle":1,"last_seen":"2027-01-01T00:59:00Z",
                         "active":[{"slot":1,"route":"claude-subscription","entry_id":"e","version":2,
                            "since":"2027-01-01T00:30:00Z","task":{"repository":"acme/demo","issue":4}}]},
                        {"relay_id":"relay-someone-else","idle":0,"last_seen":null,"active":[]}
                    ]}))
            }),
        )
        .with_state(state);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(listener, app).await });
    url
}

fn relay(code: &str, mutation: &str) -> Operation {
    Operation::Relay(
        serde_json::from_value(json!({"code":code,"name":"Desk","mutation_id":mutation,
            "routes":["claude-subscription","chatgpt-subscription"]}))
        .unwrap(),
    )
}

/// Approval and device replacement through owner API v1: relay-custody entries with their
/// relay identity, one fact per entry, retired previous versions, and live status for the
/// owner's current relays only.
#[tokio::test]
async fn relay_approval_and_device_replacement() {
    let harness = Harness::start().await;
    let verifier = Arc::new(Mutex::new(FakeVerifier::default()));
    let url = fake_verifier(verifier.clone()).await;
    let native = harness.native.clone().with_verifier(super::Verifier::new(
        reqwest::Client::new(),
        &url,
        "owner-test".to_owned(),
    ));
    let user = alice();
    harness.clock.set("2027-01-01T00:00:00+00:00");
    assert_eq!(
        harness
            .native
            .run(&user, Operation::RelayStatus)
            .await
            .unwrap(),
        json!({"service":"unconfigured","relays":[]})
    );
    let pending = native
        .run(
            &user,
            Operation::RelayPending(serde_json::from_value(json!({"code":"BCDF-GHJK"})).unwrap()),
        )
        .await
        .unwrap();
    assert_eq!(
        pending,
        json!({"name":"Desk","routes":["chatgpt-subscription","claude-subscription"],
            "expires_at":"2027-01-01T00:10:00+00:00"})
    );
    let unknown = native
        .run(
            &user,
            Operation::RelayPending(serde_json::from_value(json!({"code":"ZZZZ-ZZZZ"})).unwrap()),
        )
        .await;
    assert_matches!(
        unknown,
        Err(super::CredentialError(reqwest::StatusCode::NOT_FOUND, _))
    );

    let approved = native
        .run(&user, relay("BCDF-GHJK", "relay-approval-001"))
        .await
        .unwrap();
    let again = native
        .run(&user, relay("BCDF-GHJK", "relay-approval-001"))
        .await
        .unwrap();
    assert_eq!(approved, again);
    let entries = approved["entries"].as_object().unwrap();
    assert_eq!(entries.len(), 2);
    for (route, entry) in entries {
        assert_eq!(entry["route"], route.as_str());
        assert_eq!(entry["record"]["custody"], "relay");
        assert_eq!(entry["record"]["relay_id"], "relay-BCDF-GHJK");
        assert_eq!(entry["record"]["version"], 1);
    }
    let sent = verifier.lock().unwrap().approvals[0].clone();
    assert_eq!(sent["user"], json!({"id":101,"login":"alice"}));
    assert_eq!(sent["bindings"].as_array().unwrap().len(), 2);
    let metadata = harness.native.metadata(101, None).await.unwrap();
    assert!(
        metadata["credentials"]
            .as_array()
            .unwrap()
            .iter()
            .all(|credential| credential["relay_id"] == "relay-BCDF-GHJK")
    );

    harness.clock.set("2027-01-01T00:20:00+00:00");
    let replace = Operation::Device(
        serde_json::from_value(
            json!({"code":"LMNP-QRST","mutation_id":"relay-device-00001",
            "entries": entries.values().map(|entry| json!({"id":entry["entry_id"],
                "revision":entry["revision"]})).collect::<Vec<_>>()}),
        )
        .unwrap(),
    );
    let replaced = native.run(&user, replace).await.unwrap();
    for entry in replaced["entries"].as_object().unwrap().values() {
        assert_eq!(entry["record"]["relay_id"], "relay-LMNP-QRST");
        assert_eq!(entry["record"]["version"], 2);
        assert_eq!(entry["position"], 0);
    }
    let facts = harness.facts().await;
    let changes = facts
        .iter()
        .map(|fact| fact["change"].as_str().unwrap())
        .collect::<Vec<_>>();
    assert_eq!(changes, ["added", "added", "replaced", "replaced"]);
    for fact in &facts[2..] {
        let revoked = fact["revoked"].as_array().unwrap();
        assert_eq!(
            (
                revoked.len(),
                &revoked[0]["custody"],
                &revoked[0]["version"]
            ),
            (1, &json!("relay"), &json!(1))
        );
    }

    let status = native.run(&user, Operation::RelayStatus).await.unwrap();
    assert_eq!(status["service"], "available");
    let relays = status["relays"].as_array().unwrap();
    assert_eq!(relays.len(), 1, "only the owner's current relay");
    assert_eq!(relays[0]["relay"], "relay-LMNP-QRST");
    assert_eq!(relays[0]["last_seen"], "2027-01-01T00:59:00+00:00");
    assert_eq!(
        relays[0]["active"][0]["task"],
        json!({"repository":"acme/demo","issue":4})
    );
}

/// A reservation left by a failed approval is settled by repeating the identical approval;
/// one the verifier refuses is released.
#[tokio::test]
async fn relay_reservations_settle() {
    let harness = Harness::start().await;
    let verifier = Arc::new(Mutex::new(FakeVerifier {
        down: true,
        ..FakeVerifier::default()
    }));
    let url = fake_verifier(verifier.clone()).await;
    let native = harness.native.clone().with_verifier(super::Verifier::new(
        reqwest::Client::new(),
        &url,
        "owner-test".to_owned(),
    ));
    let user = alice();
    harness.clock.set("2027-01-01T00:00:00+00:00");
    let failed = native
        .run(&user, relay("BCDF-GHJK", "relay-settle-00001"))
        .await;
    assert_matches!(
        failed,
        Err(super::CredentialError(
            reqwest::StatusCode::SERVICE_UNAVAILABLE,
            _
        ))
    );
    let refused = native
        .run(&user, relay("LMNP-QRST", "relay-settle-00002"))
        .await;
    assert!(refused.is_err());
    // An ID a reservation holds can't be reused by a different request.
    let reused = native
        .run(
            &user,
            add(
                "anthropic-api-key",
                "Key",
                "sk-ant-reused-AAAA",
                "relay-settle-00001",
            ),
        )
        .await;
    assert_matches!(
        reused,
        Err(super::CredentialError(reqwest::StatusCode::CONFLICT, _))
    );
    sqlx::query("UPDATE accounts.relay_intents SET reserved_at = now() - interval '1 hour'")
        .execute(harness.database.pool())
        .await
        .unwrap();
    {
        let mut state = verifier.lock().unwrap();
        state.down = false;
        state.refused.push("LMNP-QRST".to_owned());
    }
    native.settle_intents().await.unwrap();
    let remaining: i64 = sqlx::query_scalar("SELECT count(*) FROM accounts.relay_intents")
        .fetch_one(harness.database.pool())
        .await
        .unwrap();
    assert_eq!(remaining, 0);
    let listing = native.run(&user, Operation::List).await.unwrap();
    let relays = listing["providers"]
        .as_array()
        .unwrap()
        .iter()
        .flat_map(|group| group["entries"].as_array().unwrap())
        .map(|entry| entry["record"]["relay_id"].as_str().unwrap().to_owned())
        .collect::<Vec<_>>();
    assert_eq!(relays, ["relay-BCDF-GHJK", "relay-BCDF-GHJK"]);
    // The settled mutation answers its retry with the committed result.
    let retried = native
        .run(&user, relay("BCDF-GHJK", "relay-settle-00001"))
        .await
        .unwrap();
    assert_eq!(retried["entries"].as_object().unwrap().len(), 2);
    assert_eq!(harness.facts().await.len(), 2);
}

/// Relay setup commands quote an IPv6 server as the Python backoffice did.
#[test]
fn relay_setup() {
    let fixture: Value =
        serde_json::from_str(include_str!("../../tests/credentials_python.json")).unwrap();
    let ipv6 = super::Credentials::new(
        reqwest::Client::new(),
        Some("http://vault.invalid".to_owned()),
        Some("[::1]:80".to_owned()),
        Some("https://example.invalid/relay".to_owned()),
    )
    .unwrap();
    assert_eq!(ipv6.setup, fixture["relay_setup_ipv6"]);
}
