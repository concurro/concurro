//! One-time imports of verified legacy display evidence, without live task ownership.

use anyhow::{Context, Result, ensure};
use chrono::{DateTime, Utc};
use concurro_db::Database;
use serde_json::{Value, json};
use sqlx::types::Json;

/// Import a complete historical display export atomically; replay preserves its original bytes.
pub async fn import_historical_display(database: &Database, manifest: &Value) -> Result<Value> {
    ensure!(
        manifest["format"] == 1,
        "unsupported historical display format"
    );
    let deployment = manifest["deployment"]
        .as_str()
        .filter(|s| !s.is_empty())
        .context("deployment missing")?;
    let observed_at = DateTime::parse_from_rfc3339(
        manifest["observed_at"]
            .as_str()
            .context("snapshot time missing")?,
    )?
    .with_timezone(&Utc);
    let views = manifest["views"]
        .as_array()
        .context("historical views missing")?;
    let mut work = database.begin().await?;
    for view in views {
        let kind = view["kind"].as_str().context("historical kind missing")?;
        ensure!(
            matches!(kind, "contribution" | "review"),
            "invalid historical kind"
        );
        let repository = view["repository"]
            .as_str()
            .context("historical repository missing")?
            .to_ascii_lowercase();
        let number = view["number"]
            .as_i64()
            .filter(|n| *n > 0)
            .context("historical number invalid")?;
        let task = &view["view"]["task"];
        ensure!(
            format!(
                "{}/{}",
                task["owner"].as_str().unwrap_or(""),
                task["repo"].as_str().unwrap_or("")
            )
            .eq_ignore_ascii_case(&repository)
                && task[if kind == "contribution" {
                    "issue"
                } else {
                    "pr"
                }] == number,
            "historical source mismatch"
        );
        ensure!(
            view["provenance"].is_object() && view["reports"].is_object(),
            "historical provenance or reports missing"
        );
        sqlx::query("INSERT INTO tasks.historical_display (deployment,kind,repository,number,observed_at,body) VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING")
            .bind(deployment).bind(kind).bind(&repository).bind(number).bind(observed_at).bind(Json(view)).execute(work.connection()).await?;
        let (saved_at, Json(saved)): (DateTime<Utc>, Json<Value>) = sqlx::query_as("SELECT observed_at,body FROM tasks.historical_display WHERE deployment=$1 AND kind=$2 AND repository=$3 AND number=$4")
            .bind(deployment).bind(kind).bind(&repository).bind(number).fetch_one(work.connection()).await?;
        ensure!(
            saved_at == observed_at && saved == *view,
            "historical snapshot changed on replay"
        );
    }
    work.commit().await?;
    Ok(json!({"views":views.len(),"deployment":deployment,"observed_at":observed_at}))
}

/// Page the latest imported display snapshot for each source identity.
pub async fn historical_display(
    database: &Database,
    repository: &str,
    kind: &str,
    after: i64,
) -> Result<Vec<Value>, sqlx::Error> {
    let rows: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('deployment',deployment,'observed_at',observed_at,'record',body) FROM (SELECT DISTINCT ON (number) * FROM tasks.historical_display WHERE repository=lower($1) AND kind=$2 AND number>$3 ORDER BY number,observed_at DESC,deployment DESC) snapshots ORDER BY number LIMIT 101")
        .bind(repository).bind(kind).bind(after).fetch_all(database.pool()).await?;
    Ok(rows.into_iter().map(|(Json(value),)| value).collect())
}
