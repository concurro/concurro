//! Nonsecret historical billing attribution for the signed-in sponsor's read views.

use concurro_db::Database;
use serde_json::Value;
use sqlx::types::Json;

/// Count retained contributed reservations by their actual payer, preserving released reservations.
pub async fn own_charged_calls(database: &Database, payer: i64) -> Result<Vec<Value>, sqlx::Error> {
    let rows: Vec<(Json<Value>,)> = sqlx::query_as(
        "WITH charges AS (SELECT t.repository_id,t.issue_number number,'contribution' kind,g.provider,g.payer_id,count(*) calls FROM tasks.funding_grants g JOIN tasks.task_state t ON t.task_id=g.task_id WHERE g.payer_id=$1 AND g.reservation_status='charged' GROUP BY t.repository_id,t.issue_number,g.provider,g.payer_id \
         UNION ALL SELECT t.repository_id,t.pr_number,'review',g.provider,g.payer_id,count(*) FROM tasks.standalone_review_funding_grants g JOIN tasks.standalone_review_tasks t ON t.id=g.task_id WHERE g.payer_id=$1 GROUP BY t.repository_id,t.pr_number,g.provider,g.payer_id) \
         SELECT jsonb_build_object('repository',CASE WHEN charges.kind='review' THEN target->>'upstream' ELSE target->>'issue_repository' END,'number',number,'kind',kind,'provider',provider,'sponsor',payer_id::text,'calls',calls) \
         FROM charges CROSS JOIN tasks.config_targets config CROSS JOIN LATERAL jsonb_array_elements(config.body) target \
         WHERE (target->>'repository_id')::bigint=charges.repository_id OR charges.kind='review' AND (target->>'upstream_repository_id')::bigint=charges.repository_id")
        .bind(payer).fetch_all(database.pool()).await?;
    let mut rows = rows
        .into_iter()
        .map(|(Json(body),)| body)
        .collect::<Vec<_>>();
    let historical: Vec<(Json<Value>,)> = sqlx::query_as(
        "WITH latest AS (SELECT DISTINCT ON (kind,repository,number) * FROM tasks.historical_display ORDER BY kind,repository,number,observed_at DESC,deployment DESC) \
         SELECT debit || jsonb_build_object('repository',repository,'number',number,'task_kind',kind,'title',body #>> '{view,task,title}','historical',true,'deployment',deployment,'observed_at',observed_at) \
         FROM latest CROSS JOIN LATERAL jsonb_array_elements(COALESCE(body #> '{usage,charged_calls}','[]'::jsonb)) debit \
         WHERE debit->>'kind'='contributed' AND debit->>'sponsor'=$1")
        .bind(payer.to_string()).fetch_all(database.pool()).await?;
    rows.extend(historical.into_iter().map(|(Json(body),)| body));
    Ok(rows)
}

/// Identify historical source snapshots whose verified checkpoint omitted the debit ledger.
pub async fn unavailable_historical_usage(database: &Database) -> Result<Vec<Value>, sqlx::Error> {
    let rows: Vec<(Json<Value>,)> = sqlx::query_as(
        "WITH latest AS (SELECT DISTINCT ON (kind,repository,number) * FROM tasks.historical_display ORDER BY kind,repository,number,observed_at DESC,deployment DESC) \
         SELECT jsonb_build_object('repository',repository,'number',number,'kind',kind,'reason','Verified checkpoint did not retain credential debit evidence.') FROM latest WHERE body #>> '{usage,status}' IS DISTINCT FROM 'available'")
        .fetch_all(database.pool()).await?;
    Ok(rows.into_iter().map(|(Json(body),)| body).collect())
}
