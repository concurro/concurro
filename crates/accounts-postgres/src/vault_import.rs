//! SQL for `accounts import-vault`: reading a stopped vault's SQLite copy and writing its
//! metadata into the native credential tables.

use std::path::Path;

use concurro_db::Database;
use sqlx::{
    FromRow, Row,
    sqlite::{SqliteConnectOptions, SqlitePool, SqlitePoolOptions},
};

use crate::credentials::{EntryRow, VersionRow};

/// One provider list as the vault holds it.
#[derive(Clone, Debug, FromRow)]
pub struct ListRow {
    /// GitHub owner ID.
    pub user_id: i64,
    /// Provider.
    pub provider: String,
    /// Current revision.
    pub revision: i64,
    /// Live entry IDs, JSON text.
    pub entries: String,
}

/// One policy revision as the vault holds it; an empty `at` is the legacy boundary.
#[derive(Clone, Debug, FromRow)]
pub struct VaultPolicyRow {
    /// GitHub owner ID.
    pub user_id: i64,
    /// Provider.
    pub provider: String,
    /// List revision.
    pub revision: i64,
    /// When it took effect, or empty.
    pub at: String,
    /// Policy document, JSON text.
    pub document: String,
}

/// One version's sealed material.
#[derive(Debug, FromRow)]
pub struct SealedRow {
    /// Wrap nonce followed by the wrapped data key.
    pub wrapped_key: Vec<u8>,
    /// Data nonce.
    pub nonce: Vec<u8>,
    /// Encrypted payload.
    pub ciphertext: Vec<u8>,
    /// The wrapping key's name.
    pub key_id: String,
    /// 1: the bare secret; 2: JSON `{secret, account}`.
    pub envelope_format: i64,
}

/// Row counts of the tables the import copies.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Counts {
    /// Entries, removed ones included.
    pub entries: i64,
    /// Provider lists.
    pub lists: i64,
    /// Policy revisions.
    pub policies: i64,
    /// Versions.
    pub versions: i64,
}

/// A read-only vault database copy.
pub struct VaultSource {
    pool: SqlitePool,
}

impl VaultSource {
    /// Open a vault database copy read-only.
    pub async fn open(path: &Path) -> Result<Self, sqlx::Error> {
        let options = SqliteConnectOptions::new().filename(path).read_only(true);
        Ok(Self {
            pool: SqlitePoolOptions::new()
                .max_connections(1)
                .connect_with(options)
                .await?,
        })
    }

    /// Relay approvals reserved but never committed or released.
    pub async fn relay_intents(&self) -> Result<i64, sqlx::Error> {
        sqlx::query_scalar(
            "SELECT (SELECT COUNT(*) FROM relay_approval_intents) \
             + (SELECT COUNT(*) FROM relay_approval_routes)",
        )
        .fetch_one(&self.pool)
        .await
    }

    /// Every entry.
    pub async fn entries(&self) -> Result<Vec<EntryRow>, sqlx::Error> {
        let rows = sqlx::query("SELECT * FROM credential_entries ORDER BY entry_id")
            .fetch_all(&self.pool)
            .await?;
        rows.iter()
            .map(|row| {
                Ok(EntryRow {
                    entry_id: row.try_get("entry_id")?,
                    user_id: row.try_get("user_id")?,
                    provider: row.try_get("provider")?,
                    route: row.try_get("route")?,
                    name: row.try_get("name")?,
                    enabled: row.try_get::<i64, _>("enabled")? != 0,
                    roles: row.try_get("roles")?,
                    profiles: row.try_get("profiles")?,
                    revision: row.try_get("revision")?,
                    created: row.try_get("created")?,
                    updated: row.try_get("updated")?,
                    removed: row.try_get("removed")?,
                })
            })
            .collect()
    }

    /// Every provider list.
    pub async fn lists(&self) -> Result<Vec<ListRow>, sqlx::Error> {
        sqlx::query_as("SELECT user_id,provider,revision,entries FROM credential_lists")
            .fetch_all(&self.pool)
            .await
    }

    /// Every policy revision.
    pub async fn policies(&self) -> Result<Vec<VaultPolicyRow>, sqlx::Error> {
        sqlx::query_as("SELECT user_id,provider,revision,at,document FROM credential_policy")
            .fetch_all(&self.pool)
            .await
    }

    /// Every version with its metadata, and whether its sealed material remains. Relay
    /// custody has no material; nulled material was destroyed.
    pub async fn versions(&self) -> Result<Vec<VersionRow>, sqlx::Error> {
        let rows = sqlx::query(
            "SELECT c.user_id,c.route,c.version,c.login,c.status,c.fingerprint,c.created,c.updated, \
             c.ciphertext IS NOT NULL AS sealed,m.entry_id,m.kind,m.models,m.retired,m.account_label, \
             m.verified_account,m.status_reason,m.validated,m.expires_at, \
             COALESCE(m.expiry_known,0) AS expiry_known,COALESCE(m.token_revision,1) AS token_revision, \
             m.last_used,COALESCE(m.custody,'vault') AS custody,m.relay_id \
             FROM credentials c LEFT JOIN credential_metadata m \
             ON c.user_id=m.user_id AND c.route=m.route AND c.version=m.version \
             ORDER BY c.user_id,c.route,c.version",
        )
        .fetch_all(&self.pool)
        .await?;
        rows.iter()
            .map(|row| {
                let custody: String = row.try_get("custody")?;
                let material = if custody == "relay" {
                    "none"
                } else if row.try_get::<bool, _>("sealed")? {
                    "importing"
                } else {
                    "destroyed"
                };
                let (user_id, route, version) = (
                    row.try_get::<i64, _>("user_id")?,
                    row.try_get::<String, _>("route")?,
                    row.try_get::<i64, _>("version")?,
                );
                Ok(VersionRow {
                    record_id: String::new(),
                    user_id,
                    route,
                    version,
                    entry_id: row
                        .try_get::<Option<String>, _>("entry_id")?
                        .unwrap_or_default(),
                    login: row.try_get("login")?,
                    status: row.try_get("status")?,
                    fingerprint: row.try_get("fingerprint")?,
                    created: row.try_get("created")?,
                    updated: row.try_get("updated")?,
                    kind: row.try_get("kind")?,
                    models: row.try_get("models")?,
                    retired: row.try_get("retired")?,
                    account_label: row.try_get("account_label")?,
                    verified_account: row.try_get("verified_account")?,
                    status_reason: row.try_get("status_reason")?,
                    validated: row.try_get("validated")?,
                    expires_at: row.try_get("expires_at")?,
                    expiry_known: row.try_get::<i64, _>("expiry_known")? != 0,
                    token_revision: row.try_get("token_revision")?,
                    last_used: row.try_get("last_used")?,
                    custody,
                    relay_id: row.try_get("relay_id")?,
                    material: material.to_owned(),
                    activated: true,
                    mutation_id: None,
                    expected_revision: None,
                    pending_entry: None,
                })
            })
            .collect()
    }

    /// One version's sealed material, if it remains.
    pub async fn sealed(
        &self,
        user_id: i64,
        route: &str,
        version: i64,
    ) -> Result<Option<SealedRow>, sqlx::Error> {
        sqlx::query_as(
            "SELECT c.wrapped_key,c.nonce,c.ciphertext,COALESCE(m.key_id,'legacy') AS key_id, \
             COALESCE(m.envelope_format,1) AS envelope_format FROM credentials c \
             LEFT JOIN credential_metadata m \
             ON c.user_id=m.user_id AND c.route=m.route AND c.version=m.version \
             WHERE c.user_id=$1 AND c.route=$2 AND c.version=$3 AND c.ciphertext IS NOT NULL",
        )
        .bind(user_id)
        .bind(route)
        .bind(version)
        .fetch_optional(&self.pool)
        .await
    }

    /// Row counts of the copied tables.
    pub async fn counts(&self) -> Result<Counts, sqlx::Error> {
        let row = sqlx::query(
            "SELECT (SELECT COUNT(*) FROM credential_entries),(SELECT COUNT(*) FROM credential_lists), \
             (SELECT COUNT(*) FROM credential_policy),(SELECT COUNT(*) FROM credentials)",
        )
        .fetch_one(&self.pool)
        .await?;
        Ok(Counts {
            entries: row.try_get(0)?,
            lists: row.try_get(1)?,
            policies: row.try_get(2)?,
            versions: row.try_get(3)?,
        })
    }
}

/// Where an import of this source stands: `None` if never started, otherwise whether its
/// metadata was committed.
pub async fn import_state(database: &Database, source: &str) -> Result<Option<bool>, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT metadata_at IS NOT NULL FROM accounts.vault_imports WHERE source_sha256=$1",
    )
    .bind(source)
    .fetch_optional(database.pool())
    .await
}

/// Whether the native tables hold any credential data.
pub async fn native_has_data(database: &Database) -> Result<bool, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT EXISTS (SELECT 1 FROM accounts.credential_entries) \
         OR EXISTS (SELECT 1 FROM accounts.credential_versions)",
    )
    .fetch_one(database.pool())
    .await
}

/// Write all imported metadata in one transaction and record the import.
pub async fn import_metadata(
    database: &Database,
    source: &str,
    entries: &[EntryRow],
    lists: &[ListRow],
    policies: &[VaultPolicyRow],
    versions: &[VersionRow],
) -> Result<(), sqlx::Error> {
    let mut work = database.begin().await?;
    for entry in entries {
        crate::credentials::insert_entry(&mut work, entry).await?;
    }
    for list in lists {
        sqlx::query(
            "INSERT INTO accounts.credential_lists (user_id,provider,revision,entries) \
             VALUES ($1,$2,$3,$4)",
        )
        .bind(list.user_id)
        .bind(&list.provider)
        .bind(list.revision)
        .bind(&list.entries)
        .execute(work.connection())
        .await?;
    }
    for policy in policies {
        sqlx::query(
            "INSERT INTO accounts.credential_policy (user_id,provider,revision,at,document) \
             VALUES ($1,$2,$3,NULLIF($4,''),$5)",
        )
        .bind(policy.user_id)
        .bind(&policy.provider)
        .bind(policy.revision)
        .bind(&policy.at)
        .bind(&policy.document)
        .execute(work.connection())
        .await?;
    }
    for version in versions {
        crate::credentials::insert_version(&mut work, version).await?;
    }
    sqlx::query("INSERT INTO accounts.vault_imports (source_sha256,metadata_at) VALUES ($1,now())")
        .bind(source)
        .execute(work.connection())
        .await?;
    work.commit().await
}

/// Imported versions whose material is still to be copied.
pub async fn importing(database: &Database) -> Result<Vec<VersionRow>, sqlx::Error> {
    let mut work = database.begin().await?;
    let versions = sqlx::query_scalar::<_, i64>(
        "SELECT DISTINCT user_id FROM accounts.credential_versions WHERE material='importing'",
    )
    .fetch_all(work.connection())
    .await?;
    let mut found = Vec::new();
    for user_id in versions {
        found.extend(
            crate::credentials::versions(&mut work, user_id)
                .await?
                .into_iter()
                .filter(|version| version.material == "importing"),
        );
    }
    work.commit().await?;
    Ok(found)
}

/// Mark an imported version's material copied.
pub async fn copied(database: &Database, record_id: &str) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE accounts.credential_versions SET material='stored' \
         WHERE record_id=$1 AND material='importing'",
    )
    .bind(record_id)
    .execute(database.pool())
    .await?;
    Ok(())
}

/// Record the import finished.
pub async fn finish_import(database: &Database, source: &str) -> Result<(), sqlx::Error> {
    sqlx::query("UPDATE accounts.vault_imports SET finished_at=now() WHERE source_sha256=$1")
        .bind(source)
        .execute(database.pool())
        .await?;
    Ok(())
}

/// Row counts of the native tables.
pub async fn native_counts(database: &Database) -> Result<Counts, sqlx::Error> {
    let row = sqlx::query(
        "SELECT (SELECT COUNT(*) FROM accounts.credential_entries), \
         (SELECT COUNT(*) FROM accounts.credential_lists), \
         (SELECT COUNT(*) FROM accounts.credential_policy), \
         (SELECT COUNT(*) FROM accounts.credential_versions)",
    )
    .fetch_one(database.pool())
    .await?;
    Ok(Counts {
        entries: row.try_get(0)?,
        lists: row.try_get(1)?,
        policies: row.try_get(2)?,
        versions: row.try_get(3)?,
    })
}
