//! Sponsor relay approval, device replacement and status through the verifier's owner API
//! v1 (`concurro-relay-hosted-api`).
//!
//! An approval reserves each route's next version in `relay_intents`, asks the verifier,
//! then commits the entries and relay-custody versions and releases the reservations. A
//! reservation a crash left behind is settled by repeating the identical approval.

use std::{collections::BTreeMap, time::Duration};

use axum::http::StatusCode;
use chrono::DateTime;
use concurro_accounts_postgres::credentials::{self as storage, EntryRow, IntentRow, VersionRow};
use concurro_contracts::CredentialChange;
use concurro_relay_hosted_api::owner::{
    APPROVALS_PATH, Approval, Approved, Binding, REGISTRATIONS_PREFIX, RELAYS_PATH, Registration,
    Relays, User as OwnerUser,
};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use time::OffsetDateTime;

use super::{
    CONFLICT, Journal, Native, REUSED, activated, arrange, journal, list, mutation_id, name, owned,
    owner, profiles, refused, retire, revision, roles, touch,
};
use crate::{
    credentials::{CredentialError, DeviceBody, RelayBody, view},
    github::User,
};

/// The routes a relay can serve.
const RELAY_ROUTES: [&str; 2] = ["chatgpt-subscription", "claude-subscription"];
/// How long a reservation may stay before settlement repeats its approval.
const SETTLE_AFTER_SECONDS: i64 = 120;

/// The verifier's owner API, with the credential only accounts holds.
#[derive(Clone)]
pub struct Verifier {
    client: Client,
    base: String,
    token: String,
}

/// The verifier's error vocabulary, mapped to the vault's answers.
fn unavailable() -> CredentialError {
    CredentialError::new(
        StatusCode::SERVICE_UNAVAILABLE,
        "relay verifier unavailable",
    )
}

impl Verifier {
    /// Bind the owner API at `base` with its bearer.
    pub fn new(client: Client, base: &str, token: String) -> Self {
        Self {
            client,
            base: base.trim_end_matches('/').to_owned(),
            token,
        }
    }

    /// A pending registration; `404` if the code is unknown or expired.
    async fn registration(&self, code: &str) -> Result<Registration, CredentialError> {
        let response = self
            .client
            .get(format!("{}{REGISTRATIONS_PREFIX}{code}", self.base))
            .bearer_auth(&self.token)
            .timeout(Duration::from_secs(10))
            .send()
            .await
            .map_err(|_| unavailable())?;
        match response.status() {
            StatusCode::NOT_FOUND => Err(CredentialError::new(
                StatusCode::NOT_FOUND,
                "unknown or expired code",
            )),
            status if status.is_success() => response.json().await.map_err(|_| unavailable()),
            _ => Err(unavailable()),
        }
    }

    /// Approve exact bindings; idempotent for identical ones. Returns the relay ID.
    async fn approve(&self, approval: &Approval) -> Result<String, CredentialError> {
        let response = self
            .client
            .post(format!("{}{APPROVALS_PATH}", self.base))
            .bearer_auth(&self.token)
            .timeout(Duration::from_secs(60))
            .json(approval)
            .send()
            .await
            .map_err(|_| unavailable())?;
        match response.status() {
            StatusCode::CONFLICT | StatusCode::NOT_FOUND => Err(CredentialError::new(
                StatusCode::CONFLICT,
                "relay approval unavailable",
            )),
            status if status.is_success() => {
                let approved: Approved = response.json().await.map_err(|_| unavailable())?;
                let relay = approved.relay_id;
                if relay.starts_with("relay-") && relay.len() <= 80 {
                    Ok(relay)
                } else {
                    Err(CredentialError::new(
                        StatusCode::SERVICE_UNAVAILABLE,
                        "relay approval returned an invalid identity",
                    ))
                }
            }
            _ => Err(unavailable()),
        }
    }

    /// Live links and leases of an owner's relays.
    async fn relays(&self, user_id: i64) -> Result<Relays, CredentialError> {
        let response = self
            .client
            .get(format!("{}{RELAYS_PATH}", self.base))
            .query(&[("user_id", user_id)])
            .bearer_auth(&self.token)
            .timeout(Duration::from_secs(10))
            .send()
            .await
            .map_err(|_| unavailable())?;
        if !response.status().is_success() {
            return Err(unavailable());
        }
        response.json().await.map_err(|_| unavailable())
    }
}

/// What a reservation finishes into once approved.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
enum Plan {
    /// New entries, one per route.
    Add {
        code: String,
        login: String,
        name: String,
        roles: Vec<String>,
        profiles: Value,
        routes: Vec<String>,
    },
    /// Existing entries at the revisions the owner read, rebound to a new device.
    Replace {
        code: String,
        login: String,
        entries: BTreeMap<String, i64>,
    },
}

impl Plan {
    fn code(&self) -> &str {
        match self {
            Self::Add { code, .. } | Self::Replace { code, .. } => code,
        }
    }

    fn login(&self) -> &str {
        match self {
            Self::Add { login, .. } | Self::Replace { login, .. } => login,
        }
    }
}

/// One route's approval binding.
#[derive(Clone, Debug, Deserialize, Serialize)]
struct Reserved {
    route: String,
    entry_id: String,
    record_id: String,
    version: i64,
}

/// A displayed Sponsor code: two groups of four consonants.
fn code(value: &str) -> Result<(), CredentialError> {
    let group =
        |part: &str| part.len() == 4 && part.bytes().all(|b| b"BCDFGHJKLMNPQRSTVWXZ".contains(&b));
    let valid = value
        .split_once('-')
        .is_some_and(|(first, second)| group(first) && group(second));
    valid.then_some(()).ok_or_else(CredentialError::malformed)
}

/// An owner API time in the vault's spelling.
fn instant(at: OffsetDateTime) -> Option<String> {
    DateTime::from_timestamp(at.unix_timestamp(), 0).map(view::stamp)
}

impl Native {
    /// The device name and exact routes a displayed code would approve.
    pub async fn relay_pending(&self, value: &str) -> Result<Value, CredentialError> {
        code(value)?;
        let verifier = self.verifier.as_ref().ok_or_else(unavailable)?;
        let found = verifier.registration(value).await?;
        let mut routes = found.routes;
        if routes.is_empty()
            || routes
                .iter()
                .any(|route| !RELAY_ROUTES.contains(&route.as_str()))
        {
            return Err(unavailable());
        }
        routes.sort();
        Ok(json!({"name":found.name,"routes":routes,"expires_at":instant(found.expires_at)}))
    }

    /// Live links of the owner's current relay approvals; an approval alone is never live.
    pub async fn relay_status(&self, user_id: i64) -> Result<Value, CredentialError> {
        let Some(verifier) = &self.verifier else {
            return Ok(json!({"service":"unconfigured","relays":[]}));
        };
        let mut work = self.database.begin().await?;
        storage::read_snapshot(&mut work).await?;
        let current = owner(&mut work, user_id).await?;
        work.commit().await?;
        let mut approved = current
            .versions
            .iter()
            .filter(|version| version.custody == "relay" && version.retired.is_none())
            .filter_map(|version| version.relay_id.clone())
            .collect::<Vec<_>>();
        approved.sort();
        approved.dedup();
        if approved.is_empty() {
            return Ok(json!({"service":"available","relays":[]}));
        }
        let Ok(found) = verifier.relays(user_id).await else {
            return Ok(json!({"service":"unavailable","relays":[]}));
        };
        let relays = found
            .relays
            .into_iter()
            .filter(|relay| approved.contains(&relay.relay_id))
            .map(|relay| {
                let active = relay
                    .active
                    .into_iter()
                    .map(|lease| {
                        json!({"slot":lease.slot,"route":lease.route,"entry_id":lease.entry_id,
                            "version":lease.version,"since":instant(lease.since),
                            "task":lease.task.map(|task| json!({"repository":task.repository,
                                "issue":task.issue}))})
                    })
                    .collect::<Vec<_>>();
                json!({"relay":relay.relay_id,"idle":relay.idle,
                    "last_seen":relay.last_seen.and_then(instant),"active":active})
            })
            .collect::<Vec<_>>();
        Ok(
            json!({"service":"available","started":instant(found.started),
            "checked":instant(found.checked),"relays":relays}),
        )
    }

    /// Approve every route of one displayed code as new entries.
    pub async fn approve_relay(
        &self,
        user: &User,
        body: RelayBody,
    ) -> Result<Value, CredentialError> {
        code(&body.code)?;
        mutation_id(&body.mutation_id)?;
        let (name, roles, profiles) = (
            name(&body.name)?,
            roles(&body.roles)?,
            profiles(&body.profiles)?,
        );
        let mut routes = body.routes.clone();
        routes.sort();
        routes.dedup();
        if routes.is_empty()
            || routes.len() != body.routes.len()
            || routes
                .iter()
                .any(|route| !RELAY_ROUTES.contains(&route.as_str()))
        {
            return Err(refused("relay custody is unavailable for this route"));
        }
        let request = json!({"add_relay":body.code,"name":name,"roles":roles,"profiles":profiles,
            "routes":routes});
        let plan = Plan::Add {
            code: body.code,
            login: user.login.clone(),
            name,
            roles,
            profiles: json!(profiles),
            routes,
        };
        self.relay_write(user.id, &body.mutation_id, &request, plan)
            .await
    }

    /// Rebind one entry per route to a newly approved device; policy and positions stay.
    pub async fn replace_device(
        &self,
        user: &User,
        body: DeviceBody,
    ) -> Result<Value, CredentialError> {
        code(&body.code)?;
        mutation_id(&body.mutation_id)?;
        let mut entries = BTreeMap::new();
        for entry in &body.entries {
            let hex = entry.id.len() == 32
                && entry
                    .id
                    .bytes()
                    .all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'));
            if !hex {
                return Err(CredentialError::malformed());
            }
            entries.insert(entry.id.clone(), revision(entry.revision)?);
        }
        if entries.len() != body.entries.len() {
            return Err(refused("an entry is listed twice"));
        }
        let request = json!({"replace_device":entries,"code":body.code});
        let plan = Plan::Replace {
            code: body.code,
            login: user.login.clone(),
            entries,
        };
        self.relay_write(user.id, &body.mutation_id, &request, plan)
            .await
    }

    /// Reserve, approve once, then commit.
    async fn relay_write(
        &self,
        user_id: i64,
        mutation: &str,
        request: &Value,
        plan: Plan,
    ) -> Result<Value, CredentialError> {
        let verifier = self.verifier.as_ref().ok_or_else(unavailable)?;
        let digest = view::digest(&request.to_string());
        let reserved = match self
            .reserve_relay(user_id, mutation, &digest, &plan)
            .await?
        {
            Ok(reserved) => reserved,
            Err(response) => return Ok(response),
        };
        let relay = verifier
            .approve(&approval(user_id, &plan, &reserved))
            .await?;
        self.commit_relay(user_id, mutation, &digest, &plan, &reserved, &relay)
            .await
    }

    /// Reserve each route's version, or reuse this mutation's reservations; a finished
    /// mutation answers with its first response instead.
    async fn reserve_relay(
        &self,
        user_id: i64,
        mutation: &str,
        digest: &str,
        plan: &Plan,
    ) -> Result<Result<Vec<Reserved>, Value>, CredentialError> {
        let mut work = self.database.begin().await?;
        storage::lock_owner(&mut work, user_id).await?;
        if let Journal::Done(response) = journal(&mut work, user_id, mutation, digest).await? {
            return Ok(Err(response));
        }
        let existing = storage::intents(&mut work, user_id, mutation).await?;
        if let Some(intent) = existing.first() {
            if intent.digest != digest {
                return Err(CredentialError::new(StatusCode::CONFLICT, REUSED));
            }
            work.commit().await?;
            return Ok(Ok(bindings(&existing)?));
        }
        let ids: Vec<(String, String)> = match plan {
            Plan::Add { routes, .. } => routes
                .iter()
                .enumerate()
                .map(|(index, route)| {
                    let name = if index == 0 {
                        format!("relay-entry/{user_id}/{mutation}")
                    } else {
                        format!("relay-entry/{user_id}/{mutation}/{route}")
                    };
                    (route.clone(), view::digest(&name)[..32].to_owned())
                })
                .collect(),
            Plan::Replace { entries, .. } => {
                let mut ids = Vec::new();
                for (entry_id, revision) in entries {
                    let entry = owned(&mut work, user_id, entry_id, Some(*revision)).await?;
                    if !RELAY_ROUTES.contains(&entry.route.as_str()) {
                        return Err(refused("relay custody is unavailable for this route"));
                    }
                    ids.push((entry.route, entry.entry_id));
                }
                ids.sort();
                if ids.windows(2).any(|pair| pair[0].0 == pair[1].0) {
                    return Err(refused("relay custody is unavailable for this route"));
                }
                ids
            }
        };
        let operation = serde_json::to_string(plan).expect("plan serializes");
        let mut reserved = Vec::new();
        for (route, entry_id) in ids {
            let version = storage::next_version(&mut work, user_id, &route).await?;
            let binding = Reserved {
                record_id: view::record_id(user_id, &route, version),
                route: route.clone(),
                entry_id,
                version,
            };
            storage::insert_intent(
                &mut work,
                &IntentRow {
                    user_id,
                    route,
                    version,
                    mutation_id: mutation.to_owned(),
                    digest: digest.to_owned(),
                    binding: serde_json::to_string(&binding).expect("binding serializes"),
                    operation: operation.clone(),
                },
            )
            .await?;
            reserved.push(binding);
        }
        work.commit().await?;
        Ok(Ok(reserved))
    }

    /// Commit approved bindings as entries and relay-custody versions, release the
    /// reservations and record the response. A replace whose entry changed meanwhile is
    /// released and answers `409`.
    async fn commit_relay(
        &self,
        user_id: i64,
        mutation: &str,
        digest: &str,
        plan: &Plan,
        reserved: &[Reserved],
        relay: &str,
    ) -> Result<Value, CredentialError> {
        let now = self.now();
        let at = view::stamp(now);
        let mut work = self.database.begin().await?;
        storage::lock_owner(&mut work, user_id).await?;
        if let Journal::Done(response) = journal(&mut work, user_id, mutation, digest).await? {
            return Ok(response);
        }
        if storage::intents(&mut work, user_id, mutation)
            .await?
            .is_empty()
        {
            return Err(CredentialError::new(StatusCode::CONFLICT, CONFLICT));
        }
        let mut views = Vec::new();
        for binding in reserved {
            let (entry, change, previous) = match plan {
                Plan::Add {
                    name,
                    roles,
                    profiles,
                    ..
                } => {
                    let entry = EntryRow {
                        entry_id: binding.entry_id.clone(),
                        user_id,
                        provider: view::route(&binding.route)
                            .map(|(provider, ..)| provider.to_owned())
                            .unwrap_or_default(),
                        route: binding.route.clone(),
                        name: name.clone(),
                        enabled: true,
                        roles: json!(roles).to_string(),
                        profiles: profiles.to_string(),
                        revision: 0,
                        created: at.clone(),
                        updated: at.clone(),
                        removed: None,
                    };
                    storage::insert_entry(&mut work, &entry).await?;
                    (entry, CredentialChange::Added, Vec::new())
                }
                Plan::Replace { entries, .. } => {
                    let revision = entries.get(&binding.entry_id).copied();
                    let entry = match owned(&mut work, user_id, &binding.entry_id, revision).await {
                        Ok(entry) => entry,
                        Err(error) => {
                            drop(work);
                            self.release(user_id, mutation).await?;
                            return Err(error);
                        }
                    };
                    let mut previous = activated(&mut work, &entry.entry_id).await?;
                    let revoked = retire(&mut work, &mut previous, &at, "replaced").await?;
                    (entry, CredentialChange::Replaced, revoked)
                }
            };
            storage::insert_version(
                &mut work,
                &relay_version(user_id, plan.login(), binding, relay, &at),
            )
            .await?;
            let entry = touch(&mut work, &entry, &at, |_| {}).await?;
            let mut order = list(&mut work, user_id, &entry.provider).await?.1;
            if change == CredentialChange::Added {
                order.push(entry.entry_id.clone());
            }
            let list_revision = arrange(&mut work, user_id, &entry.provider, &order, &at).await?;
            self.fact(
                &mut work,
                user_id,
                &entry.provider,
                Some(&entry.entry_id),
                change,
                list_revision,
                &previous,
                now,
            )
            .await?;
            views.push(entry);
        }
        let current = owner(&mut work, user_id).await?;
        let mut shaped = serde_json::Map::new();
        for entry in &views {
            shaped.insert(entry.route.clone(), current.view(entry, now));
        }
        let value = match plan {
            Plan::Add { routes, .. } if routes.len() == 1 => {
                shaped.remove(&routes[0]).unwrap_or(Value::Null)
            }
            _ => json!({"entries":shaped}),
        };
        storage::record_mutation(
            &mut work,
            user_id,
            mutation,
            digest,
            Some(&value.to_string()),
        )
        .await?;
        storage::release_intents(&mut work, user_id, mutation).await?;
        work.commit().await?;
        Ok(value)
    }

    /// Release a mutation's reservations.
    async fn release(&self, user_id: i64, mutation: &str) -> Result<(), CredentialError> {
        let mut work = self.database.begin().await?;
        storage::lock_owner(&mut work, user_id).await?;
        storage::release_intents(&mut work, user_id, mutation).await?;
        work.commit().await?;
        Ok(())
    }

    /// Settle reservations a crash left: repeat the identical approval, then commit, or
    /// release them if the verifier refuses it.
    pub async fn settle_intents(&self) -> Result<(), CredentialError> {
        let Some(verifier) = &self.verifier else {
            return Ok(());
        };
        for (user_id, mutation) in
            storage::stale_intents(&self.database, SETTLE_AFTER_SECONDS).await?
        {
            let rows = {
                let mut work = self.database.begin().await?;
                let rows = storage::intents(&mut work, user_id, &mutation).await?;
                work.commit().await?;
                rows
            };
            let Some(first) = rows.first() else { continue };
            let Ok(plan) = serde_json::from_str::<Plan>(&first.operation) else {
                self.release(user_id, &mutation).await?;
                continue;
            };
            let reserved = bindings(&rows)?;
            match verifier.approve(&approval(user_id, &plan, &reserved)).await {
                Ok(relay) => {
                    if let Err(error) = self
                        .commit_relay(user_id, &mutation, &first.digest, &plan, &reserved, &relay)
                        .await
                    {
                        tracing::warn!(user_id, ?error, "relay reservation not committed");
                    }
                }
                Err(CredentialError(StatusCode::CONFLICT, _)) => {
                    self.release(user_id, &mutation).await?
                }
                Err(_) => tracing::warn!(user_id, "relay reservation awaits the verifier"),
            }
        }
        Ok(())
    }
}

/// Decode a mutation's reserved bindings.
fn bindings(rows: &[IntentRow]) -> Result<Vec<Reserved>, CredentialError> {
    rows.iter()
        .map(|row| serde_json::from_str(&row.binding).map_err(|_| CredentialError::unavailable()))
        .collect()
}

/// The owner API approval of these bindings.
fn approval(user_id: i64, plan: &Plan, reserved: &[Reserved]) -> Approval {
    Approval {
        code: plan.code().to_owned(),
        user: OwnerUser {
            id: user_id.unsigned_abs(),
            login: plan.login().to_owned(),
        },
        bindings: reserved
            .iter()
            .map(|binding| Binding {
                route: binding.route.clone(),
                entry_id: binding.entry_id.clone(),
                record_id: binding.record_id.clone(),
                version: binding.version.unsigned_abs(),
            })
            .collect(),
    }
}

/// A relay-custody version bound to an approved relay, as the vault's `relay_record`.
fn relay_version(
    user_id: i64,
    login: &str,
    binding: &Reserved,
    relay: &str,
    at: &str,
) -> VersionRow {
    VersionRow {
        user_id,
        route: binding.route.clone(),
        version: binding.version,
        record_id: binding.record_id.clone(),
        entry_id: binding.entry_id.clone(),
        login: login.to_owned(),
        status: "valid".to_owned(),
        fingerprint: format!("relay:{}", &view::digest(relay)[..8]),
        created: at.to_owned(),
        updated: at.to_owned(),
        kind: Some("subscription".to_owned()),
        models: None,
        retired: None,
        account_label: None,
        verified_account: None,
        status_reason: Some("stored".to_owned()),
        validated: None,
        expires_at: None,
        expiry_known: false,
        token_revision: 1,
        last_used: None,
        custody: "relay".to_owned(),
        relay_id: Some(relay.to_owned()),
        material: "none".to_owned(),
        activated: true,
        mutation_id: None,
        expected_revision: None,
        pending_entry: None,
    }
}
