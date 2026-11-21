//! App-owned activity checks derived from durable standalone review rounds.

use anyhow::{Context, Result, ensure};
use chrono::Utc;
use concurro_db::Database;
use concurro_tasks_core::python_digest;
use concurro_tasks_postgres::{
    adopt_standalone_check_run, fail_standalone_check, mark_standalone_check_sent,
    miss_standalone_check_create, prior_standalone_checks, retire_standalone_check,
    settle_standalone_check, standalone_check_progress, standalone_review_check,
    standalone_review_round,
};
use reqwest::Method;
use serde_json::{Value, json};
use uuid::Uuid;

use crate::{StandaloneReviewContext, StandaloneReviewTarget};

/// List matching runs on the pinned head, including late duplicates of a prior POST.
async fn readback_standalone_checks(
    context: &StandaloneReviewContext,
    target: &StandaloneReviewTarget,
    upstream: &str,
    pr_number: i64,
    head: &str,
    external_id: &str,
) -> Result<Vec<Value>> {
    let (owner, repository) = upstream
        .split_once('/')
        .context("upstream must be owner/name")?;
    let mut page = 1;
    let mut matches = Vec::new();
    loop {
        let response: Value = context
            .github
            .read(
                upstream,
                &target.upstream_write_permissions,
                &["repos", owner, repository, "commits", head, "check-runs"],
                &[
                    ("filter", "all"),
                    ("per_page", "100"),
                    ("page", &page.to_string()),
                ],
            )
            .await?;
        let runs = response["check_runs"]
            .as_array()
            .context("check run list missing")?;
        for run in runs {
            if run["name"] == format!("Concurro review - PR {pr_number}")
                && run["external_id"] == external_id
            {
                matches.push(run.clone());
            }
        }
        if runs.len() < 100 {
            break;
        }
        page += 1;
    }
    matches.sort_by_key(|run| run["id"].as_i64().unwrap_or_default());
    Ok(matches)
}

/// Reconcile one derived Check Run without changing review admission or outcome.
pub async fn sync_standalone_review_check(
    database: &Database,
    context: &StandaloneReviewContext,
    task_id: Uuid,
) -> Result<()> {
    let Some(round) = standalone_review_round(database, task_id).await? else {
        return Ok(());
    };
    if round.stage == "closed" {
        return Ok(());
    }
    let target = context
        .targets
        .get(&round.repository_id)
        .context("standalone check target unavailable")?;
    ensure!(
        target.source.upstream.eq_ignore_ascii_case(&round.upstream),
        "standalone check target changed"
    );
    let check = standalone_review_check(database, &round).await?;
    let result: Result<()> = async {
        let (owner,repository)=round.upstream.split_once('/')
            .context("upstream must be owner/name")?;
        let (published,reviews)=standalone_check_progress(database,task_id,round.number).await?;
        let panel=round.snapshot["panel"].as_array().context("standalone panel missing")?;
        let progress=format!("{published} of {} reviewers complete.",panel.len());
        let (status,title,summary)=match round.stage.as_str() {
            "queued"=>("queued","Queued",format!("Review accepted for `{}`.",&check.head[..7])),
            "review" | "publishing"=>("in_progress","Reviewing",format!("{progress}{}",
                if round.stage=="publishing" {" Publishing reviews."} else {""})),
            _=>{
                let reason=match round.stage.as_str() {
                    "complete"=>"Review of this snapshot complete. Re-request the reviewer for reassessment.",
                    "cancelled"=>"The review request was removed before completion.",
                    "awaiting_credentials"=>"Review paused for funding consent or usable credentials. A writer can use `/concurro retry` after the cause is fixed.",
                    "budget_exhausted"=>"Review paused because its model-call budget is exhausted. An operator must raise it.",
                    "stopped"=>"Review stopped by a writer. A writer can use `/concurro retry`.",
                    _=>"Review paused. A writer can use `/concurro retry` after the cause is fixed.",
                };
                let links=reviews.iter().map(|id| format!("https://github.com/{}/pull/{}#pullrequestreview-{id}",round.upstream,round.pr_number))
                    .collect::<Vec<_>>();
                ("completed","Finished",format!("{reason}\n\nReviewed head `{}`. Base `{}` at `{}`; merge base `{}`. Round {}.\n\nReviews: {}",
                    check.head,round.snapshot["base_ref"].as_str().unwrap_or("?"),
                    round.snapshot["base_tip"].as_str().unwrap_or("?"),
                    round.snapshot["merge_base"].as_str().unwrap_or("?"),round.number,
                    if links.is_empty() {"none".to_owned()} else {links.join(" · ")}))
            }
        };
        let mut wanted=json!({"status":status,"output":{"title":title,"summary":summary}});
        if status=="completed" { wanted["conclusion"]=json!("neutral"); }
        let digest=python_digest(&wanted)?;
        let name=format!("Concurro review - PR {}",round.pr_number);
        let pr_url=format!("https://github.com/{}/pull/{}",round.upstream,round.pr_number);
        let prior=prior_standalone_checks(database,task_id,check.round,check.attempt).await?;
        if check.completed && check.sent_digest.as_deref()==Some(&digest)
            && check.recheck_until.is_none_or(|until| until<=Utc::now()) && prior.is_empty() {
            return Ok(());
        }
        for (old_round,old_attempt,head,external_id) in prior {
            for run in readback_standalone_checks(context,target,&round.upstream,
                round.pr_number,&head,&external_id).await? {
                if run["status"]!="completed" {
                    let id=run["id"].as_i64().context("prior check ID missing")?;
                    let _: Value=context.github.write(&round.upstream,&target.upstream_write_permissions,
                        Method::PATCH,&["repos",owner,repository,"check-runs",&id.to_string()],
                        Some(json!({"status":"completed","conclusion":"neutral",
                            "output":{"title":"Finished","summary":"Superseded by a later review check."}}))).await?;
                }
            }
            retire_standalone_check(database,task_id,old_round,old_attempt).await?;
        }
        let matches=readback_standalone_checks(context,target,&round.upstream,
            round.pr_number,&check.head,&check.external_id).await?;
        let run_id=matches.last().map(|run| run["id"].as_i64()
            .context("check run ID missing")).transpose()?;
        for older in matches.iter().take(matches.len().saturating_sub(1)) {
            if older["status"]!="completed" {
                let id=older["id"].as_i64().context("duplicate check ID missing")?;
                let _: Value=context.github.write(&round.upstream,&target.upstream_write_permissions,
                    Method::PATCH,&["repos",owner,repository,"check-runs",&id.to_string()],
                    Some(json!({"status":"completed","conclusion":"neutral",
                        "output":{"title":"Finished","summary":"Superseded by a newer run of this review check."}}))).await?;
            }
        }
        if let Some(id)=run_id {
            if check.run_id!=Some(id) {
                let mut work=database.begin().await?;
                adopt_standalone_check_run(&mut work,&check,id).await?;
                work.commit().await?;
            }
        } else if check.create_state=="sent" && check.create_misses<2 {
            let mut work=database.begin().await?;
            miss_standalone_check_create(&mut work,&check).await?;
            work.commit().await?;
            anyhow::bail!("uncertain standalone check create awaits later readback");
        } else {
            let token=context.github.token(&round.upstream,&target.upstream_write_permissions).await?;
            let mut work=database.begin().await?;
            if !mark_standalone_check_sent(&mut work,&check).await? {
                work.commit().await?;
                return Ok(());
            }
            work.commit().await?;
            let mut create=json!({
                "name":name,"head_sha":check.head,"external_id":check.external_id,
                "details_url":pr_url,"status":wanted["status"],"output":wanted["output"]
            });
            if status=="completed" { create["conclusion"]=json!("neutral"); }
            let created: Value=context.github.write_prepared(&round.upstream,
                &target.upstream_write_permissions,&token,Method::POST,
                &["repos",owner,repository,"check-runs"],Some(create)).await?;
            let id=created["id"].as_i64().context("created check ID missing")?;
            let mut work=database.begin().await?;
            adopt_standalone_check_run(&mut work,&check,id).await?;
            settle_standalone_check(&mut work,&check,&digest,status=="completed").await?;
            work.commit().await?;
            return Ok(());
        }
        if let Some(id)=run_id {
            if check.sent_digest.as_deref()!=Some(&digest) || check.run_id!=Some(id) {
                let _: Value=context.github.write(&round.upstream,&target.upstream_write_permissions,
                    Method::PATCH,&["repos",owner,repository,"check-runs",&id.to_string()],
                    Some(wanted)).await?;
                let mut work=database.begin().await?;
                settle_standalone_check(&mut work,&check,&digest,status=="completed").await?;
                work.commit().await?;
            }
        }
        Ok(())
    }.await;
    if let Err(error) = &result {
        fail_standalone_check(database, &check, &error.to_string()).await?;
    }
    result
}
