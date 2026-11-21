//! Recovery-command decisions under the issue transaction lock.

use std::collections::BTreeMap;

use anyhow::{Result, ensure};
use chrono::{DateTime, Utc};
use concurro_db::UnitOfWork;
use concurro_events_postgres::Outbox;
use concurro_tasks_core::{
    Authority, Decision, DesignReviewer, InitializationSnapshot, LaterCommand, Request,
    RequestKind, RequestStatus, Stage, TaskState, design_roster, pr_review_roster,
};
use serde_json::Value;
use sqlx::types::Json;
use uuid::Uuid;

use crate::{
    GitPublicationIntent, cancel_attempts, design, enqueue_gates, enqueue_git_publication,
    enqueue_implementation, enqueue_intake, enqueue_plan, pr_review,
    sponsorship::billable_model_calls,
};

/// Fresh source and PR observations used by recovery commands.
pub struct CommandEvidence {
    /// Current configuration, bundle, base and issue requirements.
    pub snapshot: InitializationSnapshot,
    /// Whether the recorded App PR is still open at the published head.
    pub pr_unchanged: bool,
    /// Proven old and new implementer skill hashes when that is the sole bundle change.
    pub implementer_skill_update: Option<(String, String)>,
}

/// Source adoption and optional exact approval restoration selected by a resume command.
pub(crate) struct ResumeAdoption {
    /// Inputs before the writer's command.
    prior: InitializationSnapshot,
    /// Current execution inputs accepted by the command.
    adopted: InitializationSnapshot,
    /// Whether a new intake assessment consumes a plan revision.
    reassess: bool,
    /// Open finding owners retained under a changed reviewer identity.
    remaps: Vec<(String, String)>,
    /// Original finding statuses when recovering one description-only reopen.
    description_repair: Option<(Uuid, Vec<(String, String)>)>,
    /// Retry that adopts only a proven implementer skill revision.
    skill_only: Option<(String, String)>,
}

/// Detect a publication that a recovery command must reconcile first.
async fn publication_pending(uow: &mut UnitOfWork<'_>, task_id: Uuid) -> anyhow::Result<bool> {
    Ok(sqlx::query_scalar(
        "SELECT EXISTS (SELECT 1 FROM tasks.git_publications \
             WHERE task_id=$1 AND intent->'pr_number'='null'::jsonb) \
         OR EXISTS (SELECT 1 FROM tasks.pr_review_publications \
             WHERE task_id=$1 AND status IN ('recorded','sent')) \
         OR EXISTS (SELECT 1 FROM tasks.pr_ready_intents \
             WHERE task_id=$1 AND status='sent')",
    )
    .bind(task_id)
    .fetch_one(uow.connection())
    .await?)
}

/// Recover only an unchanged implementation reopen with its original approval evidence.
async fn description_repair(
    uow: &mut UnitOfWork<'_>,
    current: &TaskState,
    evidence: &CommandEvidence,
) -> Result<Result<(TaskState, ResumeAdoption), &'static str>> {
    if !matches!(
        current.stage,
        Stage::Plan | Stage::Blocked | Stage::Escalated | Stage::Stopped
    ) {
        return Ok(Err("description_repair_not_waiting"));
    }
    if !evidence.pr_unchanged {
        return Ok(Err("pr_changed"));
    }
    if publication_pending(uow, current.id.0).await? {
        return Ok(Err("publication_prepared"));
    }
    let saved: Option<Json<InitializationSnapshot>> = sqlx::query_scalar(
        "SELECT snapshot FROM tasks.initialization_inputs WHERE task_id=$1 FOR UPDATE",
    )
    .bind(current.id.0)
    .fetch_optional(uow.connection())
    .await?;
    let Some(Json(saved)) = saved else {
        return Ok(Err("task_not_initialized"));
    };
    if evidence.snapshot.pins.config_sha != saved.pins.config_sha
        || evidence.snapshot.requirements != saved.requirements
        || evidence.snapshot.bundle_id != saved.bundle_id
        || evidence.snapshot.charter != saved.charter
    {
        return Ok(Err("governance_changed"));
    }
    let reopen: Option<(
        Uuid,
        String,
        String,
        Json<InitializationSnapshot>,
        Option<String>,
        Json<Vec<(String, String, Value, Option<Value>)>>,
        DateTime<Utc>,
        Option<Uuid>,
    )> = sqlx::query_as(
        "SELECT turn_id,plan_id,binding,prior_snapshot,prior_head,findings_snapshot, \
         recorded_at,recovered_request \
         FROM tasks.implementation_reopens WHERE task_id=$1 \
         ORDER BY recorded_at DESC,turn_id DESC LIMIT 1",
    )
    .bind(current.id.0)
    .fetch_optional(uow.connection())
    .await?;
    let Some((turn, plan_id, binding, Json(prior), Some(head), Json(before), recorded_at, None)) =
        reopen
    else {
        return Ok(Err("description_repair_not_available"));
    };
    let approval: Option<(String, String)> = sqlx::query_as(
        "SELECT plan_id,binding FROM tasks.plan_approvals WHERE task_id=$1 \
         ORDER BY recorded_at DESC,request_id DESC LIMIT 1",
    )
    .bind(current.id.0)
    .fetch_optional(uow.connection())
    .await?;
    let latest_head: Option<String> = sqlx::query_scalar(
        "SELECT head FROM tasks.pr_review_batches WHERE task_id=$1 AND status='settled' \
         ORDER BY round DESC,id DESC LIMIT 1",
    )
    .bind(current.id.0)
    .fetch_optional(uow.connection())
    .await?;
    let newer_plan: bool = sqlx::query_scalar(
        "SELECT EXISTS (SELECT 1 FROM tasks.plan_reports WHERE task_id=$1 AND accepted \
         AND recorded_at>$2)",
    )
    .bind(current.id.0)
    .bind(recorded_at)
    .fetch_one(uow.connection())
    .await?;
    let after: Vec<(String, String, Json<Value>, Option<Json<Value>>)> = sqlx::query_as(
        "SELECT id,status,finding,disposition FROM tasks.pr_review_findings \
         WHERE task_id=$1 ORDER BY sequence",
    )
    .bind(current.id.0)
    .fetch_all(uow.connection())
    .await?;
    let findings_unchanged = before.len() == after.len()
        && before.iter().zip(&after).all(
            |(
                (prior_id, prior_status, prior_finding, prior_disposition),
                (id, status, Json(finding), disposition),
            )| {
                id == prior_id
                    && finding == prior_finding
                    && disposition.as_ref().map(|Json(value)| value) == prior_disposition.as_ref()
                    && (status == prior_status || (prior_status == "open" && status == "withdrawn"))
            },
        );
    if prior != saved
        || approval != Some((plan_id, binding))
        || latest_head.as_deref() != Some(head.as_str())
        || newer_plan
        || !findings_unchanged
    {
        return Ok(Err("description_repair_evidence_changed"));
    }
    if billable_model_calls(uow, current).await? >= saved.pins.max_model_calls {
        return Ok(Err("model_call_budget_exhausted"));
    }
    let next = TaskState {
        stage: Stage::Implement,
        revision: current.revision + 1,
        stage_reason: Some(
            "Writer restored unchanged design approval for description repair".to_owned(),
        ),
        retry_stage: None,
        current_attempt: None,
        ..current.clone()
    };
    let adoption = ResumeAdoption {
        prior: saved.clone(),
        adopted: saved,
        reassess: false,
        remaps: Vec::new(),
        description_repair: Some((
            turn,
            before
                .into_iter()
                .map(|(id, status, _, _)| (id, status))
                .collect(),
        )),
        skill_only: None,
    };
    Ok(Ok((next, adoption)))
}

/// Apply a recovery command and retain its exact adoption inputs for the same transaction.
pub(crate) async fn decide_recovery(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    request: &Request,
    current: Option<&TaskState>,
    authority: Authority,
    evidence: Option<&CommandEvidence>,
    decision: &mut Decision,
    resume_adoption: &mut Option<ResumeAdoption>,
) -> Result<()> {
    if authority.writer
        && authority.issue_open
        && decision.status != RequestStatus::Superseded
        && let RequestKind::Later(LaterCommand::Retry {
            plan_revisions: Some(amount),
        }) = &request.kind
    {
        let refused = |reason| Decision {
            status: RequestStatus::Refused,
            reason: Some(reason),
            task: None,
            stop_order: None,
        };
        *decision = if *amount == 0 {
            refused("invalid_plan_revision_grant")
        } else if let (Some(current), Some(evidence)) = (current, evidence) {
            if !matches!(
                current.stage,
                Stage::Blocked | Stage::Stopped | Stage::Escalated
            ) {
                refused("plan_revision_not_waiting")
            } else {
                let saved: Option<(i64, i64, Json<InitializationSnapshot>)> = sqlx::query_as(
                    "SELECT spent_revisions,extra_plan_revisions,snapshot \
                     FROM tasks.initialization_inputs WHERE task_id=$1 FOR UPDATE",
                )
                .bind(current.id.0)
                .fetch_optional(uow.connection())
                .await?;
                if let Some((spent, extra, Json(snapshot))) = saved {
                    let pending = publication_pending(uow, current.id.0).await?;
                    let exhausted = current.pins.as_ref().is_some_and(|pins| {
                        u64::try_from(spent).unwrap_or(0)
                            >= pins.max_plan_revisions + u64::try_from(extra).unwrap_or(0)
                    });
                    let waiting = current.stage_reason.as_deref().is_some_and(|reason| {
                        reason.contains("Plan revision budget exhausted")
                            || reason.contains("plan revision budget was spent")
                    });
                    if !waiting || !exhausted {
                        refused("plan_revision_not_waiting")
                    } else if pending {
                        refused("publication_prepared")
                    } else if !evidence.pr_unchanged {
                        refused("pr_changed")
                    } else if evidence.snapshot.pins.config_sha != snapshot.pins.config_sha
                        || evidence.snapshot.requirements != snapshot.requirements
                        || evidence.snapshot.bundle_id != snapshot.bundle_id
                    {
                        refused("governance_changed")
                    } else {
                        sqlx::query(
                            "UPDATE tasks.initialization_inputs SET \
                             extra_plan_revisions=extra_plan_revisions+$2, \
                             spent_revisions=spent_revisions+1 WHERE task_id=$1",
                        )
                        .bind(current.id.0)
                        .bind(i64::from(*amount))
                        .execute(uow.connection())
                        .await?;
                        sqlx::query(
                            "UPDATE tasks.plan_reports SET current=false \
                             WHERE task_id=$1 AND current",
                        )
                        .bind(current.id.0)
                        .execute(uow.connection())
                        .await?;
                        Decision {
                            status: RequestStatus::Applied,
                            reason: None,
                            task: Some(TaskState {
                                stage: Stage::Plan,
                                revision: current.revision + 1,
                                stage_reason: Some(format!(
                                    "Writer granted {amount} additional plan revisions"
                                )),
                                current_attempt: None,
                                ..current.clone()
                            }),
                            stop_order: None,
                        }
                    }
                } else {
                    refused("plan_revision_not_waiting")
                }
            }
        } else {
            refused("plan_revision_not_waiting")
        };
    }
    if authority.writer
        && authority.issue_open
        && decision.status != RequestStatus::Superseded
        && request.kind
            == RequestKind::Later(LaterCommand::Retry {
                plan_revisions: None,
            })
    {
        let refused = |reason| Decision {
            status: RequestStatus::Refused,
            reason: Some(reason),
            task: None,
            stop_order: None,
        };
        *decision = if let (Some(current), Some(evidence)) = (current, evidence) {
            let saved: Option<Json<InitializationSnapshot>> = sqlx::query_scalar(
                "SELECT snapshot FROM tasks.initialization_inputs WHERE task_id=$1 FOR UPDATE",
            )
            .bind(current.id.0)
            .fetch_optional(uow.connection())
            .await?;
            if let Some(Json(saved)) = saved {
                if let Some(skill_update) = &evidence.implementer_skill_update {
                    if !matches!(
                        current.stage,
                        Stage::Blocked | Stage::Stopped | Stage::Escalated
                    ) || current.retry_stage != Some(Stage::Implement)
                    {
                        refused("retry_not_available")
                    } else if saved.configuration != evidence.snapshot.configuration
                        || saved.requirements != evidence.snapshot.requirements
                        || saved.charter != evidence.snapshot.charter
                        || saved.instruction_texts != evidence.snapshot.instruction_texts
                        || publication_pending(uow, current.id.0).await?
                    {
                        refused("governance_changed")
                    } else if !evidence.pr_unchanged {
                        refused("pr_changed")
                    } else if billable_model_calls(uow, current).await?
                        >= saved.pins.max_model_calls
                    {
                        refused("model_call_budget_exhausted")
                    } else {
                        let approval: Option<(String, String)> = sqlx::query_as(
                            "SELECT plan_id,binding FROM tasks.plan_approvals WHERE task_id=$1 \
                             ORDER BY recorded_at DESC,request_id DESC LIMIT 1",
                        )
                        .bind(current.id.0)
                        .fetch_optional(uow.connection())
                        .await?;
                        if approval.is_none()
                            || approval != design::approval_proof(uow, current.id.0).await?
                        {
                            refused("design_approval_changed")
                        } else {
                            let mut adopted = evidence.snapshot.clone();
                            adopted.pins = saved.pins.clone();
                            adopted.policy_documents = saved.policy_documents.clone();
                            adopted.base_files = saved.base_files.clone();
                            *resume_adoption = Some(ResumeAdoption {
                                prior: saved,
                                adopted: adopted.clone(),
                                reassess: false,
                                remaps: Vec::new(),
                                description_repair: None,
                                skill_only: Some(skill_update.clone()),
                            });
                            Decision {
                                status: RequestStatus::Applied,
                                reason: None,
                                task: Some(TaskState {
                                    stage: Stage::Implement,
                                    revision: current.revision + 1,
                                    stage_reason: Some(
                                        "Writer adopted verified implementer instructions"
                                            .to_owned(),
                                    ),
                                    retry_stage: None,
                                    current_attempt: None,
                                    ..current.clone()
                                }),
                                stop_order: None,
                            }
                        }
                    }
                } else if evidence.snapshot.pins.config_sha != saved.pins.config_sha
                    || evidence.snapshot.requirements != saved.requirements
                    || evidence.snapshot.bundle_id != saved.bundle_id
                {
                    refused("governance_changed")
                } else if !evidence.pr_unchanged {
                    refused("pr_changed")
                } else if current.stage == Stage::Gates {
                    Decision {
                        status: RequestStatus::Applied,
                        reason: None,
                        task: Some(TaskState {
                            revision: current.revision + 1,
                            stage_reason: Some(
                                "Writer requested a fresh gate observation".to_owned(),
                            ),
                            ..current.clone()
                        }),
                        stop_order: None,
                    }
                } else if current.stage == Stage::Blocked {
                    let failed: Option<String> = sqlx::query_scalar(
                        "SELECT status FROM tasks.pr_ready_intents WHERE task_id=$1 \
                         AND status='failed'",
                    )
                    .bind(current.id.0)
                    .fetch_optional(uow.connection())
                    .await?;
                    if failed.is_some() {
                        sqlx::query(
                            "UPDATE tasks.pr_ready_intents SET status='recorded',error=NULL \
                             WHERE task_id=$1 AND status='failed'",
                        )
                        .bind(current.id.0)
                        .execute(uow.connection())
                        .await?;
                        Decision {
                            status: RequestStatus::Applied,
                            reason: None,
                            task: Some(TaskState {
                                stage: Stage::Gates,
                                revision: current.revision + 1,
                                retry_stage: None,
                                stage_reason: Some(
                                    "Writer requested a new mark-ready observation".to_owned(),
                                ),
                                ..current.clone()
                            }),
                            stop_order: None,
                        }
                    } else if let Some(stage) = current.retry_stage {
                        match stage {
                            Stage::Intake
                            | Stage::Plan
                            | Stage::PlanQc
                            | Stage::Implement
                            | Stage::Publish
                            | Stage::Review => {
                                if stage != Stage::Publish
                                    && billable_model_calls(uow, current).await?
                                        >= current
                                            .pins
                                            .as_ref()
                                            .expect("paused pins")
                                            .max_model_calls
                                {
                                    refused("model_call_budget_exhausted")
                                } else if stage == Stage::Implement
                                    && current.stage_reason.as_deref().is_some_and(|reason| {
                                        reason.contains("fix-round allowance exhausted")
                                    })
                                {
                                    refused("fix_round_budget_exhausted")
                                } else {
                                    let next = TaskState {
                                        stage,
                                        revision: current.revision + 1,
                                        stage_reason: Some(
                                            "Writer retried the paused stage".to_owned(),
                                        ),
                                        retry_stage: None,
                                        current_attempt: None,
                                        ..current.clone()
                                    };
                                    let available = match stage {
                                        Stage::PlanQc => {
                                            design::retry_design_review(uow, &next).await?
                                        }
                                        Stage::Review => {
                                            pr_review::retry_pr_review_batch(uow, &next).await?
                                        }
                                        _ => true,
                                    };
                                    if available {
                                        Decision {
                                            status: RequestStatus::Applied,
                                            reason: None,
                                            task: Some(next),
                                            stop_order: None,
                                        }
                                    } else {
                                        refused("retry_not_available")
                                    }
                                }
                            }
                            _ => refused("retry_not_available"),
                        }
                    } else {
                        refused("retry_not_available")
                    }
                } else if matches!(current.stage, Stage::Escalated | Stage::Stopped)
                    && let Some(
                        stage @ (Stage::Intake
                        | Stage::Plan
                        | Stage::PlanQc
                        | Stage::Implement
                        | Stage::Review),
                    ) = current.retry_stage
                {
                    if billable_model_calls(uow, current).await?
                        >= current.pins.as_ref().expect("paused pins").max_model_calls
                    {
                        refused("model_call_budget_exhausted")
                    } else {
                        let next = TaskState {
                            stage,
                            revision: current.revision + 1,
                            stage_reason: Some("Writer retried the paused stage".to_owned()),
                            retry_stage: None,
                            current_attempt: None,
                            ..current.clone()
                        };
                        let available = match stage {
                            Stage::PlanQc => design::retry_design_review(uow, &next).await?,
                            Stage::Review => pr_review::retry_pr_review_batch(uow, &next).await?,
                            _ => true,
                        };
                        if available {
                            Decision {
                                status: RequestStatus::Applied,
                                reason: None,
                                task: Some(next),
                                stop_order: None,
                            }
                        } else {
                            refused("retry_not_available")
                        }
                    }
                } else {
                    refused("retry_not_available")
                }
            } else {
                refused("task_not_initialized")
            }
        } else {
            refused("retry_not_available")
        };
    }
    if authority.writer
        && authority.issue_open
        && decision.status != RequestStatus::Superseded
        && let RequestKind::Later(LaterCommand::Resume { description_only }) = &request.kind
    {
        let refused = |reason| Decision {
            status: RequestStatus::Refused,
            reason: Some(reason),
            task: None,
            stop_order: None,
        };
        *decision = if *description_only {
            if let (Some(current), Some(evidence)) = (current, evidence) {
                match description_repair(uow, current, evidence).await? {
                    Ok((next, adoption)) => {
                        *resume_adoption = Some(adoption);
                        Decision {
                            status: RequestStatus::Applied,
                            reason: None,
                            task: Some(next),
                            stop_order: None,
                        }
                    }
                    Err(reason) => refused(reason),
                }
            } else {
                refused("description_repair_not_available")
            }
        } else if let (Some(current), Some(evidence)) = (current, evidence) {
            let saved: Option<Json<InitializationSnapshot>> = sqlx::query_scalar(
                "SELECT snapshot FROM tasks.initialization_inputs WHERE task_id=$1 FOR UPDATE",
            )
            .bind(current.id.0)
            .fetch_optional(uow.connection())
            .await?;
            if let Some(Json(saved)) = saved {
                let reassess = matches!(current.stage, Stage::Rejected | Stage::Escalated)
                    && current.retry_stage.is_none();
                let next_stage = match current.stage {
                    Stage::ObservedMerged | Stage::Closed => None,
                    Stage::Blocked | Stage::Stopped => current.retry_stage,
                    Stage::Escalated if current.retry_stage.is_some() => current.retry_stage,
                    Stage::Rejected | Stage::Escalated => Some(Stage::Intake),
                    stage => Some(stage),
                };
                if !evidence.pr_unchanged {
                    refused("pr_changed")
                } else if let Some(stage) = next_stage {
                    if stage == Stage::Initializing {
                        refused("resume_stage_not_ready")
                    } else {
                        let mut adopted = evidence.snapshot.clone();
                        adopted.pins.base_sha = saved.pins.base_sha.clone();
                        adopted.policy_documents = saved.policy_documents.clone();
                        adopted.base_files = saved.base_files.clone();
                        if !reassess {
                            adopted.requirements = saved.requirements.clone();
                        }
                        let design = design_roster(&adopted.configuration);
                        let pr = pr_review_roster(&adopted.configuration);
                        if let (Ok(design), Ok(pr)) = (design, pr) {
                            let unresolved: Vec<String> = sqlx::query_scalar(
                                "SELECT DISTINCT reviewer_id FROM tasks.plan_objections \
                                 WHERE task_id=$1 AND NOT resolved",
                            )
                            .bind(current.id.0)
                            .fetch_all(uow.connection())
                            .await?;
                            let design_orphaned = unresolved
                                .iter()
                                .any(|owner| !design.iter().any(|member| member.id == *owner));
                            let open: Vec<String> = sqlx::query_scalar(
                                "SELECT DISTINCT reviewer_id FROM tasks.pr_review_findings \
                                 WHERE task_id=$1 AND status IN ('open','withdrawn')",
                            )
                            .bind(current.id.0)
                            .fetch_all(uow.connection())
                            .await?;
                            let rosters: Vec<Json<Vec<DesignReviewer>>> = sqlx::query_scalar(
                                "SELECT roster FROM tasks.pr_review_batches WHERE task_id=$1 \
                                     ORDER BY round DESC,id DESC",
                            )
                            .bind(current.id.0)
                            .fetch_all(uow.connection())
                            .await?;
                            let mut prior_owners = BTreeMap::new();
                            for Json(roster) in rosters {
                                for member in roster {
                                    prior_owners.entry(member.id).or_insert(member.provider);
                                }
                            }
                            let mut remaps = Vec::new();
                            let mut pr_orphaned = false;
                            for owner in open {
                                let next = prior_owners.get(&owner).and_then(|provider| {
                                    pr.iter().find(|member| member.provider == *provider)
                                });
                                if let Some(next) = next {
                                    if owner != next.id {
                                        remaps.push((owner, next.id.clone()));
                                    }
                                } else {
                                    pr_orphaned = true;
                                }
                            }
                            let (spent, extra): (i64, i64) = sqlx::query_as(
                                "SELECT spent_revisions,extra_plan_revisions \
                                 FROM tasks.initialization_inputs WHERE task_id=$1",
                            )
                            .bind(current.id.0)
                            .fetch_one(uow.connection())
                            .await?;
                            if design_orphaned || pr_orphaned {
                                refused("orphaned_review_owner")
                            } else if reassess
                                && u64::try_from(spent)?
                                    >= adopted.pins.max_plan_revisions + u64::try_from(extra)?
                            {
                                refused("plan_revision_exhausted")
                            } else {
                                let rebind = adopted.configuration != saved.configuration
                                    || adopted.charter != saved.charter
                                    || adopted.bundle_id != saved.bundle_id;
                                let needs_model_call =
                                    matches!(stage, Stage::Intake | Stage::Plan | Stage::Implement)
                                        || (rebind
                                            && matches!(stage, Stage::PlanQc | Stage::Review));
                                if needs_model_call
                                    && billable_model_calls(uow, current).await?
                                        >= adopted.pins.max_model_calls
                                {
                                    *decision = refused("model_call_budget_exhausted");
                                    return Ok(());
                                }
                                let next = TaskState {
                                    stage,
                                    revision: current.revision + 1,
                                    stage_reason: Some(
                                        "Writer resumed with current execution settings".to_owned(),
                                    ),
                                    retry_stage: None,
                                    pins: Some(adopted.pins.clone()),
                                    current_attempt: None,
                                    ..current.clone()
                                };
                                let available = if !rebind && current.stage != stage {
                                    match stage {
                                        Stage::PlanQc => {
                                            design::retry_design_review(uow, &next).await?
                                        }
                                        Stage::Review => {
                                            pr_review::retry_pr_review_batch(uow, &next).await?
                                        }
                                        _ => true,
                                    }
                                } else {
                                    true
                                };
                                if available {
                                    *resume_adoption = Some(ResumeAdoption {
                                        prior: saved,
                                        adopted: adopted.clone(),
                                        reassess,
                                        remaps,
                                        description_repair: None,
                                        skill_only: None,
                                    });
                                    Decision {
                                        status: RequestStatus::Applied,
                                        reason: None,
                                        task: Some(next),
                                        stop_order: None,
                                    }
                                } else {
                                    refused("resume_stage_not_ready")
                                }
                            }
                        } else {
                            refused("invalid_reviewer_roster")
                        }
                    }
                } else {
                    refused("task_terminal")
                }
            } else {
                refused("task_not_initialized")
            }
        } else {
            refused("task_not_initialized")
        };
    }
    if authority.writer
        && authority.issue_open
        && decision.status != RequestStatus::Superseded
        && request.kind == RequestKind::Later(LaterCommand::Invalidate)
    {
        let refused = |reason| Decision {
            status: RequestStatus::Refused,
            reason: Some(reason),
            task: None,
            stop_order: None,
        };
        *decision = if let (Some(current), Some(evidence)) = (current, evidence) {
            let saved: Option<Json<InitializationSnapshot>> = sqlx::query_scalar(
                "SELECT snapshot FROM tasks.initialization_inputs WHERE task_id=$1 FOR UPDATE",
            )
            .bind(current.id.0)
            .fetch_optional(uow.connection())
            .await?;
            if let Some(Json(saved)) = saved {
                if matches!(current.stage, Stage::ObservedMerged | Stage::Closed) {
                    refused("task_terminal")
                } else if publication_pending(uow, current.id.0).await? {
                    refused("publication_prepared")
                } else if !evidence.pr_unchanged {
                    refused("pr_changed")
                } else if evidence.snapshot.pins.config_sha != saved.pins.config_sha {
                    refused("configuration_changed")
                } else {
                    let prior_plan: Option<String> = sqlx::query_scalar(
                        "SELECT plan_id FROM tasks.plan_reports WHERE task_id=$1 \
                         ORDER BY recorded_at DESC,attempt_id DESC LIMIT 1",
                    )
                    .bind(current.id.0)
                    .fetch_optional(uow.connection())
                    .await?;
                    let prior_head: Option<String> = sqlx::query_scalar(
                        "SELECT head FROM tasks.pr_review_batches WHERE task_id=$1 \
                         ORDER BY round DESC,id DESC LIMIT 1",
                    )
                    .bind(current.id.0)
                    .fetch_optional(uow.connection())
                    .await?;
                    sqlx::query(
                        "INSERT INTO tasks.task_invalidations \
                         (request_id,task_id,actor,prior_base_sha,prior_bundle_id, \
                          prior_plan_id,prior_head,next_base_sha,next_bundle_id) \
                         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                    )
                    .bind(request.id)
                    .bind(current.id.0)
                    .bind(Json(&request.actor))
                    .bind(&saved.pins.base_sha)
                    .bind(&saved.bundle_id)
                    .bind(prior_plan)
                    .bind(prior_head)
                    .bind(&evidence.snapshot.pins.base_sha)
                    .bind(&evidence.snapshot.bundle_id)
                    .execute(uow.connection())
                    .await?;
                    sqlx::query(
                        "UPDATE tasks.initialization_inputs SET snapshot=$2,task_revision=$3 \
                         WHERE task_id=$1",
                    )
                    .bind(current.id.0)
                    .bind(Json(&evidence.snapshot))
                    .bind(i64::try_from(current.revision + 1)?)
                    .execute(uow.connection())
                    .await?;
                    sqlx::query(
                        "UPDATE tasks.plan_reports SET current=false \
                         WHERE task_id=$1 AND current",
                    )
                    .bind(current.id.0)
                    .execute(uow.connection())
                    .await?;
                    sqlx::query(
                        "UPDATE tasks.plan_review_batches SET status='superseded' \
                         WHERE task_id=$1 AND status IN ('recorded','admitted')",
                    )
                    .bind(current.id.0)
                    .execute(uow.connection())
                    .await?;
                    sqlx::query(
                        "UPDATE tasks.pr_review_batches SET status='superseded' \
                         WHERE task_id=$1 AND status IN ('recorded','admitted')",
                    )
                    .bind(current.id.0)
                    .execute(uow.connection())
                    .await?;
                    sqlx::query(
                        "UPDATE tasks.pr_review_findings SET status='withdrawn' \
                         WHERE task_id=$1 AND status='open'",
                    )
                    .bind(current.id.0)
                    .execute(uow.connection())
                    .await?;
                    cancel_attempts(uow, outbox, current.id.0, None).await?;
                    Decision {
                        status: RequestStatus::Applied,
                        reason: None,
                        task: Some(TaskState {
                            stage: Stage::Intake,
                            revision: current.revision + 1,
                            stage_reason: Some("Writer requested a fresh assessment".to_owned()),
                            pins: Some(evidence.snapshot.pins.clone()),
                            current_attempt: None,
                            ..current.clone()
                        }),
                        stop_order: None,
                    }
                }
            } else {
                refused("task_not_initialized")
            }
        } else {
            refused("task_not_initialized")
        };
    }
    Ok(())
}

/// Apply one approved resume receipt and its stage-specific recovery under the issue lock.
pub(crate) async fn apply_resume_adoption(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    request: &Request,
    previous: &TaskState,
    task: &TaskState,
    adoption: &ResumeAdoption,
) -> Result<()> {
    let prior_snapshot = &adoption.prior;
    let adopted = &adoption.adopted;
    let reassess = adoption.reassess;
    let remaps = &adoption.remaps;
    let rebind = adopted.configuration != prior_snapshot.configuration
        || adopted.charter != prior_snapshot.charter
        || adopted.bundle_id != prior_snapshot.bundle_id;
    let approval_binding: Option<String> = sqlx::query_scalar(
        "SELECT binding FROM tasks.plan_approvals WHERE task_id=$1 \
             ORDER BY recorded_at DESC,request_id DESC LIMIT 1",
    )
    .bind(task.id.0)
    .fetch_optional(uow.connection())
    .await?;
    sqlx::query(
        "INSERT INTO tasks.task_resumes \
             (request_id,task_id,actor,description_only,prior_snapshot,adopted_snapshot, \
              prior_stage,next_stage,approval_binding,kind,old_skill_sha,new_skill_sha) \
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)",
    )
    .bind(request.id)
    .bind(task.id.0)
    .bind(Json(&request.actor))
    .bind(adoption.description_repair.is_some())
    .bind(Json(prior_snapshot))
    .bind(Json(adopted))
    .bind(format!("{:?}", previous.stage))
    .bind(format!("{:?}", task.stage))
    .bind(approval_binding)
    .bind(if adoption.skill_only.is_some() {
        "implementer_skill_retry"
    } else {
        "resume"
    })
    .bind(adoption.skill_only.as_ref().map(|(old, _)| old))
    .bind(adoption.skill_only.as_ref().map(|(_, new)| new))
    .execute(uow.connection())
    .await?;
    if adoption.skill_only.is_some() {
        sqlx::query(
            "UPDATE tasks.initialization_inputs SET \
             implementation_session_epoch=implementation_session_epoch+1 \
             WHERE task_id=$1",
        )
        .bind(task.id.0)
        .execute(uow.connection())
        .await?;
    }
    if let Some((turn, findings)) = &adoption.description_repair {
        let approval: (String, String) = sqlx::query_as(
            "SELECT plan_id,binding FROM tasks.plan_approvals WHERE task_id=$1 \
             ORDER BY recorded_at DESC,request_id DESC LIMIT 1",
        )
        .bind(task.id.0)
        .fetch_one(uow.connection())
        .await?;
        sqlx::query("UPDATE tasks.plan_reports SET current=false WHERE task_id=$1 AND current")
            .bind(task.id.0)
            .execute(uow.connection())
            .await?;
        let restored = sqlx::query(
            "UPDATE tasks.plan_reports SET current=true WHERE task_id=$1 \
             AND plan_id=$2 AND accepted",
        )
        .bind(task.id.0)
        .bind(&approval.0)
        .execute(uow.connection())
        .await?;
        ensure!(
            restored.rows_affected() > 0,
            "original approved plan is unavailable"
        );
        for (id, status) in findings {
            sqlx::query(
                "UPDATE tasks.pr_review_findings SET status=$3 \
                 WHERE task_id=$1 AND id=$2",
            )
            .bind(task.id.0)
            .bind(id)
            .bind(status)
            .execute(uow.connection())
            .await?;
        }
        ensure!(
            design::approval_proof(uow, task.id.0).await? == Some(approval),
            "original design approval cannot be restored"
        );
        sqlx::query(
            "UPDATE tasks.implementation_reopens SET recovered_request=$2 \
             WHERE turn_id=$1 AND task_id=$3 AND recovered_request IS NULL",
        )
        .bind(turn)
        .bind(request.id)
        .bind(task.id.0)
        .execute(uow.connection())
        .await?;
    }
    sqlx::query(
        "UPDATE tasks.initialization_inputs SET snapshot=$2, \
             task_revision=CASE WHEN $3 THEN $4 ELSE task_revision END, \
             spent_revisions=spent_revisions+CASE WHEN $5 THEN 1 ELSE 0 END \
             WHERE task_id=$1",
    )
    .bind(task.id.0)
    .bind(Json(adopted))
    .bind(task.stage == Stage::Intake)
    .bind(i64::try_from(task.revision)?)
    .bind(reassess)
    .execute(uow.connection())
    .await?;
    if reassess {
        sqlx::query("UPDATE tasks.plan_reports SET current=false WHERE task_id=$1 AND current")
            .bind(task.id.0)
            .execute(uow.connection())
            .await?;
        sqlx::query(
            "UPDATE tasks.plan_review_batches SET status='superseded' \
                 WHERE task_id=$1 AND status IN ('recorded','admitted')",
        )
        .bind(task.id.0)
        .execute(uow.connection())
        .await?;
    }
    if task.stage == Stage::Plan {
        sqlx::query(
            "UPDATE tasks.plan_reports SET current=false WHERE task_id=$1 AND NOT accepted",
        )
        .bind(task.id.0)
        .execute(uow.connection())
        .await?;
    }
    if task.stage == Stage::Implement {
        sqlx::query(
            "UPDATE tasks.implementation_reports SET status='superseded' \
                 WHERE task_id=$1 AND status='rejected'",
        )
        .bind(task.id.0)
        .execute(uow.connection())
        .await?;
    }
    for (prior, next) in remaps {
        sqlx::query(
            "UPDATE tasks.pr_review_findings SET reviewer_id=$3 \
                 WHERE task_id=$1 AND reviewer_id=$2 AND status IN ('open','withdrawn')",
        )
        .bind(task.id.0)
        .bind(prior)
        .bind(next)
        .execute(uow.connection())
        .await?;
    }
    if task.stage == Stage::PlanQc && rebind {
        sqlx::query(
            "UPDATE tasks.plan_review_batches SET status='superseded' \
                 WHERE task_id=$1 AND status IN ('recorded','admitted')",
        )
        .bind(task.id.0)
        .execute(uow.connection())
        .await?;
        let current_plan: (String, Vec<u8>) = sqlx::query_as(
            "SELECT plan_id,raw_report_bytes FROM tasks.plan_reports \
                 WHERE task_id=$1 AND accepted AND current \
                 ORDER BY recorded_at DESC,attempt_id DESC LIMIT 1",
        )
        .bind(task.id.0)
        .fetch_one(uow.connection())
        .await?;
        let plan: Value = serde_json::from_slice(&current_plan.1)?;
        let generation: i64 = sqlx::query_scalar(
            "SELECT COALESCE(max(generation)+1,0) FROM tasks.plan_review_batches \
                 WHERE task_id=$1",
        )
        .bind(task.id.0)
        .fetch_one(uow.connection())
        .await?;
        design::record_design_batch(
            uow,
            task,
            adopted,
            &plan,
            &current_plan.0,
            generation,
            "fresh",
        )
        .await?;
    }
    if task.stage == Stage::Review && rebind {
        sqlx::query(
            "UPDATE tasks.pr_review_batches SET status='superseded' \
                 WHERE task_id=$1 AND status IN ('recorded','admitted')",
        )
        .bind(task.id.0)
        .execute(uow.connection())
        .await?;
        let (turn, number, Json(paths)): (Uuid, i64, Json<Vec<String>>) = sqlx::query_as(
            "SELECT publication_attempt,pr_number,changed_paths \
                 FROM tasks.pr_review_batches WHERE task_id=$1 \
                 ORDER BY round DESC,id DESC LIMIT 1",
        )
        .bind(task.id.0)
        .fetch_one(uow.connection())
        .await?;
        let Json(intent): Json<GitPublicationIntent> =
            sqlx::query_scalar("SELECT intent FROM tasks.git_publications WHERE attempt_id=$1")
                .bind(turn)
                .fetch_one(uow.connection())
                .await?;
        pr_review::record_pr_review_batch(uow, task, turn, number, &intent, &paths).await?;
    }
    if !matches!(task.stage, Stage::PlanQc | Stage::Review) || rebind {
        cancel_attempts(uow, outbox, task.id.0, None).await?;
    }
    Ok(())
}

/// Queue only the continuation selected by an applied recovery command.
pub(crate) async fn enqueue_command_stage(
    uow: &mut UnitOfWork<'_>,
    request: &Request,
    previous: Option<&TaskState>,
    task: &TaskState,
    resumed: bool,
) -> Result<()> {
    if task.stage == Stage::Plan
        && matches!(
            request.kind,
            RequestKind::Later(LaterCommand::Retry {
                plan_revisions: Some(_)
            })
        )
    {
        enqueue_plan(uow, task).await?;
    }
    if task.stage == Stage::Intake && request.kind == RequestKind::Later(LaterCommand::Invalidate) {
        enqueue_intake(uow, task, None).await?;
    }
    if task.stage == Stage::Gates
        && request.kind
            == RequestKind::Later(LaterCommand::Retry {
                plan_revisions: None,
            })
    {
        enqueue_gates(uow, task).await?;
    }
    if resumed {
        match task.stage {
            Stage::Intake => enqueue_intake(uow, task, None).await?,
            Stage::Plan => enqueue_plan(uow, task).await?,
            Stage::Implement => enqueue_implementation(uow, task).await?,
            Stage::Publish => enqueue_git_publication(uow, task).await?,
            Stage::Gates => enqueue_gates(uow, task).await?,
            _ => {}
        }
    }
    if request.kind
        == RequestKind::Later(LaterCommand::Retry {
            plan_revisions: None,
        })
        && let Some(previous) = previous
        && previous.retry_stage == Some(task.stage)
        && !resumed
    {
        match task.stage {
            Stage::Intake => {
                sqlx::query(
                    "UPDATE tasks.initialization_inputs SET task_revision=$2 WHERE task_id=$1",
                )
                .bind(task.id.0)
                .bind(i64::try_from(task.revision)?)
                .execute(uow.connection())
                .await?;
                enqueue_intake(uow, task, None).await?;
            }
            Stage::Plan => {
                sqlx::query(
                    "UPDATE tasks.plan_reports SET current=false WHERE task_id=$1 AND NOT accepted",
                )
                .bind(task.id.0)
                .execute(uow.connection())
                .await?;
                enqueue_plan(uow, task).await?;
            }
            Stage::Implement => {
                sqlx::query(
                    "UPDATE tasks.implementation_reports SET status='superseded' \
                         WHERE task_id=$1 AND status='rejected'",
                )
                .bind(task.id.0)
                .execute(uow.connection())
                .await?;
                enqueue_implementation(uow, task).await?;
            }
            Stage::Publish => enqueue_git_publication(uow, task).await?,
            _ => {}
        }
    }
    Ok(())
}
