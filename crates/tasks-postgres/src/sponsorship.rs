//! Durable issue-level consent facts, including sources received before enrollment.

use std::collections::BTreeMap;

use anyhow::{Context, ensure};
use chrono::{DateTime, Duration, Utc};
use concurro_contracts::TurnSubmissionV2;
use concurro_db::{Database, UnitOfWork};
use concurro_exchange_v1::{SponsorshipAck, TurnObservation, TurnSubmission};
use concurro_tasks_core::{
    Actor, AttemptSpec, CommentRevision, ConsentAction, ConsentFact, ConsentProjection,
    FundingMode, InitialConsent, IssueKey, Provider, Request, SourceOrder, TaskState,
    consent_facts, project_consent, python_digest,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use sqlx::types::Json;
use uuid::Uuid;

use crate::read_model::{append_consent_state, append_read_model_record};

/// Columns read by `record_credential_failure`.
#[derive(sqlx::FromRow)]
struct FailedCredentialGrantRow {
    /// Persisted task id.
    task_id: Option<Uuid>,
    /// Persisted standalone task id.
    standalone_task_id: Option<Uuid>,
    /// Persisted payer id.
    payer_id: i64,
    /// Persisted provider.
    provider: String,
    /// Persisted credential record id.
    credential_record_id: String,
    /// Persisted credential version.
    credential_version: i64,
    /// Persisted slot grant id.
    slot_grant_id: String,
    /// Persisted submit body.
    submit_body: Vec<u8>,
}

/// Inputs shared by `record_initial_sponsorship`.
pub struct InitialSponsorship<'a> {
    /// Consent and funding choices derived from the source boundary.
    pub initial: &'a InitialConsent,
    /// Credential availability for each provider at the consent boundary.
    pub available: &'a BTreeMap<Provider, Option<bool>>,
    /// Pinned target configuration identity for this funding decision.
    pub config_digest: &'a str,
    /// Target funding mode used when the source has no explicit choice.
    pub default_funding: FundingMode,
    /// Funding mode selected explicitly by the command, if any.
    pub source_funding: Option<FundingMode>,
}

/// One exact Exchange sponsorship write claimed by a fenced worker.
pub struct SponsorshipWrite {
    /// Idempotency identity in the stored request body.
    pub transition: String,
    /// Exact body persisted before its first send.
    pub body: Vec<u8>,
    /// Contribution or standalone task owning this provider slot.
    pub task_id: Uuid,
    /// Fences a late result from an older worker.
    pub claim_token: Uuid,
}

/// One issue-lock-consistent set of source facts and its local revision.
pub struct ConsentSources {
    /// Every receipt or accepted fact that can invalidate a boundary read.
    pub revision: i64,
    /// Effective commands and historical withdrawals in source order.
    pub facts: Vec<ConsentFact>,
}

/// One provider's frozen funding choice and latest durable Exchange slot.
pub struct FundingSlot {
    /// Funding mode chosen at the task's first boundary.
    pub mode: FundingMode,
    /// Latest slot request body, when this provider has a transition.
    pub slot: Option<Value>,
    /// Whether Exchange acknowledged that exact slot.
    pub acknowledged: bool,
    /// Whether this provider has no unresolved consent decision at the boundary.
    pub consent_current: bool,
}

/// Retain the normalized facts from one accepted authenticated source.
pub(crate) async fn record_consent(
    uow: &mut UnitOfWork<'_>,
    request: &Request,
) -> anyhow::Result<()> {
    for fact in consent_facts(request) {
        let inserted = sqlx::query(
            "INSERT INTO tasks.sponsorship_facts \
             (request_id,repository_id,issue_number,provider,source_at,source_id,source,source_index,fact) \
             VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) \
             ON CONFLICT (request_id,provider) DO UPDATE SET fact=EXCLUDED.fact \
             WHERE tasks.sponsorship_facts.fact=EXCLUDED.fact",
        )
        .bind(request.id)
        .bind(request.issue.repository_id)
        .bind(request.issue.number)
        .bind(fact.provider.as_str())
        .bind(fact.order.at)
        .bind(fact.order.source_id)
        .bind(&fact.order.source)
        .bind(fact.order.index)
        .bind(Json(&fact))
        .execute(uow.connection())
        .await?;
        anyhow::ensure!(
            inserted.rows_affected() == 1,
            "consent fact changed on replay"
        );
        append_read_model_record(
            uow,
            "consent",
            &format!("{}:{}", request.id, fact.provider.as_str()),
            1,
            json!({"request_id":request.id,"repository_id":request.issue.repository_id,
                "issue_number":request.issue.number,"provider":fact.provider.as_str(),
                "fact":fact,"effective":true,"source_at":fact.order.at,
                "source":fact.order.source}),
        )
        .await?;
    }
    sqlx::query(
        "UPDATE tasks.issue_controls SET consent_revision=consent_revision+1 \
         WHERE repository_id=$1 AND issue_number=$2",
    )
    .bind(request.issue.repository_id)
    .bind(request.issue.number)
    .execute(uow.connection())
    .await?;
    Ok(())
}

/// Remove a changed claim and retain its owner's withdrawal evidence.
pub(crate) async fn reconcile_comment_revision(
    uow: &mut UnitOfWork<'_>,
    revision: &CommentRevision,
    latest: Option<&str>,
) -> anyhow::Result<()> {
    let enrolled: Option<Uuid> = sqlx::query_scalar(
        "SELECT task_id FROM tasks.task_state WHERE repository_id=$1 AND issue_number=$2",
    )
    .bind(revision.issue.repository_id)
    .bind(revision.issue.number)
    .fetch_optional(uow.connection())
    .await?;
    let obsolete: Vec<(uuid::Uuid, Json<ConsentFact>)> = sqlx::query_as(
        "SELECT f.request_id,f.fact FROM tasks.sponsorship_facts f \
         JOIN tasks.requests r ON r.id=f.request_id \
         WHERE r.comment_id=$1 AND f.effective \
         AND ($2::text IS NULL OR f.source<>$2) FOR UPDATE OF f",
    )
    .bind(revision.comment_id)
    .bind(latest)
    .fetch_all(uow.connection())
    .await?;
    for (request_id, Json(previous)) in obsolete {
        sqlx::query(
            "UPDATE tasks.sponsorship_facts SET effective=false \
             WHERE request_id=$1 AND provider=$2",
        )
        .bind(request_id)
        .bind(previous.provider.as_str())
        .execute(uow.connection())
        .await?;
        append_read_model_record(
            uow,
            "consent",
            &format!("{}:{}", request_id, previous.provider.as_str()),
            2,
            json!({"request_id":request_id,"repository_id":revision.issue.repository_id,
                "issue_number":revision.issue.number,"provider":previous.provider.as_str(),
                "fact":previous,"effective":false,"source_at":previous.order.at,
                "source":previous.order.source}),
        )
        .await?;
        if matches!(
            previous.action,
            ConsentAction::Sponsor
                | ConsentAction::Funding(concurro_tasks_core::FundingMode::Contributed)
        ) {
            let mut order = revision.order.clone();
            order.index = -1;
            let removal = ConsentFact {
                provider: previous.provider,
                actor: previous.actor,
                order,
                grant_id: hex::encode(Sha256::digest(
                    format!("{}:{}", previous.grant_id, revision.order.source).as_bytes(),
                )),
                action: ConsentAction::Withdraw {
                    removed_source: Some(previous.order.source_id),
                },
            };
            sqlx::query(
                "INSERT INTO tasks.sponsorship_removals \
                 (comment_id,source,prior_request_id,repository_id,issue_number,source_at,source_id,provider,fact) \
                 VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT DO NOTHING",
            )
            .bind(revision.comment_id)
            .bind(&revision.order.source)
            .bind(request_id)
            .bind(revision.issue.repository_id)
            .bind(revision.issue.number)
            .bind(revision.order.at)
            .bind(revision.order.source_id)
            .bind(removal.provider.as_str())
            .bind(Json(&removal))
            .execute(uow.connection())
            .await?;
            append_read_model_record(
                uow,
                "consent_removal",
                &format!(
                    "{}:{}:{}:{}",
                    revision.comment_id,
                    revision.order.source,
                    request_id,
                    removal.provider.as_str()
                ),
                1,
                json!({"repository_id":revision.issue.repository_id,
                    "issue_number":revision.issue.number,"provider":removal.provider.as_str(),
                    "fact":removal,"source_at":revision.order.at,
                    "source":revision.order.source}),
            )
            .await?;
            if let Some(task) = enrolled {
                record_withdraw_transition(uow, task, &removal).await?;
            }
        }
    }
    Ok(())
}

/// Capture consent and its revision under the issue's serial decision lock.
pub async fn consent_sources(
    uow: &mut UnitOfWork<'_>,
    issue: IssueKey,
) -> Result<ConsentSources, sqlx::Error> {
    let revision: Option<i64> = sqlx::query_scalar(
        "SELECT consent_revision FROM tasks.issue_controls \
         WHERE repository_id=$1 AND issue_number=$2 FOR UPDATE",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_optional(uow.connection())
    .await?;
    let rows: Vec<(Json<ConsentFact>, bool)> = sqlx::query_as(
        "SELECT fact,effective FROM tasks.sponsorship_facts \
         WHERE repository_id=$1 AND issue_number=$2 \
         UNION ALL SELECT fact,true FROM tasks.sponsorship_removals \
         WHERE repository_id=$1 AND issue_number=$2",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_all(uow.connection())
    .await?;
    let facts = rows
        .into_iter()
        .filter_map(|(Json(mut fact), effective)| {
            if !effective {
                fact.action = match fact.action {
                    ConsentAction::Sponsor
                    | ConsentAction::Funding(concurro_tasks_core::FundingMode::Contributed) => {
                        ConsentAction::Sponsor
                    }
                    ConsentAction::Withdraw { removed_source } => {
                        ConsentAction::Withdraw { removed_source }
                    }
                    ConsentAction::Funding(concurro_tasks_core::FundingMode::Org) => return None,
                    ConsentAction::FundingRemoved { .. } => return None,
                };
            }
            Some(fact)
        })
        .collect::<Vec<_>>();
    Ok(ConsentSources {
        revision: revision.unwrap_or_default(),
        facts,
    })
}

/// Confirm current GitHub commands and known removals at one source boundary.
pub async fn confirm_current_sponsorship_sources(
    uow: &mut UnitOfWork<'_>,
    issue: IssueKey,
    boundary: &SourceOrder,
    current: &[(SourceOrder, Actor)],
) -> Result<bool, sqlx::Error> {
    let revisions: Vec<(i64, String, DateTime<Utc>)> = sqlx::query_as(
        "SELECT comment_id,source,source_at FROM tasks.comment_revisions \
         WHERE repository_id=$1 AND issue_number=$2",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_all(uow.connection())
    .await?;
    let requests: Vec<(String, String, DateTime<Utc>, i64, i32)> = sqlx::query_as(
        "SELECT source,status,source_at,source_id,source_index FROM tasks.requests \
         WHERE repository_id=$1 AND issue_number=$2 AND comment_id IS NOT NULL",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_all(uow.connection())
    .await?;
    if requests
        .iter()
        .any(|(source, status, at, source_id, index)| {
            status == "pending"
                && SourceOrder {
                    at: *at,
                    source_id: *source_id,
                    source: source.clone(),
                    index: *index,
                } <= *boundary
        })
    {
        return Ok(false);
    }
    if current.iter().any(|(order, _)| {
        !revisions
            .iter()
            .any(|(id, source, _)| *id == order.source_id && source == &order.source)
    }) {
        return Ok(false);
    }
    let facts: Vec<(Json<ConsentFact>,)> = sqlx::query_as(
        "SELECT fact FROM tasks.sponsorship_facts \
         WHERE repository_id=$1 AND issue_number=$2 AND source_at<=$3",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .bind(boundary.at)
    .fetch_all(uow.connection())
    .await?;
    for (Json(fact),) in facts {
        if &fact.order > boundary {
            continue;
        }
        if let Some((_, actor)) = current.iter().find(|(order, _)| order == &fact.order) {
            if actor.id != fact.actor.id {
                return Ok(false);
            }
        } else if !revisions.iter().any(|(id, source, at)| {
            *id == fact.order.source_id && source != &fact.order.source && *at >= fact.order.at
        }) {
            return Ok(false);
        }
    }
    Ok(true)
}

/// Read recorded issue consent without requiring an enrolled task.
pub async fn consent_projection(
    database: &Database,
    issue: IssueKey,
) -> Result<ConsentProjection, sqlx::Error> {
    let mut read = database.begin().await?;
    let sources = consent_sources(&mut read, issue).await?;
    read.commit().await?;
    Ok(project_consent(&sources.facts))
}

/// Freeze the first enrollment boundary and its one-time Vault availability answer.
pub async fn record_initial_sponsorship(
    uow: &mut UnitOfWork<'_>,
    task: &TaskState,
    request_id: Uuid,
    boundary: &SourceOrder,
    inputs: InitialSponsorship<'_>,
    consent_revision: i64,
    unresolved: &[Provider],
) -> anyhow::Result<bool> {
    let InitialSponsorship {
        initial,
        available,
        config_digest,
        default_funding,
        source_funding,
    } = inputs;
    let mode = |mode| match mode {
        FundingMode::Org => "org",
        FundingMode::Contributed => "contributed",
    };
    let inserted = sqlx::query(
        "INSERT INTO tasks.sponsorship_initializations \
         (task_id,request_id,consent_revision,unresolved,boundary,initial,availability,config_digest,default_funding,source_funding) \
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT (task_id) DO NOTHING",
    )
    .bind(task.id.0)
    .bind(request_id)
    .bind(consent_revision)
    .bind(Json(unresolved))
    .bind(Json(boundary))
    .bind(Json(initial))
    .bind(Json(available))
    .bind(config_digest)
    .bind(mode(default_funding))
    .bind(source_funding.map(mode).unwrap_or("unknown"))
    .execute(uow.connection())
    .await?;
    Ok(inserted.rows_affected() == 1)
}

/// Read PR funding facts and a revision under the standalone source lock.
pub async fn standalone_consent_sources(
    uow: &mut UnitOfWork<'_>,
    repository_id: i64,
    pr_number: i64,
    boundary: &SourceOrder,
) -> anyhow::Result<(i64, Vec<ConsentFact>)> {
    sqlx::query(
        "INSERT INTO tasks.standalone_review_controls (upstream,pr_number) \
         SELECT upstream,pr_number FROM tasks.standalone_review_tasks \
         WHERE repository_id=$1 AND pr_number=$2 ON CONFLICT DO NOTHING",
    )
    .bind(repository_id)
    .bind(pr_number)
    .execute(uow.connection())
    .await?;
    let _: Option<i64> = sqlx::query_scalar(
        "SELECT pr_number FROM tasks.standalone_review_controls \
         WHERE upstream=(SELECT upstream FROM tasks.standalone_review_tasks \
             WHERE repository_id=$1 AND pr_number=$2) AND pr_number=$2 FOR UPDATE",
    )
    .bind(repository_id)
    .bind(pr_number)
    .fetch_optional(uow.connection())
    .await?;
    let revision: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM tasks.standalone_review_commands \
         WHERE repository_id=$1 AND pr_number=$2 AND source_at<=$3",
    )
    .bind(repository_id)
    .bind(pr_number)
    .bind(boundary.at)
    .fetch_one(uow.connection())
    .await?;
    let pending: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM tasks.standalone_review_commands \
         WHERE repository_id=$1 AND pr_number=$2 AND status='pending' AND source_at<=$3)",
    )
    .bind(repository_id)
    .bind(pr_number)
    .bind(boundary.at)
    .fetch_one(uow.connection())
    .await?;
    anyhow::ensure!(
        !pending,
        "PR funding source decision is pending at boundary"
    );
    let rows: Vec<Json<ConsentFact>> = sqlx::query_scalar(
        "SELECT fact FROM tasks.standalone_review_funding_facts \
         WHERE repository_id=$1 AND pr_number=$2 \
         UNION ALL SELECT r.fact FROM tasks.standalone_review_funding_removals r \
         JOIN tasks.standalone_review_commands c ON c.id=r.command_id \
         WHERE c.repository_id=$1 AND c.pr_number=$2",
    )
    .bind(repository_id)
    .bind(pr_number)
    .fetch_all(uow.connection())
    .await?;
    let facts = rows.into_iter().map(|Json(fact)| fact).collect();
    Ok((revision, facts))
}

/// Freeze the first standalone request's consent and Vault answer exactly once.
pub async fn record_standalone_initial_sponsorship(
    uow: &mut UnitOfWork<'_>,
    task_id: Uuid,
    request_id: Uuid,
    boundary: &SourceOrder,
    inputs: InitialSponsorship<'_>,
) -> anyhow::Result<bool> {
    let InitialSponsorship {
        initial,
        available,
        config_digest: digest,
        default_funding,
        source_funding,
    } = inputs;
    let mode = |funding| match funding {
        FundingMode::Org => "org",
        FundingMode::Contributed => "contributed",
    };
    let changed=sqlx::query(
        "INSERT INTO tasks.standalone_review_sponsorship_initializations \
         (task_id,request_id,boundary,initial,availability,config_digest,default_funding,source_funding) \
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT (task_id) DO NOTHING",
    ).bind(task_id).bind(request_id).bind(Json(boundary)).bind(Json(initial))
        .bind(Json(available)).bind(digest).bind(mode(default_funding))
        .bind(source_funding.map(mode).unwrap_or("unknown"))
        .execute(uow.connection()).await?;
    Ok(changed.rows_affected() == 1)
}

/// Check whether the first standalone funding boundary was already frozen.
pub async fn standalone_sponsorship_initialized(
    database: &Database,
    task_id: Uuid,
) -> Result<bool, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM tasks.standalone_review_sponsorship_initializations \
         WHERE task_id=$1)",
    )
    .bind(task_id)
    .fetch_one(database.pool())
    .await
}

/// Read the provider slot that a new attempt may request under frozen consent.
pub async fn funding_slot(
    database: &Database,
    task: Uuid,
    provider: Provider,
) -> anyhow::Result<Option<FundingSlot>> {
    let mut read = database.begin().await?;
    let slot = funding_slot_locked(&mut read, task, provider).await?;
    read.commit().await?;
    Ok(slot)
}

/// Use the caller's issue lock when a slot decision is part of admission.
async fn funding_slot_locked(
    uow: &mut UnitOfWork<'_>,
    task: Uuid,
    provider: Provider,
) -> anyhow::Result<Option<FundingSlot>> {
    let row: Option<(Json<InitialConsent>, String, bool)> = sqlx::query_as(
        "SELECT i.initial,i.default_funding,i.unresolved ? $2 \
         FROM tasks.sponsorship_initializations i \
         JOIN tasks.task_state s ON s.task_id=i.task_id \
         JOIN tasks.issue_controls c ON c.repository_id=s.repository_id \
         AND c.issue_number=s.issue_number WHERE i.task_id=$1",
    )
    .bind(task)
    .bind(provider.as_str())
    .fetch_optional(uow.connection())
    .await?;
    let row = if row.is_none() {
        sqlx::query_as(
            "SELECT initial,default_funding,false \
             FROM tasks.standalone_review_sponsorship_initializations WHERE task_id=$1",
        )
        .bind(task)
        .fetch_optional(uow.connection())
        .await?
    } else {
        row
    };
    let Some((Json(initial), default, unresolved)) = row else {
        return Ok(None);
    };
    let default = match default.as_str() {
        "org" => FundingMode::Org,
        "contributed" => FundingMode::Contributed,
        _ => unreachable!("funding mode constrained by schema"),
    };
    let initial_mode = initial
        .projection
        .providers
        .get(&provider)
        .expect("both provider slots are frozen")
        .funding
        .unwrap_or(default);
    let latest: Option<(Vec<u8>, String)> = sqlx::query_as(
        "SELECT body,status FROM tasks.sponsorship_transitions \
         WHERE COALESCE(task_id,standalone_task_id)=$1 AND provider=$2 \
         ORDER BY sequence DESC LIMIT 1",
    )
    .bind(task)
    .bind(provider.as_str())
    .fetch_optional(uow.connection())
    .await?;
    let (slot, acknowledged) = match latest {
        Some((body, status)) => {
            let value: Value = serde_json::from_slice(&body)?;
            (Some(value["slot"].clone()), status == "acknowledged")
        }
        None => (None, false),
    };
    let mode = match slot.as_ref().and_then(|slot| slot["funding"].as_str()) {
        Some("org") => FundingMode::Org,
        Some("contributed") => FundingMode::Contributed,
        None => initial_mode,
        Some(_) => anyhow::bail!("provider slot funding mode is invalid"),
    };
    Ok(Some(FundingSlot {
        mode,
        slot,
        acknowledged,
        consent_current: !unresolved,
    }))
}

/// Recheck the exact personal payer under the issue lock before charging a call.
pub(crate) async fn validate_funding_admission(
    uow: &mut UnitOfWork<'_>,
    task: &TaskState,
    spec: &AttemptSpec,
) -> anyhow::Result<()> {
    let initialized: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM tasks.sponsorship_initializations WHERE task_id=$1)",
    )
    .bind(task.id.0)
    .fetch_one(uow.connection())
    .await?;
    if !initialized {
        if let Ok(body) = serde_json::from_slice::<Value>(&spec.submit_body) {
            anyhow::ensure!(
                body["binding"]["funding"] != "contributed",
                "personal funding has no frozen boundary"
            );
        }
        return Ok(());
    }
    let submission: TurnSubmission = serde_json::from_slice(&spec.submit_body)?;
    let request = &submission.payload["request"];
    let provider = request["provider"]
        .as_str()
        .and_then(Provider::from_harness)
        .context("attempt provider is invalid")?;
    let funding = funding_slot_locked(uow, task.id.0, provider)
        .await?
        .expect("funding boundary remains present under the issue lock");
    if funding.mode == FundingMode::Org {
        anyhow::ensure!(
            submission.binding.get("funding").is_none(),
            "org funding cannot use a personal payer"
        );
        return Ok(());
    }
    anyhow::ensure!(funding.consent_current, "sponsorship changed before charge");
    anyhow::ensure!(funding.acknowledged, "sponsorship transition pending");
    let slot = funding
        .slot
        .ok_or_else(|| anyhow::anyhow!("provider slot missing"))?;
    let admission = &request["admission"];
    let binding = &submission.binding;
    anyhow::ensure!(
        slot["status"] == "active"
            && admission["grant"] == slot
            && admission["call"] == task.used_model_calls + 1
            && admission["owner_generation"] == binding["generation"]
            && request["attempt"] == spec.id.to_string()
            && binding["version"] == 2
            && binding["funding"] == "contributed"
            && binding["sponsorship_provider"] == provider.as_str()
            && binding["sponsor"] == slot["sponsor_id"]
            && binding["epoch"] == slot["epoch"]
            && binding["grant_id"] == slot["grant_id"]
            && admission["sponsor"] == binding["sponsor"]
            && admission["credential"]["record_id"] == binding["credential"]
            && admission["credential"]["version"] == binding["credential_version"],
        "personal admission differs from acknowledged sponsorship"
    );
    Ok(())
}

/// Recheck a standalone review's exact acknowledged payer under its task lock.
pub(crate) async fn validate_standalone_funding_admission(
    uow: &mut UnitOfWork<'_>,
    task_id: Uuid,
    call: i64,
    spec: &AttemptSpec,
) -> anyhow::Result<()> {
    let submission: TurnSubmission = serde_json::from_slice(&spec.submit_body)?;
    let request = &submission.payload["request"];
    let provider = request["provider"]
        .as_str()
        .and_then(Provider::from_harness)
        .context("standalone reviewer provider is invalid")?;
    let funding = funding_slot_locked(uow, task_id, provider)
        .await?
        .ok_or_else(|| anyhow::anyhow!("standalone funding boundary is missing"))?;
    if funding.mode == FundingMode::Org {
        ensure!(
            submission.binding.get("funding").is_none(),
            "org standalone review cannot use a personal payer"
        );
        return Ok(());
    }
    ensure!(
        funding.consent_current && funding.acknowledged,
        "standalone sponsor consent or Exchange slot is pending"
    );
    let slot = funding
        .slot
        .context("standalone sponsorship slot is missing")?;
    let admission = &request["admission"];
    let binding = &submission.binding;
    ensure!(
        slot["status"] == "active"
            && admission["grant"] == slot
            && admission["call"] == call
            && admission["owner_generation"] == binding["generation"]
            && request["attempt"] == spec.id.to_string()
            && binding["version"] == 2
            && binding["funding"] == "contributed"
            && binding["sponsorship_provider"] == provider.as_str()
            && binding["sponsor"] == slot["sponsor_id"]
            && binding["epoch"] == slot["epoch"]
            && binding["grant_id"] == slot["grant_id"]
            && admission["sponsor"] == binding["sponsor"]
            && admission["credential"]["record_id"] == binding["credential"]
            && admission["credential"]["version"] == binding["credential_version"],
        "standalone personal admission differs from acknowledged sponsorship"
    );
    Ok(())
}

/// Record the selected attempt grant and charged reservation with its exact payer.
pub(crate) async fn record_funding_grant(
    uow: &mut UnitOfWork<'_>,
    spec: &AttemptSpec,
) -> anyhow::Result<()> {
    crate::lock_credential_versions(uow).await?;
    let submission = serde_json::from_slice::<TurnSubmission>(&spec.submit_body).ok();
    if let Some(submission) = &submission {
        ensure!(
            submission.turn == spec.id,
            "funding grant attempt differs from turn"
        );
    }
    let personal = submission
        .as_ref()
        .is_some_and(|submission| submission.binding["funding"] == "contributed");
    let admission = submission
        .as_ref()
        .map(|submission| &submission.payload["request"]["admission"])
        .unwrap_or(&Value::Null);
    let (payer, provider, epoch, slot_grant, credential, version, route) = if personal {
        let payer = admission["sponsor"]
            .as_str()
            .context("funding grant payer is missing")?
            .parse::<i64>()?;
        let provider = admission["sponsorship_provider"]
            .as_str()
            .context("funding grant provider is missing")?;
        let epoch = admission["epoch"]
            .as_i64()
            .context("funding grant epoch is missing")?;
        let slot_grant = admission["grant_id"]
            .as_str()
            .context("funding grant slot identity is missing")?;
        let credential = admission["credential"]["record_id"]
            .as_str()
            .context("funding grant credential is missing")?;
        let version = admission["credential"]["version"]
            .as_i64()
            .context("funding grant credential version is missing")?;
        let route = admission["route"]
            .as_str()
            .context("funding grant route is missing")?;
        (
            Some(payer),
            Some(provider),
            Some(epoch),
            Some(slot_grant),
            Some(credential),
            Some(version),
            Some(route),
        )
    } else {
        (None, None, None, None, None, None, None)
    };
    let inserted = sqlx::query(
        "INSERT INTO tasks.funding_grants \
         (attempt_id,task_id,purpose,payer_id,provider,slot_epoch,slot_grant_id, \
          credential_record_id,credential_version,route,models,limits) \
         SELECT $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12 \
         WHERE NOT EXISTS (SELECT 1 FROM tasks.revoked_credential_versions \
             WHERE payer_id=$4 AND record_id=$8 AND version=$9)",
    )
    .bind(spec.id)
    .bind(spec.task.0)
    .bind(if personal {
        "task_contributed"
    } else {
        "task_org"
    })
    .bind(payer)
    .bind(provider)
    .bind(epoch)
    .bind(slot_grant)
    .bind(credential)
    .bind(version)
    .bind(route)
    .bind(
        submission
            .as_ref()
            .map(|submission| Json(json!(submission.models))),
    )
    .bind(submission.as_ref().map(|submission| {
        Json(json!({"calls":1,"tokens":submission.budget,"lease_seconds":submission.lease}))
    }))
    .execute(uow.connection())
    .await?
    .rows_affected();
    ensure!(inserted == 1, "selected credential version was revoked");
    Ok(())
}

/// Count charged calls after unused contributed reservations were released.
pub(crate) async fn billable_model_calls(
    uow: &mut UnitOfWork<'_>,
    task: &TaskState,
) -> anyhow::Result<u64> {
    let released: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM tasks.funding_grants \
         WHERE task_id=$1 AND reservation_status='released'",
    )
    .bind(task.id.0)
    .fetch_one(uow.connection())
    .await?;
    task.used_model_calls
        .checked_sub(u64::try_from(released)?)
        .context("released calls exceed admitted calls")
}

/// Read this task's active exact-version exclusions for one sponsor and model.
pub async fn excluded_credential_versions(
    database: &Database,
    task: Uuid,
    payer: i64,
    provider: Provider,
    model: &str,
    now: DateTime<Utc>,
) -> Result<Vec<(String, i64)>, sqlx::Error> {
    sqlx::query_as(
        "SELECT credential_record_id,credential_version FROM tasks.funding_exclusions \
             WHERE COALESCE(task_id,standalone_task_id)=$1 AND payer_id=$2 AND provider=$3 \
         AND cleared_at IS NULL AND (model IS NULL OR model=$4) \
         AND (retry_at IS NULL OR retry_at>$5)",
    )
    .bind(task)
    .bind(payer)
    .bind(provider.as_str())
    .bind(model)
    .bind(now)
    .fetch_all(database.pool())
    .await
}

/// Retain an exact trusted provider failure for future credential selection.
pub(crate) async fn record_credential_failure(
    uow: &mut UnitOfWork<'_>,
    observation: &TurnObservation,
) -> anyhow::Result<bool> {
    let Some(failure) = observation.provider_failure.as_ref() else {
        return Ok(false);
    };
    let Some(category) = failure["category"].as_str().filter(|category| {
        matches!(
            *category,
            "auth" | "model_denied" | "quota_exhausted" | "rate_limited" | "unavailable"
        )
    }) else {
        return Ok(false);
    };
    let selected: Option<FailedCredentialGrantRow> = sqlx::query_as(
        "SELECT g.task_id, NULL::uuid AS standalone_task_id, g.payer_id, g.provider, g.credential_record_id, g.credential_version, g.slot_grant_id, a.submit_body \
         FROM tasks.funding_grants g \
         JOIN tasks.attempts a ON a.id=g.attempt_id \
         WHERE g.attempt_id=$1 AND g.purpose='task_contributed' \
         UNION ALL SELECT NULL::uuid,g.task_id,g.payer_id,g.provider, \
         g.credential_record_id,g.credential_version,g.slot_grant_id,a.submit_body \
         FROM tasks.standalone_review_funding_grants g \
         JOIN tasks.attempts a ON a.id=g.attempt_id \
         WHERE g.attempt_id=$1 AND g.purpose='contributed'",
    )
    .bind(observation.turn)
    .fetch_optional(uow.connection())
    .await?;
    let Some(FailedCredentialGrantRow {
        task_id: task,
        standalone_task_id: standalone_task,
        payer_id: payer,
        provider,
        credential_record_id: record,
        credential_version: version,
        slot_grant_id: slot_grant,
        submit_body: body,
    }) = selected
    else {
        return Ok(false);
    };
    let rust = serde_json::from_slice::<TurnSubmissionV2>(&body).ok();
    let legacy = if rust.is_none() {
        Some(serde_json::from_slice::<TurnSubmission>(&body)?)
    } else {
        None
    };
    let request = if let Some(submission) = &rust {
        &submission.payload.request
    } else {
        &legacy.as_ref().expect("legacy submission").payload["request"]
    };
    let Some(model) = request["model"].as_str() else {
        return Ok(false);
    };
    let binding = &failure["binding"];
    let trusted = if rust.is_some() {
        failure["grant_id"] == observation.turn.to_string()
            && failure["record_id"] == record
            && failure["version"] == version
            && request["admission"]["grant_id"] == slot_grant
    } else {
        failure["turn"] == observation.turn.to_string()
            && binding["sponsor"].as_str() == Some(payer.to_string().as_str())
            && binding["credential"] == record
            && binding["credential_version"] == version
            && binding["grant_id"] == slot_grant
    };
    if !trusted || failure["model"].as_str() != Some(model) {
        return Ok(false);
    }
    let model = if category == "model_denied" {
        Some(model.to_owned())
    } else {
        None
    };
    let hint = failure["retry_after"]
        .as_f64()
        .filter(|seconds| seconds.is_finite() && (0.0..=999_999_999.0).contains(seconds))
        .map(|seconds| seconds.ceil() as i64);
    let retry_at = if category == "quota_exhausted" {
        hint.map(|seconds| Utc::now() + Duration::seconds(seconds))
    } else if matches!(category, "rate_limited" | "unavailable") {
        let failures: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM tasks.funding_exclusions \
             WHERE COALESCE(task_id,standalone_task_id)=COALESCE($1,$5) \
             AND payer_id=$2 AND credential_record_id=$3 \
             AND credential_version=$4 AND cleared_at IS NULL \
             AND category IN ('rate_limited','unavailable')",
        )
        .bind(task)
        .bind(payer)
        .bind(&record)
        .bind(version)
        .bind(standalone_task)
        .fetch_one(uow.connection())
        .await?;
        let local = (60_i64 << failures.min(6)).min(3600);
        Some(Utc::now() + Duration::seconds(hint.unwrap_or(local)))
    } else {
        None
    };
    let retry_source = if retry_at.is_none() {
        None
    } else if hint.is_some() {
        Some("provider")
    } else {
        Some("local")
    };
    let inserted = sqlx::query(
        "INSERT INTO tasks.funding_exclusions \
         (attempt_id,task_id,standalone_task_id,payer_id,provider,credential_record_id, \
          credential_version,model,category,retry_at,retry_source) \
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) \
         ON CONFLICT (attempt_id) DO NOTHING",
    )
    .bind(observation.turn)
    .bind(task)
    .bind(standalone_task)
    .bind(payer)
    .bind(&provider)
    .bind(&record)
    .bind(version)
    .bind(model)
    .bind(category)
    .bind(retry_at)
    .bind(retry_source)
    .execute(uow.connection())
    .await?;
    Ok(inserted.rows_affected() == 1)
}

/// End temporary backoff streaks after a successful call on the same exact credential.
pub(crate) async fn record_credential_success(
    uow: &mut UnitOfWork<'_>,
    turn: Uuid,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE tasks.funding_exclusions e SET cleared_at=now() \
         FROM tasks.funding_grants g WHERE g.attempt_id=$1 \
         AND g.purpose='task_contributed' AND e.task_id=g.task_id \
         AND e.payer_id=g.payer_id AND e.provider=g.provider \
         AND e.credential_record_id=g.credential_record_id \
         AND e.credential_version=g.credential_version \
         AND e.category IN ('rate_limited','unavailable') AND e.cleared_at IS NULL",
    )
    .bind(turn)
    .execute(uow.connection())
    .await?;
    sqlx::query(
        "UPDATE tasks.funding_exclusions e SET cleared_at=now() \
         FROM tasks.standalone_review_funding_grants g WHERE g.attempt_id=$1 \
         AND g.purpose='contributed' AND e.standalone_task_id=g.task_id \
         AND e.payer_id=g.payer_id AND e.provider=g.provider \
         AND e.credential_record_id=g.credential_record_id \
         AND e.credential_version=g.credential_version \
         AND e.category IN ('rate_limited','unavailable') AND e.cleared_at IS NULL",
    )
    .bind(turn)
    .execute(uow.connection())
    .await?;
    Ok(())
}

/// Check whether one terminal turn has trusted fallback evidence.
pub(crate) async fn has_credential_exclusion(
    uow: &mut UnitOfWork<'_>,
    turn: Uuid,
) -> Result<bool, sqlx::Error> {
    sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM tasks.funding_exclusions WHERE attempt_id=$1)")
        .bind(turn)
        .fetch_one(uow.connection())
        .await
}

/// Record a sponsor's cutoff alongside the accepted withdrawal source.
pub async fn record_withdraw_transition(
    uow: &mut UnitOfWork<'_>,
    task: Uuid,
    fact: &ConsentFact,
) -> anyhow::Result<Option<String>> {
    let previous: Option<(Vec<u8>,)> = sqlx::query_as(
        "SELECT body FROM tasks.sponsorship_transitions \
         WHERE COALESCE(task_id,standalone_task_id)=$1 AND provider=$2 \
         ORDER BY sequence DESC LIMIT 1",
    )
    .bind(task)
    .bind(fact.provider.as_str())
    .fetch_optional(uow.connection())
    .await?;
    let Some((body,)) = previous else {
        return Ok(None);
    };
    let prior: Value = serde_json::from_slice(&body)?;
    let slot = &prior["slot"];
    if slot["status"] != "active"
        || slot["sponsor_id"].as_str() != Some(fact.actor.id.to_string().as_str())
        || matches!(
            &fact.action,
            ConsentAction::Withdraw {
                removed_source: Some(source)
            } if slot["source_id"] != *source
        )
    {
        return Ok(None);
    }
    let epoch = slot["epoch"]
        .as_i64()
        .ok_or_else(|| anyhow::anyhow!("sponsorship epoch missing"))?;
    let standalone_round: Option<i32> =
        sqlx::query_scalar("SELECT round FROM tasks.standalone_review_tasks WHERE id=$1")
            .bind(task)
            .fetch_optional(uow.connection())
            .await?;
    let generation = prior["generation"]
        .as_i64()
        .context("sponsorship generation missing")?
        .max(standalone_round.map(i64::from).unwrap_or_default());
    let mut transition = json!({
        "repository": prior["repository"], "issue": prior["issue"],
        "provider": fact.provider.as_str(), "generation": generation,
        "expected_epoch": epoch, "reason": "withdraw",
        "actor": fact.actor.id.to_string(), "cutoff": epoch,
        "slot": {
            "status": "withdrawn", "sponsor_id": null,
            "epoch": epoch + 1, "grant_id": fact.grant_id,
            "funding": slot["funding"],
            "source_id": fact.order.source_id,
            "source_order": [fact.order.at.to_rfc3339(), fact.order.source_id, fact.order.index],
        }
    });
    if !slot["funding_order"].is_null() {
        transition["slot"]["funding_order"] = slot["funding_order"].clone();
    }
    transition["transition"] = Value::String(python_digest(&transition)?);
    let transition_id = record_sponsorship_transition(uow, task, &transition).await?;
    let contribution: Vec<Uuid> = sqlx::query_scalar(
        "UPDATE tasks.funding_grants SET status='revoked',revoked_at=now() \
         WHERE task_id=$1 AND provider=$2 AND payer_id=$3 AND slot_epoch<=$4 \
         AND purpose='task_contributed' AND status='issued' RETURNING attempt_id",
    )
    .bind(task)
    .bind(fact.provider.as_str())
    .bind(fact.actor.id)
    .bind(epoch)
    .fetch_all(uow.connection())
    .await?;
    let standalone: Vec<Uuid> = sqlx::query_scalar(
        "UPDATE tasks.standalone_review_funding_grants SET status='revoked' \
         WHERE task_id=$1 AND provider=$2 AND payer_id=$3 AND slot_epoch<=$4 \
         AND purpose='contributed' AND status='issued' RETURNING attempt_id",
    )
    .bind(task)
    .bind(fact.provider.as_str())
    .bind(fact.actor.id)
    .bind(epoch)
    .fetch_all(uow.connection())
    .await?;
    for turn in contribution.into_iter().chain(standalone) {
        crate::credential_revocation::enqueue_retirement(uow, turn).await?;
    }
    Ok(Some(transition_id))
}

/// Include earlier unacknowledged cutoffs for the same actor and provider.
pub(crate) async fn pending_withdraw_transitions(
    uow: &mut UnitOfWork<'_>,
    task: Uuid,
    fact: &ConsentFact,
) -> anyhow::Result<Vec<String>> {
    let rows: Vec<(String, Vec<u8>)> = sqlx::query_as(
        "SELECT transition,body FROM tasks.sponsorship_transitions \
         WHERE COALESCE(task_id,standalone_task_id)=$1 AND provider=$2 \
         AND status<>'acknowledged' ORDER BY sequence",
    )
    .bind(task)
    .bind(fact.provider.as_str())
    .fetch_all(uow.connection())
    .await?;
    let actor = fact.actor.id.to_string();
    let mut pending = Vec::new();
    for (id, body) in rows {
        let body: Value = serde_json::from_slice(&body)?;
        if body["reason"] == "withdraw" && body["actor"] == actor {
            pending.push(id);
        }
    }
    Ok(pending)
}

/// Reconcile one later standalone funding fact against the latest provider slot.
pub async fn record_standalone_funding_fact_transition(
    uow: &mut UnitOfWork<'_>,
    task: Uuid,
    fact: &ConsentFact,
) -> anyhow::Result<()> {
    let inserted = sqlx::query(
        "INSERT INTO tasks.standalone_review_applied_funding_facts \
         (task_id,provider,grant_id) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING",
    )
    .bind(task)
    .bind(fact.provider.as_str())
    .bind(&fact.grant_id)
    .execute(uow.connection())
    .await?;
    if inserted.rows_affected() == 0 {
        return Ok(());
    }
    if matches!(fact.action, ConsentAction::Withdraw { .. }) {
        record_withdraw_transition(uow, task, fact).await?;
        return Ok(());
    }
    if let ConsentAction::FundingRemoved {
        removed_source,
        removes_sponsor: true,
    } = fact.action
    {
        let mut withdrawal = fact.clone();
        withdrawal.action = ConsentAction::Withdraw {
            removed_source: Some(removed_source),
        };
        record_withdraw_transition(uow, task, &withdrawal).await?;
    }
    let previous: Option<Vec<u8>> = sqlx::query_scalar(
        "SELECT body FROM tasks.sponsorship_transitions \
         WHERE standalone_task_id=$1 AND provider=$2 ORDER BY sequence DESC LIMIT 1",
    )
    .bind(task)
    .bind(fact.provider.as_str())
    .fetch_optional(uow.connection())
    .await?;
    let previous: Value =
        serde_json::from_slice(&previous.context("standalone provider slot missing")?)?;
    let old = &previous["slot"];
    let epoch = old["epoch"]
        .as_i64()
        .context("standalone slot epoch missing")?;
    let round: i32 =
        sqlx::query_scalar("SELECT round FROM tasks.standalone_review_tasks WHERE id=$1")
            .bind(task)
            .fetch_one(uow.connection())
            .await?;
    let generation = previous["generation"]
        .as_i64()
        .context("standalone sponsorship generation missing")?
        .max(i64::from(round));
    let mut slot = old.clone();
    let reason =
        match fact.action {
            ConsentAction::Sponsor => "sponsor",
            ConsentAction::Funding(FundingMode::Contributed) => {
                slot["funding"] = json!("contributed");
                slot["funding_order"] = json!([
                    fact.order.at.to_rfc3339(),
                    fact.order.source_id,
                    fact.order.index
                ]);
                "funding"
            }
            ConsentAction::Funding(FundingMode::Org) => {
                slot["funding"] = json!("org");
                slot["funding_order"] = json!([
                    fact.order.at.to_rfc3339(),
                    fact.order.source_id,
                    fact.order.index
                ]);
                "funding"
            }
            ConsentAction::FundingRemoved { removed_source, .. } => {
                if slot["funding_order"][1] != removed_source {
                    return Ok(());
                }
                let default: String=sqlx::query_scalar(
                "SELECT default_funding FROM tasks.standalone_review_sponsorship_initializations \
                 WHERE task_id=$1",
            ).bind(task).fetch_one(uow.connection()).await?;
                slot["funding"] = json!(default);
                slot.as_object_mut()
                    .expect("provider slot object")
                    .remove("funding_order");
                "funding"
            }
            ConsentAction::Withdraw { .. } => unreachable!("withdrawal returned above"),
        };
    if matches!(
        fact.action,
        ConsentAction::Sponsor | ConsentAction::Funding(FundingMode::Contributed)
    ) {
        slot["status"] = json!("active");
        slot["sponsor_id"] = json!(fact.actor.id.to_string());
        slot["login"] = json!(fact.actor.login);
        slot["grant_id"] = json!(fact.grant_id);
        slot["provenance"] = json!("explicit_comment");
        slot["source_funding_mode"] = json!("org");
        slot["source_id"] = json!(fact.order.source_id);
        slot["source_order"] = json!([
            fact.order.at.to_rfc3339(),
            fact.order.source_id,
            fact.order.index
        ]);
    }
    slot["epoch"] = json!(epoch + 1);
    let mut request = json!({
        "repository": previous["repository"], "issue": previous["issue"],
        "provider": fact.provider.as_str(), "generation": generation,
        "expected_epoch": epoch, "slot": slot, "reason": reason,
        "actor": fact.actor.id.to_string(), "cutoff": null,
    });
    request["transition"] = Value::String(python_digest(&request)?);
    record_sponsorship_transition(uow, task, &request).await?;
    Ok(())
}

/// Store an exact transition and enqueue its idempotent Exchange write.
pub async fn record_sponsorship_transition(
    uow: &mut UnitOfWork<'_>,
    task: Uuid,
    request: &Value,
) -> anyhow::Result<String> {
    let transition = request["transition"]
        .as_str()
        .filter(|value| value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit()))
        .ok_or_else(|| anyhow::anyhow!("invalid sponsorship transition identity"))?;
    let provider = request["provider"]
        .as_str()
        .filter(|value| matches!(*value, "openai" | "anthropic" | "google"))
        .ok_or_else(|| anyhow::anyhow!("invalid sponsorship provider"))?;
    let expected = request["expected_epoch"]
        .as_i64()
        .ok_or_else(|| anyhow::anyhow!("invalid expected sponsorship epoch"))?;
    let epoch = request["slot"]["epoch"]
        .as_i64()
        .ok_or_else(|| anyhow::anyhow!("invalid sponsorship epoch"))?;
    let mut unsigned = request.clone();
    unsigned
        .as_object_mut()
        .ok_or_else(|| anyhow::anyhow!("invalid sponsorship transition"))?
        .remove("transition");
    anyhow::ensure!(
        python_digest(&unsigned)? == transition,
        "sponsorship transition identity differs from its body"
    );
    let issue_number: Option<i64> =
        sqlx::query_scalar("SELECT issue_number FROM tasks.task_state WHERE task_id=$1")
            .bind(task)
            .fetch_optional(uow.connection())
            .await?;
    let standalone_number: Option<i64> =
        sqlx::query_scalar("SELECT pr_number FROM tasks.standalone_review_tasks WHERE id=$1")
            .bind(task)
            .fetch_optional(uow.connection())
            .await?;
    anyhow::ensure!(
        issue_number.or(standalone_number) == request["issue"].as_i64(),
        "sponsorship transition names another task issue"
    );
    let body = serde_json::to_vec(request)?;
    let inserted = sqlx::query(
        "INSERT INTO tasks.sponsorship_transitions \
         (transition,task_id,standalone_task_id,provider,expected_epoch,epoch,body) \
         VALUES ($1,$2,$3,$4,$5,$6,$7) \
         ON CONFLICT (transition) DO UPDATE SET body=EXCLUDED.body \
         WHERE tasks.sponsorship_transitions.task_id IS NOT DISTINCT FROM EXCLUDED.task_id \
         AND tasks.sponsorship_transitions.standalone_task_id IS NOT DISTINCT FROM EXCLUDED.standalone_task_id \
         AND tasks.sponsorship_transitions.provider=EXCLUDED.provider \
         AND tasks.sponsorship_transitions.expected_epoch=EXCLUDED.expected_epoch \
         AND tasks.sponsorship_transitions.epoch=EXCLUDED.epoch \
         AND tasks.sponsorship_transitions.body=EXCLUDED.body",
    )
    .bind(transition)
    .bind(issue_number.map(|_|task))
    .bind(standalone_number.map(|_|task))
    .bind(provider)
    .bind(expected)
    .bind(epoch)
    .bind(&body)
    .execute(uow.connection())
    .await?;
    anyhow::ensure!(
        inserted.rows_affected() == 1,
        "sponsorship transition changed on replay"
    );
    let queue = format!("sponsorship:{task}:{provider}");
    let key = format!("sponsorship-transition:{transition}");
    let _: i64 = sqlx::query_scalar(
        "SELECT (graphile_worker.add_job( \
         identifier => 'sponsorship_transition', \
         payload => json_build_object('transition',$1), \
         queue_name => $2, max_attempts => 25, job_key => $3)).id",
    )
    .bind(transition)
    .bind(queue)
    .bind(key)
    .fetch_one(uow.connection())
    .await?;
    append_consent_state(uow, task).await?;
    Ok(transition.to_owned())
}

/// Claim a recorded or uncertain write, always replaying its exact stored body.
pub async fn claim_sponsorship_transition(
    uow: &mut UnitOfWork<'_>,
    transition: &str,
) -> Result<Option<SponsorshipWrite>, sqlx::Error> {
    let row: Option<(Vec<u8>, i64, Uuid, String)> = sqlx::query_as(
        "SELECT body,expected_epoch,COALESCE(task_id,standalone_task_id),provider \
         FROM tasks.sponsorship_transitions \
         WHERE transition=$1 AND status IN ('recorded','sending') FOR UPDATE",
    )
    .bind(transition)
    .fetch_optional(uow.connection())
    .await?;
    let Some((body, expected_epoch, task, provider)) = row else {
        return Ok(None);
    };
    if expected_epoch >= 0 {
        let predecessor: Option<String> = sqlx::query_scalar(
            "SELECT status FROM tasks.sponsorship_transitions \
             WHERE COALESCE(task_id,standalone_task_id)=$1 AND provider=$2 AND epoch=$3 \
             ORDER BY sequence DESC LIMIT 1",
        )
        .bind(task)
        .bind(&provider)
        .bind(expected_epoch)
        .fetch_optional(uow.connection())
        .await?;
        if predecessor.as_deref() != Some("acknowledged") {
            let floor: Option<(i64, i64)> = sqlx::query_as(
                "SELECT f.generation,CASE WHEN $2='openai' THEN f.openai_epoch \
                 WHEN $2='anthropic' THEN f.anthropic_epoch ELSE -1 END FROM tasks.cutover_floors f \
                 JOIN tasks.task_state t ON t.repository_id=f.repository_id \
                 AND t.issue_number=f.issue_number WHERE t.task_id=$1",
            )
            .bind(task)
            .bind(&provider)
            .fetch_optional(uow.connection())
            .await?;
            let generation = serde_json::from_slice::<Value>(&body)
                .ok()
                .and_then(|body| body["generation"].as_i64());
            if !floor.is_some_and(|(prior_generation, prior_epoch)| {
                prior_epoch == expected_epoch && generation == Some(prior_generation + 1)
            }) {
                return Err(sqlx::Error::Protocol(
                    "preceding sponsorship transition is pending".to_owned(),
                ));
            }
        }
    }
    let claim_token = Uuid::now_v7();
    sqlx::query(
        "UPDATE tasks.sponsorship_transitions SET status='sending',claim_token=$2 \
         WHERE transition=$1",
    )
    .bind(transition)
    .bind(claim_token)
    .execute(uow.connection())
    .await?;
    Ok(Some(SponsorshipWrite {
        transition: transition.to_owned(),
        body,
        task_id: task,
        claim_token,
    }))
}

/// Acknowledge only the worker that still owns the exact sent transition.
pub async fn acknowledge_sponsorship_transition(
    uow: &mut UnitOfWork<'_>,
    write: &SponsorshipWrite,
    ack: &SponsorshipAck,
) -> anyhow::Result<bool> {
    anyhow::ensure!(
        ack.transitions.len() == 1 && ack.transitions[0].transition == write.transition,
        "sponsorship acknowledgement differs from write"
    );
    let changed = sqlx::query(
        "UPDATE tasks.sponsorship_transitions \
         SET status='acknowledged',claim_token=NULL,response=$3,acknowledged_at=now() \
         WHERE transition=$1 AND claim_token=$2 AND status='sending' AND epoch=$4",
    )
    .bind(&write.transition)
    .bind(write.claim_token)
    .bind(Json(ack))
    .bind(ack.transitions[0].epoch)
    .execute(uow.connection())
    .await?;
    if changed.rows_affected() == 1 {
        let state: Option<(Json<TaskState>,)> = sqlx::query_as(
            "SELECT state FROM tasks.task_state WHERE task_id=( \
             SELECT task_id FROM tasks.sponsorship_transitions WHERE transition=$1)",
        )
        .bind(&write.transition)
        .fetch_optional(uow.connection())
        .await?;
        if let Some((Json(state),)) = state
            && state.stage == concurro_tasks_core::Stage::Intake
            && state.current_attempt.is_none()
        {
            super::enqueue_intake(uow, &state, None).await?;
        }
        let standalone: Option<(Uuid, i32, String)> = sqlx::query_as(
            "SELECT t.id,t.round,t.stage FROM tasks.sponsorship_transitions s \
             JOIN tasks.standalone_review_tasks t ON t.id=s.standalone_task_id \
             WHERE s.transition=$1",
        )
        .bind(&write.transition)
        .fetch_optional(uow.connection())
        .await?;
        if let Some((task_id, round, stage)) = standalone
            && matches!(stage.as_str(), "queued" | "review" | "awaiting_credentials")
        {
            super::standalone::enqueue_standalone_review_round(uow, task_id, round).await?;
        }
        return Ok(true);
    }
    Ok(false)
}

/// Preserve a definitive Exchange refusal for operator diagnosis.
pub async fn fail_sponsorship_transition(
    uow: &mut UnitOfWork<'_>,
    write: &SponsorshipWrite,
    reason: &str,
) -> Result<bool, sqlx::Error> {
    let changed = sqlx::query(
        "UPDATE tasks.sponsorship_transitions \
         SET status='failed',claim_token=NULL,failure=$3 \
         WHERE transition=$1 AND claim_token=$2 AND status='sending'",
    )
    .bind(&write.transition)
    .bind(write.claim_token)
    .bind(reason)
    .execute(uow.connection())
    .await?;
    Ok(changed.rows_affected() == 1)
}

/// Activate a current-head funding policy at the database clock, never at source time.
pub async fn activate_funding_policy(
    uow: &mut UnitOfWork<'_>,
    repository_id: i64,
    config_sha: &str,
    config_digest: &str,
    mode: FundingMode,
) -> anyhow::Result<bool> {
    let mode = match mode {
        FundingMode::Org => "org",
        FundingMode::Contributed => "contributed",
    };
    sqlx::query(
        "INSERT INTO tasks.funding_policy_heads (repository_id) VALUES ($1) ON CONFLICT DO NOTHING",
    )
    .bind(repository_id)
    .execute(uow.connection())
    .await?;
    sqlx::query("SELECT 1 FROM tasks.funding_policy_heads WHERE repository_id=$1 FOR UPDATE")
        .bind(repository_id)
        .fetch_one(uow.connection())
        .await?;
    let previous: Option<(DateTime<Utc>, String, String)> = sqlx::query_as(
        "SELECT started_at,config_digest,funding_mode \
         FROM tasks.funding_policy_activations \
         WHERE repository_id=$1 AND ended_at IS NULL FOR UPDATE",
    )
    .bind(repository_id)
    .fetch_optional(uow.connection())
    .await?;
    if previous
        .as_ref()
        .is_some_and(|(_, digest, funding)| digest == config_digest && funding == mode)
    {
        return Ok(false);
    }
    let now: DateTime<Utc> = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(uow.connection())
        .await?;
    anyhow::ensure!(
        previous
            .as_ref()
            .is_none_or(|(started, _, _)| now > *started),
        "funding policy activation clock did not advance"
    );
    sqlx::query(
        "UPDATE tasks.funding_policy_activations SET ended_at=$2 \
         WHERE repository_id=$1 AND ended_at IS NULL",
    )
    .bind(repository_id)
    .bind(now)
    .execute(uow.connection())
    .await?;
    sqlx::query(
        "INSERT INTO tasks.funding_policy_activations \
         (repository_id,config_sha,config_digest,funding_mode,started_at) \
         VALUES ($1,$2,$3,$4,$5)",
    )
    .bind(repository_id)
    .bind(config_sha)
    .bind(config_digest)
    .bind(mode)
    .bind(now)
    .execute(uow.connection())
    .await?;
    Ok(true)
}

/// Find the exact configuration policy active when a boundary source occurred.
pub async fn funding_policy_at(
    database: &Database,
    repository_id: i64,
    config_digest: &str,
    at: DateTime<Utc>,
) -> Result<Option<FundingMode>, sqlx::Error> {
    let observed: Option<String> = sqlx::query_scalar(
        "SELECT funding_mode FROM tasks.funding_policy_activations \
         WHERE repository_id=$1 AND config_digest=$2 AND started_at<=$3 \
         AND (ended_at IS NULL OR ended_at>$3) \
         ORDER BY started_at DESC LIMIT 1",
    )
    .bind(repository_id)
    .bind(config_digest)
    .bind(at)
    .fetch_optional(database.pool())
    .await?;
    Ok(observed.map(|mode| match mode.as_str() {
        "org" => FundingMode::Org,
        "contributed" => FundingMode::Contributed,
        _ => unreachable!("funding mode constrained by schema"),
    }))
}

#[cfg(test)]
mod version_tests {
    use chrono::{Duration, Utc};
    use concurro_contracts::{CredentialChanged, TaskId};
    use concurro_db::Database;
    use concurro_exchange_v1::TurnSubmission;
    use concurro_tasks_core::AttemptSpec;
    use concurro_testkit::PostgresContainer;
    use serde_json::json;
    use uuid::Uuid;

    use super::{record_credential_failure, record_funding_grant};
    use crate::{apply_credential_change, migrate};

    /// A committed version marker refuses an unseen attempt while a replacement inserts.
    #[tokio::test]
    async fn retired_version_cannot_insert_a_later_grant() {
        let postgres = PostgresContainer::start().await.expect("PostgreSQL");
        let database = Database::connect(postgres.url(), 4)
            .await
            .expect("database");
        migrate(&database).await.expect("schema");
        let task = Uuid::now_v7();
        sqlx::query("INSERT INTO tasks.issue_controls(repository_id,issue_number) VALUES(7,42)")
            .execute(database.pool())
            .await
            .expect("issue");
        sqlx::query("INSERT INTO tasks.task_state(repository_id,issue_number,task_id,revision,state) VALUES(7,42,$1,1,'{}')")
            .bind(task).execute(database.pool()).await.expect("task");
        let change: CredentialChanged = serde_json::from_value(json!({
            "user_id":7,"sequence":1,"entry_id":"entry","provider":"anthropic",
            "change":"replaced","list_revision":2,"at":Utc::now(),
            "revoked":[{"record_id":"old-record","version":1,
                "route":"anthropic-api-key","custody":"vault"}]
        }))
        .expect("change");
        let mut work = database.begin().await.expect("marker transaction");
        apply_credential_change(&mut work, &change)
            .await
            .expect("marker");
        work.commit().await.expect("marker commit");
        let mut replacement_turn = None;
        for (record, version, allowed) in
            [("old-record", 1_i64, false), ("new-record", 2_i64, true)]
        {
            let turn = Uuid::now_v7();
            let source = TurnSubmission {
                turn,
                expected: json!({}),
                models: vec!["claude-test".to_owned()],
                budget: 1000,
                lease: 3600,
                pin: None,
                binding: json!({"funding":"contributed"}),
                payload: json!({"request":{"model":"claude-test","admission":{
                    "sponsor":"7","sponsorship_provider":"anthropic","epoch":1,
                    "grant_id":"slot","route":"anthropic-api-key",
                    "credential":{"record_id":record,"version":version}
                }}}),
            };
            let bytes = serde_json::to_vec(&source).expect("source");
            sqlx::query("INSERT INTO tasks.attempts(id,task_id,operation,expected_revision,staffing_group,fork,config_sha,submit_body,initial_deadline,deadline,unbounded_running,status) VALUES($1,$2,'intake',1,'group','acme/fork',$3,$4,now()+interval '1 hour',now()+interval '1 hour',false,'ready')")
                .bind(turn).bind(task).bind("a".repeat(40)).bind(&bytes)
                .execute(database.pool()).await.expect("attempt");
            let spec = AttemptSpec {
                id: turn,
                task: TaskId(task),
                expected_revision: 1,
                staffing_group: "group".to_owned(),
                fork: "acme/fork".to_owned(),
                config_sha: "a".repeat(40),
                submit_body: bytes,
                prompt_body: None,
                deadline: Utc::now() + Duration::hours(1),
                unbounded_running: false,
            };
            let mut work = database.begin().await.expect("grant transaction");
            let result = record_funding_grant(&mut work, &spec).await;
            assert_eq!(result.is_ok(), allowed);
            if allowed {
                replacement_turn = Some(turn);
                work.commit().await.expect("replacement grant");
            }
        }
        let grants: i64 = sqlx::query_scalar("SELECT count(*) FROM tasks.funding_grants")
            .fetch_one(database.pool())
            .await
            .expect("grant count");
        assert_eq!(grants, 1);
        let turn = replacement_turn.expect("replacement attempt");
        let observation: concurro_exchange_v1::TurnObservation = serde_json::from_value(json!({
            "turn":turn,"status":"running","created":1.0,
            "provider_failure":{
                "turn":turn.to_string(),"grant_id":"slot","model":"claude-test",
                "category":"quota_exhausted","retry_after":2.25,
                "binding":{"sponsor":"7","credential":"new-record",
                    "credential_version":2,"grant_id":"slot"}
            }
        }))
        .expect("fractional quota failure");
        let mut work = database.begin().await.expect("exclusion transaction");
        assert!(
            record_credential_failure(&mut work, &observation)
                .await
                .expect("quota exclusion")
        );
        work.commit().await.expect("exclusion commit");
        let (retry_source, temporary): (String, bool) = sqlx::query_as(
            "SELECT retry_source,retry_at>now() AND retry_at<now()+interval '10 seconds' \
             FROM tasks.funding_exclusions WHERE attempt_id=$1",
        )
        .bind(turn)
        .fetch_one(database.pool())
        .await
        .expect("temporary quota hint");
        assert_eq!(retry_source, "provider");
        assert!(temporary);
    }
}
