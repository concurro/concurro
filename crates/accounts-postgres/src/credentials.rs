//! SQL for the native credential store: entries, provider lists, policy history,
//! versions, the mutation journal and the change sequence.
//!
//! Callers run these inside one unit of work per owner mutation, after
//! [`lock_owner`], so one owner's writes are serialized as the vault's were.

use concurro_db::{Database, UnitOfWork};
use sqlx::FromRow;

/// One owner's credential entry and its current policy.
#[derive(Clone, Debug, FromRow, PartialEq, Eq)]
pub struct EntryRow {
    /// Opaque entry ID.
    pub entry_id: String,
    /// GitHub owner ID.
    pub user_id: i64,
    /// `anthropic`, `google` or `openai`.
    pub provider: String,
    /// Credential route.
    pub route: String,
    /// Owner-visible name.
    pub name: String,
    /// Whether selection may use it.
    pub enabled: bool,
    /// Role filters as JSON text.
    pub roles: String,
    /// Target profile filters as JSON text.
    pub profiles: String,
    /// Entry revision, bumped by every write.
    pub revision: i64,
    /// Creation time.
    pub created: String,
    /// Last write time.
    pub updated: String,
    /// Removal time, for a tombstone.
    pub removed: Option<String>,
}

/// One credential version's nonsecret record.
#[derive(Clone, Debug, FromRow, PartialEq, Eq)]
pub struct VersionRow {
    /// GitHub owner ID.
    pub user_id: i64,
    /// Credential route.
    pub route: String,
    /// Version, increasing per owner and route.
    pub version: i64,
    /// `sha256("{user}/{route}/{version}")`.
    pub record_id: String,
    /// The entry it belongs to.
    pub entry_id: String,
    /// Owner login when written.
    pub login: String,
    /// `valid`, `invalid` or `revoked`.
    pub status: String,
    /// Last four characters and a short digest of the secret, or `relay:<digest>`.
    pub fingerprint: String,
    /// Creation time.
    pub created: String,
    /// Last status change.
    pub updated: String,
    /// `api_key` or `subscription`.
    pub kind: Option<String>,
    /// Legacy model coverage, JSON text.
    pub models: Option<String>,
    /// When it was retired.
    pub retired: Option<String>,
    /// Owner-visible account label.
    pub account_label: Option<String>,
    /// Verified account, when known.
    pub verified_account: Option<String>,
    /// `stored`, `replaced`, `revoked` or `expired`.
    pub status_reason: Option<String>,
    /// Legacy validation time.
    pub validated: Option<String>,
    /// Known expiry.
    pub expires_at: Option<String>,
    /// Whether `expires_at` is trusted.
    pub expiry_known: bool,
    /// Legacy token revision.
    pub token_revision: i64,
    /// Last inference use.
    pub last_used: Option<String>,
    /// `vault` (secret store) or `relay`.
    pub custody: String,
    /// Relay ID for relay custody.
    pub relay_id: Option<String>,
    /// Where the version's material stands.
    pub material: String,
    /// Whether the version was ever activated, and so may be listed.
    pub activated: bool,
    /// The mutation that wrote it, while pending.
    pub mutation_id: Option<String>,
    /// For a pending replace, the entry revision it was made against.
    pub expected_revision: Option<i64>,
    /// For a pending add, the new entry's provider and policy as JSON text.
    pub pending_entry: Option<String>,
}

/// One list revision's policy document.
#[derive(Clone, Debug, FromRow, PartialEq, Eq)]
pub struct PolicyRow {
    /// Provider list.
    pub provider: String,
    /// List revision.
    pub revision: i64,
    /// When it took effect; `None` is always in force.
    pub at: Option<String>,
    /// JSON text: `[{entry_id, name, enabled, roles, profiles}]` in list order.
    pub document: String,
}

macro_rules! version_columns {
    () => {
        "user_id,route,version,record_id,entry_id,login,status,fingerprint,created,updated,kind,\
         models,retired,account_label,verified_account,status_reason,validated,expires_at,\
         expiry_known,token_revision,last_used,custody,relay_id,material,activated,mutation_id,\
         expected_revision,pending_entry"
    };
}

/// Make a just-begun transaction read one consistent snapshot, for reads outside the
/// owner lock.
pub async fn read_snapshot(work: &mut UnitOfWork<'_>) -> Result<(), sqlx::Error> {
    sqlx::query("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        .execute(work.connection())
        .await?;
    Ok(())
}

/// Serialize one owner's credential writes for the rest of the transaction.
pub async fn lock_owner(work: &mut UnitOfWork<'_>, user_id: i64) -> Result<(), sqlx::Error> {
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended('accounts.credentials:' || $1, 0))")
        .bind(user_id.to_string())
        .execute(work.connection())
        .await?;
    Ok(())
}

/// One entry of this owner, removed or not.
pub async fn entry(
    work: &mut UnitOfWork<'_>,
    user_id: i64,
    entry_id: &str,
) -> Result<Option<EntryRow>, sqlx::Error> {
    sqlx::query_as("SELECT * FROM accounts.credential_entries WHERE entry_id=$1 AND user_id=$2")
        .bind(entry_id)
        .bind(user_id)
        .fetch_optional(work.connection())
        .await
}

/// Every entry of an owner, optionally for one provider.
pub async fn entries(
    work: &mut UnitOfWork<'_>,
    user_id: i64,
    provider: Option<&str>,
) -> Result<Vec<EntryRow>, sqlx::Error> {
    sqlx::query_as(
        "SELECT * FROM accounts.credential_entries WHERE user_id=$1 \
         AND ($2::text IS NULL OR provider=$2) ORDER BY entry_id",
    )
    .bind(user_id)
    .bind(provider)
    .fetch_all(work.connection())
    .await
}

/// A provider list's revision and live entry IDs (JSON text), if it exists.
pub async fn list(
    work: &mut UnitOfWork<'_>,
    user_id: i64,
    provider: &str,
) -> Result<Option<(i64, String)>, sqlx::Error> {
    sqlx::query_as(
        "SELECT revision,entries FROM accounts.credential_lists WHERE user_id=$1 AND provider=$2",
    )
    .bind(user_id)
    .bind(provider)
    .fetch_optional(work.connection())
    .await
}

/// Every provider list of an owner: provider, revision and live entry IDs (JSON text).
pub async fn lists(
    work: &mut UnitOfWork<'_>,
    user_id: i64,
) -> Result<Vec<(String, i64, String)>, sqlx::Error> {
    sqlx::query_as(
        "SELECT provider,revision,entries FROM accounts.credential_lists WHERE user_id=$1",
    )
    .bind(user_id)
    .fetch_all(work.connection())
    .await
}

/// Write a list revision and its policy snapshot.
pub async fn set_list(
    work: &mut UnitOfWork<'_>,
    user_id: i64,
    provider: &str,
    revision: i64,
    entries: &str,
    at: Option<&str>,
    document: &str,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO accounts.credential_lists (user_id,provider,revision,entries) \
         VALUES ($1,$2,$3,$4) ON CONFLICT (user_id,provider) \
         DO UPDATE SET revision=EXCLUDED.revision, entries=EXCLUDED.entries",
    )
    .bind(user_id)
    .bind(provider)
    .bind(revision)
    .bind(entries)
    .execute(work.connection())
    .await?;
    sqlx::query(
        "INSERT INTO accounts.credential_policy (user_id,provider,revision,at,document) \
         VALUES ($1,$2,$3,$4,$5)",
    )
    .bind(user_id)
    .bind(provider)
    .bind(revision)
    .bind(at)
    .bind(document)
    .execute(work.connection())
    .await?;
    Ok(())
}

/// Every policy revision of an owner, in revision order.
pub async fn policies(
    work: &mut UnitOfWork<'_>,
    user_id: i64,
) -> Result<Vec<PolicyRow>, sqlx::Error> {
    sqlx::query_as(
        "SELECT provider,revision,at,document FROM accounts.credential_policy \
         WHERE user_id=$1 ORDER BY revision, provider",
    )
    .bind(user_id)
    .fetch_all(work.connection())
    .await
}

/// Insert a new entry.
pub async fn insert_entry(work: &mut UnitOfWork<'_>, entry: &EntryRow) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO accounts.credential_entries \
         (entry_id,user_id,provider,route,name,enabled,roles,profiles,revision,created,updated,removed) \
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)",
    )
    .bind(&entry.entry_id)
    .bind(entry.user_id)
    .bind(&entry.provider)
    .bind(&entry.route)
    .bind(&entry.name)
    .bind(entry.enabled)
    .bind(&entry.roles)
    .bind(&entry.profiles)
    .bind(entry.revision)
    .bind(&entry.created)
    .bind(&entry.updated)
    .bind(&entry.removed)
    .execute(work.connection())
    .await?;
    Ok(())
}

/// Replace an entry's mutable fields and bump its revision; returns the new row.
pub async fn update_entry(
    work: &mut UnitOfWork<'_>,
    entry: &EntryRow,
) -> Result<EntryRow, sqlx::Error> {
    sqlx::query_as(
        "UPDATE accounts.credential_entries SET name=$2,enabled=$3,roles=$4,profiles=$5, \
         updated=$6,removed=$7,revision=revision+1 WHERE entry_id=$1 RETURNING *",
    )
    .bind(&entry.entry_id)
    .bind(&entry.name)
    .bind(entry.enabled)
    .bind(&entry.roles)
    .bind(&entry.profiles)
    .bind(&entry.updated)
    .bind(&entry.removed)
    .fetch_one(work.connection())
    .await
}

/// The next version for an owner and route, after every allocated version and relay
/// reservation.
pub async fn next_version(
    work: &mut UnitOfWork<'_>,
    user_id: i64,
    route: &str,
) -> Result<i64, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT COALESCE(MAX(version),0)+1 FROM (\
         SELECT version FROM accounts.credential_versions WHERE user_id=$1 AND route=$2 \
         UNION ALL SELECT version FROM accounts.relay_intents WHERE user_id=$1 AND route=$2) v",
    )
    .bind(user_id)
    .bind(route)
    .fetch_one(work.connection())
    .await
}

/// Insert a version row.
pub async fn insert_version(
    work: &mut UnitOfWork<'_>,
    version: &VersionRow,
) -> Result<(), sqlx::Error> {
    sqlx::query(concat!(
        "INSERT INTO accounts.credential_versions (", version_columns!(), ",pending_since) VALUES \
         ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24,$25,\
         $26,$27,$28,CASE WHEN $24='pending' THEN now() END)"
    ))
    .bind(version.user_id)
    .bind(&version.route)
    .bind(version.version)
    .bind(&version.record_id)
    .bind(&version.entry_id)
    .bind(&version.login)
    .bind(&version.status)
    .bind(&version.fingerprint)
    .bind(&version.created)
    .bind(&version.updated)
    .bind(&version.kind)
    .bind(&version.models)
    .bind(&version.retired)
    .bind(&version.account_label)
    .bind(&version.verified_account)
    .bind(&version.status_reason)
    .bind(&version.validated)
    .bind(&version.expires_at)
    .bind(version.expiry_known)
    .bind(version.token_revision)
    .bind(&version.last_used)
    .bind(&version.custody)
    .bind(&version.relay_id)
    .bind(&version.material)
    .bind(version.activated)
    .bind(&version.mutation_id)
    .bind(version.expected_revision)
    .bind(&version.pending_entry)
    .execute(work.connection())
    .await?;
    Ok(())
}

/// Write back a version's times, status, retirement, material and activation fields.
/// Leaving `pending` clears the pending fields, keeping the mutation ID.
pub async fn update_version(
    work: &mut UnitOfWork<'_>,
    version: &VersionRow,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE accounts.credential_versions SET status=$2,updated=$3,retired=$4,status_reason=$5, \
         material=$6,activated=$7,last_used=$8,created=$9, \
         expected_revision=CASE WHEN $6='pending' THEN expected_revision END, \
         pending_entry=CASE WHEN $6='pending' THEN pending_entry END, \
         pending_since=CASE WHEN $6='pending' THEN pending_since END \
         WHERE record_id=$1",
    )
    .bind(&version.record_id)
    .bind(&version.status)
    .bind(&version.updated)
    .bind(&version.retired)
    .bind(&version.status_reason)
    .bind(&version.material)
    .bind(version.activated)
    .bind(&version.last_used)
    .bind(&version.created)
    .execute(work.connection())
    .await?;
    Ok(())
}

/// Every version of an owner in the vault's metadata order: by version, then route.
pub async fn versions(
    work: &mut UnitOfWork<'_>,
    user_id: i64,
) -> Result<Vec<VersionRow>, sqlx::Error> {
    sqlx::query_as(concat!(
        "SELECT ",
        version_columns!(),
        " FROM accounts.credential_versions WHERE user_id=$1 \
         ORDER BY version, route"
    ))
    .bind(user_id)
    .fetch_all(work.connection())
    .await
}

/// One version by record ID.
pub async fn version(
    work: &mut UnitOfWork<'_>,
    record_id: &str,
) -> Result<Option<VersionRow>, sqlx::Error> {
    sqlx::query_as(concat!(
        "SELECT ",
        version_columns!(),
        " FROM accounts.credential_versions WHERE record_id=$1"
    ))
    .bind(record_id)
    .fetch_optional(work.connection())
    .await
}

/// Versions of one entry, in version order.
pub async fn entry_versions(
    work: &mut UnitOfWork<'_>,
    entry_id: &str,
) -> Result<Vec<VersionRow>, sqlx::Error> {
    sqlx::query_as(concat!(
        "SELECT ",
        version_columns!(),
        " FROM accounts.credential_versions WHERE entry_id=$1 ORDER BY version"
    ))
    .bind(entry_id)
    .fetch_all(work.connection())
    .await
}

/// The pending version written by this owner mutation, if any.
pub async fn pending_for_mutation(
    work: &mut UnitOfWork<'_>,
    user_id: i64,
    mutation_id: &str,
) -> Result<Option<VersionRow>, sqlx::Error> {
    sqlx::query_as(concat!(
        "SELECT ",
        version_columns!(),
        " FROM accounts.credential_versions \
         WHERE user_id=$1 AND mutation_id=$2 AND material='pending'"
    ))
    .bind(user_id)
    .bind(mutation_id)
    .fetch_optional(work.connection())
    .await
}

/// Record IDs of versions left pending since before `seconds` ago.
pub async fn stale_pending(database: &Database, seconds: i64) -> Result<Vec<String>, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT record_id FROM accounts.credential_versions WHERE material='pending' \
         AND pending_since < now() - make_interval(secs => $1) ORDER BY pending_since",
    )
    .bind(seconds as f64)
    .fetch_all(database.pool())
    .await
}

/// Owners with an activated, unretired, valid version whose expiry is known.
pub async fn owners_with_expiring(database: &Database) -> Result<Vec<i64>, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT DISTINCT user_id FROM accounts.credential_versions WHERE activated \
         AND retired IS NULL AND status='valid' AND expiry_known AND expires_at IS NOT NULL",
    )
    .fetch_all(database.pool())
    .await
}

/// A recorded mutation's request digest and response JSON text, `None` while pending.
pub async fn mutation(
    work: &mut UnitOfWork<'_>,
    user_id: i64,
    mutation_id: &str,
) -> Result<Option<(String, Option<String>)>, sqlx::Error> {
    sqlx::query_as(
        "SELECT digest,response FROM accounts.credential_mutations \
         WHERE user_id=$1 AND mutation_id=$2",
    )
    .bind(user_id)
    .bind(mutation_id)
    .fetch_optional(work.connection())
    .await
}

/// Record a mutation's digest and its response, or `None` while its version is pending.
pub async fn record_mutation(
    work: &mut UnitOfWork<'_>,
    user_id: i64,
    mutation_id: &str,
    digest: &str,
    response: Option<&str>,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO accounts.credential_mutations (user_id,mutation_id,digest,response) \
         VALUES ($1,$2,$3,$4) ON CONFLICT (user_id,mutation_id) \
         DO UPDATE SET digest=EXCLUDED.digest, response=EXCLUDED.response",
    )
    .bind(user_id)
    .bind(mutation_id)
    .bind(digest)
    .bind(response)
    .execute(work.connection())
    .await?;
    Ok(())
}

/// Record the response of a mutation whose version activated.
pub async fn complete_mutation(
    work: &mut UnitOfWork<'_>,
    user_id: i64,
    mutation_id: &str,
    response: &str,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE accounts.credential_mutations SET response=$3 \
         WHERE user_id=$1 AND mutation_id=$2",
    )
    .bind(user_id)
    .bind(mutation_id)
    .bind(response)
    .execute(work.connection())
    .await?;
    Ok(())
}

/// Forget a still-pending mutation whose version failed or was superseded, so a retry runs
/// it again.
pub async fn forget_mutation(
    work: &mut UnitOfWork<'_>,
    user_id: i64,
    mutation_id: &str,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "DELETE FROM accounts.credential_mutations \
         WHERE user_id=$1 AND mutation_id=$2 AND response IS NULL",
    )
    .bind(user_id)
    .bind(mutation_id)
    .execute(work.connection())
    .await?;
    Ok(())
}

/// Allocate the next change sequence for an owner's fact.
pub async fn next_change(work: &mut UnitOfWork<'_>, user_id: i64) -> Result<i64, sqlx::Error> {
    sqlx::query_scalar(
        "INSERT INTO accounts.credential_changes (user_id) VALUES ($1) RETURNING sequence",
    )
    .bind(user_id)
    .fetch_one(work.connection())
    .await
}

/// Queue the destroy job for a version's material.
pub async fn queue_destroy(work: &mut UnitOfWork<'_>, record_id: &str) -> Result<(), sqlx::Error> {
    sqlx::query(
        "SELECT graphile_worker.add_job(identifier => 'destroy_credential_material', \
         payload => json_build_object('record_id',$1::text), max_attempts => 32000, \
         job_key => 'credential-destroy:' || $1)",
    )
    .bind(record_id)
    .execute(work.connection())
    .await?;
    Ok(())
}

/// Mark a version's material destroyed.
pub async fn destroyed(database: &Database, record_id: &str) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE accounts.credential_versions SET material='destroyed' \
         WHERE record_id=$1 AND material IN ('destroying','superseded')",
    )
    .bind(record_id)
    .execute(database.pool())
    .await?;
    Ok(())
}

/// Advance an activated version's `last_used` to `at`; an earlier or repeated use changes
/// nothing.
pub async fn record_use(
    work: &mut UnitOfWork<'_>,
    record_id: &str,
    at: &str,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE accounts.credential_versions SET last_used=$2 WHERE record_id=$1 AND activated \
         AND (last_used IS NULL OR last_used::timestamptz < $2::timestamptz)",
    )
    .bind(record_id)
    .bind(at)
    .execute(work.connection())
    .await?;
    Ok(())
}

/// One route's reserved version for a relay approval.
#[derive(Clone, Debug, FromRow, PartialEq, Eq)]
pub struct IntentRow {
    /// GitHub owner ID.
    pub user_id: i64,
    /// Subscription route.
    pub route: String,
    /// Reserved version.
    pub version: i64,
    /// The owner mutation reserving it.
    pub mutation_id: String,
    /// The mutation's request digest.
    pub digest: String,
    /// The approval binding, JSON text: `{route, entry_id, record_id, version}`.
    pub binding: String,
    /// The operation to finish after approval, JSON text.
    pub operation: String,
}

/// Reserve one route's version for a relay approval.
pub async fn insert_intent(
    work: &mut UnitOfWork<'_>,
    intent: &IntentRow,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO accounts.relay_intents (user_id,route,version,mutation_id,digest,binding,operation) \
         VALUES ($1,$2,$3,$4,$5,$6,$7)",
    )
    .bind(intent.user_id)
    .bind(&intent.route)
    .bind(intent.version)
    .bind(&intent.mutation_id)
    .bind(&intent.digest)
    .bind(&intent.binding)
    .bind(&intent.operation)
    .execute(work.connection())
    .await?;
    Ok(())
}

/// One mutation's reservations, by route.
pub async fn intents(
    work: &mut UnitOfWork<'_>,
    user_id: i64,
    mutation_id: &str,
) -> Result<Vec<IntentRow>, sqlx::Error> {
    sqlx::query_as(
        "SELECT user_id,route,version,mutation_id,digest,binding,operation FROM accounts.relay_intents \
         WHERE user_id=$1 AND mutation_id=$2 ORDER BY route",
    )
    .bind(user_id)
    .bind(mutation_id)
    .fetch_all(work.connection())
    .await
}

/// Release one mutation's reservations.
pub async fn release_intents(
    work: &mut UnitOfWork<'_>,
    user_id: i64,
    mutation_id: &str,
) -> Result<(), sqlx::Error> {
    sqlx::query("DELETE FROM accounts.relay_intents WHERE user_id=$1 AND mutation_id=$2")
        .bind(user_id)
        .bind(mutation_id)
        .execute(work.connection())
        .await?;
    Ok(())
}

/// Owners and mutations whose reservations are older than `seconds`.
pub async fn stale_intents(
    database: &Database,
    seconds: i64,
) -> Result<Vec<(i64, String)>, sqlx::Error> {
    sqlx::query_as(
        "SELECT DISTINCT user_id,mutation_id FROM accounts.relay_intents \
         WHERE reserved_at < now() - make_interval(secs => $1) ORDER BY user_id,mutation_id",
    )
    .bind(seconds as f64)
    .fetch_all(database.pool())
    .await
}
