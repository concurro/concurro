//! Recoverable GraphQL pending review with native file and line threads.

use anyhow::{Context, Result, bail, ensure};
use concurro_db::Database;
use concurro_github_v1::{WriteFailure, classify_write_failure};
use concurro_tasks_postgres::{
    PrReviewAction, PrReviewPublication, confirm_pr_review_action, confirm_pr_review_publication,
    fail_pr_review_action, fail_pr_review_publication, fallback_pr_review_thread,
    mark_pr_review_action_sent, mark_pr_review_sent, next_pr_review_action, pr_review_publication,
    retry_pr_review_action,
};
use reqwest::{Method, StatusCode};
use serde_json::{Value, json};

use crate::{
    GitPublicationContext, GitPublicationTarget,
    pr_review_publication::{readback_pr_review, review_pr_current},
};

/// Recognize only null-result GraphQL mutation refusals with known error codes.
fn graphql_refusal(value: &Value) -> Option<bool> {
    let mutation_empty = value["data"]
        .as_object()
        .is_none_or(|data| data.values().all(Value::is_null));
    let errors = mutation_empty
        .then(|| value["errors"].as_array())
        .flatten()
        .filter(|errors| !errors.is_empty())?;
    let codes = errors
        .iter()
        .map(|error| {
            error["type"]
                .as_str()
                .or_else(|| error["extensions"]["code"].as_str())
        })
        .collect::<Option<Vec<_>>>()?;
    codes
        .iter()
        .all(|code| {
            matches!(
                *code,
                "FORBIDDEN" | "NOT_FOUND" | "UNPROCESSABLE" | "VALIDATION"
            )
        })
        .then(|| {
            codes
                .iter()
                .all(|code| matches!(*code, "UNPROCESSABLE" | "VALIDATION"))
        })
}

/// Query GraphQL with the App's exact upstream permission scope.
async fn graphql_read(
    context: &GitPublicationContext,
    target: &GitPublicationTarget,
    query: &str,
    variables: Value,
) -> Result<Value> {
    let value: Value = context
        .github
        .write(
            &target.upstream,
            &target.upstream_write_permissions,
            Method::POST,
            &["graphql"],
            Some(json!({"query":query,"variables":variables})),
        )
        .await?;
    ensure!(value["errors"].is_null(), "GitHub GraphQL read failed");
    Ok(value["data"].clone())
}

/// Find the one pending App review created from this exact frozen marker.
async fn pending_review(
    context: &GitPublicationContext,
    target: &GitPublicationTarget,
    publication: &PrReviewPublication,
) -> Result<Option<String>> {
    let (owner, repository) = target
        .upstream
        .split_once('/')
        .context("upstream must be owner/name")?;
    let number = publication.pr_number.to_string();
    let mut page = 1;
    let mut found = None;
    loop {
        let reviews: Vec<Value> = context
            .github
            .read(
                &target.upstream,
                &target.upstream_write_permissions,
                &["repos", owner, repository, "pulls", &number, "reviews"],
                &[("per_page", "100"), ("page", &page.to_string())],
            )
            .await?;
        let more = reviews.len() == 100;
        for review in reviews {
            if review["body"] != publication.marker {
                continue;
            }
            ensure!(
                found.is_none(),
                "pending review marker appears more than once"
            );
            ensure!(
                review["state"] == "PENDING"
                    && review["commit_id"] == publication.head
                    && review["user"]["id"].as_i64() == publication.bot_user_id
                    && review["user"]["login"] == target.bot_login,
                "pending review marker has a different owner or head"
            );
            found = Some(
                review["node_id"]
                    .as_str()
                    .context("pending review node ID missing")?
                    .to_owned(),
            );
        }
        if !more {
            return Ok(found);
        }
        page += 1;
    }
}

/// Find an opening thread by its exact marker, body, owner, review and path.
async fn opening_thread(
    context: &GitPublicationContext,
    target: &GitPublicationTarget,
    publication: &PrReviewPublication,
    action: &PrReviewAction,
) -> Result<Option<String>> {
    let (owner, repository) = target
        .upstream
        .split_once('/')
        .context("upstream must be owner/name")?;
    let review = publication
        .review_node_id
        .as_deref()
        .context("pending review node is missing")?;
    let mut cursor: Option<String> = None;
    let mut found = None;
    loop {
        let data = graphql_read(
            context,
            target,
            "query($owner:String!,$name:String!,$number:Int!,$cursor:String){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100,after:$cursor){nodes{id path line startLine subjectType comments(first:1){nodes{body author{login} pullRequestReview{id}}}}pageInfo{hasNextPage endCursor}}}}}",
            json!({"owner":owner,"name":repository,"number":publication.pr_number,"cursor":cursor}),
        )
        .await?;
        let threads = &data["repository"]["pullRequest"]["reviewThreads"];
        let nodes = threads["nodes"]
            .as_array()
            .context("review threads missing")?;
        for thread in nodes {
            let opening = &thread["comments"]["nodes"][0];
            let Some(body) = opening["body"].as_str() else {
                continue;
            };
            if !body.starts_with(&action.marker) {
                continue;
            }
            ensure!(
                found.is_none(),
                "finding thread marker appears more than once"
            );
            ensure!(
                body == action.body
                    && thread["path"].as_str() == action.path.as_deref()
                    && opening["author"]["login"] == target.bot_login
                    && opening["pullRequestReview"]["id"] == review,
                "finding thread changed owner, path, review or body"
            );
            let expected = action
                .placement
                .as_deref()
                .context("thread placement missing")?;
            ensure!(
                (expected == "file" && thread["subjectType"] == "FILE")
                    || (expected == "inline"
                        && thread["subjectType"] == "LINE"
                        && thread["line"].as_i64() == action.line
                        && thread["startLine"].as_i64()
                            == action
                                .start_line
                                .filter(|start| Some(*start) != action.line)),
                "finding thread changed placement"
            );
            found = Some(
                thread["id"]
                    .as_str()
                    .context("finding thread node ID missing")?
                    .to_owned(),
            );
        }
        if threads["pageInfo"]["hasNextPage"] != true {
            return Ok(found);
        }
        cursor = Some(
            threads["pageInfo"]["endCursor"]
                .as_str()
                .context("review thread cursor missing")?
                .to_owned(),
        );
    }
}

/// Find one exact owner reply in its previously confirmed finding thread.
async fn thread_reply(
    context: &GitPublicationContext,
    target: &GitPublicationTarget,
    publication: &PrReviewPublication,
    action: &PrReviewAction,
) -> Result<Option<String>> {
    let thread = action
        .thread_node_id
        .as_deref()
        .context("disposition thread is missing")?;
    let review = publication
        .review_node_id
        .as_deref()
        .context("pending review node is missing")?;
    let mut cursor: Option<String> = None;
    let mut found = None;
    loop {
        let data = graphql_read(
            context,
            target,
            "query($id:ID!,$cursor:String){node(id:$id){... on PullRequestReviewThread{id comments(first:100,after:$cursor){nodes{id body author{login} pullRequestReview{id}}pageInfo{hasNextPage endCursor}}}}}",
            json!({"id":thread,"cursor":cursor}),
        )
        .await?;
        let node = &data["node"];
        ensure!(node["id"] == thread, "disposition thread identity changed");
        let comments = &node["comments"];
        for comment in comments["nodes"]
            .as_array()
            .context("thread replies missing")?
        {
            let Some(body) = comment["body"].as_str() else {
                continue;
            };
            if !body.starts_with(&action.marker) {
                continue;
            }
            ensure!(found.is_none(), "disposition marker appears more than once");
            ensure!(
                body == action.body
                    && comment["author"]["login"] == target.bot_login
                    && comment["pullRequestReview"]["id"] == review,
                "disposition reply changed owner, review or body"
            );
            found = Some(
                comment["id"]
                    .as_str()
                    .context("disposition comment node ID missing")?
                    .to_owned(),
            );
        }
        if comments["pageInfo"]["hasNextPage"] != true {
            return Ok(found);
        }
        cursor = Some(
            comments["pageInfo"]["endCursor"]
                .as_str()
                .context("thread reply cursor missing")?
                .to_owned(),
        );
    }
}

/// Verify resolution of only the previously owner-linked finding thread.
async fn resolved_thread(
    context: &GitPublicationContext,
    target: &GitPublicationTarget,
    action: &PrReviewAction,
) -> Result<Option<String>> {
    let thread = action
        .thread_node_id
        .as_deref()
        .context("resolution thread is missing")?;
    let data = graphql_read(
        context,
        target,
        "query($id:ID!){node(id:$id){... on PullRequestReviewThread{id isResolved resolvedBy{login}}}}",
        json!({"id":thread}),
    )
    .await?;
    ensure!(
        data["node"]["id"] == thread,
        "resolution thread identity changed"
    );
    if data["node"]["isResolved"] != true {
        return Ok(None);
    }
    ensure!(
        data["node"]["resolvedBy"]["login"] == target.bot_login,
        "finding thread was resolved by another actor"
    );
    Ok(Some(thread.to_owned()))
}

/// Read back one sent action without inferring success from its HTTP response.
async fn action_readback(
    context: &GitPublicationContext,
    target: &GitPublicationTarget,
    publication: &PrReviewPublication,
    action: &PrReviewAction,
) -> Result<Option<String>> {
    match action.kind.as_str() {
        "create" => pending_review(context, target, publication).await,
        "thread" => opening_thread(context, target, publication, action).await,
        "reply" => thread_reply(context, target, publication, action).await,
        "submit" => {
            if readback_pr_review(context, target, publication)
                .await?
                .is_some()
            {
                Ok(Some(
                    publication
                        .review_node_id
                        .clone()
                        .context("submitted review node is missing")?,
                ))
            } else {
                Ok(None)
            }
        }
        "resolve" => resolved_thread(context, target, action).await,
        _ => bail!("unsupported native review action"),
    }
}

/// Send one GraphQL action after its database fence is committed.
async fn send_action(
    context: &GitPublicationContext,
    target: &GitPublicationTarget,
    publication: &PrReviewPublication,
    action: &PrReviewAction,
    token: &str,
) -> Result<Value> {
    let review = publication.review_node_id.as_deref();
    let (query, input) = match action.kind.as_str() {
        "create" => {
            let (owner, repository) = target
                .upstream
                .split_once('/')
                .context("upstream must be owner/name")?;
            let number = publication.pr_number.to_string();
            let pr: Value = context
                .github
                .read(
                    &target.upstream,
                    &target.upstream_write_permissions,
                    &["repos", owner, repository, "pulls", &number],
                    &[],
                )
                .await?;
            ensure!(
                pr["head"]["sha"] == publication.head,
                "PR head changed before pending review"
            );
            (
                "mutation($input:AddPullRequestReviewInput!){addPullRequestReview(input:$input){pullRequestReview{id}}}",
                json!({"pullRequestId":pr["node_id"],"commitOID":publication.head,"body":publication.marker}),
            )
        }
        "thread" => {
            let mut input = json!({
                "pullRequestReviewId":review.context("pending review node missing")?,
                "path":action.path,
                "body":action.body,
                "subjectType":if action.placement.as_deref()==Some("inline") {"LINE"} else {"FILE"},
            });
            if action.placement.as_deref() == Some("inline") {
                input["line"] = json!(action.line.context("inline line missing")?);
                input["side"] = json!("RIGHT");
                if let Some(start) = action
                    .start_line
                    .filter(|start| Some(*start) != action.line)
                {
                    input["startLine"] = json!(start);
                    input["startSide"] = json!("RIGHT");
                }
            }
            (
                "mutation($input:AddPullRequestReviewThreadInput!){addPullRequestReviewThread(input:$input){thread{id}}}",
                input,
            )
        }
        "reply" => (
            "mutation($input:AddPullRequestReviewThreadReplyInput!){addPullRequestReviewThreadReply(input:$input){comment{id}}}",
            json!({"pullRequestReviewId":review.context("pending review node missing")?,
                "pullRequestReviewThreadId":action.thread_node_id,"body":action.body}),
        ),
        "submit" => (
            "mutation($input:SubmitPullRequestReviewInput!){submitPullRequestReview(input:$input){pullRequestReview{id}}}",
            json!({"pullRequestReviewId":review.context("pending review node missing")?,
                "event":"COMMENT","body":action.body}),
        ),
        "resolve" => (
            "mutation($input:ResolveReviewThreadInput!){resolveReviewThread(input:$input){thread{id isResolved}}}",
            json!({"threadId":action.thread_node_id}),
        ),
        _ => bail!("unsupported native review action"),
    };
    context
        .github
        .write_prepared(
            &target.upstream,
            &target.upstream_write_permissions,
            token,
            Method::POST,
            &["graphql"],
            Some(json!({"query":query,"variables":{"input":input}})),
        )
        .await
}

/// Build, submit and settle one pending App review through exact per-action readback.
pub(crate) async fn publish_threaded_review(
    database: &Database,
    context: &GitPublicationContext,
    target: &GitPublicationTarget,
    mut publication: PrReviewPublication,
) -> Result<()> {
    if !review_pr_current(context, target, &publication).await? {
        let mut work = database.begin().await?;
        fail_pr_review_publication(
            &mut work,
            &context.outbox,
            &publication,
            "PR changed before native review",
        )
        .await?;
        work.commit().await?;
        return Ok(());
    }
    if publication.status == "recorded" {
        let bot = context
            .github
            .bot_user_id(
                &target.upstream,
                &target.upstream_write_permissions,
                &target.bot_login,
            )
            .await?;
        let mut work = database.begin().await?;
        let marked = mark_pr_review_sent(&mut work, publication.turn, bot).await?;
        work.commit().await?;
        if !marked {
            return Ok(());
        }
        publication = pr_review_publication(database, publication.turn)
            .await?
            .context("native review disappeared after send fence")?;
    }
    loop {
        if !review_pr_current(context, target, &publication).await? {
            let mut work = database.begin().await?;
            fail_pr_review_publication(
                &mut work,
                &context.outbox,
                &publication,
                "PR changed during native review",
            )
            .await?;
            work.commit().await?;
            return Ok(());
        }
        let Some(action) = next_pr_review_action(database, publication.turn).await? else {
            let review_id = readback_pr_review(context, target, &publication)
                .await?
                .context("submitted native review is not visible")?;
            let mut work = database.begin().await?;
            confirm_pr_review_publication(&mut work, &context.outbox, publication.turn, review_id)
                .await?;
            work.commit().await?;
            return Ok(());
        };
        if action.status == "failed" {
            bail!("native review action awaits operator recovery");
        }
        let observed = action_readback(context, target, &publication, &action).await?;
        if let Some(node) = observed {
            let mut work = database.begin().await?;
            confirm_pr_review_action(&mut work, publication.turn, action.sequence, Some(&node))
                .await?;
            work.commit().await?;
            publication = pr_review_publication(database, publication.turn)
                .await?
                .context("native review disappeared after action readback")?;
            continue;
        }
        if action.status == "sent" {
            bail!("uncertain native review action awaits exact GitHub readback");
        }
        let token = context
            .github
            .token(&target.upstream, &target.upstream_write_permissions)
            .await?;
        let mut work = database.begin().await?;
        let marked =
            mark_pr_review_action_sent(&mut work, publication.turn, action.sequence).await?;
        work.commit().await?;
        if !marked {
            return Ok(());
        }
        let sent = send_action(context, target, &publication, &action, &token).await;
        if let Some(node) = action_readback(context, target, &publication, &action).await? {
            let mut work = database.begin().await?;
            confirm_pr_review_action(&mut work, publication.turn, action.sequence, Some(&node))
                .await?;
            work.commit().await?;
            publication = pr_review_publication(database, publication.turn)
                .await?
                .context("native review disappeared after action confirmation")?;
            continue;
        }
        let graphql_refusal = sent.as_ref().ok().and_then(graphql_refusal);
        let definite = match &sent {
            Ok(_) => graphql_refusal.is_some(),
            Err(error) => matches!(classify_write_failure(error), WriteFailure::Definitive(_)),
        };
        if definite {
            if action.kind == "thread" {
                let fallback_allowed = graphql_refusal == Some(true)
                    || sent.as_ref().err().is_some_and(|error| {
                        matches!(
                            classify_write_failure(error),
                            WriteFailure::Definitive(StatusCode::UNPROCESSABLE_ENTITY)
                        )
                    });
                if fallback_allowed {
                    let mut work = database.begin().await?;
                    fallback_pr_review_thread(&mut work, publication.turn, action.sequence).await?;
                    work.commit().await?;
                    continue;
                }
            }
            let mut work = database.begin().await?;
            if fail_pr_review_action(&mut work, publication.turn, action.sequence).await? {
                fail_pr_review_publication(
                    &mut work,
                    &context.outbox,
                    &publication,
                    &format!("native review {} refused by GitHub", action.kind),
                )
                .await?;
            }
            work.commit().await?;
            return Ok(());
        }
        if let Err(error) = sent {
            if matches!(
                classify_write_failure(&error),
                WriteFailure::Retryable(_, _)
            ) {
                let mut work = database.begin().await?;
                retry_pr_review_action(&mut work, publication.turn, action.sequence).await?;
                work.commit().await?;
            }
            return Err(error);
        }
        bail!("native review mutation is awaiting exact GitHub readback");
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::graphql_refusal;

    /// Reject successful or unknown GraphQL replies while accepting known refusals.
    #[test]
    fn recognizes_only_known_null_result_mutation_refusals() {
        assert_eq!(
            graphql_refusal(
                &json!({"data":{"addPullRequestReview":null},"errors":[{"type":"FORBIDDEN"}]})
            ),
            Some(false)
        );
        assert_eq!(
            graphql_refusal(
                &json!({"data":{"addPullRequestReview":null},"errors":[{"extensions":{"code":"UNPROCESSABLE"}}]})
            ),
            Some(true)
        );
        assert_eq!(
            graphql_refusal(
                &json!({"data":{"addPullRequestReview":{"pullRequestReview":{"id":"r"}}},"errors":[{"type":"FORBIDDEN"}]})
            ),
            None
        );
        assert_eq!(
            graphql_refusal(&json!({"data":null,"errors":[{"type":"INTERNAL"}]})),
            None
        );
    }
}
