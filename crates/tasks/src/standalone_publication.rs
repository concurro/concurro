//! Native reviewer-account review publication with durable send and readback fences.

use std::collections::HashMap;

use anyhow::{Context, Result, ensure};
use concurro_db::Database;
use concurro_github_v1::{WriteFailure, classify_write_failure};
use concurro_tasks_postgres::{
    StandaloneDisposition, StandaloneReviewPublication, confirm_standalone_disposition_reply,
    confirm_standalone_review_publication, fail_standalone_disposition_reply,
    fail_standalone_review_publication, fallback_standalone_review_comments,
    finish_standalone_thread_resolution, intend_standalone_thread_resolution,
    mark_standalone_disposition_reply_sent, mark_standalone_review_sent,
    next_standalone_disposition, retry_standalone_disposition_reply,
    retry_standalone_review_publication, standalone_review_publication,
};
use graphile_worker::{IntoTaskHandlerResult, TaskHandler, WorkerContext};
use reqwest::{Method, StatusCode};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use uuid::Uuid;

use crate::{StandaloneReviewContext, StandaloneReviewTarget};

/// Queued native publication for one independently validated reviewer turn.
#[derive(Deserialize, Serialize)]
pub struct StandaloneReviewPublishJob {
    /// Exact reviewer turn whose body is frozen.
    pub turn: Uuid,
}

impl TaskHandler for StandaloneReviewPublishJob {
    const IDENTIFIER: &'static str = "standalone_review_publish";

    /// Send once and reconcile all later runs from the review list.
    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let standalone = context
            .get_ext::<StandaloneReviewContext>()
            .ok_or("standalone review context unavailable")?;
        let database = Database::from_pool(context.pg_pool().clone());
        publish_standalone_review(&database, standalone, self.turn)
            .await
            .map_err(|error| error.to_string())
    }
}

/// Find one submitted review with the exact marker, account, commit and body.
async fn readback_standalone_review(
    context: &StandaloneReviewContext,
    grant: &concurro_github_v1::user_grant::UserGrant,
    publication: &StandaloneReviewPublication,
) -> Result<Option<(i64, Vec<(String, String, i64)>)>> {
    let upstream = publication.upstream.clone();
    let (owner, repository) = upstream
        .split_once('/')
        .context("upstream must be owner/name")?;
    let number = publication.pr_number.to_string();
    let mut found = None;
    let mut page = 1;
    loop {
        let reviews: Vec<Value> = context
            .github
            .user_read(
                grant,
                &["repos", owner, repository, "pulls", &number, "reviews"],
                &[("per_page", "100"), ("page", &page.to_string())],
            )
            .await?;
        let more = reviews.len() == 100;
        for review in reviews {
            let Some(body) = review["body"].as_str() else {
                continue;
            };
            if !body.starts_with(&publication.marker) {
                continue;
            }
            ensure!(
                found.is_none(),
                "standalone review marker appears more than once"
            );
            ensure!(
                body == publication.body
                    && review["commit_id"] == publication.head
                    && review["state"] == "COMMENTED"
                    && review["user"]["id"].as_i64() == Some(publication.reviewer_user_id),
                "reviewer-account review changed identity or body"
            );
            found = Some(
                review["id"]
                    .as_i64()
                    .context("matching review ID missing")?,
            );
        }
        if !more {
            break;
        }
        page += 1;
    }
    let Some(review_id) = found else {
        return Ok(None);
    };
    let mut comments = Vec::new();
    if !publication.comments.is_empty() {
        let mut page = 1;
        loop {
            let listed: Vec<Value> = context
                .github
                .user_read(
                    grant,
                    &[
                        "repos",
                        owner,
                        repository,
                        "pulls",
                        &number,
                        "reviews",
                        &review_id.to_string(),
                        "comments",
                    ],
                    &[("per_page", "100"), ("page", &page.to_string())],
                )
                .await?;
            let more = listed.len() == 100;
            comments.extend(listed);
            if !more {
                break;
            }
            page += 1;
        }
    }
    let mut comment_nodes = Vec::with_capacity(publication.comments.len());
    for expected in &publication.comments {
        let marker = expected["marker"]
            .as_str()
            .context("review comment marker missing")?;
        let matches = comments
            .iter()
            .filter(|comment| {
                comment["body"]
                    .as_str()
                    .is_some_and(|body| body.starts_with(marker))
            })
            .collect::<Vec<_>>();
        ensure!(
            matches.len() == 1,
            "standalone review comment awaits exact readback"
        );
        let comment = matches[0];
        ensure!(
            comment["body"] == expected["body"]
                && comment["path"] == expected["path"]
                && (comment["line"] == expected["line"]
                    || comment["original_line"] == expected["line"])
                && comment["commit_id"] == publication.head
                && comment["user"]["id"].as_i64() == Some(publication.reviewer_user_id),
            "reviewer-account comment changed identity or placement"
        );
        comment_nodes.push((
            expected["finding_id"]
                .as_str()
                .context("comment finding owner missing")?
                .to_owned(),
            comment["node_id"]
                .as_str()
                .context("comment node ID missing")?
                .to_owned(),
            comment["id"].as_i64().context("comment REST ID missing")?,
        ));
    }
    if comment_nodes.is_empty() {
        return Ok(Some((review_id, Vec::new())));
    }
    let target = context
        .targets
        .get(&publication.repository_id)
        .context("standalone review target unavailable")?;
    let mut thread_ids = HashMap::new();
    let mut cursor: Option<String> = None;
    loop {
        let data: Value = context.github.write(
            &upstream,
            &target.upstream_write_permissions,
            Method::POST,
            &["graphql"],
            Some(json!({
                "query":"query($owner:String!,$name:String!,$number:Int!,$cursor:String){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100,after:$cursor){nodes{id comments(first:1){nodes{id}}}pageInfo{hasNextPage endCursor}}}}}",
                "variables":{"owner":owner,"name":repository,"number":publication.pr_number,"cursor":cursor}
            })),
        ).await?;
        ensure!(data["errors"].is_null(), "review thread readback failed");
        let listed = &data["data"]["repository"]["pullRequest"]["reviewThreads"];
        for thread in listed["nodes"]
            .as_array()
            .context("review threads missing")?
        {
            let Some(root) = thread["comments"]["nodes"][0]["id"].as_str() else {
                continue;
            };
            if comment_nodes.iter().any(|(_, node, _)| node == root) {
                let id = thread["id"].as_str().context("review thread ID missing")?;
                ensure!(
                    thread_ids.insert(root.to_owned(), id.to_owned()).is_none(),
                    "review comment maps to more than one thread"
                );
            }
        }
        if listed["pageInfo"]["hasNextPage"] != true {
            break;
        }
        cursor = Some(
            listed["pageInfo"]["endCursor"]
                .as_str()
                .context("review thread cursor missing")?
                .to_owned(),
        );
    }
    let threads = comment_nodes
        .into_iter()
        .map(|(finding, comment, root_id)| {
            thread_ids
                .get(&comment)
                .map(|thread| (finding, thread.clone(), root_id))
                .context("standalone review thread awaits exact readback")
        })
        .collect::<Result<Vec<_>>>()?;
    Ok(Some((review_id, threads)))
}

/// Send one frozen native review or continue exact readback after an uncertain POST.
pub async fn publish_standalone_review(
    database: &Database,
    context: &StandaloneReviewContext,
    turn: Uuid,
) -> Result<()> {
    let Some(mut publication) = standalone_review_publication(database, turn).await? else {
        return publish_standalone_dispositions(database, context, turn).await;
    };
    let target = context
        .targets
        .get(&publication.repository_id)
        .context("standalone review target unavailable")?;
    ensure!(
        target.reviewer_id == publication.reviewer_user_id
            && target
                .source
                .upstream
                .eq_ignore_ascii_case(&publication.upstream),
        "standalone reviewer target changed"
    );
    let grant = target
        .grant
        .as_ref()
        .context("reviewer grant unavailable")?;
    let upstream = publication.upstream.clone();
    let (owner, repository) = upstream
        .split_once('/')
        .context("upstream must be owner/name")?;
    let number = publication.pr_number.to_string();
    if publication.status == "recorded" {
        let pr: Value = context
            .github
            .user_read(grant, &["repos", owner, repository, "pulls", &number], &[])
            .await?;
        ensure!(
            pr["state"] == "open",
            "standalone PR is closed; publication awaits reopening"
        );
        let token = grant.token().await?;
        ensure!(
            readback_standalone_review(context, grant, &publication)
                .await?
                .is_none(),
            "unfenced standalone review already exists before first send"
        );
        publication = standalone_review_publication(database, turn)
            .await?
            .context("standalone review publication disappeared before send")?;
        let mut work = database.begin().await?;
        let marked = mark_standalone_review_sent(&mut work, turn).await?;
        work.commit().await?;
        if !marked {
            return Ok(());
        }
        publication.status = "sent".to_owned();
        let pr: Value = context
            .github
            .user_read(grant, &["repos", owner, repository, "pulls", &number], &[])
            .await?;
        if pr["state"] != "open" {
            let mut work = database.begin().await?;
            retry_standalone_review_publication(&mut work, turn).await?;
            work.commit().await?;
            anyhow::bail!("standalone PR is closed; publication awaits reopening");
        }
        let sent: Result<Value> = context
            .github
            .user_write_prepared(
                grant,
                &token,
                Method::POST,
                &["repos", owner, repository, "pulls", &number, "reviews"],
                json!({
                    "commit_id": publication.head,
                    "event": "COMMENT",
                    "body": publication.body,
                    "comments": publication.comments.iter().map(|entry| {
                        let mut comment = entry.as_object().cloned().unwrap_or_default();
                        comment.remove("finding_id");
                        comment.remove("marker");
                        comment.retain(|_, value| !value.is_null());
                        Value::Object(comment)
                    }).collect::<Vec<_>>(),
                }),
            )
            .await;
        if let Err(error) = sent {
            match classify_write_failure(&error) {
                WriteFailure::Definitive(status) => {
                    if status == StatusCode::UNPROCESSABLE_ENTITY
                        && !publication.comments.is_empty()
                    {
                        let mut work = database.begin().await?;
                        if fallback_standalone_review_comments(&mut work, turn).await? {
                            work.commit().await?;
                            anyhow::bail!(
                                "standalone inline placement refused; retrying review body"
                            );
                        }
                        work.commit().await?;
                    }
                    let mut work = database.begin().await?;
                    fail_standalone_review_publication(&mut work, turn, &format!("HTTP {status}"))
                        .await?;
                    work.commit().await?;
                    return Ok(());
                }
                WriteFailure::Retryable(_, _) => {
                    let mut work = database.begin().await?;
                    retry_standalone_review_publication(&mut work, turn).await?;
                    work.commit().await?;
                    return Err(error);
                }
                WriteFailure::Unknown => {}
            }
        }
    }
    let Some((review_id, threads)) =
        readback_standalone_review(context, grant, &publication).await?
    else {
        anyhow::bail!("uncertain standalone review submission awaits exact reviewer readback");
    };
    let mut work = database.begin().await?;
    confirm_standalone_review_publication(&mut work, turn, review_id, &threads).await?;
    work.commit().await?;
    publish_standalone_dispositions(database, context, turn).await
}

/// Find one exact reply under the original reviewer-owned root comment.
async fn readback_standalone_disposition_reply(
    context: &StandaloneReviewContext,
    grant: &concurro_github_v1::user_grant::UserGrant,
    disposition: &StandaloneDisposition,
) -> Result<Option<i64>> {
    let (owner, repository) = disposition
        .upstream
        .split_once('/')
        .context("upstream must be owner/name")?;
    let number = disposition.pr_number.to_string();
    let mut page = 1;
    let mut found = None;
    loop {
        let comments: Vec<Value> = context
            .github
            .user_read(
                grant,
                &["repos", owner, repository, "pulls", &number, "comments"],
                &[("per_page", "100"), ("page", &page.to_string())],
            )
            .await?;
        let more = comments.len() == 100;
        for comment in comments {
            if !comment["body"]
                .as_str()
                .is_some_and(|body| body.starts_with(&disposition.marker))
            {
                continue;
            }
            ensure!(
                found.is_none(),
                "standalone disposition marker appears more than once"
            );
            ensure!(
                comment["body"] == disposition.body
                    && comment["in_reply_to_id"] == disposition.root_comment_id
                    && comment["user"]["id"] == disposition.reviewer_user_id,
                "standalone disposition reply changed owner or body"
            );
            found = Some(
                comment["id"]
                    .as_i64()
                    .context("disposition reply ID missing")?,
            );
        }
        if !more {
            return Ok(found);
        }
        page += 1;
    }
}

/// Read the exact thread's resolution state through the App's upstream scope.
async fn readback_standalone_resolution(
    context: &StandaloneReviewContext,
    target: &StandaloneReviewTarget,
    disposition: &StandaloneDisposition,
) -> Result<Option<bool>> {
    let value: Value=context.github.write(
        &disposition.upstream,&target.upstream_write_permissions,Method::POST,&["graphql"],
        Some(json!({
            "query":"query($id:ID!){node(id:$id){... on PullRequestReviewThread{id isResolved resolvedBy{login}}}}",
            "variables":{"id":disposition.thread_node_id}
        }))).await?;
    ensure!(
        value["errors"].is_null() && value["data"]["node"]["id"] == disposition.thread_node_id,
        "standalone finding thread changed identity"
    );
    if value["data"]["node"]["isResolved"] != true {
        return Ok(None);
    }
    Ok(Some(
        value["data"]["node"]["resolvedBy"]["login"] == target.source.bot_login,
    ))
}

/// Settle exact reviewer replies, then conservatively resolve their owned threads.
pub async fn publish_standalone_dispositions(
    database: &Database,
    context: &StandaloneReviewContext,
    turn: Uuid,
) -> Result<()> {
    while let Some(disposition) = next_standalone_disposition(database, turn).await? {
        let target = context
            .targets
            .get(&disposition.repository_id)
            .context("standalone review target unavailable")?;
        ensure!(
            target.reviewer_id == disposition.reviewer_user_id
                && target
                    .source
                    .upstream
                    .eq_ignore_ascii_case(&disposition.upstream),
            "standalone disposition target changed"
        );
        let grant = target
            .grant
            .as_ref()
            .context("reviewer grant unavailable")?;
        let (owner, repository) = disposition
            .upstream
            .split_once('/')
            .context("upstream must be owner/name")?;
        let number = disposition.pr_number.to_string();
        if disposition.reply_status != "confirmed" {
            if let Some(reply) =
                readback_standalone_disposition_reply(context, grant, &disposition).await?
            {
                let mut work = database.begin().await?;
                confirm_standalone_disposition_reply(
                    &mut work,
                    turn,
                    &disposition.finding_id,
                    reply,
                )
                .await?;
                work.commit().await?;
                continue;
            }
            if disposition.reply_status == "sent" {
                anyhow::bail!("uncertain reviewer disposition reply awaits exact readback");
            }
            let pr: Value = context
                .github
                .user_read(grant, &["repos", owner, repository, "pulls", &number], &[])
                .await?;
            ensure!(
                pr["state"] == "open" && pr["merged"] != true,
                "standalone PR is closed; disposition reply awaits reopening"
            );
            let token = grant.token().await?;
            let mut work = database.begin().await?;
            if !mark_standalone_disposition_reply_sent(&mut work, turn, &disposition.finding_id)
                .await?
            {
                work.commit().await?;
                continue;
            }
            work.commit().await?;
            let sent: Result<Value> = context
                .github
                .user_write_prepared(
                    grant,
                    &token,
                    Method::POST,
                    &[
                        "repos",
                        owner,
                        repository,
                        "pulls",
                        &number,
                        "comments",
                        &disposition.root_comment_id.to_string(),
                        "replies",
                    ],
                    json!({"body":disposition.body}),
                )
                .await;
            if let Err(error) = sent {
                match classify_write_failure(&error) {
                    WriteFailure::Definitive(status) => {
                        let mut work = database.begin().await?;
                        fail_standalone_disposition_reply(
                            &mut work,
                            turn,
                            &disposition.finding_id,
                            &format!("HTTP {status}"),
                        )
                        .await?;
                        work.commit().await?;
                        continue;
                    }
                    WriteFailure::Retryable(_, _) => {
                        let mut work = database.begin().await?;
                        retry_standalone_disposition_reply(
                            &mut work,
                            turn,
                            &disposition.finding_id,
                        )
                        .await?;
                        work.commit().await?;
                        return Err(error);
                    }
                    WriteFailure::Unknown => return Err(error),
                }
            }
            if let Some(reply) =
                readback_standalone_disposition_reply(context, grant, &disposition).await?
            {
                let mut work = database.begin().await?;
                confirm_standalone_disposition_reply(
                    &mut work,
                    turn,
                    &disposition.finding_id,
                    reply,
                )
                .await?;
                work.commit().await?;
                continue;
            }
            anyhow::bail!("reviewer disposition reply awaits exact readback");
        }
        if disposition.resolve_status == "none" {
            continue;
        }
        if disposition.resolve_status == "intended" {
            let state = readback_standalone_resolution(context, target, &disposition).await?;
            let mut work = database.begin().await?;
            finish_standalone_thread_resolution(
                &mut work,
                turn,
                &disposition.finding_id,
                if state == Some(true) {
                    "confirmed"
                } else {
                    "manual"
                },
            )
            .await?;
            work.commit().await?;
            continue;
        }
        let state = readback_standalone_resolution(context, target, &disposition).await?;
        if state.is_some() {
            let mut work = database.begin().await?;
            intend_standalone_thread_resolution(&mut work, turn, &disposition.finding_id).await?;
            finish_standalone_thread_resolution(
                &mut work,
                turn,
                &disposition.finding_id,
                if state == Some(true) {
                    "confirmed"
                } else {
                    "manual"
                },
            )
            .await?;
            work.commit().await?;
            continue;
        }
        let pr: Value = context
            .github
            .read(
                &disposition.upstream,
                &target.source.upstream_permissions,
                &["repos", owner, repository, "pulls", &number],
                &[],
            )
            .await?;
        ensure!(
            pr["state"] == "open" && pr["merged"] != true,
            "standalone PR is closed; thread resolution awaits reopening"
        );
        let token = context
            .github
            .token(&disposition.upstream, &target.upstream_write_permissions)
            .await?;
        let mut work = database.begin().await?;
        if !intend_standalone_thread_resolution(&mut work, turn, &disposition.finding_id).await? {
            work.commit().await?;
            continue;
        }
        work.commit().await?;
        let result: Value=context.github.write_prepared(
            &disposition.upstream,&target.upstream_write_permissions,&token,Method::POST,&["graphql"],
            Some(json!({
                "query":"mutation($input:ResolveReviewThreadInput!){resolveReviewThread(input:$input){thread{id isResolved}}}",
                "variables":{"input":{"threadId":disposition.thread_node_id}}
            }))).await?;
        if result["errors"].is_array() {
            let mut work = database.begin().await?;
            finish_standalone_thread_resolution(&mut work, turn, &disposition.finding_id, "failed")
                .await?;
            work.commit().await?;
            continue;
        }
        ensure!(
            result["data"]["resolveReviewThread"]["thread"]["id"] == disposition.thread_node_id,
            "standalone thread resolution response changed identity"
        );
        let state = readback_standalone_resolution(context, target, &disposition).await?;
        let mut work = database.begin().await?;
        finish_standalone_thread_resolution(
            &mut work,
            turn,
            &disposition.finding_id,
            if state == Some(true) {
                "confirmed"
            } else {
                "manual"
            },
        )
        .await?;
        work.commit().await?;
    }
    Ok(())
}
