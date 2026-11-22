//! The vault's owner and metadata JSON, built from native rows (`gateway/vault/vault.py`
//! `view`, `group`, `listing`, `record` and `metadata`).

use std::collections::HashMap;

use chrono::{DateTime, SecondsFormat, Utc};
use concurro_accounts_postgres::credentials::{EntryRow, PolicyRow, VersionRow};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

/// Route, provider, kind and default entry name, in the vault's order.
pub const ROUTES: [(&str, &str, &str, &str); 4] = [
    ("openai-api-key", "openai", "api_key", "OpenAI API key"),
    (
        "anthropic-api-key",
        "anthropic",
        "api_key",
        "Anthropic API key",
    ),
    (
        "chatgpt-subscription",
        "openai",
        "subscription",
        "ChatGPT subscription",
    ),
    (
        "claude-subscription",
        "anthropic",
        "subscription",
        "Claude subscription",
    ),
];

/// Providers in the vault's sorted order.
pub const PROVIDERS: [&str; 2] = ["anthropic", "openai"];

/// A route's provider, kind and default name.
pub fn route(route: &str) -> Option<(&'static str, &'static str, &'static str)> {
    ROUTES
        .iter()
        .find(|(name, ..)| *name == route)
        .map(|(_, provider, kind, label)| (*provider, *kind, *label))
}

/// Hex SHA-256 of a string, as the vault's `digest`.
pub fn digest(value: &str) -> String {
    hex::encode(Sha256::digest(value.as_bytes()))
}

/// The vault's fingerprint of a secret: its last four characters and a short digest.
pub fn fingerprint(secret: &str) -> String {
    let tail = secret
        .char_indices()
        .rev()
        .nth(3)
        .map_or(0, |(index, _)| index);
    format!("{}:{}", &secret[tail..], &digest(secret)[..8])
}

/// A version's opaque identity, `sha256("{user}/{route}/{version}")`.
pub fn record_id(user_id: i64, route: &str, version: i64) -> String {
    digest(&format!("{user_id}/{route}/{version}"))
}

/// The vault's timestamp spelling: UTC, whole seconds, `+00:00`.
pub fn stamp(at: DateTime<Utc>) -> String {
    at.to_rfc3339_opts(SecondsFormat::Secs, false)
}

/// Parse a stored or supplied aware ISO-8601 instant, as Python's `fromisoformat` reads
/// the vault's values.
pub fn parse(value: &str) -> Option<DateTime<Utc>> {
    let value = value.replace('Z', "+00:00");
    DateTime::parse_from_rfc3339(&value)
        .or_else(|_| DateTime::parse_from_str(&value, "%Y-%m-%dT%H:%M:%S%.f%:z"))
        .ok()
        .map(|parsed| parsed.with_timezone(&Utc))
}

/// Whether a version's material can still be used: stored, still being imported, or an
/// unretired relay binding.
pub fn usable(version: &VersionRow) -> bool {
    matches!(version.material.as_str(), "stored" | "importing")
        || version.custody == "relay" && version.relay_id.is_some() && version.retired.is_none()
}

/// Whether a known expiry has passed at `at`.
fn expired(version: &VersionRow, at: DateTime<Utc>) -> bool {
    version.expiry_known
        && version
            .expires_at
            .as_deref()
            .and_then(parse)
            .is_some_and(|expiry| at >= expiry)
}

/// Parse a JSON text column, `null` if absent or unreadable.
pub fn json_text(value: Option<&str>) -> Value {
    value
        .and_then(|text| serde_json::from_str(text).ok())
        .unwrap_or(Value::Null)
}

/// One version's owner-visible metadata, as the vault's `record`.
pub fn record(version: &VersionRow, now: DateTime<Utc>) -> Value {
    let (status, reason) = if expired(version, now) {
        ("invalid".to_owned(), Some("expired".to_owned()))
    } else {
        (version.status.clone(), version.status_reason.clone())
    };
    json!({"route":version.route,"status":status,"fingerprint":version.fingerprint,
        "version":version.version,"updated":version.updated,"kind":version.kind,
        "status_reason":reason,"validated":version.validated,"expires_at":version.expires_at,
        "expiry_known":version.expiry_known,"last_used":version.last_used,
        "account_label":version.account_label,"verified_account":version.verified_account,
        "models":json_text(version.models.as_deref()),
        "custody":version.custody,"relay_id":version.relay_id,"record_id":version.record_id})
}

/// One entry with its position in `list` and its latest activated version, as the vault's
/// `view`.
pub fn view(
    entry: &EntryRow,
    list: &[String],
    latest: Option<&VersionRow>,
    now: DateTime<Utc>,
) -> Value {
    json!({"entry_id":entry.entry_id,"provider":entry.provider,"route":entry.route,
        "name":entry.name,"revision":entry.revision,"created":entry.created,
        "updated":entry.updated,"removed":entry.removed,
        "kind":route(&entry.route).map(|(_, kind, _)| kind),"enabled":entry.enabled,
        "roles":json_text(Some(&entry.roles)),"profiles":json_text(Some(&entry.profiles)),
        "position":list.iter().position(|id| *id == entry.entry_id),
        "record":latest.map(|version| record(version, now))})
}

/// An owner's entries, activated versions and lists, read together.
pub struct Owner {
    /// Every entry, removed or not.
    pub entries: Vec<EntryRow>,
    /// Activated versions in version order.
    pub versions: Vec<VersionRow>,
    /// Each provider's revision and live entry IDs.
    pub lists: HashMap<String, (i64, Vec<String>)>,
}

impl Owner {
    /// The latest activated version of an entry.
    pub fn latest(&self, entry_id: &str) -> Option<&VersionRow> {
        self.versions
            .iter()
            .filter(|version| version.entry_id == entry_id)
            .max_by_key(|version| version.version)
    }

    /// One provider's revision and live entry IDs.
    pub fn list(&self, provider: &str) -> (i64, &[String]) {
        self.lists
            .get(provider)
            .map_or((0, &[][..]), |(revision, entries)| (*revision, entries))
    }

    /// One entry's view.
    pub fn view(&self, entry: &EntryRow, now: DateTime<Utc>) -> Value {
        view(
            entry,
            self.list(&entry.provider).1,
            self.latest(&entry.entry_id),
            now,
        )
    }

    /// One provider's list in priority order, as the vault's `group`.
    pub fn group(&self, provider: &str, now: DateTime<Utc>) -> Value {
        let (revision, list) = self.list(provider);
        let entries = list
            .iter()
            .filter_map(|id| self.entries.iter().find(|entry| entry.entry_id == *id))
            .map(|entry| self.view(entry, now))
            .collect::<Vec<_>>();
        json!({"provider":provider,"revision":revision,"entries":entries})
    }

    /// Both provider lists and the types a new entry may take, as the vault's `listing`.
    pub fn listing(&self, now: DateTime<Utc>) -> Value {
        let types = ROUTES
            .iter()
            .map(|(route, provider, kind, label)| {
                json!({"route":route,"provider":provider,"kind":kind,"label":label})
            })
            .collect::<Vec<_>>();
        let providers = PROVIDERS
            .iter()
            .map(|provider| self.group(provider, now))
            .collect::<Vec<_>>();
        json!({"providers":providers,"types":types})
    }
}

/// One entry's policy in a list revision's document.
type Policy = Map<String, Value>;

/// Nonsecret status of each entry's latest version and its list policy, now or as of
/// `at`, as the vault's `metadata`, plus `relay_id` on relay-custody versions.
///
/// `versions` are the owner's activated versions in the vault's order.
pub fn metadata(
    user_id: i64,
    versions: &[VersionRow],
    entries: &[EntryRow],
    policies: &[PolicyRow],
    at: Option<DateTime<Utc>>,
    now: DateTime<Utc>,
) -> Value {
    let cutoff = at.unwrap_or(now);
    let mut lists: Vec<(String, i64, Vec<Policy>)> = Vec::new();
    for policy in policies {
        if policy
            .at
            .as_deref()
            .is_none_or(|time| parse(time).is_some_and(|time| time <= cutoff))
        {
            let document = serde_json::from_str::<Vec<Map<String, Value>>>(&policy.document)
                .unwrap_or_default();
            match lists
                .iter_mut()
                .find(|(provider, ..)| *provider == policy.provider)
            {
                Some(chosen) => *chosen = (policy.provider.clone(), policy.revision, document),
                None => lists.push((policy.provider.clone(), policy.revision, document)),
            }
        }
    }
    let mut latest: Vec<(String, Map<String, Value>)> = Vec::new();
    for version in versions {
        if parse(&version.created).is_none_or(|created| created > cutoff) {
            continue;
        }
        let Some(entry) = entries
            .iter()
            .find(|entry| entry.entry_id == version.entry_id)
        else {
            continue;
        };
        let mut status = version.status.clone();
        if at.is_some() {
            if let Some(retired) = version.retired.as_deref() {
                status = if parse(retired).is_some_and(|retired| cutoff < retired) {
                    "valid"
                } else {
                    "revoked"
                }
                .to_owned();
            } else if !usable(version) {
                status = "unknown".to_owned();
            }
        } else if !usable(version) && status != "revoked" {
            status = "invalid".to_owned();
        }
        if expired(version, cutoff) {
            status = "invalid".to_owned();
        }
        let listed = lists
            .iter()
            .find(|(provider, ..)| *provider == entry.provider)
            .map_or(&[][..], |(_, _, document)| document.as_slice());
        let position = listed
            .iter()
            .position(|item| item.get("entry_id").and_then(Value::as_str) == Some(&entry.entry_id));
        let policy = match position {
            Some(position) => listed[position].clone(),
            None => json!({"name":entry.name,"enabled":false,
                "roles":json_text(Some(&entry.roles)),"profiles":json_text(Some(&entry.profiles))})
            .as_object()
            .cloned()
            .unwrap_or_default(),
        };
        let mut value = json!({"record_id":version.record_id,"user_id":user_id.to_string(),
            "route":version.route,"version":version.version,"status":status,
            "models":json_text(version.models.as_deref()),
            "created":version.created,"updated":version.updated,"kind":version.kind,
            "custody":version.custody,"status_reason":version.status_reason,
            "validated":version.validated,
            "expires_at":if version.expiry_known { version.expires_at.clone() } else { None },
            "expiry_known":version.expiry_known,"token_revision":version.token_revision,
            "provider":entry.provider});
        let object = value.as_object_mut().expect("object literal");
        object.extend(policy);
        object.insert("entry_id".to_owned(), json!(version.entry_id));
        object.insert("position".to_owned(), json!(position));
        object.insert("removed".to_owned(), json!(position.is_none()));
        if version.custody == "relay" {
            object.insert("relay_id".to_owned(), json!(version.relay_id));
        }
        let object = object.clone();
        match latest
            .iter_mut()
            .find(|(entry_id, _)| *entry_id == version.entry_id)
        {
            Some(slot) => slot.1 = object,
            None => latest.push((version.entry_id.clone(), object)),
        }
    }
    let lists = lists
        .into_iter()
        .map(|(provider, revision, document)| {
            let ids = document
                .iter()
                .map(|item| item.get("entry_id").cloned().unwrap_or(Value::Null))
                .collect::<Vec<_>>();
            (provider, json!({"revision":revision,"entries":ids}))
        })
        .collect::<Map<_, _>>();
    json!({
        "history_complete":latest.iter().all(|(_, value)| value["status"] != "unknown"),
        "credentials":latest.into_iter().map(|(_, value)| Value::Object(value)).collect::<Vec<_>>(),
        "lists":lists,
    })
}
