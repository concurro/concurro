//! Transactional source records for the backoffice projection.

use chrono::Utc;
use concurro_contracts::{Envelope, EventId, ReadModelRecord, Timestamp};
use concurro_db::UnitOfWork;
use concurro_events_postgres::{Outbox, OutboxError};
use serde_json::{Map, Value, json};
use sqlx::types::Json;
use uuid::Uuid;

/// Publish one complete source row at a monotonic source revision.
pub(crate) async fn append_read_model_record(
    work: &mut UnitOfWork<'_>,
    entity: &str,
    entity_id: &str,
    revision: u64,
    body: Value,
) -> anyhow::Result<()> {
    let data = ReadModelRecord {
        entity: entity.to_owned(),
        entity_id: entity_id.to_owned(),
        body,
    };
    let identity = format!("view:{entity}:{entity_id}:{revision}");
    let event = Envelope::snapshot(
        EventId(Uuid::new_v5(&Uuid::NAMESPACE_OID, identity.as_bytes())),
        "tasks",
        Timestamp(Utc::now()),
        format!("view:{entity}:{entity_id}"),
        revision,
        data,
    );
    let outbox = Outbox::new(30_000_000, vec!["view.>".to_owned()]);
    match outbox.append(work, &event).await {
        Ok(_) | Err(OutboxError::Duplicate) => Ok(()),
        Err(error) => Err(error.into()),
    }
}

/// Read both providers' current slot and payer state for events or baselines.
pub(crate) async fn build_consent_state(
    work: &mut UnitOfWork<'_>,
    task: Uuid,
) -> anyhow::Result<Option<(u64, Value)>> {
    let identity: (i64, i64, String) = sqlx::query_as(
        "SELECT repository_id,issue_number,'contribution'::text \
         FROM tasks.task_state WHERE task_id=$1 \
         UNION ALL SELECT repository_id,pr_number,'standalone_pr_review'::text \
         FROM tasks.standalone_review_tasks WHERE id=$1",
    )
    .bind(task)
    .fetch_one(work.connection())
    .await?;
    let rows: Vec<(String, i64, Vec<u8>)> = sqlx::query_as(
        "SELECT DISTINCT ON (provider) provider,sequence,body \
         FROM tasks.sponsorship_transitions \
         WHERE task_id=$1 OR standalone_task_id=$1 \
         ORDER BY provider,sequence DESC",
    )
    .bind(task)
    .fetch_all(work.connection())
    .await?;
    let mut slots = Map::new();
    let mut funding = Map::new();
    let mut revision = 0;
    for (provider, sequence, body) in rows {
        revision = revision.max(sequence);
        let transition: Value = serde_json::from_slice(&body)?;
        funding.insert(provider.clone(), transition["slot"]["funding"].clone());
        slots.insert(provider, transition["slot"].clone());
    }
    if revision == 0 {
        return Ok(None);
    }
    Ok(Some((
        u64::try_from(revision)?,
        json!({"task_id":task,"repository_id":identity.0,
            "issue_number":if identity.2 == "contribution" { Some(identity.1) } else { None },
            "pr_number":if identity.2 == "standalone_pr_review" { Some(identity.1) } else { None },
            "kind":identity.2,"slots":slots,"provider_funding":funding}),
    )))
}

/// Publish both providers' current slot and payer state after a transition.
pub(crate) async fn append_consent_state(
    work: &mut UnitOfWork<'_>,
    task: Uuid,
) -> anyhow::Result<()> {
    if let Some((revision, body)) = build_consent_state(work, task).await? {
        append_read_model_record(work, "consent_state", &task.to_string(), revision, body).await?;
    }
    Ok(())
}

/// Publish the complete standalone review after its final row mutation.
pub(crate) async fn append_standalone_view(
    work: &mut UnitOfWork<'_>,
    task: Uuid,
) -> anyhow::Result<()> {
    let row: Option<(i64, Json<Value>)> = sqlx::query_as(
        "SELECT t.view_revision,jsonb_build_object( \
         'task_id',t.id,'repository_id',t.repository_id,'upstream',t.upstream, \
         'pr_number',t.pr_number,'stage',t.stage,'round',t.round, \
         'snapshot',r.snapshot,'round_status',r.status, \
         'used_model_calls',t.used_model_calls,'enrolled_at',t.enrolled_at) \
         FROM tasks.standalone_review_tasks t \
         LEFT JOIN tasks.standalone_review_rounds r \
             ON r.task_id=t.id AND r.number=t.round WHERE t.id=$1",
    )
    .bind(task)
    .fetch_optional(work.connection())
    .await?;
    if let Some((revision, Json(body))) = row {
        append_read_model_record(
            work,
            "standalone_review",
            &task.to_string(),
            u64::try_from(revision)?,
            body,
        )
        .await?;
    }
    Ok(())
}

/// Publish one artifact's current send/readback state through the shared outbox.
pub(crate) async fn append_report_view(
    work: &mut UnitOfWork<'_>,
    report: Uuid,
) -> anyhow::Result<()> {
    let row: Option<(i64, Json<Value>)> = sqlx::query_as(
        "SELECT view_revision,jsonb_build_object( \
         'report_id',report_id,'task_id',task_id,'standalone_task_id',standalone_task_id, \
         'repository_id',repository_id,'issue_number',issue_number, \
         'name',name,'body',body,'status',status,'recorded_at',recorded_at) \
         FROM tasks.report_artifact_writes WHERE report_id=$1",
    )
    .bind(report)
    .fetch_optional(work.connection())
    .await?;
    if let Some((revision, Json(body))) = row {
        append_read_model_record(
            work,
            "report",
            &report.to_string(),
            u64::try_from(revision)?,
            body,
        )
        .await?;
    }
    Ok(())
}
