//! PR conversation revisions and source-ordered standalone control decisions.

use anyhow::{Result, ensure};
use chrono::{DateTime, Utc};
use concurro_db::{Database, UnitOfWork};
use concurro_tasks_core::{
    Actor, CommentRevision, ConsentAction, ConsentFact, IssueKey, LaterCommand, Request,
    RequestKind, SourceOrder, consent_facts,
};
use serde_json::Value;
use sha2::{Digest, Sha256};
use sqlx::types::Json;
use uuid::Uuid;

use crate::{
    browser_actions::RequestRecord,
    read_model::{append_read_model_record, append_standalone_view},
    sponsorship::record_standalone_funding_fact_transition,
};

/// Read a signed standalone action after validating its original actor and content.
pub async fn browser_standalone_action(
    database: &Database,
    action_id: Uuid,
    actor_id: i64,
    content: &Value,
) -> Result<Option<RequestRecord>> {
    let row: Option<(
        i64,
        Json<Value>,
        String,
        Option<String>,
        Option<DateTime<Utc>>,
        Option<Uuid>,
    )> = sqlx::query_as(
        "SELECT a.actor_id,a.content,c.status,c.reason,c.decided_at,t.id \
             FROM tasks.browser_standalone_actions a \
             JOIN tasks.standalone_review_commands c ON c.id=a.command_id \
             LEFT JOIN tasks.standalone_review_tasks t \
               ON lower(t.upstream)=lower(c.upstream) AND t.pr_number=c.pr_number \
             WHERE a.action_id=$1",
    )
    .bind(action_id)
    .fetch_optional(database.pool())
    .await?;
    let Some((stored_actor, Json(stored_content), status, reason, applied_at, task_id)) = row
    else {
        return Ok(None);
    };
    ensure!(
        stored_actor == actor_id && stored_content == *content,
        "idempotency_conflict"
    );
    Ok(Some(RequestRecord {
        request_id: action_id,
        status,
        reason,
        task_id,
        applied_at,
    }))
}

/// Admit a signed browser action through the standalone PR source queue.
pub async fn record_browser_standalone_action(
    uow: &mut UnitOfWork<'_>,
    action_id: Uuid,
    content: &Value,
    issue: IssueKey,
    upstream: &str,
    actor: &Actor,
    kind: RequestKind,
    source_at: DateTime<Utc>,
) -> Result<()> {
    let owner: Option<String> = sqlx::query_scalar(
        "SELECT owner_kind FROM tasks.pr_ownership \
         WHERE upstream=lower($1) AND pr_number=$2",
    )
    .bind(upstream)
    .bind(issue.number)
    .fetch_optional(uow.connection())
    .await?;
    ensure!(
        owner
            .as_deref()
            .is_none_or(|kind| kind == "standalone_pr_review"),
        "pr_owned_elsewhere"
    );
    let comment_id = -i64::try_from(action_id.as_u128() % i64::MAX as u128 + 1)?;
    let source = format!("browser:{action_id}");
    let revision = CommentRevision {
        issue,
        comment_id,
        order: SourceOrder {
            at: source_at,
            source_id: comment_id,
            source: source.clone(),
            index: 0,
        },
        deleted: false,
        request: Some(Request {
            id: action_id,
            channel: "browser".to_owned(),
            reply: String::new(),
            comment_id: None,
            issue,
            issue_author: actor.clone(),
            actor: actor.clone(),
            kind,
            order: SourceOrder {
                at: source_at,
                source_id: comment_id,
                source,
                index: 0,
            },
        }),
    };
    record_standalone_review_command(
        uow,
        &revision,
        upstream,
        actor,
        actor.id,
        "/concurro browser command",
    )
    .await?;
    let command_id = Uuid::new_v5(
        &Uuid::NAMESPACE_OID,
        format!(
            "standalone-command:{upstream}#{}:browser:{action_id}",
            issue.number
        )
        .as_bytes(),
    );
    sqlx::query(
        "INSERT INTO tasks.browser_standalone_actions \
         (action_id,actor_id,content,command_id) VALUES ($1,$2,$3,$4) \
         ON CONFLICT (action_id) DO NOTHING",
    )
    .bind(action_id)
    .bind(actor.id)
    .bind(Json(content))
    .bind(command_id)
    .execute(uow.connection())
    .await?;
    let row: (i64, Json<Value>, Uuid) = sqlx::query_as(
        "SELECT actor_id,content,command_id FROM tasks.browser_standalone_actions \
         WHERE action_id=$1 FOR UPDATE",
    )
    .bind(action_id)
    .fetch_one(uow.connection())
    .await?;
    ensure!(
        row.0 == actor.id && row.1.0 == *content && row.2 == command_id,
        "idempotency_conflict"
    );
    Ok(())
}

/// Read one standalone browser action by ID for the internal outcome route.
pub async fn standalone_action_record(
    database: &Database,
    action_id: Uuid,
) -> Result<Option<RequestRecord>> {
    let row: Option<(i64, Json<Value>)> = sqlx::query_as(
        "SELECT actor_id,content FROM tasks.browser_standalone_actions WHERE action_id=$1",
    )
    .bind(action_id)
    .fetch_optional(database.pool())
    .await?;
    match row {
        Some((actor_id, Json(content))) => {
            browser_standalone_action(database, action_id, actor_id, &content).await
        }
        None => Ok(None),
    }
}

/// One immutable PR comment revision awaiting standalone command authority.
pub struct StandaloneReviewCommand {
    /// Stable source revision identity.
    pub id: Uuid,
    /// Numeric upstream repository identity.
    pub repository_id: i64,
    /// Upstream repository name.
    pub upstream: String,
    /// PR number.
    pub pr_number: i64,
    /// Comment creation or edit time.
    pub source_at: DateTime<Utc>,
    /// Numeric source comment ID.
    pub comment_id: i64,
    /// Stable digest-qualified source revision.
    pub source: String,
    /// GitHub actor for this revision.
    pub actor: Actor,
    /// Parsed command when supported.
    pub kind: Option<RequestKind>,
    /// Exact source text at this revision.
    pub body: String,
    /// Whether this revision removed a previous command.
    pub deleted: bool,
}

/// Retain every command-like PR revision, including unsupported and edited text.
pub async fn record_standalone_review_command(
    uow: &mut UnitOfWork<'_>,
    revision: &CommentRevision,
    upstream: &str,
    actor: &Actor,
    author_id: i64,
    body: &str,
) -> Result<()> {
    if !revision.deleted && !body.trim_start().starts_with("/concurro") {
        let previous: bool = sqlx::query_scalar(
            "SELECT EXISTS(SELECT 1 FROM tasks.standalone_review_commands \
             WHERE repository_id=$1 AND pr_number=$2 AND comment_id=$3)",
        )
        .bind(revision.issue.repository_id)
        .bind(revision.issue.number)
        .bind(revision.comment_id)
        .fetch_one(uow.connection())
        .await?;
        if !previous {
            return Ok(());
        }
    }
    let id = Uuid::new_v5(
        &Uuid::NAMESPACE_OID,
        format!(
            "standalone-command:{upstream}#{}:{}",
            revision.issue.number, revision.order.source
        )
        .as_bytes(),
    );
    let kind = revision.request.as_ref().map(|request| Json(&request.kind));
    sqlx::query(
        "INSERT INTO tasks.standalone_review_commands \
         (id,repository_id,upstream,pr_number,comment_id,source,source_at,actor, \
          comment_author_id,kind,body,deleted) \
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) \
         ON CONFLICT (upstream,pr_number,source) DO NOTHING",
    )
    .bind(id)
    .bind(revision.issue.repository_id)
    .bind(upstream)
    .bind(revision.issue.number)
    .bind(revision.comment_id)
    .bind(&revision.order.source)
    .bind(revision.order.at)
    .bind(Json(actor))
    .bind(author_id)
    .bind(kind)
    .bind(body)
    .bind(revision.deleted)
    .execute(uow.connection())
    .await?;
    let _: i64 = sqlx::query_scalar(
        "SELECT (graphile_worker.add_job( \
         identifier => 'standalone_review_decision', \
         payload => json_build_object('repository_id',$1,'upstream',$2,'pr_number',$3), \
         queue_name => $4,max_attempts => 25,job_key => $5)).id",
    )
    .bind(revision.issue.repository_id)
    .bind(upstream)
    .bind(revision.issue.number)
    .bind(format!(
        "standalone-review:{}:{}",
        revision.issue.repository_id, revision.issue.number
    ))
    .bind(format!("standalone-command:{id}"))
    .fetch_one(uow.connection())
    .await?;
    Ok(())
}

/// Read the oldest undecided command revision on one PR.
pub async fn next_standalone_review_command(
    database: &Database,
    upstream: &str,
    pr_number: i64,
) -> Result<Option<StandaloneReviewCommand>> {
    let row: Option<(
        Uuid,
        i64,
        DateTime<Utc>,
        i64,
        String,
        Json<Actor>,
        Option<Json<RequestKind>>,
        String,
        bool,
    )> = sqlx::query_as(
        "SELECT id,repository_id,source_at,comment_id,source,actor,kind,body,deleted \
         FROM tasks.standalone_review_commands \
         WHERE upstream=$1 AND pr_number=$2 AND status='pending' \
         ORDER BY source_at,comment_id,source LIMIT 1",
    )
    .bind(upstream)
    .bind(pr_number)
    .fetch_optional(database.pool())
    .await?;
    Ok(row.map(
        |(id, repository_id, source_at, comment_id, source, Json(actor), kind, body, deleted)| {
            StandaloneReviewCommand {
                id,
                repository_id,
                upstream: upstream.to_owned(),
                pr_number,
                source_at,
                comment_id,
                source,
                actor,
                kind: kind.map(|value| value.0),
                body,
                deleted,
            }
        },
    ))
}

/// Fold a new PR revision over earlier effective funding claims from that comment.
pub async fn record_standalone_funding_revision(
    uow: &mut UnitOfWork<'_>,
    command: &StandaloneReviewCommand,
    accept_new: bool,
) -> Result<()> {
    sqlx::query(
        "INSERT INTO tasks.standalone_review_controls (upstream,pr_number) \
         VALUES ($1,$2) ON CONFLICT DO NOTHING",
    )
    .bind(&command.upstream)
    .bind(command.pr_number)
    .execute(uow.connection())
    .await?;
    let _: i64 = sqlx::query_scalar(
        "SELECT pr_number FROM tasks.standalone_review_controls \
         WHERE upstream=$1 AND pr_number=$2 FOR UPDATE",
    )
    .bind(&command.upstream)
    .bind(command.pr_number)
    .fetch_one(uow.connection())
    .await?;
    let initialized_task: Option<Uuid> = sqlx::query_scalar(
        "SELECT t.id FROM tasks.standalone_review_tasks t \
         JOIN tasks.standalone_review_sponsorship_initializations i ON i.task_id=t.id \
         WHERE t.upstream=$1 AND t.pr_number=$2 FOR UPDATE OF t",
    )
    .bind(&command.upstream)
    .bind(command.pr_number)
    .fetch_optional(uow.connection())
    .await?;
    let previous: Vec<(Uuid, Json<ConsentFact>)> = sqlx::query_as(
        "SELECT f.command_id,f.fact FROM tasks.standalone_review_funding_facts f \
         JOIN tasks.standalone_review_commands c ON c.id=f.command_id \
         WHERE f.repository_id=$1 AND f.pr_number=$2 AND f.comment_id=$3 \
         AND f.effective AND f.command_id<>$4 FOR UPDATE OF f",
    )
    .bind(command.repository_id)
    .bind(command.pr_number)
    .bind(command.comment_id)
    .bind(command.id)
    .fetch_all(uow.connection())
    .await?;
    for (prior_id, Json(prior)) in previous {
        sqlx::query(
            "UPDATE tasks.standalone_review_funding_facts SET effective=false \
             WHERE command_id=$1 AND provider=$2",
        )
        .bind(prior_id)
        .bind(prior.provider.as_str())
        .execute(uow.connection())
        .await?;
        append_read_model_record(
            uow,
            "standalone_funding",
            &format!("{}:{}", prior_id, prior.provider.as_str()),
            2,
            serde_json::json!({"repository_id":command.repository_id,
                "pr_number":command.pr_number,"provider":prior.provider.as_str(),
                "fact":prior,"effective":false}),
        )
        .await?;
        let removal = match prior.action {
            ConsentAction::Sponsor => Some(ConsentAction::Withdraw {
                removed_source: Some(prior.order.source_id),
            }),
            ConsentAction::Funding(mode) => Some(ConsentAction::FundingRemoved {
                removed_source: prior.order.source_id,
                removes_sponsor: mode == concurro_tasks_core::FundingMode::Contributed,
            }),
            ConsentAction::Withdraw { .. } | ConsentAction::FundingRemoved { .. } => None,
        };
        if let Some(removal) = removal {
            let fact = ConsentFact {
                provider: prior.provider,
                actor: prior.actor,
                order: SourceOrder {
                    at: command.source_at,
                    source_id: command.comment_id,
                    source: command.source.clone(),
                    index: -1,
                },
                grant_id: hex::encode(Sha256::digest(
                    format!("{}:{}", prior.grant_id, command.source).as_bytes(),
                )),
                action: removal,
            };
            sqlx::query(
                "INSERT INTO tasks.standalone_review_funding_removals \
                 (command_id,prior_command_id,provider,fact) VALUES ($1,$2,$3,$4) \
                 ON CONFLICT DO NOTHING",
            )
            .bind(command.id)
            .bind(prior_id)
            .bind(fact.provider.as_str())
            .bind(Json(&fact))
            .execute(uow.connection())
            .await?;
            append_read_model_record(
                uow,
                "standalone_funding_removal",
                &format!("{}:{}:{}", command.id, prior_id, fact.provider.as_str()),
                1,
                serde_json::json!({"repository_id":command.repository_id,
                    "pr_number":command.pr_number,"provider":fact.provider.as_str(),
                    "fact":fact}),
            )
            .await?;
            if let Some(task_id) = initialized_task {
                record_standalone_funding_fact_transition(uow, task_id, &fact).await?;
            }
        }
    }
    if !accept_new {
        return Ok(());
    }
    let Some(kind) = &command.kind else {
        return Ok(());
    };
    let kinds = match kind {
        RequestKind::Later(LaterCommand::Sponsor { provider: None }) => vec![
            RequestKind::Later(LaterCommand::Sponsor {
                provider: Some("openai".to_owned()),
            }),
            RequestKind::Later(LaterCommand::Sponsor {
                provider: Some("anthropic".to_owned()),
            }),
        ],
        _ => vec![kind.clone()],
    };
    for kind in kinds {
        let request = Request {
            id: command.id,
            channel: "standalone_comment".to_owned(),
            reply: format!("issue_comment:{}", command.comment_id),
            comment_id: Some(command.comment_id),
            issue: IssueKey {
                repository_id: command.repository_id,
                number: command.pr_number,
            },
            issue_author: command.actor.clone(),
            actor: command.actor.clone(),
            kind,
            order: SourceOrder {
                at: command.source_at,
                source_id: command.comment_id,
                source: command.source.clone(),
                index: 0,
            },
        };
        for fact in consent_facts(&request) {
            sqlx::query(
                "INSERT INTO tasks.standalone_review_funding_facts \
                 (command_id,repository_id,pr_number,comment_id,provider,fact) \
                 VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING",
            )
            .bind(command.id)
            .bind(command.repository_id)
            .bind(command.pr_number)
            .bind(command.comment_id)
            .bind(fact.provider.as_str())
            .bind(Json(&fact))
            .execute(uow.connection())
            .await?;
            append_read_model_record(
                uow,
                "standalone_funding",
                &format!("{}:{}", command.id, fact.provider.as_str()),
                1,
                serde_json::json!({"repository_id":command.repository_id,
                    "pr_number":command.pr_number,"provider":fact.provider.as_str(),
                    "fact":fact,"effective":true}),
            )
            .await?;
            if let Some(task_id) = initialized_task {
                record_standalone_funding_fact_transition(uow, task_id, &fact).await?;
            }
        }
    }
    Ok(())
}

/// Apply stop or retry to one current standalone round under its task lock.
pub async fn apply_standalone_review_control(
    uow: &mut UnitOfWork<'_>,
    command: &StandaloneReviewCommand,
    adoption: Option<&Value>,
) -> Result<(&'static str, &'static str)> {
    let task: Option<(Uuid, String, i32)> = sqlx::query_as(
        "SELECT id,stage,round FROM tasks.standalone_review_tasks \
         WHERE upstream=$1 AND pr_number=$2 FOR UPDATE",
    )
    .bind(&command.upstream)
    .bind(command.pr_number)
    .fetch_optional(uow.connection())
    .await?;
    let Some((task_id, stage, round)) = task else {
        return Ok(("noop", "No active standalone review."));
    };
    match command.kind.as_ref() {
        Some(RequestKind::Stop) => {
            if matches!(
                stage.as_str(),
                "complete" | "closed" | "cancelled" | "stopped"
            ) {
                return Ok(("noop", "The review is already finished or stopped."));
            }
            sqlx::query("UPDATE tasks.standalone_review_tasks SET stage='stopped' WHERE id=$1")
                .bind(task_id)
                .execute(uow.connection())
                .await?;
            sqlx::query(
                "UPDATE tasks.standalone_review_rounds SET status='stopped' \
                WHERE task_id=$1 AND number=$2",
            )
            .bind(task_id)
            .bind(round)
            .execute(uow.connection())
            .await?;
            append_standalone_view(uow, task_id).await?;
            Ok(("applied", "Stopped this review round."))
        }
        Some(RequestKind::Later(LaterCommand::Retry {
            plan_revisions: None,
        }))
        | Some(RequestKind::Later(LaterCommand::Resume {
            description_only: false,
        })) => {
            let resume = matches!(
                command.kind,
                Some(RequestKind::Later(LaterCommand::Resume {
                    description_only: false
                }))
            );
            if !matches!(
                stage.as_str(),
                "stopped" | "blocked" | "awaiting_credentials" | "budget_exhausted"
            ) {
                return Ok(("noop", "The review is already active or finished."));
            }
            let active: bool = sqlx::query_scalar(
                "SELECT EXISTS(SELECT 1 FROM tasks.standalone_review_attempts s \
                 JOIN tasks.attempts a ON a.id=s.attempt_id \
                 WHERE s.task_id=$1 AND s.round=$2 AND a.status IN \
                 ('ready','sending','queued','running','unknown'))",
            )
            .bind(task_id)
            .bind(round)
            .fetch_one(uow.connection())
            .await?;
            if active {
                return Ok(("noop", "A reviewer turn is still settling."));
            }
            if resume {
                let Some(adoption) = adoption else {
                    return Ok(("refused", "Current review configuration is unavailable."));
                };
                ensure!(
                    adoption["config_sha"].as_str().is_some()
                        && adoption["configuration"].is_object()
                        && adoption["panel"].is_array(),
                    "standalone resume adoption is incomplete"
                );
                let previous: Json<Value> = sqlx::query_scalar(
                    "SELECT snapshot FROM tasks.standalone_review_rounds \
                     WHERE task_id=$1 AND number=$2 FOR UPDATE",
                )
                .bind(task_id)
                .bind(round)
                .fetch_one(uow.connection())
                .await?;
                let mut adopted = previous.0.clone();
                let object = adopted
                    .as_object_mut()
                    .ok_or_else(|| anyhow::anyhow!("standalone round snapshot is invalid"))?;
                for key in ["config_sha", "configuration", "panel"] {
                    object.insert(key.to_owned(), adoption[key].clone());
                }
                sqlx::query(
                    "UPDATE tasks.standalone_review_rounds SET snapshot=$3 \
                    WHERE task_id=$1 AND number=$2",
                )
                .bind(task_id)
                .bind(round)
                .bind(Json(&adopted))
                .execute(uow.connection())
                .await?;
                sqlx::query(
                    "INSERT INTO tasks.standalone_review_adoptions \
                    (command_id,task_id,round,previous_snapshot,adopted_snapshot) \
                    VALUES ($1,$2,$3,$4,$5)",
                )
                .bind(command.id)
                .bind(task_id)
                .bind(round)
                .bind(previous)
                .bind(Json(adopted))
                .execute(uow.connection())
                .await?;
            }
            sqlx::query(
                "UPDATE tasks.standalone_review_attempts SET status='retrying' \
                WHERE attempt_id=(SELECT attempt_id FROM tasks.standalone_review_attempts \
                    WHERE task_id=$1 AND round=$2 AND status='failed' \
                    ORDER BY attempt_number DESC LIMIT 1)",
            )
            .bind(task_id)
            .bind(round)
            .execute(uow.connection())
            .await?;
            let next = if resume
                && matches!(stage.as_str(), "awaiting_credentials" | "budget_exhausted")
            {
                stage.as_str()
            } else {
                "review"
            };
            sqlx::query("UPDATE tasks.standalone_review_tasks SET stage=$2 WHERE id=$1")
                .bind(task_id)
                .bind(next)
                .execute(uow.connection())
                .await?;
            sqlx::query(
                "UPDATE tasks.standalone_review_rounds SET status=$3 \
                WHERE task_id=$1 AND number=$2",
            )
            .bind(task_id)
            .bind(round)
            .bind(next)
            .execute(uow.connection())
            .await?;
            let _: i64 = sqlx::query_scalar(
                "SELECT (graphile_worker.add_job(identifier => 'standalone_review_round', \
                 payload => json_build_object('task_id',$1,'round',$2), \
                 max_attempts => 25,job_key => $3)).id",
            )
            .bind(task_id)
            .bind(round)
            .bind(format!("standalone-review-round:{task_id}:{round}"))
            .fetch_one(uow.connection())
            .await?;
            append_standalone_view(uow, task_id).await?;
            Ok((
                "applied",
                if resume {
                    "Adopted current review settings and instructions."
                } else {
                    "Retried this review round."
                },
            ))
        }
        _ => Ok((
            "refused",
            "Supported commands: stop, retry, resume, and funding commands.",
        )),
    }
}

/// Settle one command revision and enqueue its unique App-authored reply.
pub async fn settle_standalone_review_command(
    uow: &mut UnitOfWork<'_>,
    command: &StandaloneReviewCommand,
    status: &str,
    reason: &str,
) -> Result<bool> {
    ensure!(
        matches!(status, "applied" | "noop" | "refused"),
        "invalid standalone command disposition"
    );
    let changed = sqlx::query(
        "UPDATE tasks.standalone_review_commands SET status=$2,reason=$3,decided_at=now() \
         WHERE id=$1 AND status='pending'",
    )
    .bind(command.id)
    .bind(status)
    .bind(reason)
    .execute(uow.connection())
    .await?;
    if changed.rows_affected() == 0 {
        return Ok(false);
    }
    if command.deleted {
        return Ok(true);
    }
    let marker = format!("<!-- concurro:reply:v1:{} -->", command.id);
    let label = match status {
        "applied" => "Applied",
        "noop" => "No-op",
        _ => "Rejected",
    };
    let body = format!("{marker}\n{label}: {reason}");
    let reply = Uuid::new_v5(
        &Uuid::NAMESPACE_OID,
        format!("publication-comment:{}", command.id).as_bytes(),
    );
    sqlx::query(
        "INSERT INTO tasks.publication_comments \
         (id,standalone_command_id,repository_id,issue_number,marker,body,status) \
         VALUES ($1,$2,$3,$4,$5,$6,'recorded') ON CONFLICT (id) DO NOTHING",
    )
    .bind(reply)
    .bind(command.id)
    .bind(command.repository_id)
    .bind(command.pr_number)
    .bind(marker)
    .bind(body)
    .execute(uow.connection())
    .await?;
    let _: i64 = sqlx::query_scalar(
        "SELECT (graphile_worker.add_job(identifier => 'publish_request_reply', \
         payload => json_build_object('id',$1::text),queue_name => $2, \
         max_attempts => 25,job_key => $3)).id",
    )
    .bind(reply)
    .bind(format!(
        "publication:issue:{}:{}",
        command.repository_id, command.pr_number
    ))
    .bind(format!("publication-comment:{reply}"))
    .fetch_one(uow.connection())
    .await?;
    Ok(true)
}

/// Recover command revisions whose source decision job was interrupted.
pub async fn pending_standalone_review_commands(
    database: &Database,
) -> Result<Vec<(i64, String, i64)>, sqlx::Error> {
    sqlx::query_as(
        "SELECT DISTINCT repository_id,upstream,pr_number \
         FROM tasks.standalone_review_commands WHERE status='pending' \
         ORDER BY repository_id,upstream,pr_number",
    )
    .fetch_all(database.pool())
    .await
}
