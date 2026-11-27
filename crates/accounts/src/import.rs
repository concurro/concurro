//! `accounts capture-vault` and `accounts import-vault`: moving a stopped vault's
//! credential store into the native tables and OpenBao, then checking the result against
//! the vault's own metadata answers.

use std::{
    collections::{BTreeMap, BTreeSet, HashMap},
    path::{Path, PathBuf},
};

use aes_gcm::{
    Aes256Gcm, KeyInit, Nonce,
    aead::{Aead, Payload},
};
use anyhow::{Context, Result, bail, ensure};
use chrono::{DateTime, Timelike, Utc};
use concurro_accounts_postgres::{
    credentials::VersionRow,
    vault_import::{self, SealedRow, VaultSource},
};
use concurro_db::Database;
use concurro_secrets::{ExposeSecret, OpenBao, SecretString};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::credentials::{material_path, read_metadata, view};

/// The vault's AES-GCM nonce length.
const NONCE: usize = 12;

/// The vault's metadata answers, recorded before it stopped.
#[derive(Debug, Deserialize, Serialize)]
pub struct Capture {
    /// When the current reads were taken.
    pub now: String,
    /// Each owner's answer at each recorded instant, and now (`at` absent).
    pub reads: Vec<Read>,
}

/// One recorded metadata answer.
#[derive(Debug, Deserialize, Serialize)]
pub struct Read {
    /// GitHub owner ID.
    pub user_id: i64,
    /// The instant read, or `None` for now.
    pub at: Option<String>,
    /// The vault's answer.
    pub response: Value,
}

/// Every owner in a vault copy, and the instants worth reading their metadata at: each
/// policy revision, version creation and retirement.
pub async fn moments(vault_db: &Path) -> Result<BTreeMap<i64, BTreeSet<String>>> {
    let source = VaultSource::open(vault_db).await?;
    let mut moments: BTreeMap<i64, BTreeSet<String>> = BTreeMap::new();
    for policy in source.policies().await? {
        let instants = moments.entry(policy.user_id).or_default();
        if !policy.at.is_empty() {
            instants.insert(policy.at);
        }
    }
    for version in source.versions().await? {
        let instants = moments.entry(version.user_id).or_default();
        instants.insert(version.created);
        instants.extend(version.retired);
    }
    Ok(moments)
}

/// Record the live vault's metadata answers for every owner in `vault_db`, at every moment
/// and now.
pub async fn capture(vault_url: &str, service_token: &str, vault_db: &Path) -> Result<Capture> {
    let client = reqwest::Client::new();
    let now = view::stamp(Utc::now().with_nanosecond(0).expect("whole second"));
    let mut reads = Vec::new();
    for (user_id, instants) in moments(vault_db).await? {
        for at in instants.into_iter().map(Some).chain([None]) {
            let mut request = client
                .get(format!(
                    "{}/metadata/{user_id}",
                    vault_url.trim_end_matches('/')
                ))
                .bearer_auth(service_token);
            if let Some(at) = &at {
                request = request.query(&[("at", at)]);
            }
            let response = request.send().await?.error_for_status()?.json().await?;
            reads.push(Read {
                user_id,
                at,
                response,
            });
        }
    }
    Ok(Capture { now, reads })
}

/// Where to read the vault and its keys.
pub struct ImportOptions {
    /// The stopped vault's database copy.
    pub vault_db: PathBuf,
    /// The vault's master key file (`VAULT_MASTER_KEY_FILE`).
    pub master_key: PathBuf,
    /// The vault's keyring file (`VAULT_KEYRING_FILE`), if it has one.
    pub keyring: Option<PathBuf>,
    /// The capture recorded before the vault stopped.
    pub capture: PathBuf,
}

/// An import's outcome: identities, counts and pass or fail, never secret material.
#[derive(Debug, Serialize)]
pub struct Report {
    /// SHA-256 of the vault database copy.
    pub source_sha256: String,
    /// Whether this run committed the metadata or found it already committed.
    pub metadata: &'static str,
    /// Versions whose material this run copied.
    pub copied: usize,
    /// Row counts, vault and native, per table.
    pub counts: BTreeMap<&'static str, [i64; 2]>,
    /// Recorded reads checked.
    pub reads_checked: usize,
    /// Recorded reads the native store answered differently, as `user_id` and `at`.
    pub reads_mismatched: Vec<(i64, Option<String>)>,
    /// `pass` or `fail`.
    pub result: &'static str,
}

/// The vault's wrapping keys by name.
struct Keyring(HashMap<String, Aes256Gcm>);

impl Keyring {
    /// Load the master key, or the keyring's named keys, as the vault's `load_envelope`.
    fn load(master: &Path, keyring: Option<&Path>) -> Result<Self> {
        let Some(keyring) = keyring else {
            return Ok(Self(HashMap::from([("legacy".to_owned(), key(master)?)])));
        };
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct File {
            #[allow(dead_code)]
            active: String,
            keys: HashMap<String, PathBuf>,
        }
        let file: File =
            serde_json::from_slice(&std::fs::read(keyring)?).context("invalid wrapping keyring")?;
        let keys = file
            .keys
            .iter()
            .map(|(name, path)| Ok((name.clone(), key(path)?)))
            .collect::<Result<_>>()?;
        Ok(Self(keys))
    }

    /// Open one version's sealed payload: `(secret, account)`.
    fn open(&self, sealed: &SealedRow, aad: &[u8]) -> Result<(String, Option<Value>)> {
        let wrapping = self
            .0
            .get(&sealed.key_id)
            .with_context(|| format!("no wrapping key {}", sealed.key_id))?;
        ensure!(
            sealed.wrapped_key.len() > NONCE && sealed.nonce.len() == NONCE,
            "malformed envelope"
        );
        let (wrap_nonce, wrapped) = sealed.wrapped_key.split_at(NONCE);
        let wrap_nonce: [u8; NONCE] = wrap_nonce.try_into().expect("split at the nonce length");
        let nonce: [u8; NONCE] = sealed
            .nonce
            .as_slice()
            .try_into()
            .expect("checked nonce length");
        let data_key = wrapping
            .decrypt(&Nonce::from(wrap_nonce), Payload { msg: wrapped, aad })
            .map_err(|_| anyhow::anyhow!("envelope does not open"))?;
        let plaintext = Aes256Gcm::new_from_slice(&data_key)
            .map_err(|_| anyhow::anyhow!("malformed data key"))?
            .decrypt(
                &Nonce::from(nonce),
                Payload {
                    msg: &sealed.ciphertext,
                    aad,
                },
            )
            .map_err(|_| anyhow::anyhow!("envelope does not open"))?;
        let plaintext = String::from_utf8(plaintext).context("payload is not text")?;
        if sealed.envelope_format == 2 {
            let mut payload: Value =
                serde_json::from_str(&plaintext).context("malformed payload")?;
            let secret = payload["secret"]
                .as_str()
                .context("payload has no secret")?
                .to_owned();
            let account = Some(payload["account"].take()).filter(|account| !account.is_null());
            Ok((secret, account))
        } else {
            Ok((plaintext, None))
        }
    }
}

/// One 32-byte wrapping key from a file of 64 hex characters.
fn key(path: &Path) -> Result<Aes256Gcm> {
    let text = std::fs::read_to_string(path).with_context(|| format!("read {}", path.display()))?;
    let bytes = hex::decode(text.trim())
        .ok()
        .filter(|bytes| bytes.len() == 32);
    let bytes = bytes.context("master key file must hold 32 bytes as 64 hex characters")?;
    Ok(Aes256Gcm::new_from_slice(&bytes).expect("32-byte key"))
}

/// Import the vault copy's metadata in one transaction, copy its material to OpenBao with
/// the `migrator` client, then compare every recorded read and table count. Rerunning
/// resumes, and adds nothing to a finished import.
pub async fn import(
    database: &Database,
    migrator: &OpenBao,
    options: &ImportOptions,
) -> Result<Report> {
    let source_sha256 = hex::encode(Sha256::digest(
        std::fs::read(&options.vault_db).context("read the vault database copy")?,
    ));
    let source = VaultSource::open(&options.vault_db).await?;
    ensure!(
        source.relay_intents().await? == 0,
        "the vault copy holds a relay approval in progress; settle it before importing"
    );
    let capture: Capture = serde_json::from_slice(&std::fs::read(&options.capture)?)
        .context("invalid capture file")?;
    let keyring = Keyring::load(&options.master_key, options.keyring.as_deref())?;
    let metadata = match vault_import::import_state(database, &source_sha256).await? {
        Some(true) => "already imported",
        Some(false) => bail!("an earlier import of this copy did not commit its metadata"),
        None => {
            ensure!(
                !vault_import::native_has_data(database).await?,
                "the native credential tables already hold data from another import"
            );
            let mut versions = source.versions().await?;
            for version in &mut versions {
                ensure!(
                    !version.entry_id.is_empty(),
                    "version {}/{}/{} has no entry",
                    version.user_id,
                    version.route,
                    version.version
                );
                version.record_id =
                    view::record_id(version.user_id, &version.route, version.version);
            }
            vault_import::import_metadata(
                database,
                &source_sha256,
                &source.entries().await?,
                &source.lists().await?,
                &source.policies().await?,
                &versions,
            )
            .await?;
            "imported"
        }
    };
    let mut copied = 0;
    for version in vault_import::importing(database).await? {
        copy(&source, &keyring, migrator, &version).await?;
        vault_import::copied(database, &version.record_id).await?;
        copied += 1;
    }
    let now = view::parse(&capture.now).context("invalid capture time")?;
    let reads_mismatched = verify(database, &capture, now).await?;
    let (vault, native) = (
        source.counts().await?,
        vault_import::native_counts(database).await?,
    );
    let counts = BTreeMap::from([
        ("entries", [vault.entries, native.entries]),
        ("lists", [vault.lists, native.lists]),
        ("policies", [vault.policies, native.policies]),
        ("versions", [vault.versions, native.versions]),
    ]);
    let pass = reads_mismatched.is_empty() && vault == native;
    if pass {
        vault_import::finish_import(database, &source_sha256).await?;
    }
    Ok(Report {
        source_sha256,
        metadata,
        copied,
        counts,
        reads_checked: capture.reads.len(),
        reads_mismatched,
        result: if pass { "pass" } else { "fail" },
    })
}

/// Copy one version's material, checked against its fingerprint and read back. A path
/// that already exists is compared instead of written.
async fn copy(
    source: &VaultSource,
    keyring: &Keyring,
    migrator: &OpenBao,
    version: &VersionRow,
) -> Result<()> {
    let record_id = &version.record_id;
    let sealed = source
        .sealed(version.user_id, &version.route, version.version)
        .await?
        .with_context(|| format!("material of {record_id} is missing from the vault copy"))?;
    let aad = format!(
        "concurro-vault/{}/{}/{}",
        version.user_id, version.route, version.version
    );
    let (secret, account) = keyring
        .open(&sealed, aad.as_bytes())
        .with_context(|| format!("material of {record_id} does not open"))?;
    ensure!(
        view::fingerprint(&secret) == version.fingerprint,
        "material of {record_id} does not match its fingerprint"
    );
    let mut fields = BTreeMap::from([("secret".to_owned(), SecretString::from(secret))]);
    if let Some(account) = account {
        fields.insert(
            "account".to_owned(),
            SecretString::from(account.to_string()),
        );
    }
    let path = material_path(record_id);
    if migrator.metadata(&path).await?.is_none() {
        migrator.create(&path, &fields).await?;
    }
    let stored = migrator.refresh(&path).await?;
    let names = stored
        .field_names()
        .map(str::to_owned)
        .collect::<BTreeSet<_>>();
    let same = names == fields.keys().cloned().collect()
        && fields.iter().all(|(name, value)| {
            stored
                .field(name)
                .is_ok_and(|stored| stored.expose_secret() == value.expose_secret())
        });
    ensure!(same, "OpenBao holds different material for {record_id}");
    Ok(())
}

/// The recorded reads the native store answers differently. `relay_id`, which the vault
/// doesn't return, is left out of the comparison.
async fn verify(
    database: &Database,
    capture: &Capture,
    now: DateTime<Utc>,
) -> Result<Vec<(i64, Option<String>)>> {
    let mut mismatched = Vec::new();
    for read in &capture.reads {
        let mut actual = read_metadata(database, read.user_id, read.at.as_deref(), now)
            .await
            .map_err(|error| anyhow::anyhow!("metadata read failed: {error:?}"))?;
        for credential in actual["credentials"].as_array_mut().into_iter().flatten() {
            credential
                .as_object_mut()
                .map(|object| object.remove("relay_id"));
        }
        if actual != read.response {
            mismatched.push((read.user_id, read.at.clone()));
        }
    }
    Ok(mismatched)
}

#[cfg(test)]
mod tests {
    use std::assert_matches;

    use reqwest::Method;
    use serde_json::json;

    use super::*;
    use crate::credentials::{
        Operation,
        tests::{Harness, assert_same, call, translate},
    };
    use crate::github::User;

    const FIXTURES: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/vault_history");

    /// A writable copy of the recorded vault database and its options.
    fn options(name: &str) -> ImportOptions {
        let directory =
            std::env::temp_dir().join(format!("concurro-import-{}-{name}", std::process::id()));
        std::fs::create_dir_all(&directory).unwrap();
        let vault_db = directory.join("vault.db");
        std::fs::copy(format!("{FIXTURES}/vault.db"), &vault_db).unwrap();
        ImportOptions {
            vault_db,
            master_key: format!("{FIXTURES}/master.key").into(),
            keyring: None,
            capture: format!("{FIXTURES}/capture.json").into(),
        }
    }

    /// A client with the `migrator` policy, its role created as the platform guide says.
    async fn migrator(harness: &mut Harness) -> OpenBao {
        let (status, _) = call(
            harness.openbao.url(),
            Some(&harness.admin),
            Method::POST,
            "auth/approle/role/migrator",
            Some(json!({"token_policies":"migrator","token_ttl":"1h","token_max_ttl":"4h"})),
        )
        .await;
        assert!(status.is_success());
        harness.service("migrator").await
    }

    /// Write a path's material directly.
    async fn plant(migrator: &OpenBao, record_id: &str, secret: &str) {
        let fields = BTreeMap::from([("secret".to_owned(), SecretString::from(secret.to_owned()))]);
        migrator
            .create(&material_path(record_id), &fields)
            .await
            .unwrap();
    }

    /// A recorded vault, imported with an interruption mid-material, answers every recorded
    /// metadata read and owner listing as the vault did; running it again adds nothing.
    #[tokio::test]
    async fn import_matches_vault() {
        let mut harness = Harness::start().await;
        let migrator = migrator(&mut harness).await;
        let options = options("parity");
        // A crash after one create, before it was marked: its path is compared, not written.
        let copied = view::record_id(202, "claude-subscription", 1);
        plant(&migrator, &copied, "sk-ant-oat-legacy-0001").await;
        // A different value at another path stops the import partway, naming it.
        let blocked = view::record_id(202, "openai-api-key", 1);
        plant(&migrator, &blocked, "something else").await;
        let stopped = import(&harness.database, &migrator, &options)
            .await
            .unwrap_err();
        assert!(format!("{stopped:#}").contains(&blocked), "{stopped:#}");
        harness
            .accounts
            .destroy(&material_path(&blocked))
            .await
            .unwrap();

        let report = import(&harness.database, &migrator, &options)
            .await
            .unwrap();
        assert_eq!(report.metadata, "already imported");
        assert_eq!(report.result, "pass", "{report:?}");
        assert!(report.copied > 0);
        assert!(
            report
                .counts
                .values()
                .all(|[vault, native]| vault == native)
        );
        let rerun = import(&harness.database, &migrator, &options)
            .await
            .unwrap();
        assert_eq!(
            (rerun.metadata, rerun.copied, rerun.result),
            ("already imported", 0, "pass")
        );

        let capture: Value =
            serde_json::from_str(&std::fs::read_to_string(&options.capture).unwrap()).unwrap();
        harness.clock.set(capture["now"].as_str().unwrap());
        for read in capture["reads"].as_array().unwrap() {
            let mut actual = harness
                .native
                .metadata(read["user_id"].as_i64().unwrap(), read["at"].as_str())
                .await
                .unwrap();
            let relay = actual["credentials"]
                .as_array()
                .unwrap()
                .iter()
                .filter_map(|credential| credential.get("relay_id").cloned())
                .collect::<Vec<_>>();
            if read["user_id"] == 303 && read["at"].is_null() {
                assert!(
                    !relay.is_empty()
                        && relay
                            .iter()
                            .all(|id| id.as_str().is_some_and(|id| id.starts_with("relay-")))
                );
            }
            for credential in actual["credentials"].as_array_mut().unwrap() {
                credential.as_object_mut().unwrap().remove("relay_id");
            }
            assert_same(
                &actual,
                &read["response"],
                &format!("{} at {}", read["user_id"], read["at"]),
            );
        }
        for listing in capture["listings"].as_array().unwrap() {
            let user = User {
                id: listing["user_id"].as_i64().unwrap(),
                login: "owner".to_owned(),
                name: None,
                avatar_url: None,
            };
            let actual = harness.native.run(&user, Operation::List).await.unwrap();
            assert_same(
                &actual,
                &translate(&listing["listing"], &[]),
                &format!("listing of {}", user.id),
            );
        }
        // Every imported version with material is stored, and reads back only to the migrator.
        let versions = harness.versions(101).await;
        assert!(
            versions
                .iter()
                .all(|version| version.material != "importing")
        );
        let stored = versions
            .iter()
            .find(|version| version.material == "stored")
            .unwrap();
        assert_matches!(
            harness
                .accounts
                .refresh(&material_path(&stored.record_id))
                .await,
            Err(concurro_secrets::Error::Refused { .. })
        );
        let account = migrator
            .refresh(&material_path(&view::record_id(
                101,
                "chatgpt-subscription",
                1,
            )))
            .await
            .unwrap();
        assert_eq!(
            account.field("account").unwrap().expose_secret(),
            r#"{"chatgpt_account_id":"acct-0001"}"#
        );
    }

    /// A version whose material doesn't match its fingerprint stops the import, naming it;
    /// a copy with a relay approval in progress isn't imported.
    #[tokio::test]
    async fn import_refuses_inconsistent_copies() {
        let mut harness = Harness::start().await;
        let migrator = migrator(&mut harness).await;
        let options = options("refusals");
        let copy = sqlx::SqlitePool::connect(&format!("sqlite://{}", options.vault_db.display()))
            .await
            .unwrap();
        sqlx::query("UPDATE credentials SET fingerprint='0001:00000000' WHERE user_id=202 AND route='openai-api-key'")
            .execute(&copy)
            .await
            .unwrap();
        let stopped = import(&harness.database, &migrator, &options)
            .await
            .unwrap_err();
        let named = view::record_id(202, "openai-api-key", 1);
        assert!(format!("{stopped:#}").contains(&named), "{stopped:#}");
        assert!(!format!("{stopped:#}").contains("sk-proj"));
        sqlx::query("INSERT INTO relay_approval_intents VALUES(303,'pending-intent','d','claude-subscription',9,'{}')")
            .execute(&copy)
            .await
            .unwrap();
        copy.close().await;
        let refused = import(&harness.database, &migrator, &options)
            .await
            .unwrap_err();
        assert!(
            format!("{refused:#}").contains("relay approval"),
            "{refused:#}"
        );
    }
}
