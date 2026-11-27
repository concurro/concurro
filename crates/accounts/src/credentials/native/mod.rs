//! The native credential store: accounts' own tables, with each version's material in
//! OpenBao at `personal/<record_id>`.
//!
//! Every owner write runs under that owner's advisory lock. A new secret is written in
//! three steps: a `pending` version row, the OpenBao create, then activation, which
//! changes the entry, list and policy history and appends the change fact together.
//! Settlement finishes or fails versions a crash left pending.

mod relay;

use std::{collections::BTreeMap, fmt, sync::Arc};

use axum::http::StatusCode;
use chrono::{DateTime, Timelike, Utc};
use concurro_accounts_postgres::credentials::{self as storage, EntryRow, VersionRow};
use concurro_contracts::{
    CredentialChange, CredentialChanged, Envelope, RevokedVersion, Timestamp,
};
use concurro_db::{Database, UnitOfWork};
use concurro_events_postgres::{Outbox, OutboxError};
use concurro_secrets::OpenBao;
use secrecy::SecretString;
use serde_json::{Value, json};

use super::{
    AddBody, CredentialError, EditBody, Operation, OrderBody, ProfileFilter, SecretBody,
    view::{self, Owner},
};
use crate::github::User;

pub use relay::Verifier;

/// The store's clock, injectable for tests.
pub type Clock = Arc<dyn Fn() -> DateTime<Utc> + Send + Sync>;

const CONFLICT: &str = "credential changed; refresh before retrying";
const REUSED: &str = "mutation ID was already used for a different request";
/// The producer named on change facts.
const PRODUCER: &str = "accounts";
/// How long a version may stay pending before settlement decides it.
pub const SETTLE_AFTER_SECONDS: i64 = 120;

impl From<sqlx::Error> for CredentialError {
    fn from(error: sqlx::Error) -> Self {
        unavailable(error)
    }
}

impl From<OutboxError> for CredentialError {
    fn from(error: OutboxError) -> Self {
        unavailable(error)
    }
}

impl From<concurro_secrets::Error> for CredentialError {
    fn from(error: concurro_secrets::Error) -> Self {
        unavailable(error)
    }
}

/// Log a storage or OpenBao failure, which names no secret, and answer `503`.
fn unavailable(error: impl fmt::Display) -> CredentialError {
    tracing::warn!(%error, "credential store unavailable");
    CredentialError::unavailable()
}

/// A `422` with the vault's wording.
fn refused(detail: &str) -> CredentialError {
    CredentialError::new(StatusCode::UNPROCESSABLE_ENTITY, detail)
}

/// A version's OpenBao path.
pub fn material_path(record_id: &str) -> String {
    format!("personal/{record_id}")
}

/// The native store's dependencies.
#[derive(Clone)]
pub struct Native {
    database: Database,
    openbao: OpenBao,
    outbox: Outbox,
    clock: Clock,
    /// The verifier's owner API, for relay approval and status.
    verifier: Option<Verifier>,
}

impl fmt::Debug for Native {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.debug_struct("Native").finish_non_exhaustive()
    }
}

/// A mutation's journal state.
enum Journal {
    New,
    Done(Value),
    Pending,
}

/// What a pending version will activate into.
enum Target {
    Add {
        route: &'static str,
        entry_id: String,
        policy: Value,
    },
    Replace {
        entry_id: String,
        revision: i64,
    },
}

/// Where a secret write's reservation left it.
enum Reserved {
    New(String),
    Pending(String),
    Done(Value),
}

/// How an attempt to activate or settle a version ended.
#[derive(Debug)]
pub enum Outcome {
    /// The version is active; the mutation's response.
    Activated(Value),
    /// Another change won; the mutation's earlier response, if it already succeeded.
    Superseded(Option<Value>),
    /// The material never arrived; a retry writes it again.
    Failed,
}

impl Outcome {
    /// The browser answer.
    fn response(self) -> Result<Value, CredentialError> {
        match self {
            Self::Activated(value) | Self::Superseded(Some(value)) => Ok(value),
            Self::Superseded(None) => Err(CredentialError::new(StatusCode::CONFLICT, CONFLICT)),
            Self::Failed => Err(CredentialError::unavailable()),
        }
    }
}

/// A checked candidate secret and its nonsecret account metadata.
struct Material {
    secret: String,
    account_id: Option<String>,
    account_label: Option<String>,
}

impl Material {
    /// Check a token and its route's account header, as the vault's `candidate`.
    fn check(route: &str, secret: &str, account_id: Option<&str>) -> Result<Self, CredentialError> {
        if route == "antigravity-subscription" {
            return Err(refused(
                "Connect Antigravity through Sponsor using its agy login",
            ));
        }
        let header =
            |value: &str| !value.is_empty() && value.bytes().all(|b| (33..=126).contains(&b));
        let secret = secret.trim();
        if !header(secret) {
            return Err(refused("secret is empty or has unsupported characters"));
        }
        if route != "chatgpt-subscription" {
            if account_id.is_some() {
                return Err(refused("this route does not use an account ID"));
            }
            return Ok(Self {
                secret: secret.to_owned(),
                account_id: None,
                account_label: None,
            });
        }
        let account_id = account_id.filter(|value| header(value)).ok_or_else(|| {
            refused("ChatGPT account ID is required and must be a valid header value")
        })?;
        Ok(Self {
            secret: secret.to_owned(),
            account_id: Some(account_id.to_owned()),
            account_label: Some(format!(
                "Supplied ChatGPT account {}",
                &view::digest(account_id)[..8]
            )),
        })
    }

    /// The vault's fingerprint of the secret.
    fn fingerprint(&self) -> String {
        view::fingerprint(&self.secret)
    }

    /// The OpenBao value, `{secret, account?}`.
    fn fields(&self) -> BTreeMap<String, SecretString> {
        let mut fields = BTreeMap::from([("secret".to_owned(), self.secret.clone().into())]);
        if let Some(account_id) = &self.account_id {
            fields.insert(
                "account".to_owned(),
                json!({"chatgpt_account_id": account_id}).to_string().into(),
            );
        }
        fields
    }
}

/// Validate the vault's owner mutation ID.
fn mutation_id(value: &str) -> Result<(), CredentialError> {
    let valid = (16..=64).contains(&value.len())
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-');
    valid.then_some(()).ok_or_else(CredentialError::malformed)
}

/// Validate and trim an entry name.
fn name(value: &str) -> Result<String, CredentialError> {
    let length = value.chars().count();
    let valid = (1..=80).contains(&length)
        && !value.chars().any(|c| c.is_control() && (c as u32) < 0x80)
        && value.chars().any(|c| !c.is_whitespace());
    valid
        .then(|| value.trim().to_owned())
        .ok_or_else(CredentialError::malformed)
}

/// Validate role filters and drop repeats.
fn roles(values: &[String]) -> Result<Vec<String>, CredentialError> {
    let valid = values.len() <= 16
        && values.iter().all(|role| {
            (1..=32).contains(&role.len())
                && role.bytes().all(|b| b.is_ascii_lowercase() || b == b'_')
        });
    valid
        .then(|| distinct(values))
        .ok_or_else(CredentialError::malformed)
}

/// Validate target profile filters and drop repeats.
fn profiles(values: &[ProfileFilter]) -> Result<Vec<ProfileFilter>, CredentialError> {
    let part = |value: &str| {
        (1..=100).contains(&value.chars().count())
            && value
                .chars()
                .all(|c| c.is_alphanumeric() || matches!(c, '_' | '.' | '-'))
    };
    let valid = values.len() <= 64
        && values.iter().all(|filter| {
            filter
                .target
                .split_once('/')
                .is_some_and(|(owner, repo)| part(owner) && part(repo))
                && (1..=100).contains(&filter.profile.len())
                && filter.profile.bytes().all(|b| (0x21..=0x7e).contains(&b))
        });
    valid
        .then(|| distinct(values))
        .ok_or_else(CredentialError::malformed)
}

/// Keep the first of repeated values, in order.
fn distinct<T: Clone + PartialEq>(values: &[T]) -> Vec<T> {
    let mut kept: Vec<T> = Vec::with_capacity(values.len());
    for value in values {
        if !kept.contains(value) {
            kept.push(value.clone());
        }
    }
    kept
}

/// Validate a revision the owner last read.
fn revision(value: i64) -> Result<i64, CredentialError> {
    (value >= 0)
        .then_some(value)
        .ok_or_else(CredentialError::malformed)
}

/// A JSON text column.
fn json_text(value: &str) -> Value {
    view::json_text(Some(value))
}

impl Native {
    /// Bind the accounts database, OpenBao client and outbox.
    pub fn new(database: Database, openbao: OpenBao, outbox: Outbox, clock: Clock) -> Self {
        Self {
            database,
            openbao,
            outbox,
            clock,
            verifier: None,
        }
    }

    /// Approve relays and read their status through the verifier's owner API.
    pub fn with_verifier(mut self, verifier: Verifier) -> Self {
        self.verifier = Some(verifier);
        self
    }

    /// The store's current time.
    fn now(&self) -> DateTime<Utc> {
        (self.clock)()
    }

    /// Run one owner operation, answering in the vault's shapes.
    pub async fn run(&self, user: &User, operation: Operation) -> Result<Value, CredentialError> {
        match operation {
            Operation::List => {
                let mut work = self.database.begin().await?;
                storage::read_snapshot(&mut work).await?;
                let listing = owner(&mut work, user.id).await?.listing(self.now());
                work.commit().await?;
                Ok(listing)
            }
            Operation::Add { route, body } => self.add(user, route, body).await,
            Operation::Edit { entry_id, body } => self.edit(user.id, &entry_id, body).await,
            Operation::Replace { entry_id, body } => self.replace(user, &entry_id, body).await,
            Operation::Reorder { provider, body } => self.reorder(user.id, &provider, body).await,
            Operation::Remove { entry_id, revision } => {
                self.remove(user.id, &entry_id, revision).await
            }
            Operation::RelayStatus => self.relay_status(user.id).await,
            Operation::RelayPending(body) => self.relay_pending(&body.code).await,
            Operation::Relay(body) => self.approve_relay(user, body).await,
            Operation::Device(body) => self.replace_device(user, body).await,
        }
    }

    /// Nonsecret metadata of an owner's credentials, now or as of `at`, as the vault's
    /// `/metadata/{user_id}?at=` plus `relay_id`.
    pub async fn metadata(&self, user_id: i64, at: Option<&str>) -> Result<Value, CredentialError> {
        read_metadata(&self.database, user_id, at, self.now()).await
    }

    /// Append a new entry holding a secret to its provider's list.
    async fn add(
        &self,
        user: &User,
        route: &'static str,
        body: AddBody,
    ) -> Result<Value, CredentialError> {
        mutation_id(&body.mutation_id)?;
        let (name, roles, profiles) = (
            name(&body.name)?,
            roles(&body.roles)?,
            profiles(&body.profiles)?,
        );
        let material = Material::check(route, &body.secret, body.account_id.as_deref())?;
        let policy = json!({"name":name,"roles":roles,"profiles":profiles});
        let request = json!({"add":route,"name":name,"roles":roles,"profiles":profiles,
            "account_id":body.account_id});
        let entry_id =
            view::digest(&format!("entry/{}/{}", user.id, body.mutation_id))[..32].to_owned();
        let target = Target::Add {
            route,
            entry_id,
            policy,
        };
        self.write_secret(user, &body.mutation_id, &request, &target, &material)
            .await
    }

    /// Give one entry a new version; its policy and peers are untouched.
    async fn replace(
        &self,
        user: &User,
        entry_id: &str,
        body: SecretBody,
    ) -> Result<Value, CredentialError> {
        mutation_id(&body.mutation_id)?;
        let revision = revision(body.revision)?;
        let route = {
            let mut work = self.database.begin().await?;
            let entry = storage::entry(&mut work, user.id, entry_id).await?;
            work.commit().await?;
            entry
                .ok_or_else(|| CredentialError::new(StatusCode::NOT_FOUND, "no such credential"))?
                .route
        };
        let material = Material::check(&route, &body.secret, body.account_id.as_deref())?;
        let request = json!({"replace":entry_id,"revision":revision,"account_id":body.account_id});
        let target = Target::Replace {
            entry_id: entry_id.to_owned(),
            revision,
        };
        self.write_secret(user, &body.mutation_id, &request, &target, &material)
            .await
    }

    /// Reserve a pending version, create its material, then activate it. A retry of a
    /// mutation whose version is still pending settles that version first.
    async fn write_secret(
        &self,
        user: &User,
        mutation: &str,
        request: &Value,
        target: &Target,
        material: &Material,
    ) -> Result<Value, CredentialError> {
        let digest = view::digest(&request.to_string());
        for _ in 0..2 {
            let record_id = match self
                .reserve(user, mutation, &digest, target, material)
                .await?
            {
                Reserved::Done(response) => return Ok(response),
                Reserved::Pending(record_id) => match self.settle(&record_id).await? {
                    Outcome::Failed => continue,
                    outcome => return outcome.response(),
                },
                Reserved::New(record_id) => record_id,
            };
            self.openbao
                .create(&material_path(&record_id), &material.fields())
                .await?;
            return self.activate(&record_id).await?.response();
        }
        Err(CredentialError::unavailable())
    }

    /// Check the journal and the entry, then insert the pending version.
    async fn reserve(
        &self,
        user: &User,
        mutation: &str,
        digest: &str,
        target: &Target,
        material: &Material,
    ) -> Result<Reserved, CredentialError> {
        let at = view::stamp(self.now());
        let mut work = self.database.begin().await?;
        storage::lock_owner(&mut work, user.id).await?;
        match journal(&mut work, user.id, mutation, digest).await? {
            Journal::Done(response) => return Ok(Reserved::Done(response)),
            Journal::Pending => {
                if let Some(version) =
                    storage::pending_for_mutation(&mut work, user.id, mutation).await?
                {
                    work.commit().await?;
                    return Ok(Reserved::Pending(version.record_id));
                }
            }
            Journal::New => {}
        }
        let (route, entry_id, expected_revision, pending_entry) = match target {
            Target::Add {
                route,
                entry_id,
                policy,
            } => {
                let mut pending = policy.clone();
                pending["provider"] = json!(view::route(route).map(|(provider, ..)| provider));
                (
                    (*route).to_owned(),
                    entry_id.clone(),
                    None,
                    Some(pending.to_string()),
                )
            }
            Target::Replace { entry_id, revision } => {
                let entry = owned(&mut work, user.id, entry_id, Some(*revision)).await?;
                (entry.route, entry_id.clone(), Some(*revision), None)
            }
        };
        let version = storage::next_version(&mut work, user.id, &route).await?;
        let record_id = view::record_id(user.id, &route, version);
        storage::insert_version(
            &mut work,
            &VersionRow {
                user_id: user.id,
                kind: view::route(&route).map(|(_, kind, _)| kind.to_owned()),
                route,
                version,
                record_id: record_id.clone(),
                entry_id,
                login: user.login.clone(),
                status: "valid".to_owned(),
                fingerprint: material.fingerprint(),
                created: at.clone(),
                updated: at,
                models: None,
                retired: None,
                account_label: material.account_label.clone(),
                verified_account: None,
                status_reason: Some("stored".to_owned()),
                validated: None,
                expires_at: None,
                expiry_known: false,
                token_revision: 1,
                last_used: None,
                custody: "vault".to_owned(),
                relay_id: None,
                material: "pending".to_owned(),
                activated: false,
                mutation_id: Some(mutation.to_owned()),
                expected_revision,
                pending_entry,
            },
        )
        .await?;
        storage::record_mutation(&mut work, user.id, mutation, digest, None).await?;
        work.commit().await?;
        Ok(Reserved::New(record_id))
    }

    /// Activate a pending version whose material exists, or supersede it if its entry
    /// changed meanwhile.
    pub async fn activate(&self, record_id: &str) -> Result<Outcome, CredentialError> {
        let now = self.now();
        let at = view::stamp(now);
        let mut work = self.database.begin().await?;
        let Some(version) = storage::version(&mut work, record_id).await? else {
            return Ok(Outcome::Failed);
        };
        let user_id = version.user_id;
        storage::lock_owner(&mut work, user_id).await?;
        let mut version = storage::version(&mut work, record_id)
            .await?
            .expect("versions are never deleted");
        let mutation = version.mutation_id.clone().unwrap_or_default();
        if version.material != "pending" {
            let outcome = if version.activated {
                Outcome::Activated(
                    response(&mut work, user_id, &mutation)
                        .await?
                        .unwrap_or(Value::Null),
                )
            } else if version.material == "failed" {
                // The create landed after settlement gave up on it.
                version.material = "superseded".to_owned();
                storage::update_version(&mut work, &version).await?;
                storage::queue_destroy(&mut work, record_id).await?;
                Outcome::Failed
            } else {
                Outcome::Superseded(response(&mut work, user_id, &mutation).await?)
            };
            work.commit().await?;
            return Ok(outcome);
        }
        let existing = storage::entry(&mut work, user_id, &version.entry_id).await?;
        let adding = version.expected_revision.is_none();
        let entry = match (version.expected_revision, existing) {
            (Some(expected), Some(entry))
                if entry.removed.is_none() && entry.revision == expected =>
            {
                Some(entry)
            }
            (None, None) => {
                let pending = json_text(version.pending_entry.as_deref().unwrap_or("null"));
                let entry = EntryRow {
                    entry_id: version.entry_id.clone(),
                    user_id,
                    provider: pending["provider"].as_str().unwrap_or_default().to_owned(),
                    route: version.route.clone(),
                    name: pending["name"].as_str().unwrap_or_default().to_owned(),
                    enabled: true,
                    roles: pending["roles"].to_string(),
                    profiles: pending["profiles"].to_string(),
                    revision: 0,
                    created: at.clone(),
                    updated: at.clone(),
                    removed: None,
                };
                storage::insert_entry(&mut work, &entry).await?;
                Some(entry)
            }
            _ => None,
        };
        let Some(entry) = entry else {
            version.material = "superseded".to_owned();
            storage::update_version(&mut work, &version).await?;
            storage::queue_destroy(&mut work, record_id).await?;
            storage::forget_mutation(&mut work, user_id, &mutation).await?;
            let earlier = response(&mut work, user_id, &mutation).await?;
            work.commit().await?;
            return Ok(Outcome::Superseded(earlier));
        };
        let mut previous = activated(&mut work, &entry.entry_id).await?;
        let revoked = retire(&mut work, &mut previous, &at, "replaced").await?;
        version.material = "stored".to_owned();
        version.activated = true;
        version.created = at.clone();
        version.updated = at.clone();
        storage::update_version(&mut work, &version).await?;
        let entry = touch(&mut work, &entry, &at, |_| {}).await?;
        let mut list = list(&mut work, user_id, &entry.provider).await?.1;
        if adding {
            list.push(entry.entry_id.clone());
        }
        let list_revision = arrange(&mut work, user_id, &entry.provider, &list, &at).await?;
        let change = if adding {
            CredentialChange::Added
        } else {
            CredentialChange::Replaced
        };
        self.fact(
            &mut work,
            user_id,
            &entry.provider,
            Some(&entry.entry_id),
            change,
            list_revision,
            &revoked,
            now,
        )
        .await?;
        let value = owner(&mut work, user_id).await?.view(&entry, now);
        storage::complete_mutation(&mut work, user_id, &mutation, &value.to_string()).await?;
        work.commit().await?;
        Ok(Outcome::Activated(value))
    }

    /// Decide a pending version by whether OpenBao holds its material.
    pub async fn settle(&self, record_id: &str) -> Result<Outcome, CredentialError> {
        let pending = {
            let mut work = self.database.begin().await?;
            let version = storage::version(&mut work, record_id).await?;
            work.commit().await?;
            version.is_some_and(|version| version.material == "pending")
        };
        if !pending
            || self
                .openbao
                .metadata(&material_path(record_id))
                .await?
                .is_some()
        {
            return self.activate(record_id).await;
        }
        let mut work = self.database.begin().await?;
        let Some(version) = storage::version(&mut work, record_id).await? else {
            return Ok(Outcome::Failed);
        };
        storage::lock_owner(&mut work, version.user_id).await?;
        let mut version = storage::version(&mut work, record_id)
            .await?
            .expect("versions are never deleted");
        if version.material != "pending" {
            work.commit().await?;
            return self.activate(record_id).await;
        }
        version.material = "failed".to_owned();
        storage::update_version(&mut work, &version).await?;
        if let Some(mutation) = &version.mutation_id {
            storage::forget_mutation(&mut work, version.user_id, mutation).await?;
        }
        work.commit().await?;
        Ok(Outcome::Failed)
    }

    /// Settle every version left pending longer than [`SETTLE_AFTER_SECONDS`].
    pub async fn settle_stale(&self) -> Result<(), CredentialError> {
        for record_id in storage::stale_pending(&self.database, SETTLE_AFTER_SECONDS).await? {
            if let Err(error) = self.settle(&record_id).await {
                tracing::warn!(%record_id, ?error, "pending credential version not settled");
            }
        }
        Ok(())
    }

    /// Destroy a version's material and its metadata; an absent path is already done.
    pub async fn destroy(&self, record_id: &str) -> Result<(), CredentialError> {
        self.openbao.destroy(&material_path(record_id)).await?;
        storage::destroyed(&self.database, record_id).await?;
        Ok(())
    }

    /// Mark valid versions whose known expiry has passed `invalid`, destroy their
    /// material and append an `unusable` change for each.
    pub async fn expire(&self) -> Result<(), CredentialError> {
        let now = self.now();
        let at = view::stamp(now);
        for user_id in storage::owners_with_expiring(&self.database).await? {
            let mut work = self.database.begin().await?;
            storage::lock_owner(&mut work, user_id).await?;
            let current = owner(&mut work, user_id).await?;
            for mut version in current.versions.iter().cloned() {
                let due = version.status == "valid"
                    && version.retired.is_none()
                    && version.expiry_known
                    && version
                        .expires_at
                        .as_deref()
                        .and_then(view::parse)
                        .is_some_and(|expiry| now >= expiry);
                let Some(entry) = current
                    .entries
                    .iter()
                    .find(|entry| entry.entry_id == version.entry_id)
                    .filter(|_| due)
                else {
                    continue;
                };
                version.status = "invalid".to_owned();
                version.status_reason = Some("expired".to_owned());
                version.updated = at.clone();
                version.retired = Some(at.clone());
                if matches!(version.material.as_str(), "stored" | "importing") {
                    version.material = "destroying".to_owned();
                    storage::queue_destroy(&mut work, &version.record_id).await?;
                }
                storage::update_version(&mut work, &version).await?;
                let list_revision = current.list(&entry.provider).0;
                self.fact(
                    &mut work,
                    user_id,
                    &entry.provider,
                    Some(&entry.entry_id),
                    CredentialChange::Unusable,
                    list_revision,
                    std::slice::from_ref(&version),
                    now,
                )
                .await?;
            }
            work.commit().await?;
        }
        Ok(())
    }

    /// Change an entry's name, enabled state or filters.
    async fn edit(
        &self,
        user_id: i64,
        entry_id: &str,
        body: EditBody,
    ) -> Result<Value, CredentialError> {
        mutation_id(&body.mutation_id)?;
        let revision = revision(body.revision)?;
        let name = body.name.as_deref().map(name).transpose()?;
        let roles = body.roles.as_deref().map(roles).transpose()?;
        let profiles = body.profiles.as_deref().map(profiles).transpose()?;
        if name.is_none() && body.enabled.is_none() && roles.is_none() && profiles.is_none() {
            return Err(refused("no changes supplied"));
        }
        let request = json!({"edit":entry_id,"revision":revision,"name":name,
            "enabled":body.enabled,"roles":roles,"profiles":profiles});
        let mut transaction = match self
            .begin_policy(user_id, &body.mutation_id, &request)
            .await?
        {
            Ok(work) => work,
            Err(response) => return Ok(response),
        };
        let work = &mut transaction;
        let now = self.now();
        let at = view::stamp(now);
        let value = {
            let entry = owned(work, user_id, entry_id, Some(revision)).await?;
            let entry = touch(work, &entry, &at, |entry| {
                if let Some(name) = &name {
                    entry.name = name.clone();
                }
                if let Some(enabled) = body.enabled {
                    entry.enabled = enabled;
                }
                if let Some(roles) = &roles {
                    entry.roles = json!(roles).to_string();
                }
                if let Some(profiles) = &profiles {
                    entry.profiles = json!(profiles).to_string();
                }
            })
            .await?;
            let order = list(work, user_id, &entry.provider).await?.1;
            let list_revision = arrange(work, user_id, &entry.provider, &order, &at).await?;
            self.fact(
                work,
                user_id,
                &entry.provider,
                Some(&entry.entry_id),
                CredentialChange::Edited,
                list_revision,
                &[],
                now,
            )
            .await?;
            owner(work, user_id).await?.view(&entry, now)
        };
        finish_policy(transaction, user_id, &body.mutation_id, &request, &value).await?;
        Ok(value)
    }

    /// Commit an exact new order of a provider's current entries.
    async fn reorder(
        &self,
        user_id: i64,
        provider: &str,
        body: OrderBody,
    ) -> Result<Value, CredentialError> {
        if !view::PROVIDERS.contains(&provider) {
            return Err(CredentialError::new(
                StatusCode::NOT_FOUND,
                "unknown provider",
            ));
        }
        mutation_id(&body.mutation_id)?;
        let revision = revision(body.revision)?;
        if !body
            .entries
            .iter()
            .all(|id| id.len() == 32 && id.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f')))
        {
            return Err(CredentialError::malformed());
        }
        let request = json!({"reorder":provider,"revision":revision,"entries":body.entries});
        let mut transaction = match self
            .begin_policy(user_id, &body.mutation_id, &request)
            .await?
        {
            Ok(work) => work,
            Err(response) => return Ok(response),
        };
        let work = &mut transaction;
        let now = self.now();
        let at = view::stamp(now);
        let value = {
            let (current, mut existing) = list(work, user_id, provider).await?;
            let mut requested = body.entries.clone();
            existing.sort();
            requested.sort();
            if current != revision || existing != requested {
                return Err(CredentialError::new(StatusCode::CONFLICT, CONFLICT));
            }
            let list_revision = arrange(work, user_id, provider, &body.entries, &at).await?;
            self.fact(
                work,
                user_id,
                provider,
                None,
                CredentialChange::Reordered,
                list_revision,
                &[],
                now,
            )
            .await?;
            owner(work, user_id).await?.group(provider, now)
        };
        finish_policy(transaction, user_id, &body.mutation_id, &request, &value).await?;
        Ok(value)
    }

    /// Tombstone an entry and retire its versions; repeating the removal changes nothing.
    async fn remove(
        &self,
        user_id: i64,
        entry_id: &str,
        revision: i64,
    ) -> Result<Value, CredentialError> {
        let now = self.now();
        let at = view::stamp(now);
        let mut work = self.database.begin().await?;
        storage::lock_owner(&mut work, user_id).await?;
        let entry = match owned(&mut work, user_id, entry_id, Some(revision)).await {
            Err(CredentialError(StatusCode::GONE, _)) => {
                let current = owner(&mut work, user_id).await?;
                let entry = storage::entry(&mut work, user_id, entry_id)
                    .await?
                    .expect("a removed entry is kept");
                work.commit().await?;
                return Ok(current.view(&entry, now));
            }
            result => result?,
        };
        let mut versions = activated(&mut work, entry_id).await?;
        retire(&mut work, &mut versions, &at, "revoked").await?;
        if let Some(latest) = versions.iter_mut().max_by_key(|version| version.version) {
            latest.status = "revoked".to_owned();
            latest.status_reason = Some("revoked".to_owned());
            latest.updated = at.clone();
            storage::update_version(&mut work, latest).await?;
        }
        let entry = touch(&mut work, &entry, &at, |entry| {
            entry.removed = Some(at.clone())
        })
        .await?;
        let mut order = list(&mut work, user_id, &entry.provider).await?.1;
        order.retain(|id| *id != entry.entry_id);
        let list_revision = arrange(&mut work, user_id, &entry.provider, &order, &at).await?;
        self.fact(
            &mut work,
            user_id,
            &entry.provider,
            Some(entry_id),
            CredentialChange::Removed,
            list_revision,
            &versions,
            now,
        )
        .await?;
        let value = owner(&mut work, user_id).await?.view(&entry, now);
        work.commit().await?;
        Ok(value)
    }

    /// Begin a metadata-only owner mutation under the owner lock; a replay answers with
    /// its first response instead.
    async fn begin_policy(
        &self,
        user_id: i64,
        mutation: &str,
        request: &Value,
    ) -> Result<Result<UnitOfWork<'static>, Value>, CredentialError> {
        let mut work = self.database.begin().await?;
        storage::lock_owner(&mut work, user_id).await?;
        let digest = view::digest(&request.to_string());
        Ok(
            match journal(&mut work, user_id, mutation, &digest).await? {
                Journal::Done(response) => Err(response),
                Journal::New | Journal::Pending => Ok(work),
            },
        )
    }

    /// Append one change fact.
    #[allow(clippy::too_many_arguments)]
    async fn fact(
        &self,
        work: &mut UnitOfWork<'_>,
        user_id: i64,
        provider: &str,
        entry_id: Option<&str>,
        change: CredentialChange,
        list_revision: i64,
        revoked: &[VersionRow],
        now: DateTime<Utc>,
    ) -> Result<(), CredentialError> {
        let data = CredentialChanged {
            user_id,
            sequence: storage::next_change(work, user_id).await?,
            entry_id: entry_id.map(str::to_owned),
            provider: provider.to_owned(),
            change,
            list_revision,
            revoked: revoked
                .iter()
                .map(|version| RevokedVersion {
                    record_id: version.record_id.clone(),
                    version: version.version,
                    route: version.route.clone(),
                    custody: version.custody.clone(),
                })
                .collect(),
            at: Timestamp(now),
        };
        let event = Envelope::fact(data.event_id(), PRODUCER, Timestamp(now), data);
        self.outbox.append(work, &event).await?;
        Ok(())
    }
}

/// Record a metadata-only mutation's response and commit it.
async fn finish_policy(
    mut work: UnitOfWork<'static>,
    user_id: i64,
    mutation: &str,
    request: &Value,
    value: &Value,
) -> Result<(), CredentialError> {
    let digest = view::digest(&request.to_string());
    storage::record_mutation(
        &mut work,
        user_id,
        mutation,
        &digest,
        Some(&value.to_string()),
    )
    .await?;
    work.commit().await?;
    Ok(())
}

/// The OpenBao value for a test secret.
#[cfg(test)]
pub fn test_fields(secret: &str) -> BTreeMap<String, SecretString> {
    Material::check("anthropic-api-key", secret, None)
        .expect("valid test secret")
        .fields()
}

#[cfg(test)]
impl Native {
    /// Reserve a pending replace without creating its material, as a crash would leave it.
    pub async fn reserve_replace(
        &self,
        user: &User,
        entry_id: &str,
        revision: i64,
        secret: &str,
        mutation: &str,
    ) -> Result<String, CredentialError> {
        let request = json!({"replace":entry_id,"revision":revision,"account_id":null});
        let digest = view::digest(&request.to_string());
        let target = Target::Replace {
            entry_id: entry_id.to_owned(),
            revision,
        };
        let material = Material::check("anthropic-api-key", secret, None)?;
        match self
            .reserve(user, mutation, &digest, &target, &material)
            .await?
        {
            Reserved::New(record_id) => Ok(record_id),
            _ => panic!("a fresh mutation reserves a new version"),
        }
    }
}

/// Nonsecret metadata of an owner's credentials, at `now` or as of `at`, as the vault's
/// `/metadata/{user_id}?at=` plus `relay_id`.
pub async fn read_metadata(
    database: &Database,
    user_id: i64,
    at: Option<&str>,
    now: DateTime<Utc>,
) -> Result<Value, CredentialError> {
    if user_id < 1 {
        return Err(refused("invalid user identity"));
    }
    let at = at
        .map(|value| {
            view::parse(value)
                .and_then(|at| at.with_nanosecond(0))
                .ok_or_else(|| refused("invalid metadata timestamp"))
        })
        .transpose()?;
    let mut work = database.begin().await?;
    storage::read_snapshot(&mut work).await?;
    let current = owner(&mut work, user_id).await?;
    let policies = storage::policies(&mut work, user_id).await?;
    work.commit().await?;
    Ok(view::metadata(
        user_id,
        &current.versions,
        &current.entries,
        &policies,
        at,
        now,
    ))
}

/// Read a mutation's journal state. An ID a relay reservation holds for another request
/// is refused, as an ID recorded for one is.
async fn journal(
    work: &mut UnitOfWork<'_>,
    user_id: i64,
    mutation: &str,
    digest: &str,
) -> Result<Journal, CredentialError> {
    Ok(match storage::mutation(work, user_id, mutation).await? {
        None => {
            let reserved = storage::intents(work, user_id, mutation).await?;
            if reserved.iter().any(|intent| intent.digest != digest) {
                return Err(CredentialError::new(StatusCode::CONFLICT, REUSED));
            }
            Journal::New
        }
        Some((recorded, _)) if recorded != digest => {
            return Err(CredentialError::new(StatusCode::CONFLICT, REUSED));
        }
        Some((_, Some(response))) => Journal::Done(json_text(&response)),
        Some((_, None)) => Journal::Pending,
    })
}

/// A mutation's recorded response.
async fn response(
    work: &mut UnitOfWork<'_>,
    user_id: i64,
    mutation: &str,
) -> Result<Option<Value>, CredentialError> {
    Ok(storage::mutation(work, user_id, mutation)
        .await?
        .and_then(|(_, response)| response)
        .map(|response| json_text(&response)))
}

/// This owner's live entry; a tombstone or a stale revision refuses the write.
async fn owned(
    work: &mut UnitOfWork<'_>,
    user_id: i64,
    entry_id: &str,
    revision: Option<i64>,
) -> Result<EntryRow, CredentialError> {
    let entry = storage::entry(work, user_id, entry_id)
        .await?
        .ok_or_else(|| CredentialError::new(StatusCode::NOT_FOUND, "no such credential"))?;
    if entry.removed.is_some() {
        return Err(CredentialError::new(
            StatusCode::GONE,
            "credential was removed",
        ));
    }
    if revision.is_some_and(|revision| revision != entry.revision) {
        return Err(CredentialError::new(StatusCode::CONFLICT, CONFLICT));
    }
    Ok(entry)
}

/// An owner's entries, activated versions and lists.
pub async fn owner(work: &mut UnitOfWork<'_>, user_id: i64) -> Result<Owner, CredentialError> {
    let lists = storage::lists(work, user_id)
        .await?
        .into_iter()
        .map(|(provider, revision, entries)| {
            (
                provider,
                (revision, serde_json::from_str(&entries).unwrap_or_default()),
            )
        })
        .collect();
    Ok(Owner {
        entries: storage::entries(work, user_id, None).await?,
        versions: storage::versions(work, user_id)
            .await?
            .into_iter()
            .filter(|version| version.activated)
            .collect(),
        lists,
    })
}

/// A provider list's revision and live entry IDs.
async fn list(
    work: &mut UnitOfWork<'_>,
    user_id: i64,
    provider: &str,
) -> Result<(i64, Vec<String>), CredentialError> {
    Ok(storage::list(work, user_id, provider)
        .await?
        .map(|(revision, entries)| (revision, serde_json::from_str(&entries).unwrap_or_default()))
        .unwrap_or_default())
}

/// An entry's activated versions, in version order.
async fn activated(
    work: &mut UnitOfWork<'_>,
    entry_id: &str,
) -> Result<Vec<VersionRow>, CredentialError> {
    Ok(storage::entry_versions(work, entry_id)
        .await?
        .into_iter()
        .filter(|version| version.activated)
        .collect())
}

/// Retire the usable versions among `versions` and queue their material's destruction,
/// as the vault's `destroy`; returns the versions retired.
async fn retire(
    work: &mut UnitOfWork<'_>,
    versions: &mut [VersionRow],
    at: &str,
    reason: &str,
) -> Result<Vec<VersionRow>, CredentialError> {
    let mut retired = Vec::new();
    for version in versions.iter_mut().filter(|version| view::usable(version)) {
        version.retired.get_or_insert_with(|| at.to_owned());
        version.status_reason = Some(reason.to_owned());
        if matches!(version.material.as_str(), "stored" | "importing") {
            version.material = "destroying".to_owned();
            storage::queue_destroy(work, &version.record_id).await?;
        }
        storage::update_version(work, version).await?;
        retired.push(version.clone());
    }
    Ok(retired)
}

/// Bump an entry's revision after applying `change` to it.
async fn touch(
    work: &mut UnitOfWork<'_>,
    entry: &EntryRow,
    at: &str,
    change: impl FnOnce(&mut EntryRow),
) -> Result<EntryRow, CredentialError> {
    let mut changed = entry.clone();
    change(&mut changed);
    changed.updated = at.to_owned();
    Ok(storage::update_entry(work, &changed).await?)
}

/// Commit a list revision and the policy snapshot it carries; returns the revision.
async fn arrange(
    work: &mut UnitOfWork<'_>,
    user_id: i64,
    provider: &str,
    order: &[String],
    at: &str,
) -> Result<i64, CredentialError> {
    let revision = list(work, user_id, provider).await?.0 + 1;
    let rows = storage::entries(work, user_id, Some(provider)).await?;
    let document = order
        .iter()
        .filter_map(|id| rows.iter().find(|row| row.entry_id == *id))
        .map(|row| {
            json!({"entry_id":row.entry_id,"name":row.name,"enabled":row.enabled,
                "roles":json_text(&row.roles),"profiles":json_text(&row.profiles)})
        })
        .collect::<Vec<_>>();
    storage::set_list(
        work,
        user_id,
        provider,
        revision,
        &json!(order).to_string(),
        Some(at),
        &json!(document).to_string(),
    )
    .await?;
    Ok(revision)
}
