//! Source-ordered sponsor consent, independent of funding policy activation and payment.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::{Actor, IssueKey, LaterCommand, Request, RequestKind, SourceOrder, python_digest};

/// Inputs shared by `initial_sponsorship_transitions`.
pub struct InitialFundingContext<'a> {
    /// Task generation whose funding or review identity is being bound.
    pub generation: i64,
    /// Previous provider slot epochs preserved during adoption.
    pub prior_epochs: Option<&'a BTreeMap<Provider, i64>>,
    /// Target funding mode used when the source has no explicit choice.
    pub default_funding: FundingMode,
    /// Funding mode selected explicitly by the command, if any.
    pub source_funding: Option<FundingMode>,
    /// Authenticated browser or comment source for these consent facts.
    pub source_channel: &'a str,
}

/// Provider whose personal credentials may fund a task attempt.
#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Provider {
    /// OpenAI API keys or ChatGPT subscriptions.
    Openai,
    /// Anthropic API keys or Claude subscriptions.
    Anthropic,
    /// Gemini API keys or Antigravity subscriptions.
    Google,
}

impl Provider {
    /// Providers participating in consent and funding projections.
    pub const ALL: [Self; 3] = [Self::Openai, Self::Anthropic, Self::Google];

    /// Resolve a canonical provider name.
    pub fn from_name(name: &str) -> Option<Self> {
        Self::ALL
            .into_iter()
            .find(|provider| provider.as_str() == name)
    }

    /// Resolve the provider behind a supported agent CLI.
    pub fn from_harness(harness: &str) -> Option<Self> {
        Self::ALL
            .into_iter()
            .find(|provider| provider.harness() == harness)
    }

    /// Agent CLI associated with this provider.
    pub fn harness(self) -> &'static str {
        match self {
            Self::Openai => "codex",
            Self::Anthropic => "claude",
            Self::Google => "agy",
        }
    }

    /// Validate model-specific selections before a profile can enter a grant.
    pub fn validate_model(self, model: &str, effort: &str) -> anyhow::Result<()> {
        if self == Self::Google {
            concurro_agy::validate_model(model, effort)?;
        }
        Ok(())
    }

    /// Return the provider's canonical command and storage name.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Openai => "openai",
            Self::Anthropic => "anthropic",
            Self::Google => "google",
        }
    }
}

/// Explicit provider funding choice, distinct from a sponsor's consent.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum FundingMode {
    /// Target organization funds this provider.
    Org,
    /// An eligible consenting person may fund this provider.
    Contributed,
}

/// Effective sponsorship command from one verified source revision.
#[derive(Clone, Debug, Eq, PartialEq, Deserialize, Serialize)]
pub enum ConsentAction {
    /// Offer this actor's credentials to the provider.
    Sponsor,
    /// Choose a funding mode; contributed also offers the actor's credentials.
    Funding(FundingMode),
    /// Withdraw this actor's offer, optionally naming the source it removes.
    Withdraw { removed_source: Option<i64> },
    /// Remove one edited funding choice and its sponsor offer if it supplied one.
    FundingRemoved {
        /// Source comment whose choice was removed.
        removed_source: i64,
        /// Whether that choice also offered this actor's credentials.
        removes_sponsor: bool,
    },
}

/// Immutable effective command and stable actor identity from one source.
#[derive(Clone, Debug, Eq, PartialEq, Deserialize, Serialize)]
pub struct ConsentFact {
    /// Provider targeted by the command.
    pub provider: Provider,
    /// GitHub actor who made the command.
    pub actor: Actor,
    /// Revision-aware GitHub source order.
    pub order: SourceOrder,
    /// Stable grant or withdrawal evidence identity.
    pub grant_id: String,
    /// Effective command at this source revision.
    pub action: ConsentAction,
}

/// Sponsor occupying a provider's current consent slot.
#[derive(Clone, Debug, Eq, PartialEq, Deserialize, Serialize)]
pub enum SponsorSlot {
    /// No sponsor has occupied the slot.
    Unassigned,
    /// This actor most recently offered consent.
    Active {
        /// Stable GitHub sponsor identity.
        sponsor: Actor,
        /// Evidence identity of the latest offer.
        grant_id: String,
        /// Source from which this offer came.
        source: SourceOrder,
    },
    /// The latest occupying sponsor withdrew; earlier sponsors stay superseded.
    Withdrawn {
        /// Evidence identity of the withdrawal.
        grant_id: String,
    },
}

/// Folded consent and explicit funding choice for one provider.
#[derive(Clone, Debug, Eq, PartialEq, Deserialize, Serialize)]
pub struct ProviderConsent {
    /// Current sponsor slot.
    pub slot: SponsorSlot,
    /// Most recent explicit funding choice, independent of the slot.
    pub funding: Option<FundingMode>,
    /// Source of that explicit funding choice.
    pub funding_order: Option<SourceOrder>,
    /// Latest withdrawal evidence per GitHub actor.
    pub tombstones: BTreeMap<i64, String>,
}

/// Folded provider state for an issue, including pre-enrollment commands.
#[derive(Clone, Debug, Eq, PartialEq, Deserialize, Serialize)]
pub struct ConsentProjection {
    /// All providers, including providers with no facts.
    pub providers: BTreeMap<Provider, ProviderConsent>,
}

/// Consent frozen at first enrollment, with the providers filled by the initiator.
#[derive(Clone, Debug, Eq, PartialEq, Deserialize, Serialize)]
pub struct InitialConsent {
    /// Explicit-first provider projection at the boundary.
    pub projection: ConsentProjection,
    /// Provider slots filled by the initiator rather than an explicit command.
    pub implicit: BTreeSet<Provider>,
}

/// Normalize an authenticated sponsorship request into one fact per provider.
pub fn consent_facts(request: &Request) -> Vec<ConsentFact> {
    let (provider, action) = match &request.kind {
        RequestKind::Later(LaterCommand::Sponsor {
            provider: Some(provider),
        }) => (provider, ConsentAction::Sponsor),
        RequestKind::Later(LaterCommand::Funding { provider, mode }) => (
            provider,
            ConsentAction::Funding(match mode.as_str() {
                "org" => FundingMode::Org,
                "personal" => FundingMode::Contributed,
                _ => return Vec::new(),
            }),
        ),
        RequestKind::Later(LaterCommand::Withdraw { provider }) => (
            provider,
            ConsentAction::Withdraw {
                removed_source: None,
            },
        ),
        _ => return Vec::new(),
    };
    let Some(provider) = Provider::from_name(provider) else {
        return Vec::new();
    };
    vec![ConsentFact {
        provider,
        actor: request.actor.clone(),
        order: request.order.clone(),
        grant_id: hex::encode(Sha256::digest(
            format!("{}:{}", request.order.source, provider.as_str()).as_bytes(),
        )),
        action,
    }]
}

/// Fold source revisions in GitHub order; a withdrawal never restores an older payer.
pub fn project_consent(facts: &[ConsentFact]) -> ConsentProjection {
    let empty = || ProviderConsent {
        slot: SponsorSlot::Unassigned,
        funding: None,
        funding_order: None,
        tombstones: BTreeMap::new(),
    };
    let mut providers = Provider::ALL
        .into_iter()
        .map(|provider| (provider, empty()))
        .collect::<BTreeMap<_, _>>();
    let mut ordered = facts.iter().collect::<Vec<_>>();
    ordered.sort_by_key(|fact| &fact.order);
    for fact in ordered {
        let state = providers
            .get_mut(&fact.provider)
            .expect("all providers are initialized");
        if let ConsentAction::Funding(mode) = fact.action {
            state.funding = Some(mode);
            state.funding_order = Some(fact.order.clone());
            if mode == FundingMode::Org {
                continue;
            }
        }
        match &fact.action {
            ConsentAction::Sponsor | ConsentAction::Funding(FundingMode::Contributed) => {
                state.slot = SponsorSlot::Active {
                    sponsor: fact.actor.clone(),
                    grant_id: fact.grant_id.clone(),
                    source: fact.order.clone(),
                };
                state.tombstones.remove(&fact.actor.id);
            }
            ConsentAction::Withdraw { removed_source } => {
                state
                    .tombstones
                    .insert(fact.actor.id, fact.grant_id.clone());
                if let SponsorSlot::Active {
                    sponsor, source, ..
                } = &state.slot
                    && sponsor.id == fact.actor.id
                    && removed_source.is_none_or(|removed| removed == source.source_id)
                {
                    state.slot = SponsorSlot::Withdrawn {
                        grant_id: fact.grant_id.clone(),
                    };
                }
            }
            ConsentAction::FundingRemoved {
                removed_source,
                removes_sponsor,
            } => {
                if state
                    .funding_order
                    .as_ref()
                    .is_some_and(|order| order.source_id == *removed_source)
                {
                    state.funding = None;
                    state.funding_order = None;
                }
                if *removes_sponsor {
                    state
                        .tombstones
                        .insert(fact.actor.id, fact.grant_id.clone());
                    if let SponsorSlot::Active {
                        sponsor, source, ..
                    } = &state.slot
                        && sponsor.id == fact.actor.id
                        && source.source_id == *removed_source
                    {
                        state.slot = SponsorSlot::Withdrawn {
                            grant_id: fact.grant_id.clone(),
                        };
                    }
                }
            }
            ConsentAction::Funding(FundingMode::Org) => {}
        }
    }
    ConsentProjection { providers }
}

/// Freeze explicit facts at the first source boundary, then fill eligible vacant slots.
pub fn initialize_consent(
    facts: &[ConsentFact],
    boundary: &SourceOrder,
    initiator: &Actor,
    available: &BTreeMap<Provider, Option<bool>>,
) -> Option<InitialConsent> {
    let before = facts
        .iter()
        .filter(|fact| &fact.order <= boundary)
        .cloned()
        .collect::<Vec<_>>();
    let mut projection = project_consent(&before);
    let mut implicit = BTreeSet::new();
    for (provider, state) in &mut projection.providers {
        if matches!(state.slot, SponsorSlot::Active { .. })
            || state.tombstones.contains_key(&initiator.id)
        {
            continue;
        }
        let eligible = available.get(provider).copied().flatten()?;
        if eligible {
            let mut source = boundary.clone();
            source.index = -1;
            state.slot = SponsorSlot::Active {
                sponsor: initiator.clone(),
                grant_id: hex::encode(Sha256::digest(
                    format!("{}:{}:{}", boundary.source, initiator.id, provider.as_str())
                        .as_bytes(),
                )),
                source,
            };
            implicit.insert(*provider);
        }
    }
    Some(InitialConsent {
        projection,
        implicit,
    })
}

/// Build the exact initial Exchange slot requests from a frozen boundary.
pub fn initial_sponsorship_transitions(
    initial: &InitialConsent,
    repository: &str,
    issue: IssueKey,
    initiator: &Actor,
    inputs: InitialFundingContext<'_>,
) -> anyhow::Result<Vec<Value>> {
    let InitialFundingContext {
        generation,
        prior_epochs,
        default_funding,
        source_funding,
        source_channel,
    } = inputs;
    anyhow::ensure!(generation > 0, "sponsorship generation is invalid");
    let mode = |funding| match funding {
        FundingMode::Org => "org",
        FundingMode::Contributed => "contributed",
    };
    initial
        .projection
        .providers
        .iter()
        .map(|(provider, state)| {
            let funding = mode(state.funding.unwrap_or(default_funding));
            let mut slot = match &state.slot {
                SponsorSlot::Unassigned => json!({
                    "status": "unassigned", "sponsor_id": null, "epoch": 0,
                    "funding": funding,
                }),
                SponsorSlot::Withdrawn { grant_id } => json!({
                    "status": "withdrawn", "sponsor_id": null, "epoch": 0,
                    "grant_id": grant_id, "funding": funding,
                }),
                SponsorSlot::Active {
                    sponsor,
                    grant_id,
                    source,
                } => {
                    let implicit = initial.implicit.contains(provider);
                    let provenance = if implicit {
                        match source_channel {
                            "label" => "label",
                            "review_request" => "review_request",
                            _ => "task_start",
                        }
                    } else {
                        "explicit_comment"
                    };
                    let source_mode = if implicit {
                        source_funding.map(mode).unwrap_or("unknown")
                    } else {
                        "org"
                    };
                    json!({
                        "status": "active", "sponsor_id": sponsor.id.to_string(),
                        "login": sponsor.login, "grant_id": grant_id, "epoch": 1,
                        "funding": funding, "provenance": provenance,
                        "source_funding_mode": source_mode,
                        "source_id": source.source_id,
                        "source_order": [source.at.to_rfc3339(), source.source_id, source.index],
                    })
                }
            };
            if let Some(order) = &state.funding_order {
                slot["funding_order"] =
                    json!([order.at.to_rfc3339(), order.source_id, order.index]);
            }
            let expected_epoch = prior_epochs
                .and_then(|epochs| epochs.get(provider).copied())
                .unwrap_or(-1);
            if expected_epoch >= 0 {
                slot["epoch"] = json!(expected_epoch + 1);
            }
            let mut request = json!({
                "repository": repository, "issue": issue.number,
                "provider": provider.as_str(), "generation": generation,
                "expected_epoch": expected_epoch, "slot": slot, "reason": "initialize",
                "actor": initiator.id.to_string(), "cutoff": null,
            });
            request["transition"] = Value::String(python_digest(&request)?);
            Ok(request)
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::InitialFundingContext;
    use std::collections::BTreeMap;

    use chrono::{TimeZone, Utc};

    use super::{
        ConsentAction, ConsentFact, FundingMode, Provider, SponsorSlot,
        initial_sponsorship_transitions, initialize_consent, project_consent,
    };
    use crate::{Actor, IssueKey, SourceOrder, python_digest};

    /// Make one source-ordered command with an explicit actor and source identity.
    fn fact(
        source_id: i64,
        actor_id: i64,
        provider: Provider,
        action: ConsentAction,
    ) -> ConsentFact {
        ConsentFact {
            provider,
            actor: Actor {
                id: actor_id,
                login: format!("user-{actor_id}"),
            },
            order: SourceOrder {
                at: Utc.timestamp_opt(source_id, 0).single().expect("time"),
                source_id,
                source: format!("source-{source_id}"),
                index: 0,
            },
            grant_id: format!("grant-{source_id}"),
            action,
        }
    }

    #[test]
    fn withdrawal_never_restores_an_older_sponsor() {
        let facts = [
            fact(
                3,
                20,
                Provider::Openai,
                ConsentAction::Withdraw {
                    removed_source: None,
                },
            ),
            fact(1, 10, Provider::Openai, ConsentAction::Sponsor),
            fact(2, 20, Provider::Openai, ConsentAction::Sponsor),
        ];
        let projection = project_consent(&facts);
        let state = &projection.providers[&Provider::Openai];
        assert_eq!(
            state.slot,
            SponsorSlot::Withdrawn {
                grant_id: "grant-3".into()
            }
        );
        assert_eq!(
            state.tombstones.get(&20).map(String::as_str),
            Some("grant-3")
        );
        assert_eq!(
            projection.providers[&Provider::Anthropic].slot,
            SponsorSlot::Unassigned
        );
    }

    #[test]
    fn funding_mode_and_consent_are_independent() {
        let facts = [
            fact(1, 10, Provider::Openai, ConsentAction::Sponsor),
            fact(
                2,
                20,
                Provider::Openai,
                ConsentAction::Funding(FundingMode::Org),
            ),
            fact(
                3,
                30,
                Provider::Openai,
                ConsentAction::Funding(FundingMode::Contributed),
            ),
            fact(
                4,
                30,
                Provider::Openai,
                ConsentAction::Withdraw {
                    removed_source: Some(1),
                },
            ),
        ];
        let state = &project_consent(&facts).providers[&Provider::Openai];
        assert_eq!(state.funding, Some(FundingMode::Contributed));
        assert_eq!(
            state.funding_order.as_ref().map(|order| order.source_id),
            Some(3)
        );
        std::assert_matches!(&state.slot, SponsorSlot::Active { sponsor, source, .. } if sponsor.id == 30 && source.source_id == 3);
        assert!(state.tombstones.contains_key(&30));
        let removed = fact(
            5,
            30,
            Provider::Openai,
            ConsentAction::FundingRemoved {
                removed_source: 3,
                removes_sponsor: true,
            },
        );
        let removed_projection =
            project_consent(&[facts.as_slice(), std::slice::from_ref(&removed)].concat());
        let state = &removed_projection.providers[&Provider::Openai];
        assert_eq!(state.funding, None);
        std::assert_matches!(state.slot, SponsorSlot::Withdrawn { .. });
        let newer = fact(
            4,
            20,
            Provider::Openai,
            ConsentAction::Funding(FundingMode::Org),
        );
        let newer_projection = project_consent(&[facts.as_slice(), &[newer, removed]].concat());
        let state = &newer_projection.providers[&Provider::Openai];
        assert_eq!(state.funding, Some(FundingMode::Org));
    }

    #[test]
    fn first_boundary_prefers_explicit_consent_and_honors_initiator_tombstones() {
        let explicit = fact(3, 10, Provider::Openai, ConsentAction::Sponsor);
        let withdrawal = fact(
            2,
            20,
            Provider::Anthropic,
            ConsentAction::Withdraw {
                removed_source: None,
            },
        );
        let boundary = fact(3, 20, Provider::Anthropic, ConsentAction::Sponsor).order;
        let mut later = fact(4, 30, Provider::Openai, ConsentAction::Sponsor);
        later.order.at = boundary.at;
        let available = BTreeMap::from([
            (Provider::Openai, Some(true)),
            (Provider::Anthropic, Some(true)),
            (Provider::Google, Some(false)),
        ]);
        let frozen = initialize_consent(
            &[later, withdrawal.clone(), explicit],
            &boundary,
            &Actor {
                id: 20,
                login: "initiator".to_owned(),
            },
            &available,
        )
        .expect("known availability");
        std::assert_matches!(&frozen.projection.providers[&Provider::Openai].slot, SponsorSlot::Active { sponsor, .. } if sponsor.id == 10);
        assert_eq!(
            frozen.projection.providers[&Provider::Anthropic].slot,
            SponsorSlot::Unassigned
        );
        assert!(frozen.implicit.is_empty());
        let other = Actor {
            id: 40,
            login: "other".to_owned(),
        };
        let filled = initialize_consent(&[withdrawal], &boundary, &other, &available)
            .expect("known availability");
        assert!(filled.implicit.contains(&Provider::Anthropic));
        std::assert_matches!(&filled.projection.providers[&Provider::Anthropic].slot, SponsorSlot::Active { sponsor, source, .. } if sponsor.id == other.id && source.index == -1);
        let transitions = initial_sponsorship_transitions(
            &filled,
            "org/fork",
            IssueKey {
                repository_id: 7,
                number: 12,
            },
            &other,
            InitialFundingContext {
                generation: 1,
                prior_epochs: None,
                default_funding: FundingMode::Contributed,
                source_funding: Some(FundingMode::Contributed),
                source_channel: "comment",
            },
        )
        .expect("initial transitions");
        assert_eq!(transitions.len(), 3);
        let anthropic = transitions
            .iter()
            .find(|request| request["provider"] == "anthropic")
            .expect("anthropic transition");
        assert_eq!(anthropic["slot"]["epoch"], 1);
        assert_eq!(anthropic["slot"]["source_funding_mode"], "contributed");
        let mut unhashed = anthropic.clone();
        let identity = unhashed["transition"]
            .as_str()
            .expect("transition ID")
            .to_owned();
        unhashed
            .as_object_mut()
            .expect("object")
            .remove("transition");
        assert_eq!(identity, python_digest(&unhashed).expect("identity"));
        let unknown = BTreeMap::from([(Provider::Openai, Some(true)), (Provider::Anthropic, None)]);
        assert!(initialize_consent(&[], &boundary, &other, &unknown,).is_none());
    }
}
